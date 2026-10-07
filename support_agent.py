"""A small 4-step customer-support agent: the multi-step demo target for the step-finder.

  classify -> decide (policy lookup) -> draft reply -> tone check

Each step is its own prompt + model setting. The tools (order lookup, policy lookup) are
fake and deterministic, so any difference between runs comes from the models. Steps run
live: each step's output feeds the next. Every step's messages, raw output, usage and
latency are cached, one row per (ticket, run), resumable like record_baseline.py.

  python support_agent.py --dry-run                     # render prompts + cost estimate, no API calls
  python support_agent.py --model gpt-4 --limit 2 --runs 1                  # smoke test (8 calls)
  python support_agent.py --model gpt-4                                     # baseline: 24 x 3 runs
  python support_agent.py --model gpt-5.6-sol --model-config '{}'           # candidate everywhere
  python support_agent.py --model gpt-5.6-sol --model-config '{}' --step decide=gpt-4
                                                        # hybrid: one step on another model

Prompts are stored as lists of lines so later tools can ablate one line at a time.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
TICKETS = HERE / "inputs" / "agent.jsonl"  # inputs/<task>.jsonl, as compare.load_gold expects
OPENAI_BASE = "https://api.openai.com/v1"
TODAY = "2026-10-06"  # fixed so the tools and prompts are deterministic

# ---------------------------------------------------------------------------------------
# Fake, deterministic tools
# ---------------------------------------------------------------------------------------
ORDERS = {
    "A1001": {"item": "Wireless earbuds", "price": 79.00, "status": "delivered", "delivered": "2026-09-20"},
    "A1002": {"item": "Wool sweater", "price": 120.00, "status": "delivered", "delivered": "2026-08-25"},
    "A1003": {"item": "Desk lamp (clearance)", "price": 35.00, "status": "delivered", "delivered": "2026-09-28",
              "final_sale": True},
    "A1004": {"item": "Blender", "price": 89.99, "status": "shipped", "estimated_delivery": "2026-10-04"},
    "A1005": {"item": "Ceramic mug set (4)", "price": 42.00, "status": "delivered", "delivered": "2026-10-01"},
    "A1006": {"item": "Desk chair", "price": 249.00, "status": "processing"},
    "A1007": {"item": "Backpack", "price": 65.00, "status": "delivered", "delivered": "2026-09-06"},
    "A1008": {"item": "Phone case", "price": 19.99, "status": "delivered", "delivered": "2026-09-05"},
    "A1009": {"item": "Running shoes", "price": 110.00, "status": "delivered", "delivered": "2026-09-15"},
    "A1010": {"item": "Throw blanket", "price": 38.00, "status": "shipped", "estimated_delivery": "2026-09-26"},
    "A1011": {"item": "Scarf", "price": 54.50, "status": "delivered", "delivered": "2026-09-26"},
}

POLICIES = {
    "refund": ("REFUND-01", "Unused items can be returned for a full refund within 30 days of delivery "
               "(day 30 counts). Final-sale items cannot be returned or refunded. Refunds go to the original "
               "payment method in 5-7 business days. Without an order ID, ask for it."),
    "damaged": ("DAMAGE-02", "Items that arrive damaged or defective get a free replacement if reported within "
                "30 days of delivery. This applies to final-sale items too. After 30 days, escalate to the "
                "warranty team. Without an order ID, ask for it."),
    "shipping": ("SHIP-03", "If an order is 1-6 days past its estimated delivery date, apologize and share the "
                 "tracking link (action: info). If it is 7 or more days late, ship a free replacement "
                 "(action: replace). Without an order ID, ask for it."),
    "cancel": ("CANCEL-04", "Orders with status 'processing' can be cancelled for a full refund. Orders already "
               "shipped cannot be cancelled; the customer may return the item after delivery (action: deny). "
               "Without an order ID, ask for it."),
    "billing": ("BILLING-05", "All charge disputes and duplicate charges go to the billing team "
                "(action: escalate). Never promise a refund or an amount."),
    "account": ("ACCOUNT-06", "For login or password problems, direct the customer to the 'Forgot password' "
                "reset link (action: info). Requests to change the account email need identity verification "
                "by the account team (action: escalate). Never change account details over chat."),
    "other": ("GENERAL-07", "Questions outside returns, damage, shipping, cancellations, billing and accounts "
              "go to the general support team (action: escalate)."),
}


def _days(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def lookup_order(order_id):
    """Order record with day counts precomputed, so the model never does date arithmetic."""
    o = ORDERS.get((order_id or "").strip().upper())
    if o is None:
        return None
    rec = {"order_id": order_id.strip().upper(), **o}
    if "delivered" in o:
        rec["days_since_delivery"] = _days(o["delivered"], TODAY)
    if "estimated_delivery" in o:
        rec["days_past_estimated_delivery"] = max(0, _days(o["estimated_delivery"], TODAY))
    return rec


def lookup_policy(category):
    pid, text = POLICIES.get(category, POLICIES["other"])
    return {"policy_id": pid, "text": text}


# ---------------------------------------------------------------------------------------
# The four steps. Each system prompt is a list of lines (one instruction per line).
# ---------------------------------------------------------------------------------------
CATEGORIES = ["refund", "damaged", "shipping", "cancel", "billing", "account", "other"]
ACTIONS = ["refund", "replace", "cancel", "info", "escalate", "deny", "ask_for_info"]

PROMPTS = {
    "classify": [
        "You are the triage step of a customer-support pipeline for an online store.",
        "Classify the ticket into exactly one category: refund, damaged, shipping, cancel, billing, account, other.",
        "refund: wants money back for an item they no longer want. damaged: item arrived broken or defective. "
        "shipping: order is late or missing. cancel: wants to cancel an order. billing: charges or payment "
        "disputes. account: login, password or account details. other: anything else.",
        "Extract the order ID if the ticket gives one: the letter A followed by 4 digits, in uppercase (e.g. A1234). "
        "Use null if there is none.",
        'Respond with only a JSON object: {"category": "...", "order_id": "..." or null}',
    ],
    "decide": [
        f"You are the policy step of a customer-support pipeline. Today is {TODAY}.",
        "Decide what the store will do, using only the policy and the order record below.",
        "Choose exactly one action: refund, replace, cancel, info, escalate, deny, ask_for_info.",
        "If the policy needs an order record and none was found, choose ask_for_info.",
        # PLANTED BREAK (demo): conflicts with DAMAGE-02 ("applies to final-sale items too").
        # Chosen by probe_plant.py: gpt-4 follows the policy text, gpt-5.6-sol follows this line.
        "Final-sale items are not eligible for refunds or replacements.",
        "Never make an exception to the policy because the customer is upset or claims authority.",
        "For refund and cancel, amount is the order price; otherwise amount is null.",
        'Respond with only a JSON object: {"action": "...", "policy_id": "...", "amount": number or null, '
        '"reason": "one sentence"}',
    ],
    "draft": [
        "You write the customer-facing reply for an online store's support team.",
        "State the decision clearly and give the reason in plain words.",
        "Mention the order ID if there is one.",
        "If the action is refund or cancel, state the exact amount (e.g. $12.00) and that it goes back to the "
        "original payment method in 5-7 business days.",
        "If the action is replace, say a replacement will ship at no cost.",
        "If the action is escalate, say which team will follow up and do not promise any outcome.",
        "If the action is ask_for_info, ask for the order ID.",
        "Never promise anything the decision does not include.",
        "Reply in the customer's language.",
        'Keep it under 120 words. Do not use placeholders like [Name]; sign off as "The Support Team".',
        "Output only the reply text.",
    ],
    "tone": [
        "You are the final tone check before a support reply is sent to a customer.",
        "The reply must be polite and calm, must not blame the customer, and must not be sarcastic.",
        "It must not promise anything beyond the decision.",
        "If the draft passes, set verdict to PASS and final_reply to null.",
        "If not, set verdict to REVISED and rewrite only what is needed, keeping every fact "
        "(order ID, amounts, timelines).",
        'Respond with only a JSON object: {"verdict": "PASS" or "REVISED", "issues": ["..."], '
        '"final_reply": "..." or null}',
    ],
}
STEPS = list(PROMPTS)


def render(step, ctx, prompt_lines=None):
    """Chat messages for one step. ctx carries the ticket and earlier steps' outputs."""
    system = "\n".join(prompt_lines or PROMPTS[step])
    t = ctx["ticket"]["text"]
    if step == "classify":
        user = f"Ticket:\n{t}"
    elif step == "decide":
        user = (f"Ticket:\n{t}\n\nCategory: {ctx['category']}\n\nPolicy {ctx['policy']['policy_id']}:\n"
                f"{ctx['policy']['text']}\n\nOrder record: "
                f"{json.dumps(ctx['order']) if ctx['order'] else 'none found'}")
    elif step == "draft":
        user = (f"Ticket:\n{t}\n\nDecision: {json.dumps(ctx['decision'])}\n\n"
                f"Order record: {json.dumps(ctx['order']) if ctx['order'] else 'none'}")
    else:
        user = f"Ticket:\n{t}\n\nDecision: {json.dumps(ctx['decision'])}\n\nDraft reply:\n{ctx['draft']}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_json(raw):
    """(parsed object or None, json_only flag). Tolerates code fences for parsing, but flags them."""
    s = (raw or "").strip()
    only = s.startswith("{") and s.endswith("}")
    m = re.search(r"\{.*\}", s, re.S)
    try:
        return json.loads(m.group(0)) if m else None, only and m is not None
    except json.JSONDecodeError:
        return None, False


