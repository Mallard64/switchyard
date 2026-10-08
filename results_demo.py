"""Build reports/demo/results.json: the dashboard's v1 schema for the real model-only migration, plus a `demo` block.

  python results_demo.py            # reads cached runs + model_only.py / assign.py outputs; no API calls

v1 fields (repo, baseline, prompt, inputs, candidates[].summary/results[], causes[], pr) keep the exact shape of
dashboard/results.sample.json; everything new is additive: `synthetic` on every result and cause, and `demo`
(per-step assignment, cost and savings, repair/reproduce rates, confidence, the patch diff, before/after
examples, the step-finder benchmark). Every number comes from a run in results/; prices from config/prices.yml.
"""
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import support_agent as A  # noqa: E402
from assign import total, usage_per_step  # noqa: E402
from compare import build_report, load  # noqa: E402

RESULTS = HERE / "results"
OUT = HERE / "reports" / "demo" / "results.json"
OLD = "gpt-4"
CAUSE_SOL = "tone-timeline"
STATUS = {"REGRESSED": "regressed", "IMPROVED": "improved", "CHANGED": "changed", "SAME": "pass"}
# Assumption for the savings view, not a measurement: the buyer edits it on the page.
MONTHLY_REQUESTS_DEFAULT = 100_000


def jl(name):
    return RESULTS / name


def describe(row):
    if not row or row.get("error"):
        return "error" if row else "no run"
    d = row.get("decision") or {}
    return f"[{row.get('category')} / {d.get('action')}]\n{row.get('final_reply') or ''}".strip()


def p50_ms(paths):
    vals = [sum(s.get("latency_s") or 0 for s in r["steps"]) * 1000
            for p in paths for r in (json.loads(l) for l in open(p)) if not r.get("error") and r.get("steps")]
    return round(statistics.median(vals)) if vals else None


def merged(rep_dev, rep_ho):
    items = rep_dev["items"] + rep_ho["items"]
    counts = {k: rep_dev["verdict_counts"][k] + rep_ho["verdict_counts"][k] for k in rep_dev["verdict_counts"]}
    return items, counts


def candidate(model_label, files, old_files, cause_id, heldout_ids, after=None):
    items, counts = merged(build_report(old_files[0], files[0]), build_report(old_files[1], files[1]))
    rows = {**load(files[0]), **load(files[1])}
    base = {**load(old_files[0]), **load(old_files[1])}
    fixed = {**load(after[0]), **load(after[1])} if after else {}
    fixed_counts = merged(build_report(old_files[0], after[0]), build_report(old_files[1], after[1]))[1] if after else None
    results = []
    for it in items:
        tid = it["input_id"]
        runs = rows.get(tid, [])
        results.append({
            "input_id": tid, "status": STATUS[it["verdict"]],
            "score": round(statistics.mean(it["candidate"]["score_runs"]), 4),
            "runs": {"passed": sum(bool(r.get("passed_all")) for r in runs), "total": len(runs)},
            "baseline_output": describe((base.get(tid) or [None])[0]), "output": describe(runs[0] if runs else None),
            "cause_id": cause_id if it["verdict"] == "REGRESSED" else None,
            "after_fix_output": describe(fixed[tid][0]) if it["verdict"] == "REGRESSED" and tid in fixed else None,
            # additive
            "synthetic": False, "split": "heldout" if tid in heldout_ids else "dev", "verdict": it["verdict"],
            "failed_checks": sorted({x.get("check") for x in it["regressions"] if x.get("check")}),
        })
    cost = total(usage_per_step(files))
    return {"model": model_label, "provider": "OpenAI",
            "summary": {"passed": len(items) - counts["REGRESSED"], "total": len(items),
                        "passed_after_fix": len(items) - (fixed_counts or counts)["REGRESSED"],
                        "cost_change_pct": None, "cost_per_1k_calls_usd": cost,
                        "latency_p50_ms": p50_ms(files), "speed_vs_baseline": None,
                        "recommended": False, "verdict_counts": counts,
                        **({"verdict_counts_after_fix": fixed_counts} if fixed_counts else {})},
            "results": results}


