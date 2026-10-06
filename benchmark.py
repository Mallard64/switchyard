"""Planted-bug benchmark: inject known single-component breaks into the gpt-4 NER prompt,
then measure whether migrate.py's machinery (a) catches the regression, (b) points the cause
at the component we actually broke, and (c) fixes it (0 regressions after a patch).

Each break edits exactly one editable component (description or one label_definitions entry)
by deleting part of it, blurring it, or contradicting it -- the categories in CLAUDE.md's
planted-bug task. The real gpt-4 baseline (results/ner__gpt-4__gpt-4.jsonl) is the untouched
reference; every break re-runs gpt-4 itself with only the prompt changed, via
record_baseline.py's --patch/--tag, which keeps its own resumable cache.

  python benchmark.py --dry-run           # show the 10 breaks + cost estimate, no calls
  python benchmark.py --only break01       # smoke test one break (~60 calls, ~$1)
  python benchmark.py                      # all breaks + the false-alarm check
  python benchmark.py --skip-fix           # catch/cause only, skip the fix step (half the cost)

Everything lands in reports/benchmark_ner/.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from compare import build_report  # noqa: E402
from migrate import (editable_components, find_causes, call_llm, fixer_prompt, FIXER_SYSTEM,  # noqa: E402
                     parse_json, patch_from_edit, n_ok_rows)

TASK, VARIANT, MODEL, RUNS = "ner", "gpt-4", "gpt-4", 3
RESULTS = HERE / "results"
OUT = HERE / "reports" / "benchmark_ner"
BASELINE = RESULTS / f"{TASK}__{VARIANT}__{MODEL}.jsonl"

ORIG = editable_components(TASK)
DESC, DISH, INGREDIENT, EQUIPMENT = (ORIG["description"], ORIG["label_definitions.DISH"],
                                     ORIG["label_definitions.INGREDIENT"], ORIG["label_definitions.EQUIPMENT"])

BREAKS = [
    {"id": "break01", "component": "description", "kind": "delete",
     "new_text": DESC.replace("\nPronouns are not entities.", "")},
    {"id": "break02", "component": "description", "kind": "delete",
     "new_text": DESC.replace("\nAdjectives, verbs, adverbs are not entities.", "")},
    {"id": "break03", "component": "description", "kind": "contradict",
     "new_text": DESC.replace("Adjectives, verbs, adverbs are not entities.",
                              "Adjectives, verbs, and adverbs can also be entities when they name a "
                              "cooking action or quality.")},
    {"id": "break04", "component": "description", "kind": "delete",
     "new_text": DESC.replace(" and any kind of cooking equipment", "")},
    {"id": "break05", "component": "label_definitions.DISH", "kind": "blur",
     "new_text": "Known food dishes."},
    {"id": "break06", "component": "label_definitions.DISH", "kind": "contradict",
     "new_text": DISH + ", including raw ingredients plated for serving, e.g. sliced tomatoes"},
    {"id": "break07", "component": "label_definitions.INGREDIENT", "kind": "blur",
     "new_text": "Individual parts of a food dish."},
    {"id": "break08", "component": "label_definitions.INGREDIENT", "kind": "contradict",
     "new_text": INGREDIENT[:-1] + ", and any finished dish served alongside it."},
    {"id": "break09", "component": "label_definitions.EQUIPMENT", "kind": "blur",
     "new_text": "Any kind of cooking equipment."},
    {"id": "break10", "component": "label_definitions.EQUIPMENT", "kind": "contradict",
     "new_text": EQUIPMENT + ", bowl, plate, cup"},
]
CALLS_PER_RUN = 20 * RUNS  # 20 inputs


def patch_for(component, new_text):
    if component == "description":
        return {"description": new_text}
    label = component.split(".", 1)[1]
    return {"label_definitions": {label: new_text}}


def run_record(patch, tag):
    pfile = OUT / "patches" / f"{tag}.json"
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(json.dumps(patch, indent=2))
    path = RESULTS / f"{TASK}__{VARIANT}__{MODEL}__{tag}.jsonl"
    cmd = [sys.executable, str(HERE / "record_baseline.py"), "--task", TASK, "--variant", VARIANT,
           "--patch", str(pfile), "--tag", tag, "--runs", str(RUNS)]
    print(f"   $ record {tag}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    tail = [l for l in proc.stdout.splitlines() if "ERROR" in l or "Could not build" in l]
    if tail:
        print("     " + "\n     ".join(tail[:3]))
    return path


def total_cost(path):
    if not path.exists():
        return 0.0
    c = 0.0
    for line in open(path):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        c += r.get("cost_usd") or 0.0
    return c


def run_break(b, skip_fix, spend):
    print(f"\n[{b['id']}] {b['component']} ({b['kind']})")
    patch = patch_for(b["component"], b["new_text"])
    path = run_record(patch, b["id"])
    spend[0] += total_cost(path)
    if not n_ok_rows(path):
        return {**b, "caught": None, "error": "no successful rows"}
    report = build_report(BASELINE, path)
    caught = report["verdict_counts"]["REGRESSED"] > 0
    causes = find_causes(report, ORIG) if caught else []
    named = [s["component"] for c in causes for s in c["suspects"] if s["editable"]]
    top = [c["suspects"][0]["component"] for c in causes if c["suspects"] and c["suspects"][0]["editable"]]
    component_named = b["component"] in named
    component_top = b["component"] in top
    print(f"   caught: {caught} ({report['verdict_counts']['REGRESSED']} regressed)  "
         f"component_named: {component_named}  component_top: {component_top}")
    result = {**b, "caught": caught, "n_regressed": report["verdict_counts"]["REGRESSED"],
              "regressed_ids": [it["input_id"] for it in report["items"] if it["verdict"] == "REGRESSED"],
              "suspects_seen": sorted(set(named)), "component_named": component_named,
              "component_top": component_top, "fix_attempted": False, "fix_accepted": None, "fix_edit": None}
    if not caught or skip_fix:
        return result
    print(f"   fixer call ({MODEL}) ...")
    try:
        edit = parse_json(call_llm(MODEL, FIXER_SYSTEM, fixer_prompt(TASK, ORIG, causes, report, [])))
        edit["old_text"] = ORIG.get(edit["component"])
        fix_patch = patch_from_edit(edit)
    except (ValueError, RuntimeError, KeyError) as e:
        result.update(fix_attempted=True, fix_accepted=False, fix_edit={"error": str(e)})
        print(f"   fixer error: {e}")
        return result
    fix_tag = f"{b['id']}_fix1"
    fix_path = run_record(fix_patch, fix_tag)
    spend[0] += total_cost(fix_path)
    if not n_ok_rows(fix_path):
        result.update(fix_attempted=True, fix_accepted=False, fix_edit=edit)
        return result
    fix_report = build_report(BASELINE, fix_path)
    accepted = fix_report["verdict_counts"]["REGRESSED"] == 0
    print(f"   fix edits {edit['component']}  ->  accepted: {accepted} "
         f"({fix_report['verdict_counts']})")
    result.update(fix_attempted=True, fix_accepted=accepted, fix_edit=edit,
                  fix_verdict_counts=fix_report["verdict_counts"])
    return result


def false_alarm_check(spend):
    print("\n[false-alarm] fresh gpt-4 re-run (no patch) vs. stored baseline")
    pfile_cmd = [sys.executable, str(HERE / "record_baseline.py"), "--task", TASK, "--variant", VARIANT,
                "--tag", "rerun", "--runs", str(RUNS)]
    proc = subprocess.run(pfile_cmd, capture_output=True, text=True)
    tail = [l for l in proc.stdout.splitlines() if "ERROR" in l]
    if tail:
        print("     " + "\n     ".join(tail[:3]))
    path = RESULTS / f"{TASK}__{VARIANT}__{MODEL}__rerun.jsonl"
    spend[0] += total_cost(path)
    if not n_ok_rows(path):
        return {"error": "no successful rows"}
    report = build_report(BASELINE, path)
    print(f"   verdict_counts: {report['verdict_counts']}")
    return {"verdict_counts": report["verdict_counts"], "false_alarms": report["verdict_counts"]["REGRESSED"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", help="run only these break ids, e.g. break01")
    ap.add_argument("--skip-fix", action="store_true", help="catch/cause only, skip the fix step")
    ap.add_argument("--skip-false-alarm", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and cost estimate; no calls")
    args = ap.parse_args()

    breaks = [b for b in BREAKS if not args.only or b["id"] in args.only]
    if args.dry_run:
        print(f"{len(breaks)} break(s), {RUNS} runs x 20 inputs = {CALLS_PER_RUN} calls each (record step).")
        per_call = total_cost(BASELINE) / max(n_ok_rows(BASELINE), 1)
        est_low = len(breaks) * CALLS_PER_RUN * per_call
        est_high = est_low * 2  # + one fix re-run each
        print(f"gpt-4 cost/call from stored baseline: ${per_call:.4f}")
        print(f"Estimated cost: ${est_low:.2f} (no fixes needed) to ${est_high:.2f} (every break needs a fix)"
             + ("" if args.skip_fix else "") + (" + ~1 false-alarm re-run (~$" +
               f"{CALLS_PER_RUN * per_call:.2f})" if not args.skip_false_alarm else ""))
        for b in breaks:
            print(f"  {b['id']}: {b['component']} ({b['kind']})")
        return

    if not n_ok_rows(BASELINE):
        sys.exit(f"No baseline at {BASELINE}. Run record_baseline.py first.")
    OUT.mkdir(parents=True, exist_ok=True)
    spend = [0.0]
    results = [run_break(b, args.skip_fix, spend) for b in breaks]
    false_alarm = None if args.skip_false_alarm else false_alarm_check(spend)

    caught = [r for r in results if r.get("caught")]
    summary = {
        "breaks": results,
        "false_alarm": false_alarm,
        "n_breaks": len(results),
        "catch_rate": sum(1 for r in results if r.get("caught")) / len(results) if results else None,
        "right_component_rate": (sum(1 for r in caught if r["component_named"]) / len(caught)
                                 if caught else None),
        "top_component_rate": (sum(1 for r in caught if r["component_top"]) / len(caught)
                               if caught else None),
        "fix_rate": (sum(1 for r in caught if r.get("fix_accepted")) /
                    sum(1 for r in caught if r.get("fix_attempted")) if any(r.get("fix_attempted") for r in caught)
                    else None),
        "spend_usd": round(spend[0], 4),
    }
    (OUT / "results.json").write_text(json.dumps(summary, indent=2, default=list))
    print(f"\n{len(results)} breaks: catch rate {summary['catch_rate']}, "
         f"right-component rate {summary['right_component_rate']}, fix rate {summary['fix_rate']}")
    if false_alarm:
        print(f"false alarms on a fresh baseline re-run: {false_alarm['false_alarms']}")
    print(f"spend this run: ${spend[0]:.2f}  ->  {OUT / 'results.json'}")


if __name__ == "__main__":
    main()