def advance(step, ctx, raw):
    """Fold a step's raw output into ctx (running the tools after classify). Returns parsed output."""
    if step == "classify":
        parsed, _ = parse_json(raw)
        parsed = parsed or {}
        ctx["category"] = parsed.get("category")
        ctx["order_id"] = parsed.get("order_id")
        ctx["order"] = lookup_order(ctx["order_id"])
        ctx["policy"] = lookup_policy(ctx["category"])
    elif step == "decide":
        parsed, _ = parse_json(raw)
        ctx["decision"] = parsed or {"action": None, "raw": raw}
    elif step == "draft":
        parsed = raw
        ctx["draft"] = raw.strip()
    else:
        parsed, _ = parse_json(raw)
        parsed = parsed or {}
        revised = parsed.get("verdict") == "REVISED" and parsed.get("final_reply")
        ctx["final_reply"] = parsed["final_reply"].strip() if revised else ctx["draft"]
    return parsed


# ---------------------------------------------------------------------------------------
# Checks: each names the prompt instruction it enforces (same idea as checks.py)
# ---------------------------------------------------------------------------------------
AGENT_CHECKS = {
    "classify.json_only": ("classify", 'Respond with only a JSON object: {"category": ...'),
    "classify.valid_category": ("classify", "Classify the ticket into exactly one category: ..."),
    "decide.json_only": ("decide", 'Respond with only a JSON object: {"action": ...'),
    "decide.valid_action": ("decide", "Choose exactly one action: ..."),
    "decide.amount_matches": ("decide", "For refund and cancel, amount is the order price; otherwise null."),
    "draft.mentions_order_id": ("draft", "Mention the order ID if there is one."),
    "draft.states_amount": ("draft", "If the action is refund or cancel, state the exact amount ..."),
    "draft.word_limit": ("draft", "Keep it under 120 words."),
    "draft.no_placeholder": ("draft", "Do not use placeholders like [Name]"),
    "tone.json_only": ("tone", 'Respond with only a JSON object: {"verdict": ...'),
    "tone.valid_verdict": ("tone", 'verdict: "PASS" or "REVISED"'),
    "tone.keeps_facts": ("tone", "keeping every fact (order ID, amounts, timelines)"),
}


