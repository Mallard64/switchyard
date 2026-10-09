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
  python benchmark.py --ablate             # line-level ablation on the caught breaks (reuses caches)

Everything lands in reports/benchmark_ner/.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from compare import build_report, load  # noqa: E402
from record_baseline import cost  # noqa: E402
from migrate import (editable_components, find_causes, call_llm, fixer_prompt, FIXER_SYSTEM,  # noqa: E402
                     parse_json, patch_from_edit, n_ok_rows)

TASK, VARIANT, RUNS = "ner", "gpt-4", 3
RESULTS = HERE / "results"
# Set by configure(): the model the breaks run on. gpt-4 (default) reproduces the original benchmark;
# a cheap model (e.g. gpt-5.6-terra) re-bases it: baseline = that model on the unbroken prompt.
MODEL, MODEL_CONFIG, OUT, BASELINE = "gpt-4", None, HERE / "reports" / "benchmark_ner", None


def configure(model, model_config, baseline_runs=3):
    """baseline_runs=6 stacks the noise-floor re-run onto the baseline (worst of 6 runs, not 3);
    false alarms are then measured on a separate, held-out re-run."""
    global MODEL, MODEL_CONFIG, OUT, BASELINE, RERUN_TAG
    MODEL, MODEL_CONFIG = model, model_config
    OUT = HERE / "reports" / ("benchmark_ner" if model == "gpt-4" else f"benchmark_ner_{model}")
    BASELINE = RESULTS / f"{TASK}__{VARIANT}__{model}.jsonl"
    RERUN_TAG = "rerun"
    if baseline_runs == 6:
        BASELINE = [BASELINE, RESULTS / f"{TASK}__{VARIANT}__{model}__rerun.jsonl"]
        OUT = OUT.with_name(OUT.name + "_base6")
        RERUN_TAG = "rerun2"


def model_args():
    """record_baseline.py flags for the configured model (none for the original gpt-4 benchmark)."""
    if MODEL == "gpt-4":
        return []
    return ["--name", MODEL] + (["--model-config", json.dumps(MODEL_CONFIG)] if MODEL_CONFIG is not None else [])


configure("gpt-4", None)

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
# Held-out set (Oct 7), written before any run and never used to tune the ranking. Meant to be strong
# enough that a modern model reacts; a screen run checks that before detection is scored.
BREAKS_V2 = [
    {"id": "v2b01", "component": "description", "kind": "contradict",
     "new_text": DESC.replace("Pronouns are not entities.", "Pronouns that refer to food are entities.")},
    {"id": "v2b02", "component": "description", "kind": "contradict",
     "new_text": DESC + "\nCooking methods such as grilling, roasting or frying count as EQUIPMENT."},
    {"id": "v2b03", "component": "description", "kind": "blur",
     "new_text": "Entities are any words related to food or cooking."},
    {"id": "v2b04", "component": "label_definitions.DISH", "kind": "contradict",
     "new_text": "Any food item, including single ingredients."},
    {"id": "v2b05", "component": "label_definitions.DISH", "kind": "contradict",
     "new_text": "Restaurant menu items only; home-cooked food is not a dish."},
    {"id": "v2b06", "component": "label_definitions.INGREDIENT", "kind": "contradict",
     "new_text": "Raw, unprocessed foods only; sauces, cheeses and doughs are not ingredients."},
    {"id": "v2b07", "component": "label_definitions.INGREDIENT", "kind": "blur",
     "new_text": "Spices and seasonings."},
    {"id": "v2b08", "component": "label_definitions.EQUIPMENT", "kind": "contradict",
     "new_text": "Electric appliances only, e.g. blender, microwave, toaster."},
    {"id": "v2b09", "component": "label_definitions.EQUIPMENT", "kind": "contradict",
     "new_text": "Any physical object in the kitchen, e.g. plates, cups, bowls, napkins, countertops."},
    {"id": "v2b10", "component": "label_definitions.EQUIPMENT", "kind": "delete", "new_text": ""},
]
BREAK_SETS = {"v1": BREAKS, "v2": BREAKS_V2}
N_INPUTS = sum(1 for l in open(HERE / "inputs" / f"{TASK}.jsonl") if l.strip())
CALLS_PER_RUN = N_INPUTS * RUNS


