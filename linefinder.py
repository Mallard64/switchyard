"""Line-finder: prove which prompt lines of a causal step broke on the new model.

Reads the step-finder report. For each regressed ticket and each causal step, removes one line
of that step's prompt at a time and re-runs the ticket on the new model (3 runs; steps before
the causal one replayed from the candidate's cached run). A line is `confirmed` when removing it
makes the ticket stop regressing under compare.py's noise-floor rule. A word-overlap heuristic
(line vs. what the model said at that step) only decides which lines are tested first.

  python linefinder.py --candidate gpt-5.6-sol --candidate-config '{}'
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import support_agent as A  # noqa: E402
from compare import compare_input, load, load_gold  # noqa: E402
from stepfinder import make_plan, new_calls_cost, run_config  # noqa: E402

REPORTS = HERE / "reports" / "agent"
STOP = set("a an and are as at be by for if in is it its of on or the to with you your this that "
           "only one any not no do".split())


def words(text):
    return {w for w in re.findall(r"[a-z][a-z-]+", text.lower()) if w not in STOP}


def heuristic(line, said):
    """Share of the line's content words that the model used at this step (ordering only)."""
    w = words(line)
    return round(len(w & words(said)) / len(w), 3) if w else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", default="gpt-4")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--candidate-config", type=json.loads, default={})
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-lines", type=int, help="test only the N lines the heuristic ranks first")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--outdir", default=str(HERE / "results"))
    args = ap.parse_args()
    outdir = Path(args.outdir)
    sf = json.loads((REPORTS / f"stepfinder_{args.candidate}.json").read_text())
    base, cand = load(outdir / sf["baseline_file"]), load(outdir / sf["candidate_file"])
    gold = load_gold("agent")
    cand_src = {tid: {r["run"]: r for r in rs} for tid, rs in cand.items()}
    tickets = {t["id"]: t for t in A.load_tickets()}
    plan = make_plan((args.candidate, args.candidate_config), {})
    out_items, files = [], []

    for tk in sf["tickets"]:
        tid = tk["input_id"]
        for step in tk["causal_steps"]:
            k = A.STEPS.index(step)
            lines = A.PROMPTS[step]
            said = " ".join(next(s for s in r["steps"] if s["step"] == step)["raw"] for r in cand[tid])
            order = sorted(range(len(lines)), key=lambda i: -heuristic(lines[i], said))[: args.max_lines]
            results = []
            for i in order:
                label = f"{args.candidate}__ablate-{step}-L{i}"
                path = run_config([tickets[tid]], label, plan, cand_src, k, args.runs, outdir, args.workers,
                                  prompts={step: lines[:i] + lines[i + 1:]})
                files.append(path)
                item = compare_input("agent", base[tid], load(path)[tid], gold[tid])
                worst_base = min(item["baseline"]["score_runs"])
                repaired = sum(s >= worst_base for s in item["candidate"]["score_runs"])
                results.append({"line_index": i, "line": lines[i], "heuristic": heuristic(lines[i], said),
                                "verdict": item["verdict"], "output": item["candidate"]["output"],
                                "score_runs": item["candidate"]["score_runs"],
                                "repaired_runs": f"{repaired}/{len(item['candidate']['score_runs'])}",
                                "status": "confirmed" if item["verdict"] != "REGRESSED" else "no_effect"})
            results.sort(key=lambda r: (r["status"] != "confirmed", -int(r["repaired_runs"].split("/")[0]),
                                        -r["heuristic"]))
            out_items.append({"input_id": tid, "step": step, "baseline_output": tk["baseline_output"],
                              "candidate_output": tk["candidate_output"], "lines_tested": len(order),
                              "lines_total": len(lines), "lines": results,
                              "confirmed": [r["line"] for r in results if r["status"] == "confirmed"]})

    result = {"candidate": args.candidate, "old_model": args.old, "runs": args.runs,
              "method": "remove one line of the causal step's prompt; re-run on the new model; confirmed = "
                        "ticket no longer REGRESSED under compare.py's noise-floor rule",
              "items": out_items, "live_calls": new_calls_cost(files)}
    out = REPORTS / f"linefinder_{args.candidate}.json"
    out.write_text(json.dumps(result, indent=2))
    for it in out_items:
        print(f"\n{it['input_id']} / {it['step']}: {it['baseline_output']} -> {it['candidate_output']}  "
              f"({it['lines_tested']}/{it['lines_total']} lines tested)")
        for r in it["lines"]:
            print(f"  {r['status']:<10} L{r['line_index']} repaired {r['repaired_runs']}  heur {r['heuristic']:.2f}  "
                  f"{r['output']:<18} {r['line'][:70]}")
    print(f"\nlive calls: {result['live_calls']}\n-> {out}")


if __name__ == "__main__":
    main()
