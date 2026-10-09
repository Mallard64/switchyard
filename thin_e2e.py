"""Thin end-to-end: old-model baseline vs 1-2 new models on the support agent, written for the dashboard.

  python thin_e2e.py                                   # gpt-4 vs gpt-5.6-sol + gpt-5.6-terra, cached runs
  python thin_e2e.py --candidates gpt-5.6-terra        # one candidate

Per run, three hard checks (all must pass):
  valid_json       classify, decide and tone each returned a parseable JSON object
  required_fields  each of those objects has every field its prompt asks for
  label_match      category and action equal the ticket's expected outcome
Per ticket, the noise rule: "broken" only if the old model passed >= 2/3 of its runs and the new model
<= 1/3; "fixed" is the reverse; anything else is "same". Old-model run-to-run noise can't flag a ticket.

Reads cached results/agent__<model>.jsonl (never writes them). Writes public/results.json in the
dashboard's v1 schema (dashboard/results.sample.json). Step-finder causes are merged in when
reports/overnight/stepswap_<model>.json exists (stepswap.py).
"""
import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import support_agent as A  # noqa: E402

RESULTS = HERE / "results"
OUT = HERE / "public" / "results.json"
STEPSWAP = HERE / "reports" / "overnight"
REQUIRED = {"classify": ("category", "order_id"),
            "decide": ("action", "policy_id", "amount", "reason"),
            "tone": ("verdict", "issues", "final_reply")}
CHECKS = ("valid_json", "required_fields", "label_match")


def run_checks(row, ticket):
    """The three hard checks for one cached run. An errored run fails all of them."""
    if row.get("error") or not row.get("steps"):
        return {c: False for c in CHECKS}
    parsed = {s["step"]: A.parse_json(s["raw"])[0] for s in row["steps"]}
    objs = {st: parsed.get(st) for st in REQUIRED}
    valid = all(isinstance(o, dict) for o in objs.values())
    fields = valid and all(all(f in objs[st] for f in fs) for st, fs in REQUIRED.items())
    gold = ticket["gold"]
    labels = valid and objs["classify"].get("category") == gold["category"] and objs["decide"].get("action") == gold["action"]
    return {"valid_json": valid, "required_fields": bool(fields), "label_match": bool(labels)}


def passes(rows, ticket):
    return [all(run_checks(r, ticket).values()) for r in rows]


def verdict(old_passed, old_total, new_passed, new_total):
    """The noise rule, as fractions so it holds for any run count (3 runs: >= 2/3 and <= 1/3)."""
    if old_total == 0 or new_total == 0:
        return "unknown"
    old, new = old_passed / old_total, new_passed / new_total
    if old >= 2 / 3 and new <= 1 / 3:
        return "broken"
    if old <= 1 / 3 and new >= 2 / 3:
        return "fixed"
    return "same"


def load_rows(path):
    rows = {}
    for line in open(path):
        r = json.loads(line)
        rows.setdefault(r["input_id"], {})[r["run"]] = r  # a later row for the same run (a retry) wins
    return {tid: [runs[k] for k in sorted(runs)] for tid, runs in rows.items()}


def describe(row):
    if not row or row.get("error"):
        return f"error: {row.get('error') if row else 'no run'}"
    d = row.get("decision") or {}
    return f"category={row.get('category')} action={d.get('action')}\n{row.get('final_reply') or ''}".strip()


def run_cost(row):
    usd = [s.get("cost_usd") if s.get("cost_usd") is not None else A.cost(s["model"], s.get("usage"))
           for s in row.get("steps") or []]
    return None if not usd or any(u is None for u in usd) else sum(usd)


def run_ms(row):
    return sum(s.get("latency_s") or 0 for s in row.get("steps") or []) * 1000


def stats(rows_by_ticket):
    rows = [r for rs in rows_by_ticket.values() for r in rs if not r.get("error")]
    costs = [run_cost(r) for r in rows]
    known = [c for c in costs if c is not None]
    return {"cost_per_1k": round(statistics.mean(known) * 1000, 2) if known and len(known) == len(costs) else None,
            "p50_ms": round(statistics.median(run_ms(r) for r in rows)) if rows else None}


def compare(old_rows, new_rows, tickets):
    out = []
    for t in tickets:
        o, n = old_rows.get(t["id"], []), new_rows.get(t["id"], [])
        op, np_ = passes(o, t), passes(n, t)
        failed = sorted({c for r in n for c, ok in run_checks(r, t).items() if not ok})
        out.append({"ticket": t, "old_runs": o, "new_runs": n, "old_passed": sum(op), "new_passed": sum(np_),
                    "verdict": verdict(sum(op), len(op), sum(np_), len(np_)), "failed_checks": failed})
    return out