def patch_for(component, new_text):
    if component == "description":
        return {"description": new_text}
    label = component.split(".", 1)[1]
    return {"label_definitions": {label: new_text}}


def run_record(patch, tag, runs=None):
    pfile = OUT / "patches" / f"{tag}.json"
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(json.dumps(patch, indent=2))
    path = RESULTS / f"{TASK}__{VARIANT}__{MODEL}__{tag}.jsonl"
    cmd = [sys.executable, str(HERE / "record_baseline.py"), "--task", TASK, "--variant", VARIANT,
           "--patch", str(pfile), "--tag", tag, "--runs", str(runs or RUNS)] + model_args()
    print(f"   $ record {tag}")
    before = total_cost(path)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    NEW_SPEND[0] += total_cost(path) - before  # only calls made now, not cached rows
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
        c += r.get("cost_usd") or cost(r.get("model"), r.get("usage")) or 0.0
    return c


ORIGINAL_IDS = {f"ner-{i:02d}" for i in range(1, 21)}  # the first 20 inputs (before the Oct 7 probes)


PREV_EDITS, PREV_DIRS = {}, []
NEW_SPEND = [0.0]  # USD of API calls actually made in this invocation


def load_prev_edits():
    """Fixer edits from earlier runs of this benchmark (this model), keyed by (break id, attempt)."""
    global PREV_DIRS
    dirs = [OUT, OUT.with_name(OUT.name.removesuffix("_base6"))]
    PREV_DIRS = [d for d in dict.fromkeys(dirs) if d.exists()]
    for d in PREV_DIRS:
        f = d / "results.json"
        if not f.exists():
            continue
        for r in json.loads(f.read_text()).get("breaks", []):
            for a in r.get("fix_attempts") or []:
                if a.get("edit"):
                    PREV_EDITS.setdefault((r["id"], a["attempt"]), a["edit"])


def subset(report, ids):
    """The report restricted to these input ids (verdict counts recomputed)."""
    items = [it for it in report["items"] if it["input_id"] in ids]
    counts = {v: sum(it["verdict"] == v for it in items) for v in ["REGRESSED", "IMPROVED", "CHANGED", "SAME"]}
    return {**report, "items": items, "verdict_counts": counts, "inputs_compared": len(items)}


def cause_scores(report, component, comps):
    caught = report["verdict_counts"]["REGRESSED"] > 0
    causes = find_causes(report, comps) if caught else []
    named = [s["component"] for c in causes for s in c["suspects"] if s["editable"]]
    top = [c["suspects"][0]["component"] for c in causes if c["suspects"] and c["suspects"][0]["editable"]]
    return {"caught": caught, "n_regressed": report["verdict_counts"]["REGRESSED"],
            "regressed_ids": [it["input_id"] for it in report["items"] if it["verdict"] == "REGRESSED"],
            "suspects_seen": sorted(set(named)), "component_named": component in named,
            "component_top": component in top}, causes