def main():
    import yaml
    mo = json.loads((HERE / "reports" / "model_only" / "result_gpt-5.6-sol.json").read_text())
    asg = json.loads((HERE / "reports" / "assignment" / "result.json").read_text())
    prices = yaml.safe_load((HERE / "config" / "prices.yml").read_text())["models"]
    patch = mo["patch"]
    tag = mo["heldout_after"]["file"].split("__patch-")[1].split("__")[0]
    old_files = (jl(f"agent__{OLD}__noplant.jsonl"), jl(f"agent__{OLD}__noplant__hard.jsonl"))
    sol_files = (jl("agent__gpt-5.6-sol__noplant.jsonl"), jl("agent__gpt-5.6-sol__noplant__hard.jsonl"))
    sol_fixed = (jl(f"agent__gpt-5.6-sol-np__patch-{tag}.jsonl"), jl(f"agent__gpt-5.6-sol-np__patch-{tag}__hard.jsonl"))
    terra_files = (jl("agent__gpt-5.6-terra__noplant.jsonl"), jl("agent__gpt-5.6-terra__noplant__hard.jsonl"))
    dev, hard = A.load_tickets(sets=("agent",)), A.load_tickets(sets=("agent_hard",))
    heldout_ids = {t["id"] for t in hard}
    costs, totals = asg["costs_per_1k_ticket_runs"], asg["totals_usd_per_1k"]
    old_cost = totals["all_old"]
    base_ms = p50_ms(old_files)

    sol = candidate("gpt-5.6-sol", sol_files, old_files, CAUSE_SOL, heldout_ids, sol_fixed)
    terra = candidate("gpt-5.6-terra", terra_files, old_files, "terra-timeline", heldout_ids)
    sol["summary"]["recommended"] = True
    for c in (sol, terra):
        c["summary"]["cost_change_pct"] = round((c["summary"]["cost_per_1k_calls_usd"] / old_cost - 1) * 100, 1)
        c["summary"]["speed_vs_baseline"] = round(base_ms / c["summary"]["latency_p50_ms"], 2)
    noisy = (build_report(old_files[0], sol_files[0])["baseline"]["unstable_inputs"]
             + build_report(old_files[1], sol_files[1])["baseline"]["unstable_inputs"])

    step = patch["step"]
    li = patch["edits"][0]["line_index"]
    sf = mo["stepfinder"]
    heldout_after = mo["heldout_after"]["verdict_counts"]
    line_repairs = next(r["repairs"] for r in mo["lines"]["results"] if r["line_index"] == li)
    causes = [
        {"id": CAUSE_SOL, "line": li + 1, "step": step, "synthetic": False,
         "title": "The tone check deletes the refund timeline",
         "fixed_title": "Fixed: the tone check keeps the refund timeline",
         "reason": ("gpt-5.6-sol's tone step reads \"5-7 business days\" as a promise beyond the decision and rewrites "
                    "the reply without it. gpt-4 passes the same draft unchanged. Swapping only the tone step back to "
                    f"gpt-4 repairs {sf['per_step'][step]['repair']}/{sf['per_step'][step]['of']} dev tickets; removing "
                    f"tone line {li + 1} alone repairs {line_repairs}/{sf['per_step'][step]['of']}."),
         "check": "hard_check: draft.states_timeline",
         "fix": {"old_line": patch["edits"][0]["old"], "new_line": patch["edits"][0]["new"], "status": "verified",
                 "retest": {"passed": mo["heldout_after"]["tickets"] - heldout_after["REGRESSED"],
                            "total": mo["heldout_after"]["tickets"]}}},
        {"id": "terra-timeline", "line": None, "step": None, "synthetic": False,
         "title": "gpt-5.6-terra also drops the refund timeline (not localized)",
         "fixed_title": "", "reason": "Same failing check on terra; the step-finder was run on gpt-5.6-sol only.",
         "check": "hard_check: draft.states_timeline",
         "fix": {"old_line": "", "new_line": "", "status": "none", "retest": None}},
    ]

    def first_row(paths, tid):
        for p in paths:
            rs = load(p).get(tid)
            if rs:
                return rs[0]
        return None

    def tone(r):
        return next(s["raw"] for s in r["steps"] if s["step"] == "tone") if r else None

    examples = []
    for tid in ["t21", "t36"]:
        o, n, f = first_row(old_files, tid), first_row(sol_files, tid), first_row(sol_fixed, tid)
        examples.append({"input_id": tid, "split": "heldout" if tid in heldout_ids else "dev", "synthetic": False,
                         "text": next(t["text"] for t in dev + hard if t["id"] == tid),
                         "old": {"tone": tone(o), "final_reply": o["final_reply"] if o else None},
                         "new": {"tone": tone(n), "final_reply": n["final_reply"] if n else None},
                         "fixed": {"tone": tone(f), "final_reply": f["final_reply"] if f else None}})

    per_step = []
    for s in A.STEPS:
        ladder = asg["per_step"][s]
        per_step.append({
            "step": s, "synthetic": False,
            "all_old": costs["all_old"].get(s), "all_new": costs["all_new"].get(s), "mix": costs["mix"].get(s),
            "recommended_model": asg["recommended"]["assignment"][s],
            "ladder": [{"model": t["model"], "passed": t["passed"], "regressed": t["regressed"]} for t in ladder["tried"]],
            "ladder_choice": ladder["chosen"],
            "repair": {"n": sf["per_step"][s]["repair"], "of": sf["per_step"][s]["of"]},
            "reproduce": {"n": sf["per_step"][s]["reproduce"], "of": sf["per_step"][s]["of"]},
            "guilty": s == step})

    bench = (HERE / "reports" / "agent_bench" / "RESULTS.md").read_text()
    assert "| Exactly the planted step(s) | 34 |" in bench and "| 38 | 39 |" in bench, "benchmark numbers changed"
    data = {
        "schema_version": 1,
        "_note": "Real runs of the 4-step support agent (clean prompts, nothing planted). Tickets are hand-written "
                 "(synthetic); model behavior is real. cost_per_1k_calls_usd = per 1k ticket runs (4 calls).",
        "run_id": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": {"name": "samegrade support agent (demo)", "call_site": f"support_agent.py: PROMPTS['{step}']"},
        "baseline": {"model": OLD, "provider": "OpenAI", "retires_on": "2026-10-23", "runs_per_input": 3,
                     "noise_rate": round(noisy / (len(dev) + len(hard)), 4), "cost_per_1k_calls_usd": old_cost,
                     "latency_p50_ms": base_ms},
        "prompt": {"file": f"support_agent.py PROMPTS['{step}']", "lines": A.PROMPTS[step]},
        "inputs": [{"id": t["id"], "text": t["text"], "split": "heldout" if t["id"] in heldout_ids else "dev",
                    "synthetic": True} for t in dev + hard],
        "candidates": [sol, terra],
        "causes": causes,
        "pr": {"status": "draft", "title": "Move the support agent to gpt-5.6-sol; keep the refund timeline in the tone check",
               "url": None, "branch": None, "lines_changed": len(patch["edits"])},
        "synthetic": False,
        "demo": {
            "synthetic": False,
            "what_is_synthetic": ["the 36 tickets and their expected outcomes are hand-written",
                                  "the step-finder benchmark (planted prompt breaks)"],
            "pair": mo["pair"],
            "confidence": {"runs_per_ticket": 3, "dev_tickets": len(dev), "heldout_tickets": len(hard),
                           "regressed_dev": mo["dev"]["verdict_counts"]["REGRESSED"],
                           "regressed_heldout_before": mo["heldout_before"]["verdict_counts"]["REGRESSED"],
                           "regressed_heldout_after": heldout_after["REGRESSED"],
                           "old_model_unstable_tickets": noisy,
                           "rule": "regressed = most new-model runs fail a hard check the old model passes, or score "
                                   "below the old model's worst run"},
            "per_step": per_step,
            "totals_usd_per_1k": totals, "savings": asg["savings"], "recommended": asg["recommended"],
            "mix": {"assignment": asg["assignment"], "accepted": asg["accepted"], "dev": asg["mixed"]["dev"]["verdict_counts"],
                    "heldout": asg["mixed"]["heldout"]["verdict_counts"], "heldout_regressed": asg["mixed"]["heldout"]["regressed"]},
            "patch": {"step": step, "diff": patch["diff"], "edits": patch["edits"], "rationale": patch["rationale"],
                      "fixer": mo["fix"]["fixer_model"], "fixer_saw": mo["fix"]["saw"], "attempts": len(mo["fix"]["attempts"]),
                      "dev_after": patch["dev_verdict_counts"], "heldout_before": mo["heldout_before"]["verdict_counts"],
                      "heldout_after": heldout_after, "synthetic": False},
            "lines": mo["lines"],
            "examples": examples,
            "benchmark": {"synthetic": True, "source": "reports/agent_bench/RESULTS.md",
                          "held_out_exact": {"n": 34, "of": 39}, "two_way_confirmed": {"n": 38, "of": 39},
                          "wrong_step": {"n": 0, "of": 39},
                          "note": "9 planted prompt breaks, terra as old and sol as new; held-out numbers (before the narrowing fix)"},
            "prices": {m: {k: p.get(k) for k in ("input_per_1m", "output_per_1m", "source", "checked")}
                       for m, p in prices.items() if m in ("gpt-4", "gpt-5.6-sol", "gpt-5.6-terra")},
            "monthly_requests_default": MONTHLY_REQUESTS_DEFAULT,
            "monthly_requests_note": "assumption for the savings calculator; edit it on the page",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, default=str) + "\n")
    s = sol["summary"]
    print(f"sol: {s['verdict_counts']} -> after fix {s.get('verdict_counts_after_fix')}  ${s['cost_per_1k_calls_usd']}/1k  "
          f"terra: {terra['summary']['verdict_counts']}  old ${old_cost}/1k  -> {OUT}")


if __name__ == "__main__":
    main()
