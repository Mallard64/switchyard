"""Migrate the 4-step support agent to a new model and write the PR, with per-fix review.

  python migrate_agent.py --candidate gpt-5.6-sol --candidate-config '{}'    # build/refresh the report
  python migrate_agent.py ... --accept fix1          # engineer accepts a fix (no re-run)
  python migrate_agent.py ... --reject fix1          # engineer rejects a fix (no re-run)
  python migrate_agent.py ... --apply-edit edit.json # engineer's own lines: full re-run, re-verify, new PR

Pipeline (every stage cached; a refresh with nothing new makes no API calls):
  compare (compare.py) -> step-finder (stepfinder.py) -> line-finder (linefinder.py)
  -> one proposed fix per proven line: remove it -> full re-run of all tickets on the candidate
  with the fix -> PR.md + prompts.diff + migration.json in reports/migration_agent/.

Edit file (JSON), either form:
  {"fix": "fix1", "step": "decide", "line_index": 4, "new_text": "replacement line" | null}
  {"fix": "fix1", "step": "decide", "lines": ["full", "new", "prompt", "lines"]}
"fix" is optional; when given, the edit replaces that fix (its decision becomes "edited" once the
full re-run shows 0 regressions; otherwise it stays "pending" with the failing evidence shown).
"""
import argparse
import difflib
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import pr_report  # noqa: E402
from pipeline_view import experiments  # noqa: E402
import support_agent as A  # noqa: E402
from compare import build_report, load  # noqa: E402
from stepfinder import make_plan, run_config  # noqa: E402

OUT = HERE / "reports" / "migration_agent"
REPORTS = HERE / "reports" / "agent"
EDIT_FORMAT = {"fix": "fix id this edit replaces (optional)", "step": "classify|decide|draft|tone",
               "line_index": "0-based line to replace (PR.md shows lines 1-based)", "new_text": "replacement line, or null to delete it",
               "lines": "alternatively: the full new list of prompt lines for the step",
               "edits": "alternatively: a list of {step, line_index, new_text} edits, verified together"}


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:8]


def tagged(model, args):
    return Path(args.results) / f"agent__{model}{'__' + args.tag if args.tag else ''}.jsonl"


def verify(args, prompt_edits, label):
    """Full re-run with one or more step prompts replaced."""
    cand = load(tagged(args.candidate, args))
    source = {tid: {r["run"]: r for r in rs} for tid, rs in cand.items()}
    tickets = [t for t in A.load_tickets(sets="all") if t["id"] in cand]
    path = run_config(tickets, label, make_plan((args.candidate, args.candidate_config), {}), source, 0,
                      args.runs, Path(args.results), args.workers, prompts=prompt_edits)
    return build_report(tagged(args.old, args), path)