def run_break(b, skip_fix, spend, max_attempts=1):
    print(f"\n[{b['id']}] {b['component']} ({b['kind']})")
    patch = patch_for(b["component"], b["new_text"])
    path = run_record(patch, b["id"])
    if not n_ok_rows(path):
        return {**b, "caught": None, "error": "no successful rows"}
    report = build_report(BASELINE, path)
    comps = {**ORIG, b["component"]: b["new_text"]}  # the prompt as it is now: broken
    result, causes = cause_scores(report, b["component"], comps)
    result["first20"] = cause_scores(subset(report, ORIGINAL_IDS), b["component"], comps)[0]
    print(f"   caught: {result['caught']} ({result['n_regressed']} regressed: {result['regressed_ids']})  "
          f"component_named: {result['component_named']}  component_top: {result['component_top']}   "
          f"[first 20 only: caught {result['first20']['caught']}]")
    result = {**b, **result, "fix_attempted": False, "fix_accepted": None, "fix_edit": None, "fix_attempts": []}
    if not result["caught"] or skip_fix:
        return result
    history = []
    for n in range(1, max_attempts + 1):
        cached = PREV_EDITS.get((b["id"], n))
        print(f"   fixer call {n} ({MODEL}) ..." if not cached else f"   fix {n}: reusing cached edit (no fixer call)")
        try:
            if cached:
                edit = cached
            else:
                # The fixer sees the broken prompt (what an engineer would have), not the original.
                edit = parse_json(call_llm(MODEL, FIXER_SYSTEM, fixer_prompt(TASK, comps, causes, report, history)))
                edit["old_text"] = comps.get(edit["component"])
            fix_patch = patch_from_edit(edit)
        except (ValueError, RuntimeError, KeyError) as e:
            result["fix_attempts"].append({"attempt": n, "error": str(e)})
            print(f"   fixer error: {e}")
            continue
        # The fix edits the *broken* prompt: keep the break, apply the edit on top.
        full = json.loads(json.dumps(patch))
        for k, v in fix_patch.items():
            if k == "label_definitions":
                full.setdefault("label_definitions", {}).update(v)
            else:
                full[k] = v
        # Cache files are keyed by patch content, so a different edit never reuses another edit's rows.
        tag = f"{b['id']}_fix{n}"
        legacy = [d / "patches" / f"{tag}.json" for d in PREV_DIRS if (d / "patches" / f"{tag}.json").exists()]
        if not (legacy and json.loads(legacy[0].read_text()) == full):
            tag += "_" + hashlib.sha256(json.dumps(full, sort_keys=True).encode()).hexdigest()[:8]
        fix_path = run_record(full, tag)
        if not n_ok_rows(fix_path):
            result["fix_attempts"].append({"attempt": n, "edit": edit, "error": "re-run produced no results"})
            continue
        fix_report = build_report(BASELINE, fix_path)
        ok = fix_report["verdict_counts"]["REGRESSED"] == 0
        still = [it["input_id"] for it in fix_report["items"] if it["verdict"] == "REGRESSED"]
        print(f"   fix {n} edits {edit['component']} -> {fix_report['verdict_counts']} {'ACCEPTED' if ok else still}")
        result["fix_attempts"].append({"attempt": n, "edit": edit, "verdict_counts": fix_report["verdict_counts"],
                                       "still_regressed": still, "accepted": ok,
                                       "accepted_first20": subset(fix_report, ORIGINAL_IDS)["verdict_counts"]["REGRESSED"] == 0})
        if ok:
            break
        history.append({"component": edit["component"], "new_text": edit["new_text"],
                        "outcome": f"still regressed {still}"})
    good = [a for a in result["fix_attempts"] if a.get("accepted")]
    result.update(fix_attempted=bool(result["fix_attempts"]), fix_accepted=bool(good),
                  fix_first_attempt=bool(result["fix_attempts"]) and bool(result["fix_attempts"][0].get("accepted")),
                  fix_edit=good[0]["edit"] if good else None,
                  fix_targets_planted=bool(good) and good[0]["edit"]["component"] == b["component"])
    return result


def false_alarm_check(spend):
    print(f"\n[false-alarm] fresh {MODEL} re-run (no patch, tag {RERUN_TAG}) vs. stored baseline")
    pfile_cmd = [sys.executable, str(HERE / "record_baseline.py"), "--task", TASK, "--variant", VARIANT,
                "--tag", RERUN_TAG, "--runs", str(RUNS)] + model_args()
    before = total_cost(RESULTS / f"{TASK}__{VARIANT}__{MODEL}__{RERUN_TAG}.jsonl")
    proc = subprocess.run(pfile_cmd, capture_output=True, text=True)
    NEW_SPEND[0] += total_cost(RESULTS / f"{TASK}__{VARIANT}__{MODEL}__{RERUN_TAG}.jsonl") - before
    tail = [l for l in proc.stdout.splitlines() if "ERROR" in l]
    if tail:
        print("     " + "\n     ".join(tail[:3]))
    path = RESULTS / f"{TASK}__{VARIANT}__{MODEL}__{RERUN_TAG}.jsonl"
    if not n_ok_rows(path):
        return {"error": "no successful rows"}
    report = build_report(BASELINE, path)
    print(f"   verdict_counts: {report['verdict_counts']}")
    return {"verdict_counts": report["verdict_counts"], "false_alarms": report["verdict_counts"]["REGRESSED"]}


