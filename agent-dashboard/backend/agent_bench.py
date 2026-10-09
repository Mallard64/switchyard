"""Step-finder benchmark: plant breaks in known steps of the support agent and check that the
step-finder names exactly those steps. Also asks an LLM "which step failed?" on the same tickets,
as the comparison the pitch cites (LLM step attribution is right ~14-29% on Who&When).

Setup (all cheap models; written before any run):
  old = gpt-5.6-terra, new = gpt-5.6-sol, both at their default sampling.
  A break simulates "the new model misreads step k": it changes only the NEW model's prompt for
  step k (the old model keeps the clean prompt), so the true cause is known.
  Each break's candidate run replays the steps before the broken one from sol's clean cached run.
  Tickets that already regress with clean sol vs terra ("background") are excluded from scoring.

  python agent_bench.py --dry-run     # list breaks, no calls
  python agent_bench.py               # run everything (cached + resumable)
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import support_agent as A  # noqa: E402
from compare import build_report, load  # noqa: E402
from stepfinder import localize, new_calls_cost, plan_of, run_config, spec  # noqa: E402

RESULTS = HERE / "results"
OUT = HERE / "reports" / "agent_bench"
OLD = spec("gpt-5.6-terra", {})
NEW = spec("gpt-5.6-sol", {})
P = A.PROMPTS


def replaced(step, i, line):
    return P[step][:i] + [line] + P[step][i + 1:]


def appended(step, line):
    return P[step][:-1] + [line, P[step][-1]]  # keep the output-format line last


ORDER_DIGITS = "Extract the order ID as its 4 digits only, without the letter (e.g. 1234). Use null if there is none."
NO_FACTS = "Do not mention order IDs, amounts or timelines; keep it general."
# Held-out breaks, written Oct 7 before any run. {step: new prompt lines} for the NEW model only.
BREAKS = [
    {"id": "c1", "desc": "classify: money-back requests routed to billing",
     "prompts": {"classify": appended("classify", "Treat any request for money back as billing.")}},
    {"id": "c2", "desc": "classify: order ID extracted without its letter",
     "prompts": {"classify": replaced("classify", 3, ORDER_DIGITS)}},
    {"id": "d1", "desc": "decide: upset or urgent customers get what they ask for",
     "prompts": {"decide": appended("decide", "If the customer is upset or says it is urgent, approve their request.")}},
    {"id": "d2", "desc": "decide: damaged items refunded instead of replaced",
     "prompts": {"decide": appended("decide", "For damaged items, refund the order price instead of sending a "
                                              "replacement.")}},
    {"id": "r1", "desc": "draft: no order IDs, amounts or timelines",
     "prompts": {"draft": replaced("draft", 2, NO_FACTS)}},
    {"id": "r2", "desc": "draft: always promise a refund if still unhappy",
     "prompts": {"draft": appended("draft", "Always end by saying we will refund them in full if they are still "
                                            "unhappy.")}},
    {"id": "t1", "desc": "tone: always rewrite to at most 20 words",
     "prompts": {"tone": appended("tone", "Always set verdict to REVISED and shorten the reply to at most 20 words.")}},
    {"id": "t2", "desc": "tone: JSON wrapped in a code block",
     "prompts": {"tone": replaced("tone", len(P["tone"]) - 1, "Respond with the JSON object inside a ```json code "
                                  "block: {\"verdict\": \"PASS\" or \"REVISED\", \"issues\": [\"...\"], "
                                  "\"final_reply\": \"...\" or null}")}},
    {"id": "p1", "desc": "two steps: classify drops the order ID letter AND draft omits IDs/amounts",
     "prompts": {"classify": replaced("classify", 3, ORDER_DIGITS), "draft": replaced("draft", 2, NO_FACTS)}},
]

# Downstream-symptom set (Oct 7, written before any run): the cause is planted in one step but the
# visible error shows up in a later step, so a trace reader is tempted to blame the later step.
DOWNSTREAM = [
    {"id": "u1", "desc": "classify calls damaged-item tickets refunds (symptom: decide refunds instead of replacing)",
     "prompts": {"classify": appended("classify", "Tickets about broken or damaged items are refund requests.")}},
    {"id": "u2", "desc": "decide deducts a 15% restocking fee (symptom: the reply states the wrong amount)",
     "prompts": {"decide": appended("decide", "For refunds and cancellations, deduct a 15% restocking fee from the "
                                              "amount.")}},
    {"id": "u3", "desc": "classify calls late or missing orders cancellations (symptom: decide denies or cancels)",
     "prompts": {"classify": appended("classify", "Tickets about late or missing orders are cancel requests.")}},
]
BREAK_SETS = {"v1": BREAKS, "downstream": DOWNSTREAM}

ATTRIBUTION_SYSTEM = (
    "You debug a 4-step customer-support pipeline: classify -> decide -> draft -> tone. After a model upgrade, "
    "this ticket's final result became wrong. You get the step instructions, the expected outcome, and the "
    "full trace of the failing run. Name the one step whose output first went wrong and caused the failure.")
ATTRIBUTION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["step", "reason"],
                      "properties": {"step": {"type": "string", "enum": A.STEPS}, "reason": {"type": "string"}}}


def attribute_with_llm(ticket, row, model="gpt-5.6-sol", info="gold", old_row=None):
    """Baseline: ask an LLM which step failed, from the failing trace (Who&When-style).
    info="gold": also gets the expected category, order ID, action and reply rules (easy mode).
    info="final": gets only what a migration has: the old model's final decision and reply."""
    trace = "\n\n".join(f"[{s['step']}] output:\n{s['raw']}" for s in row["steps"])
    steps = "\n\n".join(f"[{s}] instructions:\n" + "\n".join(P[s]) for s in A.STEPS)
    g = ticket["gold"]
    if info == "gold":
        expected = (f"Expected: category {g['category']}, order ID {g['order_id']}, action {g['action']}; reply must "
                    f"include {ticket['must_include']} and must not include {ticket['must_not_include']}.")
    else:
        expected = (f"Before the upgrade, the pipeline's final result was: action {(old_row['decision'] or {}).get('action')}"
                    f"; reply:\n{old_row['final_reply']}")
    user = (f"{steps}\n\nTicket:\n{ticket['text']}\n\n{expected}\n\nFailing trace (after the upgrade):\n{trace}"
            f"\n\nWhich step first went wrong?")
    out = A.call_openai(model, {"response_format": {"type": "json_schema", "json_schema": {
        "name": "attribution", "strict": True, "schema": ATTRIBUTION_SCHEMA}}},
        [{"role": "system", "content": ATTRIBUTION_SYSTEM}, {"role": "user", "content": user}])
    return json.loads(out["raw"]), A.cost(model, out["usage"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-tickets", type=int, default=8, help="localize at most this many regressed tickets per break")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--only", nargs="+")
    ap.add_argument("--no-llm-baseline", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--set", choices=sorted(BREAK_SETS), default="v1")
    ap.add_argument("--old", default=OLD["label"])
    ap.add_argument("--new", default=NEW["label"])
    ap.add_argument("--attributor", default="gpt-5.6-sol", help="model for the LLM 'which step failed?' baseline")
    ap.add_argument("--attr-info", choices=["gold", "final"], default="gold",
                    help="what the attributor sees besides the trace (final = only the old model's final output)")
    args = ap.parse_args()
    global OUT
    old_spec, new_spec = spec(args.old, {}), spec(args.new, {})
    if (args.set, args.old, args.new) != ("v1", OLD["label"], NEW["label"]):
        OUT = OUT.with_name(f"agent_bench_{args.set}_{args.old}_{args.new}")
    breaks = [b for b in BREAK_SETS[args.set] if not args.only or b["id"] in args.only]
    if args.dry_run:
        for b in breaks:
            print(f"{b['id']}: {b['desc']}  (steps {sorted(b['prompts'], key=A.STEPS.index)})")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    tickets = {t["id"]: t for t in A.load_tickets()}
    base_path, clean_path = RESULTS / f"agent__{old_spec['label']}.jsonl", RESULTS / f"agent__{new_spec['label']}.jsonl"
    background = [it["input_id"] for it in build_report(base_path, clean_path)["items"] if it["verdict"] == "REGRESSED"]
    print(f"background (clean {new_spec['label']} vs {old_spec['label']}): {background}")
    clean_src = {tid: {r["run"]: r for r in rs} for tid, rs in load(clean_path).items()}
    attributions_path = OUT / "llm_attributions.jsonl"
    cached_attr = {}
    if attributions_path.exists():
        for line in open(attributions_path):
            r = json.loads(line)
            cached_attr[(r["break"], r["input_id"])] = r
    results, files = [], []

    for b in breaks:
        planted = sorted(b["prompts"], key=A.STEPS.index)
        new_b = spec(new_spec["model"], new_spec["params"], b["prompts"], f"{new_spec['label']}+{b['id']}")
        first = min(A.STEPS.index(s) for s in planted)
        cand_path = run_config(list(tickets.values()), new_b["label"], plan_of(new_b, {}), clean_src, first,
                               args.runs, RESULTS, args.workers)
        files.append(cand_path)
        rep = build_report(base_path, cand_path)
        regressed = [it["input_id"] for it in rep["items"]
                     if it["verdict"] == "REGRESSED" and it["input_id"] not in background]
        sample = regressed[: args.max_tickets]
        print(f"\n[{b['id']}] {b['desc']}: {len(regressed)} regressed (excl. background); localizing {sample}")
        r = {"id": b["id"], "desc": b["desc"], "planted_steps": planted, "regressed": regressed,
             "localized": sample, "tickets": []}
        if sample:
            # Localize only the sampled tickets: a candidate file restricted to them.
            sub = cand_path.with_name(cand_path.stem + f"__sample{len(sample)}.jsonl")
            sub.write_text("".join(l for l in open(cand_path) if json.loads(l)["input_id"] in sample))
            loc = localize(old_spec, new_b, base_path, sub, args.runs, RESULTS, args.workers)
            r["live_calls_localize"] = loc["live_calls"]
            cand_rows, base_rows = load(cand_path), load(base_path)
            for tk in loc["tickets"]:
                tid = tk["input_id"]
                found = tk["causal_steps"]
                # correct = exactly the planted steps, or (multi-step plant) a confirmed subset of them:
                # on some tickets only one of the planted breaks matters.
                verdict = ("correct" if found == planted
                           else "correct_subset" if found and set(found) < set(planted) and tk["status"] == "confirmed"
                           else "unexplained" if not found
                           else "over_attributed" if set(planted) <= set(found)
                           else "partial" if set(found) & set(planted) else "wrong")
                llm = None
                if not args.no_llm_baseline:
                    key = (b["id"], tid)
                    if key not in cached_attr:
                        ans, cost = attribute_with_llm(tickets[tid], cand_rows[tid][0], args.attributor,
                                                       args.attr_info, base_rows[tid][0])
                        cached_attr[key] = {"break": b["id"], "input_id": tid, "step": ans["step"],
                                            "reason": ans["reason"], "cost_usd": cost}
                        with open(attributions_path, "a") as f:
                            f.write(json.dumps(cached_attr[key]) + "\n")
                    llm = cached_attr[key]["step"]
                # The LLM names one step; for the two-step break, naming either planted step counts.
                r["tickets"].append({"input_id": tid, "causal_steps": found, "status": tk["status"],
                                     "verdict": verdict, "llm_says": llm,
                                     "llm_correct": None if llm is None else llm in planted})
                print(f"   {tid}: step-finder {found} ({tk['status']}) -> {verdict};  LLM says {llm}")
        results.append(r)

    scored = [t for r in results for t in r["tickets"]]
    n = len(scored)
    summary = {
        "old": old_spec["label"], "new": new_spec["label"], "runs": args.runs, "max_tickets_per_break": args.max_tickets,
        "break_set": args.set, "attributor": args.attributor, "attributor_info": args.attr_info,
        "background": background, "breaks": len(results),
        "breaks_with_effect": sum(bool(r["regressed"]) for r in results), "tickets_localized": n,
        "stepfinder": {v: sum(t["verdict"] == v for t in scored)
                       for v in ("correct", "correct_subset", "over_attributed", "partial", "wrong", "unexplained")},
        "llm_baseline_exact": None if args.no_llm_baseline else sum(
            t["llm_says"] is not None and [t["llm_says"]] == r["planted_steps"] for r in results for t in r["tickets"]),
        "stepfinder_confirmed_both_ways": sum(t["status"] == "confirmed" for t in scored),
        "llm_baseline_correct": None if args.no_llm_baseline else sum(bool(t["llm_correct"]) for t in scored),
        "breaks_detail": results,
        "live_calls_candidate_runs": new_calls_cost(files),
        "llm_baseline_cost_usd": round(sum(r.get("cost_usd") or 0 for r in cached_attr.values()), 4),
    }
    (OUT / "results.json").write_text(json.dumps(summary, indent=2))
    print(f"\nbreaks with an effect: {summary['breaks_with_effect']}/{len(results)}; tickets localized: {n}")
    print(f"step-finder: {summary['stepfinder']}  (confirmed both ways: {summary['stepfinder_confirmed_both_ways']})")
    if summary["llm_baseline_correct"] is not None:
        print(f"LLM 'which step failed?' baseline: names a planted step {summary['llm_baseline_correct']}/{n}, "
              f"exactly the planted step(s) {summary['llm_baseline_exact']}/{n}")
    print(f"-> {OUT / 'results.json'}")


if __name__ == "__main__":
    main()