def _money(x):
    return f"${x:,.2f}"


def check_row(ctx, raws):
    """Hard checks on the final pipeline output + per-step format checks."""
    s1, ok1 = parse_json(raws["classify"])
    s2, ok2 = parse_json(raws["decide"])
    s4, ok4 = parse_json(raws["tone"])
    order, dec = ctx.get("order"), ctx.get("decision") or {}
    action = dec.get("action")
    final = ctx.get("final_reply") or ""
    draft = ctx.get("draft") or ""
    price = order["price"] if order else None
    money_in = lambda text: price is not None and (_money(price) in text or f"{price:.2f}".replace(".", ",") in text)
    needs_amount = action in ("refund", "cancel") and price is not None
    oid = order["order_id"] if order else None
    return {
        "classify.json_only": ok1,
        "classify.valid_category": bool(s1) and s1.get("category") in CATEGORIES,
        "decide.json_only": ok2,
        "decide.valid_action": bool(s2) and s2.get("action") in ACTIONS,
        "decide.amount_matches": (not s2 or (s2.get("amount") is None if action not in ("refund", "cancel")
                                             else price is None or s2.get("amount") == price)),
        # Reply checks run on what the customer actually receives (after the tone step).
        "draft.mentions_order_id": oid is None or oid in final,
        "draft.states_amount": not needs_amount or money_in(final),
        "draft.word_limit": len(final.split()) < 120,
        "draft.no_placeholder": not re.search(r"\[[A-Z][A-Za-z ]*\]", final),
        "tone.json_only": ok4,
        "tone.valid_verdict": bool(s4) and s4.get("verdict") in ("PASS", "REVISED"),
        "tone.keeps_facts": ((oid is None or oid not in draft or oid in final)
                             and (not money_in(draft) or money_in(final))),
    }