def verification(rep, regressed_before, runs, report_file):
    reg = [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED"]
    (OUT / report_file).write_text(json.dumps(rep, indent=2, default=list))
    return {"inputs": rep["inputs_compared"], "runs": runs, "verdict_counts": rep["verdict_counts"],
            "still_regressed": [i for i in reg if i in regressed_before],
            "newly_regressed": [i for i in reg if i not in regressed_before],
            "passes": rep["verdict_counts"]["REGRESSED"] == 0, "report_file": report_file}


def bench_note():
    """One sentence on how reliable the step-finder is, from the planted-break benchmark (if run).
    Quotes the HELD-OUT column of RESULTS.md: results.json only keeps the numbers after the narrowing fix,
    which was designed on the same tickets."""
    f, md = HERE / "reports" / "agent_bench" / "results.json", HERE / "reports" / "agent_bench" / "RESULTS.md"
    if not f.exists() or not md.exists():
        return ""
    import re
    r, text = json.loads(f.read_text()), md.read_text()
    n = r["tickets_localized"]
    row = lambda label: re.search(rf"^\| {re.escape(label)}[^|]*\| \**(\d+)\**", text, re.M)
    exact, both, wrong = row("Exactly the planted step(s)"), row("Confirmed both ways"), row("Wrong step")
    if not (exact and both and wrong):
        return ""
    return (f" How reliable this is (synthetic benchmark, {r['breaks']} planted breaks, {n} tickets, held-out): the "
            f"step-finder named exactly the planted step(s) on {exact.group(1)}/{n}, confirmed both ways on "
            f"{both.group(1)}/{n}, and named a wrong step on {wrong.group(1)} (`reports/agent_bench/RESULTS.md`).")


def score_rows(rep, fixed):
    """Rows for the evidence table: baseline | candidate (swap only) | candidate + fix."""
    b, c = rep["baseline"], rep["candidate"]
    f = fixed["candidate"] if fixed else None
    cost = lambda s: f"${s['cost_per_1k_calls_usd']:.2f}" if s and s["cost_per_1k_calls_usd"] is not None else "n/a"
    fx = lambda fn: fn(f) if f else "–"
    return [
        ("Passed all hard checks", f"{b['passed_all']:.0%}", f"{c['passed_all']:.0%}", fx(lambda s: f"{s['passed_all']:.0%}")),
        ("Gold score (category, order ID, action, reply content)", f"{b['accuracy']:.2f}", f"{c['accuracy']:.2f}",
         fx(lambda s: f"{s['accuracy']:.2f}")),
        ("Tickets regressed vs baseline", "–", rep["verdict_counts"]["REGRESSED"],
         fixed["verdict_counts"]["REGRESSED"] if fixed else "–"),
        ("Tickets whose decision varies between runs", b["unstable_inputs"], c["unstable_inputs"],
         fx(lambda s: s["unstable_inputs"])),
        ("Pipeline latency p50 (4 steps)", f"{b['latency_p50_s']:.2f}s", f"{c['latency_p50_s']:.2f}s",
         fx(lambda s: f"{s['latency_p50_s']:.2f}s")),
        ("Cost per 1k tickets", cost(b), cost(c), fx(cost)),
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", default="gpt-4")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--candidate-config", type=json.loads, default={})
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--results", default=str(HERE / "results"))
    ap.add_argument("--accept", action="append", default=[])
    ap.add_argument("--reject", action="append", default=[])
    ap.add_argument("--apply-edit", help="JSON file with the engineer's edited lines (see docstring)")
    ap.add_argument("--tag", help="ticket-set tag: agent__<model>__<tag>.jsonl in, reports/migration_agent_<tag> out")
    args = ap.parse_args()
    global OUT
    if args.tag:
        OUT = OUT.with_name(f"migration_agent_{args.tag}")
    started = datetime.now(timezone.utc).isoformat()
    results = Path(args.results)
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = json.dumps(args.candidate_config)
    prev = json.loads((OUT / "migration.json").read_text()) if (OUT / "migration.json").exists() else {}
    prev = prev if prev.get("candidate") == args.candidate else {}

    # 1. compare, 2. step-finder + line-finder (all cached) -------------------------------------
    rep = build_report(tagged(args.old, args), tagged(args.candidate, args))
    (OUT / f"compare_{args.candidate}.json").write_text(json.dumps(rep, indent=2, default=list))
    regressed = [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED"]
    print(f"[1/4] {args.old} -> {args.candidate}: {rep['verdict_counts']}")
    sf = lf = None
    if regressed:
        for script in ("stepfinder.py", "linefinder.py"):
            subprocess.run([sys.executable, str(HERE / script), "--candidate", args.candidate, "--candidate-config", cfg,
                            "--runs", str(args.runs)] + (["--tag", args.tag] if args.tag else []),
                           check=True, capture_output=True, text=True)
        suffix = f"_{args.tag}" if args.tag else ""
        sf = json.loads((REPORTS / f"stepfinder_{args.candidate}{suffix}.json").read_text())
        lf = json.loads((REPORTS / f"linefinder_{args.candidate}{suffix}.json").read_text())
        print("[2/4] step-finder: " + ", ".join(f"{s} {v['explains']}/{v['of']}" for s, v in sf["summary"].items()))

    # 3. fixes: one per proven line, plus engineer edits ----------------------------------------
    # An engineer-supplied edit supersedes auto-removal proposals. This matters when several
    # independent prompt lines must be fixed together: no individual removal can pass the full suite.
    fixes = {} if args.apply_edit else {f["id"]: f for f in prev.get("fixes", [])}
    proven = []
    for it in (lf or {}).get("items", []):
        for r in it["lines"]:
            if r["status"] == "confirmed" and (it["step"], r["line_index"]) not in [p[:2] for p in proven]:
                proven.append((it["step"], r["line_index"], r["line"]))
    engineer_edited = any(f.get("source", "").startswith("engineer") for f in fixes.values())
    for n, (step, i, line) in ([] if args.apply_edit or engineer_edited else enumerate(proven, 1)):
        fid = f"fix{n}"
        if fixes.get(fid, {}).get("source", "").startswith("engineer"):
            continue  # an engineer edit replaced this fix; keep it
        lines = A.PROMPTS[step][:i] + A.PROMPTS[step][i + 1:]
        v = verification(verify(args, {step: lines}, f"{args.candidate}__fix-{step}-L{i}{'__' + args.tag if args.tag else ''}"), regressed, args.runs,
                         f"compare_{args.candidate}_{fid}.json")
        fixes[fid] = {"id": fid, "step": step, "component": f"{step} prompt, line {i + 1}", "line_index": i,
                      "kind": "remove_line", "old_text": line, "new_text": None, "new_lines": lines,
                      "summary": f"remove line {i + 1} of the `{step}` prompt",
                      "caution": "Removing a line can also drop intent the tests don't cover. If this line states "
                                 "a rule you need, edit it instead of removing it.",
                      "source": "line-finder (removing this line alone repaired the regression)",
                      "verification": v, "decision": fixes.get(fid, {}).get("decision", "pending"),
                      "decided_at": fixes.get(fid, {}).get("decided_at")}
        print(f"[3/4] {fid}: remove {step} line {i + 1} -> {v['verdict_counts']}")

    if args.apply_edit:
        e = json.loads(Path(args.apply_edit).read_text())
        if "edits" in e:
            prompt_edits = {}
            changes = []
            for change in e["edits"]:
                step, i, new_text = change["step"], change["line_index"], change.get("new_text")
                lines = prompt_edits.get(step, list(A.PROMPTS[step]))
                old_text = lines[i]
                lines = lines[:i] + ([new_text] if new_text else []) + lines[i + 1:]
                prompt_edits[step] = lines
                changes.append({"step": step, "line_index": i, "old_text": old_text, "new_text": new_text})
            step = " + ".join(s for s in A.STEPS if s in prompt_edits)
            old_text = "\n".join(f"[{c['step']} line {c['line_index'] + 1}] {c['old_text']}" for c in changes)
            new_text = "\n".join(f"[{c['step']} line {c['line_index'] + 1}] {c['new_text'] or ''}" for c in changes)
            lines = None
        else:
            step = e["step"]
            if "lines" in e:
                lines, old_text, new_text = e["lines"], "\n".join(A.PROMPTS[step]), "\n".join(e["lines"])
            else:
                i = e["line_index"]
                old_text, new_text = A.PROMPTS[step][i], e.get("new_text")
                lines = A.PROMPTS[step][:i] + ([new_text] if new_text else []) + A.PROMPTS[step][i + 1:]
            prompt_edits = {step: lines}
        fid = e.get("fix") or f"edit{sum(f['id'].startswith('edit') for f in fixes.values()) + 1}"
        edit_label = f"{args.candidate}__edit-{sha(prompt_edits)}" + (f"__{args.tag}" if args.tag else "")
        v = verification(verify(args, prompt_edits, edit_label), regressed,
                         args.runs, f"compare_{args.candidate}_{fid}_edit.json")
        original = fixes.get(fid)
        where = (f"{step} prompts ({len(changes)} lines)" if "edits" in e else
                 f"{step} prompt" + ("" if "lines" in e else f", line {e['line_index'] + 1}"))
        fixes[fid] = {"id": fid, "step": step, "component": where, "kind": "edit",
                      "old_text": old_text, "new_text": new_text, "new_lines": lines,
                      **({"new_prompts": prompt_edits, "changes": changes} if "edits" in e else {}),
                      "summary": f"engineer edit of the `{step}` prompt" + ("s" if "edits" in e else ""),
                      "source": "engineer edit" + (f" replacing proposed {fid}" if original else ""),
                      "original": original, "verification": v, "edit_file": Path(args.apply_edit).name,
                      "decision": "edited" if v["passes"] else "pending",
                      "decided_at": started if v["passes"] else None}
        print(f"[3/4] {fid}: engineer edit -> {v['verdict_counts']} -> "
              f"{'edited (accepted)' if v['passes'] else 'still regressing; left pending'}")

    for fid in args.accept + args.reject:
        if fid not in fixes:
            sys.exit(f"no fix {fid}; have {sorted(fixes)}")
    for fid, decision in [(f, "accepted") for f in args.accept] + [(f, "rejected") for f in args.reject]:
        fixes[fid].update(decision=decision, decided_at=started)

    # 4. PR ---------------------------------------------------------------------------------
    fix_list = sorted(fixes.values(), key=lambda f: f["id"])
    live = pr_report.live_fixes({"fixes": fix_list})
    shown = live[0] if live else next((f for f in fix_list if f["decision"] == "pending"
                                       and f["verification"]["passes"]), None)
    shown_rep = json.loads((OUT / shown["verification"]["report_file"]).read_text()) if shown else None
    causes = []
    for it in rep["items"]:
        if it["verdict"] != "REGRESSED":
            continue
        tid = it["input_id"]
        tk = next((t for t in (sf or {}).get("tickets", []) if t["input_id"] == tid), None)
        lit = [x for x in (lf or {}).get("items", []) if x["input_id"] == tid]
        conf = [{"component": f"{x['step']} prompt, line {r['line_index'] + 1}", "text": r["line"], "step": x["step"],
                 "line_index": r["line_index"]} for x in lit for r in x["lines"] if r["status"] == "confirmed"]
        ev = []
        if tk:
            ev += [f"`{args.candidate}` everywhere except `{s}` (on `{args.old}`) → {j['verdict']} ({j['output']})"
                   for s, j in tk["necessity"].items()]
            ev += [f"`{args.old}` everywhere except `{s}` (on `{args.candidate}`) → {j['verdict']} ({j['output']})"
                   for s, j in tk["sufficiency"].items()]
        fixed_item = next((x for x in shown_rep["items"] if x["input_id"] == tid), None) if shown_rep else None
        causes.append({"input_id": tid, "text": it["text"], "baseline_output": it["baseline"]["output"],
                       "candidate_output": it["candidate"]["output"],
                       "fixed_output": fixed_item["candidate"]["output"] if fixed_item else None,
                       "causal_steps": tk["causal_steps"] if tk else [], "step_status": tk["status"] if tk else None,
                       "step_evidence": ev, "confirmed_lines": conf,
                       "experiments": experiments(tk, it["baseline"]["output"], it["candidate"]["output"]) if tk else [],
                       "lines_tested": sum(x["lines_tested"] for x in lit),
                       "status": "confirmed" if conf or (tk and tk["causal_steps"]) else "suspected",
                       "confirmed_by": "ablation" if conf else ("step-swap" if tk and tk["causal_steps"] else None),
                       "confirmed_component": conf[0]["component"] if conf else None, "suspects": []})
    other = [f"{it['verdict'].lower()}: `{it['input_id']}` \"{it['text'][:70]}\": {it['baseline']['output']} → "
             f"{it['candidate']['output']}" for it in rep["items"] if it["verdict"] in ("CHANGED", "IMPROVED")]
    m = {
        "group": "Migrations", "label": f"agent · {args.old} → {args.candidate}" + (f" · {args.tag} tickets" if args.tag else ""),
        "task": "agent", "pipeline": "4-step support agent", "candidate": args.candidate, "old_model": args.old,
        "new_model": args.candidate, "candidate_config": args.candidate_config, "n_inputs": rep["inputs_compared"],
        "cli": f"python migrate_agent.py --candidate {args.candidate} --candidate-config '{cfg}'",
        "title": f"Move the support agent from `{args.old}` to `{args.candidate}` before the Oct 23, 2026 shutdown",
        "why": [f"OpenAI shuts down `{args.old}` (`gpt-4-0613`) on Oct 23, 2026; after that every step of this agent fails."],
        "model_change": ([f"`{args.candidate}` only accepts its default temperature, so steps run with "
                          f"`{cfg}` instead of `temperature: 0`, and outputs can vary between runs."]
                         if args.candidate_config == {} else []),
        "steps": ({"names": A.STEPS, "summary": sf["summary"],
                   "method": f"For each regressed ticket, each step was swapped back to `{args.old}` one at a time "
                             f"({args.runs} runs each, earlier steps replayed from the candidate's own run). A step is "
                             f"causal when swapping it alone makes the ticket stop regressing; it is then checked the "
                             f"other way round (only that step on `{args.candidate}`)." + bench_note()}
                  if sf else None),
        "causes": causes,
        "evidence": {"table": score_rows(rep, shown_rep),
                     "noise_floor": f"`{args.old}` made the same decision in all {args.runs} runs on "
                                    f"{rep['inputs_compared'] - rep['baseline']['unstable_inputs']}/{rep['inputs_compared']} "
                                    f"tickets. A ticket counts as regressed only if most `{args.candidate}` runs fail a "
                                    f"hard check `{args.old}` passes, or score below `{args.old}`'s worst run."},
        "fixes": fix_list, "edit_format": EDIT_FORMAT, "other_differences": other,
        "lines": [{"step": x["step"], "line_index": r["line_index"], "text": r["line"], "proven": r["status"] == "confirmed",
                   "repaired": r["repaired_runs"]} for x in (lf or {}).get("items", [])[:1] for r in
                  sorted(x["lines"], key=lambda r: r["line_index"])],
        "limits": ["24 synthetic tickets with hand-written expected outcomes; scores depend on those labels.",
                   "**The `decide` prompt line \"Final-sale items are not eligible for refunds or replacements.\" was "
                   "planted** for this demo, chosen because it breaks only the new model (see "
                   "`reports/agent/plant_probe.json`). Apart from it, this candidate showed no regressions.",
                   "Line-level proof works when the new model over-applies a line that is present. It cannot find a "
                   "missing instruction; those are proven at component level by the fix instead."],
        "started": started, "finished": datetime.now(timezone.utc).isoformat(),
        "ready_to_merge": not regressed or any(f["verification"]["passes"] for f in live),
        "comparison_file": f"compare_{args.candidate}.json",
    }
    (OUT / "PR.md").write_text(pr_report.render(m))
    diff = []
    for f in live:
        prompts = f.get("new_prompts") or {f["step"]: f["new_lines"]}
        for step, lines in prompts.items():
            diff += difflib.unified_diff([l + "\n" for l in A.PROMPTS[step]], [l + "\n" for l in lines],
                                         fromfile=f"a/support_agent.py PROMPTS[{step!r}]",
                                         tofile=f"b/support_agent.py PROMPTS[{step!r}]")
    (OUT / "prompts.diff").write_text("".join(diff))
    (OUT / "migration.json").write_text(json.dumps(m, indent=2, default=list))
    import dashboard_export
    dashboard_export.export_dir(OUT)  # Switchyard dashboard view of the same report
    print(f"[4/4] PR -> {OUT / 'PR.md'}  (fixes: " + ", ".join(f"{f['id']}={f['decision']}" for f in fix_list)
          + f")  ready to merge: {m['ready_to_merge']}")


if __name__ == "__main__":
    main()