def check_causes(model, items):
    """Without step-finder results: one cause per failing hard check, so every broken ticket has a cause."""
    causes = {}
    for it in items:
        if it["verdict"] == "broken":
            c = it["failed_checks"][0] if it["failed_checks"] else "label_match"
            causes.setdefault(c, []).append(it["ticket"]["id"])
            it["cause_id"] = f"{model}:{c}"
    return [{"id": f"{model}:{c}", "line": None, "title": f"{model}: hard check `{c}` fails on {len(ids)} ticket(s)",
             "fixed_title": f"{model}: `{c}` passes again", "check": f"hard_check: {c}",
             "reason": f"Broken under the noise rule (old >= 2/3 runs pass, new <= 1/3): {', '.join(ids)}. "
                       "Step-finder not run for this model yet.",
             "fix": {"old_line": "", "new_line": "", "status": "none", "retest": None}}
            for c, ids in causes.items()]


def step_causes(model, items, sw, retest=None):
    """With stepswap.py results: one cause per guilty step ('step X causes N% of failures').
    retest: {"passed", "total"} from a full re-run with the top line removed, when one exists."""
    by_ticket = {t["ticket"]: t for t in sw["tickets"]}
    n = len(sw["tickets"])
    causes = []
    for step, s in sw["summary"].items():
        if not s["necessary"]:
            continue
        lf = (sw.get("lines") or {}).get(step) or {}
        top = lf.get("top")
        causes.append({
            "id": f"{model}:{step}", "line": top["line_index"] + 1 if top else None, "step": step,
            "title": f"{model}: step `{step}` causes {s['necessary']}/{n} failures ({s['necessary'] / n:.0%})",
            "fixed_title": f"{model}: step `{step}` repaired",
            "check": "step_swap",
            "reason": f"Putting the old model back at `{step}` alone repairs {s['necessary']}/{n} broken tickets "
                      f"(rescue); putting the new model at `{step}` alone breaks {s['sufficient']} of those again "
                      f"(break). 3 runs each, live, judged with the same noise rule.",
            "fix": {"old_line": top["line"] if top else "", "new_line": "",
                    "status": ("verified" if retest["passed"] == retest["total"] else "failed")
                    if top and retest else "none", "retest": retest if top else None}})
    for it in items:
        if it["verdict"] == "broken":
            st = (by_ticket.get(it["ticket"]["id"]) or {}).get("necessary") or []
            it["cause_id"] = f"{model}:{st[0]}" if st else None
    unexplained = [it["ticket"]["id"] for it in items if it["verdict"] == "broken" and not it.get("cause_id")]
    if unexplained:
        causes.append({"id": f"{model}:unexplained", "line": None, "step": None, "check": "step_swap",
                       "title": f"{model}: no single step explains {len(unexplained)} failure(s)",
                       "fixed_title": "", "reason": f"Rescue at every single step left these broken: {', '.join(unexplained)}.",
                       "fix": {"old_line": "", "new_line": "", "status": "none", "retest": None}})
        for it in items:
            if it["verdict"] == "broken" and not it.get("cause_id"):
                it["cause_id"] = f"{model}:unexplained"
    return causes


def removal_retest(old_rows, model, sw, tickets, results):
    """Full re-run with the guilty line removed. The only such run is the planted line's (--no-plant)."""
    tops = [v["top"]["line"] for v in (sw.get("lines") or {}).values() if v.get("top")]
    path = results / f"agent__{model}__noplant.jsonl"
    if A.PLANTED[1] not in tops or not path.exists():
        return None
    return compare(old_rows, load_rows(path), tickets)