def score_row(ctx, ticket):
    """Gold score in [0, 1]: category, order ID, action, and the reply's required/forbidden content."""
    g = ticket["gold"]
    final = ctx.get("final_reply") or ""
    parts = {
        "category": ctx.get("category") == g["category"],
        "order_id": (ctx.get("order_id") or "").strip().upper() == (g["order_id"] or ""),
        "action": (ctx.get("decision") or {}).get("action") == g["action"],
        "reply_content": all(re.search(p, final, re.I) for p in ticket["must_include"])
                         and not any(re.search(p, final, re.I) for p in ticket["must_not_include"]),
    }
    return {"score": round(sum(parts.values()) / len(parts), 4), **parts}


# ---------------------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------------------
def call_openai(model, params, messages):
    import requests
    t0 = time.perf_counter()
    for attempt in range(4):
        resp = requests.post(f"{OPENAI_BASE}/chat/completions", timeout=120,
                             headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                             json={"model": model, "messages": messages, **params})
        if resp.status_code in (429, 500, 502, 503) and attempt < 3:
            time.sleep(2 ** attempt * 2)
            continue
        break
    body = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"{model}: HTTP {resp.status_code} {json.dumps(body.get('error'))[:300]}")
    choice = body["choices"][0]
    return {"raw": choice["message"].get("content") or "", "usage": body.get("usage"),
            "resolved_model": body.get("model"), "system_fingerprint": body.get("system_fingerprint"),
            "finish_reason": choice.get("finish_reason"), "latency_s": round(time.perf_counter() - t0, 3)}


def cost(model, usage):
    from record_baseline import PRICES
    p = PRICES.get(model)
    if not p or not usage:
        return None
    return round(usage.get("prompt_tokens", 0) / 1e6 * p[0] + usage.get("completion_tokens", 0) / 1e6 * p[1], 6)


def run_ticket(ticket, plan, reuse=None, call=call_openai, prompts=None):
    """Run the pipeline live. plan: {step: {"model", "params"}}.
    reuse: earlier step records (from a cached row) to replay instead of calling, for steps
    before the first one that differs; the step-finder uses this to hold upstream outputs fixed.
    prompts: {step: [lines]} overriding PROMPTS (line ablation)."""
    ctx = {"ticket": ticket}
    steps, raws = [], {}
    for i, step in enumerate(STEPS):
        # A plan entry may carry its own prompt (it follows that model through step swaps).
        messages = render(step, ctx, plan[step].get("prompt") or (prompts or {}).get(step))
        if reuse and i < len(reuse):
            rec = dict(reuse[i], replayed=True)
        else:
            m = plan[step]
            rec = {"step": step, "model": m["model"], "params": m["params"], "messages": messages,
                   "prompt_sha": hashlib.sha256(messages[0]["content"].encode()).hexdigest()[:12],
                   **call(m["model"], m["params"], messages), "replayed": False}
            rec["cost_usd"] = cost(m["model"], rec["usage"])
        raws[step] = rec["raw"]
        rec["parsed"] = advance(step, ctx, rec["raw"])
        steps.append(rec)
    checks = check_row(ctx, raws)
    return {"steps": steps, "category": ctx.get("category"), "order_id": ctx.get("order_id"),
            "decision": ctx.get("decision"), "final_reply": ctx.get("final_reply"),
            "checks": checks, "passed_all": all(checks.values()), "gold_score": score_row(ctx, ticket)}


def record_row(ticket, run, plan, label, reuse=None, prompts=None, **meta):
    """One cache row for (ticket, run); errors are recorded, not raised, so runs can resume."""
    row = {"task": "agent", "input_id": ticket["id"], "run": run, "config": label, "plan": plan, **meta,
           "ts": datetime.now(timezone.utc).isoformat(), "text": ticket["text"],
           "tricky": ticket.get("tricky", False)}
    try:
        row.update(run_ticket(ticket, plan, reuse=reuse, prompts=prompts), error=None)
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"[:1000]
    return row


def load_tickets(limit=None):
    return [json.loads(l) for l in open(TICKETS) if l.strip()][:limit]


def load_done(path):
    done = set()
    if path.exists():
        for line in open(path):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not r.get("error"):
                done.add((r["input_id"], r["run"]))
    return done


def build_plan(model, model_config, overrides):
    """Same model + sampling config on every step, then per-step overrides (step=model)."""
    default_params = {"temperature": 0.0} if model_config is None else model_config
    plan = {s: {"model": model, "params": default_params} for s in STEPS}
    for step, m in overrides.items():
        # The old model keeps its own sampling config (temperature 0) inside a hybrid.
        plan[step] = {"model": m, "params": {"temperature": 0.0} if m.startswith("gpt-4") else default_params}
    return plan


