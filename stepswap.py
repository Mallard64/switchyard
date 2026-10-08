"""Step-finder with the overnight brief's rule: rescue + break at every step, then (optionally) line ablation.

  python stepswap.py --new gpt-5.6-sol                # rescue/break for every broken ticket
  python stepswap.py --new gpt-5.6-sol --lines        # + ablate each line of the guilty step(s)

For each ticket thin_e2e.py calls "broken" (old passed >= 2/3 runs, new <= 1/3), and each step k:
  rescue  new model everywhere, old model at step k. Steps before k are replayed from the new model's
          own run, so only step k onward changes. Repaired if the hybrid passes >= 2/3 runs.
  break   old model everywhere, new model at step k (steps before k replayed from the old run).
          Reproduces the failure if the hybrid is "broken" by the same rule.
Step k "causes" a ticket's failure when rescue at k repairs it; it is confirmed both ways when break at
k also reproduces it. --lines: for each guilty step, remove one prompt line at a time with the new
model everywhere; a line is causal when removing it alone repairs the ticket.
Every run is live end to end, 3 runs each, cached (results/agent__<config>.jsonl and llm.py's hash cache).
Writes reports/overnight/stepswap_<new>.json; thin_e2e.py merges it into public/results.json.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import support_agent as A  # noqa: E402
import thin_e2e as T  # noqa: E402
from stepfinder import label_of, plan_of, run_config, spec  # noqa: E402

OUT = HERE / "reports" / "overnight"
RESULTS = HERE / "results"
OLD_PARAMS = {"temperature": 0.0}


def swap_plans(old, new):
    """{step: {"rescue": (label, plan, first_live, source), "break": ...}} for old/new spec() dicts.
    first_live is the step's index: earlier steps replay from the source side's own run."""
    out = {}
    for k, step in enumerate(A.STEPS):
        out[step] = {"rescue": (label_of(new, {step: old}), plan_of(new, {step: old}), k, "new"),
                     "break": (label_of(old, {step: new}), plan_of(old, {step: new}), k, "old")}
    return out


def repaired(passed, total):
    return total > 0 and passed / total >= 2 / 3


def step_summary(tickets):
    """tickets: [{"necessary": [steps], "sufficient": [steps]}] -> per-step counts and the headline sentences."""
    n = len(tickets)
    summary = {st: {"necessary": sum(st in t["necessary"] for t in tickets),
                    "sufficient": sum(st in t["sufficient"] for t in tickets)} for st in A.STEPS}
    sentences = [f"step {st} causes {s['necessary'] / n:.0%} of failures ({s['necessary']}/{n}; "
                 f"{s['sufficient']} confirmed by break)" for st, s in summary.items() if n and s["necessary"]]
    unexplained = sum(not t["necessary"] for t in tickets)
    if unexplained:
        sentences.append(f"no single step explains {unexplained}/{n} failure(s)")
    return summary, sentences


def by_run(rows):
    return {tid: {r["run"]: r for r in rs} for tid, rs in rows.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", default="gpt-4")
    ap.add_argument("--new", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--lines", action="store_true", help="ablate each line of the guilty step(s) (task 6)")
    args = ap.parse_args()

    tickets = A.load_tickets()
    old_rows, new_rows = T.load_rows(RESULTS / f"agent__{args.old}.jsonl"), T.load_rows(RESULTS / f"agent__{args.new}.jsonl")
    items = T.compare(old_rows, new_rows, tickets)
    broken = [it for it in items if it["verdict"] == "broken"]
    print(f"{args.old} -> {args.new}: {len(broken)} broken: {[it['ticket']['id'] for it in broken]}")
    old, new = spec(args.old, OLD_PARAMS), spec(args.new, {})
    src = {"old": by_run(old_rows), "new": by_run(new_rows)}
    plans = swap_plans(old, new)

    def hybrid(ticket, label, plan, first_live, side, prompts=None):
        path = run_config([ticket], label, plan, src[side], first_live, args.runs, RESULTS, args.workers, prompts=prompts)
        rows = T.load_rows(path).get(ticket["id"], [])[: args.runs]
        p = T.passes(rows, ticket)
        return {"passed": sum(p), "total": len(p), "file": path.name}

    out_tickets = []
    for it in broken:
        t = it["ticket"]
        res = {"ticket": t["id"], "old_passed": it["old_passed"], "new_passed": it["new_passed"],
               "rescue": {}, "break": {}}
        for step in A.STEPS:
            res["rescue"][step] = hybrid(t, *plans[step]["rescue"])
            res["break"][step] = hybrid(t, *plans[step]["break"])
        res["necessary"] = [s for s in A.STEPS if repaired(res["rescue"][s]["passed"], res["rescue"][s]["total"])]
        res["sufficient"] = [s for s in res["necessary"]
                             if T.verdict(it["old_passed"], len(it["old_runs"]),
                                          res["break"][s]["passed"], res["break"][s]["total"]) == "broken"]
        print(f"  {t['id']}: rescue " + " ".join(f"{s}={res['rescue'][s]['passed']}/{res['rescue'][s]['total']}" for s in A.STEPS)
              + " | break " + " ".join(f"{s}={res['break'][s]['passed']}/{res['break'][s]['total']}" for s in A.STEPS)
              + f" -> causal {res['necessary']}, confirmed {res['sufficient']}")
        out_tickets.append(res)

    summary, sentences = step_summary(out_tickets)
    lines = {}
    if args.lines:
        for step in [s for s in A.STEPS if summary[s]["necessary"]]:
            k = A.STEPS.index(step)
            plan = plan_of(new, {})
            per_line = []
            for i, line in enumerate(A.PROMPTS[step]):
                fixed = 0
                for res in [r for r in out_tickets if step in r["necessary"]]:
                    t = next(x for x in tickets if x["id"] == res["ticket"])
                    h = hybrid(t, f"{args.new}__ablate-{step}-L{i}", plan, k, "new",
                               prompts={step: A.PROMPTS[step][:i] + A.PROMPTS[step][i + 1:]})
                    fixed += repaired(h["passed"], h["total"])
                per_line.append({"line_index": i, "line": line, "repairs": fixed})
            ranked = sorted(per_line, key=lambda x: -x["repairs"])
            top = ranked[0] if ranked and ranked[0]["repairs"] else None
            lines[step] = {"lines": per_line, "top": top, "of": summary[step]["necessary"]}
            print(f"  lines in {step}: " + (f"L{top['line_index'] + 1} repairs {top['repairs']}/{summary[step]['necessary']}: "
                                             f"{top['line']!r}" if top else "no single line repairs it"))
    for s in sentences:
        print("  " + s)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"stepswap_{args.new}.json"
    path.write_text(json.dumps({"old": args.old, "new": args.new, "runs": args.runs, "rule": "rescue repaired = hybrid "
                                "passes >= 2/3; break reproduces = old >= 2/3 and hybrid <= 1/3",
                                "tickets": out_tickets, "summary": summary, "sentences": sentences, "lines": lines,
                                "generated_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
    print(f"-> {path}")


if __name__ == "__main__":
    main()