def planted_units(spec):
    """unit_ids in the broken component whose text is not in the original (None for pure deletions)."""
    from ablation import split_units
    orig = " ".join(ORIG[spec["component"]].split())
    new = [u["unit_id"] for u in split_units({spec["component"]: spec["new_text"]}) if u["text"] not in orig]
    return set(new) or None


def per_run_scores(paths):
    """{(file, run): (mean lenient F1, hard-check pass rate)} for each run in these results files."""
    from compare import load_gold, view
    gold, out = load_gold(TASK), {}
    for p in paths:
        by_run = {}
        for rs in load(p).values():
            for r in rs:
                by_run.setdefault(r["run"], []).append(r)
        for run, rs in by_run.items():
            out[(Path(p).name, run)] = (sum(view(TASK, r, gold[r["input_id"]])["score"] for r in rs) / len(rs),
                                        sum(r["passed_all"] for r in rs) / len(rs))
    return out


def screen(breaks):
    """One run per break on all inputs. Effective = F1 or hard-check pass rate below every unbroken run
    (baseline, its re-run and the held-out re-run): an effect test independent of the regression rule."""
    unbroken = per_run_scores(BASELINE + [RESULTS / f"{TASK}__{VARIANT}__{MODEL}__{RERUN_TAG}.jsonl"])
    f1_floor = min(v[0] for v in unbroken.values())
    pass_floor = min(v[1] for v in unbroken.values())
    print(f"\n[screen] {len(unbroken)} unbroken runs: F1 {f1_floor:.3f}-{max(v[0] for v in unbroken.values()):.3f}, "
          f"pass rate >= {pass_floor:.2f}")
    out = {}
    for b in breaks:
        path = run_record(patch_for(b["component"], b["new_text"]), f"{b['id']}_screen", runs=1)
        f1, passed = next(iter(per_run_scores([path]).values()))
        out[b["id"]] = {"f1": round(f1, 4), "pass_rate": round(passed, 4), "f1_floor": round(f1_floor, 4),
                        "pass_floor": round(pass_floor, 4), "effective": f1 < f1_floor or passed < pass_floor}
        print(f"   {b['id']}: F1 {f1:.3f}  pass {passed:.2f}  -> {'EFFECTIVE' if out[b['id']]['effective'] else 'no effect'}")
    return out


def rescore(breaks):
    """Cause-ranking metrics from cached break runs (for comparing ranking changes at no cost)."""
    rows = []
    for b in breaks:
        path = RESULTS / f"{TASK}__{VARIANT}__{MODEL}__{b['id']}.jsonl"
        if not path.exists():
            continue
        report = build_report(BASELINE, path)
        comps = {**ORIG, b["component"]: b["new_text"]}
        full = cause_scores(report, b["component"], comps)[0]
        sub = cause_scores(subset(report, ORIGINAL_IDS), b["component"], comps)[0]
        rows.append((b["id"], full, sub))
        print(f"  {b['id']}: all {full['caught']}/{full['component_named']}/{full['component_top']}   "
              f"first20 {sub['caught']}/{sub['component_named']}/{sub['component_top']}   (caught/named/top)")
    for name, k in (("all inputs", 1), ("first 20", 2)):
        caught = [r[k] for r in rows if r[k]["caught"]]
        print(f"{name}: caught {len(caught)}/{len(rows)}, named {sum(c['component_named'] for c in caught)}, "
              f"top {sum(c['component_top'] for c in caught)}")


