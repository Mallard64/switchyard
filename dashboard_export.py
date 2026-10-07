"""Export an agent migration report in the shape the Switchyard dashboard reads.

The dashboard (../switchyard-dashboard) renders the full-migration fixture shape that migrate.py
already writes for NER. Agent reports (migrate_agent.py, demo_e2e.py) use a different dict, so
this writes reports/<dir>/dashboard.json in the fixture shape: baseline, candidates, chosen,
causes, fix_attempts, final, pr, comparison. Agent outputs become lists ("category: x",
"action: y") so the dashboard's entity views can highlight exactly what changed.

  python dashboard_export.py reports/migration_agent reports/demo_e2e_u3
"""
import difflib
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent


def as_list(output):
    """compare.py's agent output "category/action" -> ["category: ...", "action: ..."]."""
    if isinstance(output, list):
        return output
    cat, _, action = str(output).partition("/")
    return [f"category: {cat}", f"action: {action}"]


def with_lists(rep):
    rep = json.loads(json.dumps(rep))
    for it in rep["items"]:
        for side in ("baseline", "candidate"):
            it[side]["output"] = as_list(it[side]["output"])
    return rep


def proposed_diff(m):
    """Diff of the accepted fixes, or of the first proposal while nothing is accepted yet."""
    live = [f for f in m["fixes"] if f["decision"] in ("accepted", "edited")] or m["fixes"][:1]
    out = []
    for f in live:
        old = [l + "\n" for l in (f.get("old_text") or "").splitlines()]
        new = [l + "\n" for l in (f.get("new_text") or "").splitlines()]
        out += difflib.unified_diff(old, new, fromfile=f"a/{f['step']} prompt", tofile=f"b/{f['step']} prompt")
    return "".join(out)


def export(out_dir, m, cand_rep, fix_reports):
    """m: the pr_report migration dict; cand_rep: compare report (model swap only);
    fix_reports: {fix_id: compare report with that fix}."""
    live = [f for f in m["fixes"] if f["decision"] in ("accepted", "edited")]
    final_rep = fix_reports[live[0]["id"]] if live else cand_rep
    c = cand_rep["candidate"]
    causes = []
    for cs in m["causes"]:
        evidence = list(cs.get("step_evidence", []))
        evidence += [f"Proven line in {u['component']}: \"{u['text']}\"" for u in cs.get("confirmed_lines", [])]
        suspects = [{"component": u["component"], "score": 2, "editable": True} for u in cs.get("confirmed_lines", [])]
        suspects += [{"component": f"step: {s}", "score": 1, "editable": True} for s in cs.get("causal_steps", [])]
        causes.append({"input_id": cs["input_id"], "text": cs["text"],
                       "baseline_output": as_list(cs["baseline_output"]),
                       "candidate_output": as_list(cs["candidate_output"]),
                       "evidence": evidence, "suspects": suspects, "status": cs["status"],
                       "confirmed_component": suspects[0]["component"] if suspects else None})
    attempts = []
    for n, f in enumerate(m["fixes"], 1):
        v = f["verification"]
        attempts.append({"attempt": n, "edit": {"component": f["component"], "old_text": f.get("old_text"),
                                                 "new_text": f.get("new_text") or "", "rationale": f.get("source")},
                         "verdict_counts": v["verdict_counts"], "accepted": f["decision"] in ("accepted", "edited"),
                         "outcome": f"{f['decision']} · full re-run: {v['verdict_counts']['REGRESSED']} regressed"})
    steps = sorted({f["step"] for f in m["fixes"]})
    dash = {
        "task": "agent", "started": m.get("started"), "finished": m.get("finished"),
        "baseline": {"file": cand_rep["baseline_file"], **cand_rep["baseline"]},
        "candidates": [{"model": m["new_model"], "resolved_model": c["resolved_model"],
                        "regressed": cand_rep["verdict_counts"]["REGRESSED"], "verdict_counts": cand_rep["verdict_counts"],
                        "accuracy": c["accuracy"], "accuracy_metric": c["accuracy_metric"], "passed_all": c["passed_all"],
                        "unstable_inputs": c["unstable_inputs"], "latency_p50_s": c["latency_p50_s"],
                        "latency_p95_s": c["latency_p95_s"], "cost_per_1k_calls_usd": c["cost_per_1k_calls_usd"],
                        "rank": 1}],
        "chosen": m["new_model"], "causes": causes, "fix_attempts": attempts,
        "final": {"ready_to_merge": not cand_rep["verdict_counts"]["REGRESSED"] or bool(live), "model": m["new_model"],
                  "prompt_edit": next((a["edit"] for a in attempts if a["accepted"]), None),
                  "verdict_counts": final_rep["verdict_counts"], "candidate": final_rep["candidate"]},
        "pr": {"title": m["title"], "target_repo": "samegrade · 4-step support agent",
               "target_path": "support_agent.py " + ", ".join(f"PROMPTS['{s}']" for s in steps),
               "diff": proposed_diff(m), "url": None},
        "comparison": with_lists(final_rep),
        # Extra context (older dashboard versions ignore it): step-finder summary and review list.
        "steps": m.get("steps"), "fixes": m["fixes"],
    }
    Path(out_dir, "dashboard.json").write_text(json.dumps(dash, indent=2, default=list))
    return dash


def export_dir(out_dir):
    out_dir = Path(out_dir)
    m = json.loads((out_dir / "migration.json").read_text())
    cand_file = out_dir / (m.get("comparison_file") or "compare_candidate.json")
    fix_reports = {f["id"]: json.loads((out_dir / f["verification"]["report_file"]).read_text()) for f in m["fixes"]}
    return export(out_dir, m, json.loads(cand_file.read_text()), fix_reports)


if __name__ == "__main__":
    for d in sys.argv[1:] or [HERE / "reports" / "migration_agent"]:
        export_dir(d)
        print(f"-> {Path(d) / 'dashboard.json'}")