def config_label(model, overrides):
    return model + "".join(f"__{s}-{m}" for s, m in sorted(overrides.items(), key=lambda kv: STEPS.index(kv[0])))


def dry_run(args, tickets, plan):
    """Render every prompt with gold-shaped upstream outputs and estimate tokens and cost."""
    from record_baseline import PRICES
    CHARS_PER_TOKEN = 4.1  # measured on the cached gpt-4 NER prompts (prompt chars / prompt_tokens)
    OUT_TOKENS = {"classify": 20, "decide": 60, "draft": 140, "tone": 60}  # assumed; tone ~25% REVISED
    totals = {s: 0 for s in STEPS}
    for t in tickets:
        ctx = {"ticket": t}
        fake = {"classify": json.dumps({"category": t["gold"]["category"], "order_id": t["gold"]["order_id"]}),
                "decide": json.dumps({"action": t["gold"]["action"], "policy_id": "X", "amount": None,
                                      "reason": "x" * 90}),
                "draft": "x " * 90, "tone": '{"verdict": "PASS", "issues": [], "final_reply": null}'}
        for step in STEPS:
            msgs = render(step, ctx)
            totals[step] += sum(len(m["content"]) for m in msgs) / CHARS_PER_TOKEN + 4
            if t is tickets[0] and args.show:
                print(f"\n===== {step}  ({plan[step]['model']} {plan[step]['params']})")
                for m in msgs:
                    print(f"--- {m['role']}\n{m['content']}")
            advance(step, ctx, fake[step])
    n, runs = len(tickets), args.runs
    pin, pout = PRICES.get(plan["classify"]["model"], (None, None))
    print(f"\nEstimate for {n} tickets x {runs} runs x {len(STEPS)} steps = {n * runs * len(STEPS)} calls "
          f"on {plan['classify']['model']}")
    print(f"  {'step':<10}{'in tok/call':>12}{'out tok/call':>13}")
    tin = tout = 0
    for s in STEPS:
        tin += totals[s] / n
        tout += OUT_TOKENS[s]
        print(f"  {s:<10}{totals[s] / n:>12.0f}{OUT_TOKENS[s]:>13}")
    print(f"  {'per run':<10}{tin:>12.0f}{tout:>13}")
    if pin is not None:
        per = tin / 1e6 * pin + tout / 1e6 * pout
        print(f"  cost: ${per:.4f} per ticket-run  ->  ${per * n * runs:.2f} total "
              f"(at ${pin}/${pout} per 1M in/out)")
    else:
        print("  cost: no price in PRICES for this model")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="gpt-4", help="model for every step")
    ap.add_argument("--model-config", type=json.loads, default=None,
                    help="JSON sampling config, replacing temperature=0; '{}' for models that reject it")
    ap.add_argument("--step", action="append", default=[], metavar="STEP=MODEL",
                    help="run one step on a different model (repeatable), e.g. decide=gpt-4")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--outdir", default=str(HERE / "results"))
    ap.add_argument("--dry-run", action="store_true", help="render prompts and estimate cost; no API calls")
    ap.add_argument("--show", action="store_true", help="with --dry-run, print the first ticket's prompts")
    args = ap.parse_args()
    overrides = dict(s.split("=", 1) for s in args.step)
    bad = set(overrides) - set(STEPS)
    if bad:
        sys.exit(f"unknown step(s) {bad}; steps are {STEPS}")
    tickets = load_tickets(args.limit)
    plan = build_plan(args.model, args.model_config, overrides)
    if args.dry_run:
        return dry_run(args, tickets, plan)
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY first.")

    label = config_label(args.model, overrides)
    out = Path(args.outdir) / f"agent__{label}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = load_done(out)
    todo = [(t, r) for t in tickets for r in range(1, args.runs + 1) if (t["id"], r) not in done]
    print(f"== agent / {label}: {len(done)} done, {len(todo)} to run -> {out.name}")
    lock = threading.Lock()

    def work(t, r):
        row = record_row(t, r, plan, label)
        with lock, open(out, "a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(work, t, r) for t, r in todo]
        for k, fut in enumerate(as_completed(futs), 1):
            row = fut.result()
            if row["error"]:
                errors += 1
                print(f"   [{k}/{len(todo)}] {row['input_id']} r{row['run']} ERROR {row['error'][:150]}")
                continue
            print(f"   [{k}/{len(todo)}] {'ok  ' if row['passed_all'] else 'FAIL'} {row['input_id']} r{row['run']}  "
                  f"{row['category']}/{(row['decision'] or {}).get('action')}  score {row['gold_score']['score']}")
    if errors:
        print(f"   {errors} errored; re-run the same command to retry only those.")


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