def ablate_caught(spend):
    """Re-score cause-finding on the caught breaks: heuristic top suspect vs. ablation."""
    from ablation import ablate
    prev = json.loads((OUT / "results.json").read_text())
    out = []
    for b in [x for x in prev["breaks"] if x.get("caught")]:
        spec = next(x for x in BREAKS if x["id"] == b["id"])
        print(f"\n[{b['id']}] {spec['component']} ({spec['kind']})")
        report = build_report(BASELINE, RESULTS / f"{TASK}__{VARIANT}__{MODEL}__{b['id']}.jsonl")
        comps = {**ORIG, spec["component"]: spec["new_text"]}
        causes = find_causes(report, comps)
        heur_top = [c["suspects"][0]["component"] for c in causes if c["suspects"] and c["suspects"][0]["editable"]]
        abl_files = lambda: sum(total_cost(p) for p in RESULTS.glob(f"{TASK}__{VARIANT}__{MODEL}__{b['id']}_abl-*.jsonl"))
        before = abl_files()
        ablate(TASK, BASELINE, comps, causes, variant=VARIANT, name=None if MODEL == "gpt-4" else MODEL,
               model_config=MODEL_CONFIG, base_patch=patch_for(spec["component"], spec["new_text"]),
               tag_prefix=f"{b['id']}_", runs=RUNS, results_dir=RESULTS, patch_dir=OUT / "patches")
        spend[0] += abl_files() - before  # only calls made now
        planted = planted_units(spec)
        abl_top = [c["confirmed_lines"][0]["component"] for c in causes if c["confirmed_lines"]]
        confirmed = [u for c in causes for u in c["confirmed_lines"]]
        hits = [u for u in confirmed if planted and u["unit_id"] in planted]
        r = {"id": b["id"], "component": spec["component"], "kind": spec["kind"],
             "regressed_ids": [c["input_id"] for c in causes],
             "top_before": spec["component"] in heur_top, "top_after": spec["component"] in abl_top,
             "planted_units": sorted(planted or []),
             "unstable_inputs": [c["input_id"] for c in causes if not c["ablation_control"]["stable"]],
             "any_confirmed": bool(confirmed), "planted_unit_confirmed": bool(hits) if planted else None,
             "confirmed_units": confirmed,
             "other_units_confirmed": [u for u in confirmed if u not in hits],
             "causes": causes}
        print(f"   top-ranked: heuristic {r['top_before']} -> ablation {r['top_after']}   "
              f"planted {r['planted_units']} confirmed: {r['planted_unit_confirmed']}   confirmed: "
              f"{[u['unit_id'] for u in confirmed]}   unstable: {r['unstable_inputs']}")
        out.append(r)
    n = len(out)
    summary = {"n_caught": n,
               "top_rate_before": sum(r["top_before"] for r in out) / n if n else None,
               "top_rate_after": sum(r["top_after"] for r in out) / n if n else None,
               "breaks": out, "spend_usd": round(spend[0], 4)}
    (OUT / "ablation.json").write_text(json.dumps(summary, indent=2, default=list))
    print(f"\ntop-ranked component: {sum(r['top_before'] for r in out)}/{n} (heuristic) -> "
          f"{sum(r['top_after'] for r in out)}/{n} (ablation)   spend ${spend[0]:.2f} -> {OUT / 'ablation.json'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", help="run only these break ids, e.g. break01")
    ap.add_argument("--skip-fix", action="store_true", help="catch/cause only, skip the fix step")
    ap.add_argument("--skip-false-alarm", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and cost estimate; no calls")
    ap.add_argument("--ablate", action="store_true", help="line-level ablation on the caught breaks")
    ap.add_argument("--max-fix-attempts", type=int, default=3)
    ap.add_argument("--breaks", choices=sorted(BREAK_SETS), default="v1",
                    help="v2 = held-out set: screened first, detection scored on effective breaks only")
    ap.add_argument("--rescore", action="store_true",
                    help="recompute catch / component / top-ranked from cached break runs only (no API calls)")
    ap.add_argument("--model", default="gpt-4", help="model the breaks run on (default: the original gpt-4 benchmark)")
    ap.add_argument("--model-config", type=json.loads, default=None, help="e.g. '{}' for gpt-5.6 models")
    ap.add_argument("--baseline-runs", type=int, choices=[3, 6], default=3,
                    help="6 = stack the noise-floor re-run onto the baseline (needs the 'rerun' file)")
    args = ap.parse_args()
    configure(args.model, args.model_config, args.baseline_runs)
    global OUT
    if args.breaks != "v1":
        OUT = OUT.with_name(OUT.name + f"_{args.breaks}")
    load_prev_edits()
    if args.ablate:
        return ablate_caught([0.0])
    if args.rescore:
        return rescore(BREAK_SETS[args.breaks])

    breaks = [b for b in BREAK_SETS[args.breaks] if not args.only or b["id"] in args.only]
    if args.dry_run:
        print(f"{len(breaks)} break(s), {RUNS} runs x {N_INPUTS} inputs = {CALLS_PER_RUN} calls each (record step).")
        base0 = BASELINE[0] if isinstance(BASELINE, list) else BASELINE
        per_call = total_cost(base0) / max(n_ok_rows(base0), 1)
        est_low = len(breaks) * CALLS_PER_RUN * per_call
        est_high = est_low * 2  # + one fix re-run each
        print(f"gpt-4 cost/call from stored baseline: ${per_call:.4f}")
        print(f"Estimated cost: ${est_low:.2f} (no fixes needed) to ${est_high:.2f} (every break needs a fix)"
             + ("" if args.skip_fix else "") + (" + ~1 false-alarm re-run (~$" +
               f"{CALLS_PER_RUN * per_call:.2f})" if not args.skip_false_alarm else ""))
        for b in breaks:
            print(f"  {b['id']}: {b['component']} ({b['kind']})")
        return

    if not all(n_ok_rows(p) for p in (BASELINE if isinstance(BASELINE, list) else [BASELINE])):
        sys.exit(f"No baseline at {BASELINE}. Run record_baseline.py first.")
    OUT.mkdir(parents=True, exist_ok=True)
    spend = [0.0]
    screened = None
    if args.breaks == "v2":
        if args.baseline_runs != 6:
            sys.exit("--breaks v2 needs --baseline-runs 6 (the screen uses the held-out re-run)")
        screened = screen(breaks)
        breaks = [b for b in breaks if screened[b["id"]]["effective"]]
    results = [run_break(b, args.skip_fix, spend, args.max_fix_attempts) for b in breaks]
    false_alarm = None if args.skip_false_alarm else false_alarm_check(spend)

    def rates(rs, key=None):
        get = (lambda r: r[key]) if key else (lambda r: r)
        caught = [r for r in rs if get(r).get("caught")]
        n = len(rs)
        return {"n_breaks": n, "caught": len(caught), "catch_rate": len(caught) / n if n else None,
                "right_component_rate": sum(get(r)["component_named"] for r in caught) / len(caught) if caught else None,
                "top_component_rate": sum(get(r)["component_top"] for r in caught) / len(caught) if caught else None}
    caught = [r for r in results if r.get("caught")]
    tried = [r for r in caught if r.get("fix_attempted")]
    summary = {
        "model": MODEL, "model_config": MODEL_CONFIG, "n_inputs": N_INPUTS, "runs": RUNS,
        "break_set": args.breaks, "screen": screened,
        "breaks": results, "false_alarm": false_alarm,
        "all_inputs": rates(results), "first20": rates(results, "first20"),
        "fix_rate_first_attempt": sum(r["fix_first_attempt"] for r in tried) / len(tried) if tried else None,
        "fix_rate_within_attempts": sum(r["fix_accepted"] for r in tried) / len(tried) if tried else None,
        "max_fix_attempts": args.max_fix_attempts,
        "spend_usd": round(NEW_SPEND[0], 4),
    }
    (OUT / "results.json").write_text(json.dumps(summary, indent=2, default=list))
    for k in ("first20", "all_inputs"):
        print(f"\n{k}: {summary[k]}")
    print(f"fix rate: first attempt {summary['fix_rate_first_attempt']}, "
          f"within {args.max_fix_attempts} {summary['fix_rate_within_attempts']}")
    if false_alarm:
        print(f"false alarms on a fresh baseline re-run: {false_alarm['false_alarms']}")
    print(f"new API spend this run: ${NEW_SPEND[0]:.2f}  ->  {OUT / 'results.json'}")


if __name__ == "__main__":
    main()
