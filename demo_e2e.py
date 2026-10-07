"""End-to-end proof on one planted break, cheap and fast: step-finder -> line-finder -> minimal fix
-> full re-run -> PR. Old and new are both gpt-5.6-terra; the break changes only the new side's
prompt, so the true cause is known and the run shows the whole product on one page.

  python demo_e2e.py --break u3          # 1 run per config, terra only (well under $1)
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import pr_report  # noqa: E402
import support_agent as A  # noqa: E402
from agent_bench import BREAK_SETS  # noqa: E402
from compare import build_report, compare_input, load, load_gold  # noqa: E402
from stepfinder import localize, new_calls_cost, plan_of, run_config, spec  # noqa: E402

RESULTS = HERE / "results"


def score_rows(rep, fixed):
    b, c, f = rep["baseline"], rep["candidate"], fixed["candidate"] if fixed else None
    cost = lambda s: f"${s['cost_per_1k_calls_usd']:.2f}" if s and s["cost_per_1k_calls_usd"] is not None else "n/a"
    return [("Passed all hard checks", f"{b['passed_all']:.0%}", f"{c['passed_all']:.0%}", f"{f['passed_all']:.0%}" if f else "–"),
            ("Gold score", f"{b['accuracy']:.2f}", f"{c['accuracy']:.2f}", f"{f['accuracy']:.2f}" if f else "–"),
            ("Tickets regressed vs old", "–", rep["verdict_counts"]["REGRESSED"],
             fixed["verdict_counts"]["REGRESSED"] if fixed else "–"),
            ("Cost per 1k tickets", cost(b), cost(c), cost(f))]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--break", dest="brk", default="u3")
    ap.add_argument("--model", default="gpt-5.6-terra")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--max-tickets", type=int, default=3, help="tickets to localize and ablate on")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--tag", help="fresh run: new cache files and reports/demo_e2e_<break>_live (dashboard Run button)")
    args = ap.parse_args()
    b = next(x for s in BREAK_SETS.values() for x in s if x["id"] == args.brk)
    out = HERE / "reports" / f"demo_e2e_{args.brk}{'_live' if args.tag else ''}"
    out.mkdir(parents=True, exist_ok=True)
    old = spec(args.model, {})
    new = spec(args.model, {}, b["prompts"], f"{args.model}+{args.brk}" + (f"-live-{args.tag}" if args.tag else ""))
    tickets = list(A.load_tickets())
    base_path = RESULTS / f"agent__{args.model}.jsonl"
    clean_src = {tid: {r["run"]: r for r in rs} for tid, rs in load(base_path).items()}
    first = min(A.STEPS.index(s) for s in b["prompts"])
    files = []

    # 1. Candidate run (cached from agent_bench when the settings match).
    cand_path = run_config(tickets, new["label"], plan_of(new, {}), clean_src, first, args.runs, RESULTS, args.workers)
    files.append(cand_path)
    rep = build_report(base_path, cand_path)
    regressed = [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED"]
    (out / "compare_candidate.json").write_text(json.dumps(rep, indent=2, default=list))
    print(f"[1/5] {old['label']} -> {new['label']}: {rep['verdict_counts']}")

    # 2. Step-finder on a sample of regressed tickets.
    sample = regressed[: args.max_tickets]
    sub = cand_path.with_name(cand_path.stem + f"__sample{len(sample)}.jsonl")
    sub.write_text("".join(l for l in open(cand_path) if json.loads(l)["input_id"] in sample))
    loc = localize(old, new, base_path, sub, args.runs, RESULTS, args.workers)
    print("[2/5] step-finder: " + ", ".join(f"{t['input_id']} -> {t['causal_steps']} ({t['status']})" for t in loc["tickets"]))
    steps = sorted({s for t in loc["tickets"] if t["status"] == "confirmed" for s in t["causal_steps"]},
                   key=A.STEPS.index)

    # 3. Line-finder: remove one line of the causal step's (new-side) prompt at a time.
    base, gold = load(base_path), load_gold("agent")
    cand_src = {tid: {r["run"]: r for r in rs} for tid, rs in load(cand_path).items()}
    confirmed_lines, line_results = [], {}
    for step in steps:
        lines = new["prompts"].get(step, A.PROMPTS[step])
        k = A.STEPS.index(step)
        for i in range(len(lines)):
            path = run_config([t for t in tickets if t["id"] in sample], f"{new['label']}__ablate-{step}-L{i}",
                              plan_of(new, {}), cand_src, k, args.runs, RESULTS, args.workers,
                              prompts={step: lines[:i] + lines[i + 1:]})
            files.append(path)
            rows = load(path)
            verdicts = {tid: compare_input("agent", base[tid], rows[tid], gold[tid])["verdict"] for tid in sample}
            repaired = sum(v != "REGRESSED" for v in verdicts.values())
            line_results[(step, i)] = {"line": lines[i], "repaired": f"{repaired}/{len(sample)}"}
            if repaired * 2 > len(sample):
                confirmed_lines.append((step, i, lines[i]))
    print("[3/5] proven lines: " + "; ".join(f"{s} line {i + 1}: \"{l}\"" for s, i, l in confirmed_lines))

    # 4. Fix: remove each proven line; full re-run of every ticket to verify.
    fixes = []
    for n, (step, i, line) in enumerate(confirmed_lines, 1):
        lines = new["prompts"].get(step, A.PROMPTS[step])
        fixed_lines = lines[:i] + lines[i + 1:]
        path = run_config(tickets, f"{new['label']}__fix-{step}-L{i}", plan_of(new, {}), cand_src, A.STEPS.index(step),
                          args.runs, RESULTS, args.workers, prompts={step: fixed_lines})
        files.append(path)
        frep = build_report(base_path, path)
        reg = [it["input_id"] for it in frep["items"] if it["verdict"] == "REGRESSED"]
        (out / f"compare_fix{n}.json").write_text(json.dumps(frep, indent=2, default=list))
        fixes.append({"id": f"fix{n}", "step": step, "component": f"{step} prompt, line {i + 1}", "kind": "remove_line",
                      "old_text": line, "new_text": None, "new_lines": fixed_lines, "line_index": i,
                      "summary": f"remove line {i + 1} of the `{step}` prompt",
                      "source": "line-finder (removing this line alone repaired the sampled regressions)",
                      "verification": {"inputs": frep["inputs_compared"], "runs": args.runs,
                                       "verdict_counts": frep["verdict_counts"],
                                       "still_regressed": [x for x in reg if x in regressed],
                                       "newly_regressed": [x for x in reg if x not in regressed],
                                       "passes": not reg, "report_file": f"compare_fix{n}.json"},
                      "decision": "pending", "decided_at": None, "_report": frep})
        print(f"[4/5] fix{n}: remove {step} line {i + 1} -> {frep['verdict_counts']}")

    # 5. PR.
    shown = fixes[0]["_report"] if fixes else None
    causes = []
    for t in loc["tickets"]:
        item = next(x for x in rep["items"] if x["input_id"] == t["input_id"])
        fixed_item = next((x for x in shown["items"] if x["input_id"] == t["input_id"]), None) if shown else None
        ev = [f"`{new['label']}` everywhere except `{s}` (old prompt) → {j['verdict']} ({j['output']})"
              for s, j in t["necessity"].items()]
        ev += [f"old everywhere except `{s}` (new prompt) → {j['verdict']} ({j['output']})"
               for s, j in t["sufficiency"].items()]
        conf = [{"component": f"{s} prompt, line {i + 1}", "text": l} for s, i, l in confirmed_lines if s in t["causal_steps"]]
        causes.append({"input_id": t["input_id"], "text": item["text"], "baseline_output": item["baseline"]["output"],
                       "candidate_output": item["candidate"]["output"],
                       "fixed_output": fixed_item["candidate"]["output"] if fixed_item else None,
                       "step_evidence": ev, "confirmed_lines": conf, "causal_steps": t["causal_steps"],
                       "lines_tested": sum(1 for (s, _) in line_results if s in t["causal_steps"]),
                       "status": "confirmed" if t["status"] == "confirmed" else "suspected",
                       "confirmed_by": "ablation" if conf else "step-swap", "confirmed_component": None, "suspects": []})
    n = len(loc["tickets"])
    summary = {s: {"explains": sum(s in t["causal_steps"] for t in loc["tickets"]), "of": n} for s in A.STEPS}
    for v in summary.values():
        v["pct"] = round(100 * v["explains"] / n) if n else 0
    m = {"title": f"Demo: the `{args.brk}` regression shows up downstream, but the cause is one "
                  f"`{'`/`'.join(b['prompts'])}` line",
         "why": [f"Planted break for the demo: {b['desc']}. Old and new are both `{args.model}`; only the new side's "
                 "prompt changed, so the true cause is known."],
         "comparison_file": "compare_candidate.json",
         "old_model": f"{args.model} (clean)", "new_model": new["label"], "n_inputs": rep["inputs_compared"],
         "model_change": [], "cli": f"python demo_e2e.py --break {args.brk}",
         "steps": {"names": A.STEPS, "summary": summary,
                   "method": f"For {n} regressed tickets, each step was swapped back to the old side one at a time "
                             f"({args.runs} run each, earlier steps replayed); a causal step is then checked the "
                             "other way round (only that step on the new side)."},
         "causes": causes, "evidence": {"table": score_rows(rep, shown),
                                        "noise_floor": f"{args.runs} run per configuration (kept minimal for cost); "
                                                       "the old side has 3 cached runs."},
         "fixes": [{k: v for k, v in f.items() if k != "_report"} for f in fixes], "other_differences": [],
         "limits": [f"{args.runs} run per configuration and {n} tickets localized: enough to show the method, not a "
                    "noise-robust measurement.", "Planted break; 24 synthetic tickets."]}
    (out / "PR.md").write_text(pr_report.render(m))
    diff = []
    for f in fixes:
        diff += difflib.unified_diff([l + "\n" for l in new["prompts"].get(f["step"], A.PROMPTS[f["step"]])],
                                     [l + "\n" for l in f["new_lines"]], fromfile=f"a/{f['step']} prompt",
                                     tofile=f"b/{f['step']} prompt")
    (out / "prompts.diff").write_text("".join(diff))
    m["live_calls"] = new_calls_cost(files)
    (out / "migration.json").write_text(json.dumps(m, indent=2, default=list))
    import dashboard_export
    dashboard_export.export_dir(out)  # Switchyard dashboard view of the same report
    print(f"[5/5] PR -> {out / 'PR.md'}   live calls: {m['live_calls']}")


if __name__ == "__main__":
    main()