def build(old, candidates, tickets, results=RESULTS, stepswap_dir=STEPSWAP):
    old_rows = load_rows(results / f"agent__{old}.jsonl")
    ost = stats(old_rows)
    noisy = sum(1 for t in tickets if 0 < sum(passes(old_rows.get(t["id"], []), t)) < len(old_rows.get(t["id"], [])))
    cands, causes, step_finder, guilty = [], [], {}, None
    for model in candidates:
        new_rows = load_rows(results / f"agent__{model}.jsonl")
        items = compare(old_rows, new_rows, tickets)
        sw_path = stepswap_dir / f"stepswap_{model}.json"
        sw = json.loads(sw_path.read_text()) if sw_path.exists() else None
        fix_items = None
        if sw:
            fix_items = removal_retest(old_rows, model, sw, tickets, results)
            retest = {"passed": sum(f["verdict"] != "broken" for f in fix_items), "total": len(fix_items)} if fix_items else None
            causes += step_causes(model, items, sw, retest)
            step_finder[model] = {"broken": len(sw["tickets"]), "summary": sw["summary"], "sentences": sw["sentences"],
                                  "lines": sw.get("lines") or {}}
            ranked = sorted(sw["summary"].items(), key=lambda kv: -kv[1]["necessary"])
            guilty = guilty or (ranked[0][0] if ranked and ranked[0][1]["necessary"] else None)
        else:
            causes += check_causes(model, items)
        after = {f["ticket"]["id"]: f for f in fix_items or []}
        nst = stats(new_rows)
        counts = {v: sum(it["verdict"] == v for it in items) for v in ("broken", "fixed", "same", "unknown")}
        cands.append({
            "model": model, "provider": "OpenAI",
            "summary": {"passed": len(items) - counts["broken"], "total": len(items),
                        "passed_after_fix": sum(f["verdict"] != "broken" for f in fix_items) if fix_items
                        else len(items) - counts["broken"],
                        "cost_change_pct": round((nst["cost_per_1k"] / ost["cost_per_1k"] - 1) * 100)
                        if nst["cost_per_1k"] and ost["cost_per_1k"] else None,
                        "cost_per_1k_calls_usd": nst["cost_per_1k"], "latency_p50_ms": nst["p50_ms"],
                        "speed_vs_baseline": round(ost["p50_ms"] / nst["p50_ms"], 2) if nst["p50_ms"] else None,
                        "recommended": counts["broken"] == 0, "verdict_counts": counts},
            "results": [{"input_id": it["ticket"]["id"],
                         "status": {"broken": "regressed", "fixed": "improved"}.get(it["verdict"], "pass"),
                         "score": round(statistics.mean(r["gold_score"]["score"] for r in it["new_runs"]
                                                        if r.get("gold_score")), 4)
                         if any(r.get("gold_score") for r in it["new_runs"]) else 0,
                         "runs": {"passed": it["new_passed"], "total": len(it["new_runs"])},
                         "baseline_runs": {"passed": it["old_passed"], "total": len(it["old_runs"])},
                         "failed_checks": it["failed_checks"],
                         "baseline_output": describe(it["old_runs"][0] if it["old_runs"] else None),
                         "output": describe(it["new_runs"][0] if it["new_runs"] else None),
                         "cause_id": it.get("cause_id"),
                         "after_fix_output": describe(after[it["ticket"]["id"]]["new_runs"][0])
                         if it["verdict"] == "broken" and it["ticket"]["id"] in after else None} for it in items]})
    step = guilty or "decide"
    return {
        "schema_version": 1,
        "_note": "Real cached runs of the 4-step support agent (support_agent.py), not sample data. "
                 "cost_per_1k_calls_usd is per 1k ticket runs (4 calls each).",
        "run_id": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": {"name": "upshift support agent (demo)", "call_site": "support_agent.py: PROMPTS / run_ticket"},
        "baseline": {"model": old, "provider": "OpenAI", "retires_on": "2026-10-23", "runs_per_input": 3,
                     "noise_rate": round(noisy / len(tickets), 4), "cost_per_1k_calls_usd": ost["cost_per_1k"],
                     "latency_p50_ms": ost["p50_ms"]},
        "prompt": {"file": f"support_agent.py PROMPTS['{step}']", "lines": A.PROMPTS[step]},
        "inputs": [{"id": t["id"], "text": t["text"]} for t in tickets],
        "candidates": cands,
        "causes": causes,
        "pr": {"status": "none", "title": f"Move the support agent off {old}", "url": None, "branch": None,
               "lines_changed": 0},
        "rule": "broken = old passed >= 2/3 runs and new passed <= 1/3 (hard checks: " + ", ".join(CHECKS) + ")",
        **({"step_finder": step_finder} if step_finder else {}),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", default="gpt-4")
    ap.add_argument("--candidates", nargs="+", default=["gpt-5.6-sol", "gpt-5.6-terra"])
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    tickets = A.load_tickets()
    data = build(args.old, args.candidates, tickets)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n")
    for c in data["candidates"]:
        s = c["summary"]
        broken = [r["input_id"] for r in c["results"] if r["status"] == "regressed"]
        print(f"{c['model']}: {s['verdict_counts']}  broken={broken}  cost/1k=${s['cost_per_1k_calls_usd']}  "
              f"p50={s['latency_p50_ms']}ms")
    for c in data["causes"]:
        print(f"  cause {c['id']}: {c['title']}")
    print(f"baseline {args.old}: noise {data['baseline']['noise_rate']:.0%}, cost/1k=${data['baseline']['cost_per_1k_calls_usd']}")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
