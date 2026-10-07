"""End-to-end migration: test several candidate models, pick the best, find the prompt
component behind each regression, fix it, prove the fix by re-running everything, and
write a pull request.

  python migrate.py --task ner --candidates gpt-5.6-sol gpt-5.6-terra --model-config '{}'

Steps (all results are cached, so re-running only does missing work):
  1. Record each candidate on the same inputs as the baseline (record_baseline.py).
  2. Compare each against the baseline (compare.py) and rank them.
  3. Cause: map every regression on the best candidate to the prompt components it implicates
     (a failed hard check -> its instruction; a wrong/spurious/missed label -> that label's
     definition; a span-boundary error -> the task description). That heuristic only orders
     the search: ablation.py then removes one sentence at a time, re-runs the failing inputs
     on the candidate, and marks a cause "confirmed" (by ablation) when removing a sentence
     makes the input stop regressing.
  4. Fix: an LLM rewrites ONE implicated component. The full suite is re-run with the patch.
     The fix is accepted only if no input regresses against the baseline. A cause is
     "confirmed" when editing that component alone removes the regression.
  5. PR: a patched copy of the upstream example config, a unified diff, and a PR description.

Everything lands in reports/migration_<task>/ ; migration.json is what the dashboard reads.
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from compare import build_report  # noqa: E402

OPENAI_BASE = "https://api.openai.com/v1"
TASK_CONFIG = {"ner": HERE / "upstream" / "ner_fewshot.cfg", "textcat": HERE / "upstream" / "textcat_zeroshot.cfg"}
UPSTREAM_PATH = {"ner": "usage_examples/ner_v3_openai/fewshot.cfg",
                 "textcat": "usage_examples/textcat_openai/zeroshot.cfg"}


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------
def results_path(outdir, task, variant, model, tag=None):
    return Path(outdir) / f"{task}__{variant}__{model}{'__' + tag if tag else ''}.jsonl"


def n_ok_rows(path):
    if not path.exists():
        return 0
    n = 0
    for line in open(path):
        try:
            n += not json.loads(line).get("error")
        except json.JSONDecodeError:
            pass
    return n


def record(args, model, patch_file=None, tag=None):
    cmd = [sys.executable, str(HERE / "record_baseline.py"), "--task", args.task, "--variant", args.variant,
           "--name", model, "--runs", str(args.runs), "--outdir", args.results]
    if args.model_config is not None:
        cmd += ["--model-config", json.dumps(args.model_config)]
    if args.api_base:
        cmd += ["--api-base", args.api_base]
    if patch_file:
        cmd += ["--patch", str(patch_file), "--tag", tag]
    print(f"   $ record {model}{' + ' + tag if tag else ''}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    tail = [l for l in proc.stdout.splitlines() if "ERROR" in l or "Could not build" in l]
    if tail:
        print("     " + "\n     ".join(tail[:3]) + (f"\n     ... {len(tail) - 3} more" if len(tail) > 3 else ""))
    return results_path(args.results, args.task, args.variant, model, tag)


def editable_components(task):
    """Prompt parts a config change can edit, read from the upstream example config."""
    from spacy.util import load_config
    cfg = load_config(TASK_CONFIG[task], interpolate=False)["components"]["llm"]["task"]
    comps = {}
    if cfg.get("description"):
        comps["description"] = cfg["description"]
    for label, text in (cfg.get("label_definitions") or {}).items():
        comps[f"label_definitions.{label}"] = text
    return comps


def rank_key(rep):
    c = rep["candidate"]
    return (rep["verdict_counts"]["REGRESSED"], -c["accuracy"], -c["passed_all"],
            c["cost_per_1k_calls_usd"] if c["cost_per_1k_calls_usd"] is not None else float("inf"),
            c["latency_p50_s"])


# ---------------------------------------------------------------------------------------
# cause
# ---------------------------------------------------------------------------------------
def find_causes(report, components):
    causes = []
    for it in report["items"]:
        if it["verdict"] != "REGRESSED":
            continue
        suspects = Counter()
        evidence = []
        for r in it["regressions"]:
            if r["kind"] == "hard_check":
                suspects[f"template:{r['check']}"] += 5
                evidence.append(f"hard check '{r['check']}' fails {r['candidate_fail']} runs "
                                f"(baseline {r['baseline_fail']}); instruction: \"{r['instruction']}\"")
        for e in it["candidate"]["errors"]:
            if e["type"] in ("spurious", "wrong_label"):
                label = e["pred"][1] if isinstance(e["pred"], (list, tuple)) else e["pred"]
                suspects[f"label_definitions.{label}"] += 2
                suspects["description"] += 1
                evidence.append(f"{e['type']}: predicted {tuple(e['pred'])}"
                                + (f", gold {tuple(e['gold'])}" if "gold" in e else ""))
            elif e["type"] == "missed":
                suspects[f"label_definitions.{e['gold'][1]}"] += 2
                suspects["description"] += 1
                evidence.append(f"missed {tuple(e['gold'])}")
            elif e["type"] == "boundary":
                suspects["description"] += 2
                evidence.append(f"boundary: {tuple(e['pred'])} vs gold {tuple(e['gold'])}")
        ranked = [{"component": c, "score": s, "editable": c in components,
                   "text": components.get(c)} for c, s in suspects.most_common()]
        causes.append({"input_id": it["input_id"], "text": it["text"],
                       "baseline_output": it["baseline"]["output"], "candidate_output": it["candidate"]["output"],
                       "evidence": evidence, "suspects": ranked, "status": "suspected", "confirmed_component": None,
                       "confirmed_by": None, "confirmed_lines": [], "lines": []})
    return causes


# ---------------------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------------------
FIXER_SYSTEM = """You are a prompt engineer migrating an LLM pipeline to a new model.
The new model regressed on some inputs compared with the old model. Propose ONE minimal edit
to ONE editable prompt component so the new model behaves like the old one on the failing
inputs, without changing its behaviour on anything else.
Rules: edit only a component listed as editable; keep the original wording where possible and
add the smallest clarification that resolves the ambiguity; do not mention specific test inputs.
Reply with JSON only: {"component": "<id>", "new_text": "<full replacement text>", "rationale": "<one sentence>"}"""


def fixer_prompt(task, components, causes, report, history):
    lines = ["Editable prompt components:"]
    for cid, text in components.items():
        lines.append(f"- {cid}: {json.dumps(text)}")
    lines.append("\nRegressed inputs (old model output is the reference):")
    for c in causes:
        lines.append(f"- Text: {c['text']}\n  old model: {c['baseline_output']}\n  new model: {c['candidate_output']}"
                     f"\n  evidence: {'; '.join(c['evidence'])}"
                     + (f"\n  proven cause (removing these sentences removes the regression): "
                        f"{[(u['component'], u['text']) for u in c.get('confirmed_lines', [])]}"
                        if c.get("confirmed_lines") else "")
                     + f"\n  most likely components: {[s['component'] for s in c['suspects'] if s['editable']][:3]}")
    keep = [it for it in report["items"] if it["verdict"] in ("SAME", "IMPROVED")][:6]
    lines.append("\nInputs that are currently correct and must stay that way (sample):")
    for it in keep:
        lines.append(f"- {it['text']}  ->  {it['candidate']['output']}")
    for h in history:
        lines.append(f"\nA previous attempt edited {h['component']} to {json.dumps(h['new_text'])} and was rejected: "
                     f"{h['outcome']}. Try a different edit.")
    return "\n".join(lines)


def call_llm(model, system, user, api_base=None, timeout=120):
    url = (api_base or OPENAI_BASE).rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "messages": [{"role": "system", "content": system},
                                                    {"role": "user", "content": user}]}).encode()
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or attempt == 3:
                raise RuntimeError(f"fixer call failed: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
        time.sleep(2 ** attempt)


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in fixer reply: {text[:200]}")
    return json.loads(m.group(0))


def patch_from_edit(edit):
    cid, text = edit["component"], edit["new_text"]
    if cid == "description":
        return {"description": text}
    if cid.startswith("label_definitions."):
        return {"label_definitions": {cid.split(".", 1)[1]: text}}
    raise ValueError(f"component not editable: {cid}")


# ---------------------------------------------------------------------------------------
# PR
# ---------------------------------------------------------------------------------------
def patched_config_text(task, model, model_config, edit):
    src = TASK_CONFIG[task].read_text()
    out, i, lines = [], 0, src.splitlines()
    while i < len(lines):
        line = lines[i]
        if line.strip() == "[components.llm.model]":
            out.append(line)
            out.append('@llm_models = "spacy.GPT-4.v3"')
            out.append(f'name = "{model}"')
            out.append(f"config = {json.dumps(model_config if model_config is not None else {'temperature': 0.0})}")
            i += 1
            while i < len(lines) and not lines[i].startswith("["):
                i += 1
            if i < len(lines):
                out.append("")
            continue
        if edit and edit["component"] == "description" and line.startswith("description ="):
            words = edit["new_text"].split()
            wrapped, cur = [], "description ="
            for w in words:
                if len(cur) + len(w) + 1 > 60:
                    wrapped.append(cur)
                    cur = "   "
                cur += " " + w
            wrapped.append(cur)
            out.extend(wrapped)
            i += 1
            while i < len(lines) and lines[i].startswith((" ", "\t")):
                i += 1
            continue
        if edit and edit["component"].startswith("label_definitions."):
            label = edit["component"].split(".", 1)[1]
            if re.match(rf"^{re.escape(label)}\s*=", line):
                out.append(f"{label} = {json.dumps(edit['new_text'])}")
                i += 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out).rstrip() + "\n"


def fmt_pct(x):
    return f"{x:.0%}"


def pr_body(task, chosen, base_rep, cand_rep, final_rep, causes, accepted, candidates, model_config, args):
    b, c = base_rep["baseline"], cand_rep["candidate"]
    f = final_rep["candidate"] if final_rep else None
    temp_note = ""
    if model_config is not None and "temperature" not in model_config:
        temp_note = (f"\n- `spacy.GPT-4.v3` sends `temperature: 0.0` by default, which `{chosen}` rejects "
                     f"(\"Only the default (1) value is supported\"). Changing only the model name makes every request fail, "
                     f"so the config sets `config = {json.dumps(model_config)}`.")
    rows = [("Passed all hard checks", fmt_pct(b["passed_all"]), fmt_pct(c["passed_all"]), f and fmt_pct(f["passed_all"])),
            (b["accuracy_metric"], f"{b['accuracy']:.2f}", f"{c['accuracy']:.2f}", f and f"{f['accuracy']:.2f}"),
            ("Inputs regressed vs baseline", "-", str(cand_rep["verdict_counts"]["REGRESSED"]),
             final_rep and str(final_rep["verdict_counts"]["REGRESSED"])),
            ("Inputs whose output varies between runs", str(b["unstable_inputs"]), str(c["unstable_inputs"]),
             f and str(f["unstable_inputs"])),
            ("Latency p50", f"{b['latency_p50_s']:.2f}s", f"{c['latency_p50_s']:.2f}s", f and f"{f['latency_p50_s']:.2f}s")]
    if b["cost_per_1k_calls_usd"] is not None and c["cost_per_1k_calls_usd"] is not None:
        rows.append(("Cost per 1k calls", f"${b['cost_per_1k_calls_usd']:.2f}", f"${c['cost_per_1k_calls_usd']:.2f}",
                     f and f["cost_per_1k_calls_usd"] is not None and f"${f['cost_per_1k_calls_usd']:.2f}"))
    head = "| | Baseline (`" + b["resolved_model"][0] + "`) | `" + chosen + "`, model swap only |"
    sep = "|---|---|---|"
    if final_rep:
        head += " `" + chosen + "` + prompt fix |"
        sep += "---|"
    table = [head, sep] + ["| " + " | ".join(str(x) for x in (r if final_rep else r[:3])) + " |" for r in rows]

    out = [f"# Move the `{UPSTREAM_PATH[task].split('/')[1]}` example to `{chosen}` before the Oct 23, 2026 model shutdowns", "",
           "## Why", "",
           "This example uses `spacy.GPT-3-5.v1` (`gpt-3.5-turbo`, which resolves to `gpt-3.5-turbo-0125`), and the "
           "obvious upgrade, `spacy.GPT-4.v3`, defaults to `gpt-4` (`gpt-4-0613`). OpenAI shuts down both on "
           "**Oct 23, 2026** ([deprecations](https://developers.openai.com/api/docs/deprecations)). spacy-llm checks "
           "`/v1/models` when a pipeline loads and raises `ValueError` for a model that isn't listed, so after that date "
           "this example stops loading rather than degrading.", "",
           "## What changes", "",
           f"- Model: `spacy.GPT-4.v3` with `name = \"{chosen}\"`.{temp_note}"]
    if accepted:
        cid = accepted["component"]
        out.append(f"- Prompt: `{cid}` reworded (diff below). {accepted['rationale']}")
    out += ["", "## Evidence", "",
            f"Baseline: this example's prompt on `spacy.GPT-4.v3` (`gpt-4`), the stronger of the two retiring models. "
            f"{base_rep['inputs_compared']} inputs, each run {args.runs} times per model. An input counts as regressed only "
            "if most new-model runs fail a hard check the old model passes, or score below the old model's worst run.", ""]
    out += table
    if len(candidates) > 1:
        out += ["", "Other candidates tested:", ""]
        for cd in candidates:
            if cd["model"] == chosen:
                continue
            if cd.get("unavailable"):
                out.append(f"- `{cd['model']}`: not available ({cd['unavailable']})")
            else:
                out.append(f"- `{cd['model']}`: {cd['regressed']} regressed, {cd['accuracy_metric']} {cd['accuracy']:.2f}")
    if causes:
        out += ["", "## Regressions found, and their cause", ""]
        for cs in causes:
            out += [f"**\"{cs['text']}\"**", "",
                    f"- `gpt-4`: {cs['baseline_output']}", f"- `{chosen}`: {cs['candidate_output']}",
                    f"- Evidence: {'; '.join(cs['evidence'])}"]
            for u in cs.get("confirmed_lines", []):
                out.append(f"- Proven line (`{u['component']}`): \"{u['text']}\". Removing only this sentence "
                           "makes the input stop regressing.")
            if cs["status"] == "confirmed" and "fix" in (cs.get("confirmed_by") or "fix"):
                out.append(f"- Cause (confirmed by fix): `{cs['confirmed_component']}`. Editing only that component "
                           "removes the regression, with no new regressions elsewhere.")
            else:
                top = [s["component"] for s in cs["suspects"]][:2]
                out.append(f"- Suspected cause: {', '.join(f'`{t}`' for t in top)} (not confirmed by a fix)")
            out.append("")
    if accepted:
        out += ["### Prompt change", "", "```diff", f"- {accepted['old_text']}", f"+ {accepted['new_text']}", "```", ""]
    rest = [it for it in (final_rep or cand_rep)["items"] if it["verdict"] in ("CHANGED", "IMPROVED")]
    if rest:
        out += ["## Other differences (not regressions; worth a look)", ""]
        for it in rest:
            out.append(f"- {it['verdict'].lower()}: \"{it['text']}\" — `gpt-4` {it['baseline']['output']} → "
                       f"`{chosen}` {it['candidate']['output']}")
        out.append("")
    out += ["## Limits", "",
            f"- {base_rep['inputs_compared']} hand-labelled inputs; accuracy figures depend on those labels.",
            f"- `{chosen}` only accepts the default temperature, so its outputs can vary between runs; the "
            "regression rule above requires a majority of runs to fail.", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", choices=["ner", "textcat"], default="ner")
    ap.add_argument("--variant", default="gpt-4", help="baseline variant the candidates replace")
    ap.add_argument("--baseline", help="baseline results file (default results/<task>__<variant>__gpt-4.jsonl)")
    ap.add_argument("--candidates", nargs="+", required=True)
    ap.add_argument("--model-config", type=json.loads, default=None,
                    help="sampling config for candidates, e.g. '{}' for models that reject temperature=0")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--fixer-model", help="model that proposes prompt edits (default: the chosen candidate)")
    ap.add_argument("--max-fix-attempts", type=int, default=3)
    ap.add_argument("--results", default=str(HERE / "results"))
    ap.add_argument("--out", default=None, help="default reports/migration_<task>")
    ap.add_argument("--api-base", help="send OpenAI calls here instead (testing/proxy)")
    ap.add_argument("--no-ablate", action="store_true", help="skip line-level ablation (heuristic causes only)")
    args = ap.parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY first.")
    out = Path(args.out or HERE / "reports" / f"migration_{args.task}")
    out.mkdir(parents=True, exist_ok=True)
    base_path = Path(args.baseline or results_path(args.results, args.task, args.variant, "gpt-4"))
    if not n_ok_rows(base_path):
        sys.exit(f"No baseline results at {base_path}. Run record_baseline.py first.")
    components = editable_components(args.task)
    started = datetime.now(timezone.utc).isoformat()

    # 1-2. record + compare candidates ------------------------------------------------
    print(f"[1/5] Candidates on {args.task} ({len(args.candidates)})")
    candidates, reports = [], {}
    for model in args.candidates:
        path = record(args, model)
        if not n_ok_rows(path):
            candidates.append({"model": model, "unavailable": "no successful calls; see record_baseline output"})
            continue
        rep = build_report(base_path, path)
        reports[model] = rep
        (out / f"compare_{model}.json").write_text(json.dumps(rep, indent=2, default=list))
        c = rep["candidate"]
        candidates.append({"model": model, "resolved_model": c["resolved_model"], "regressed": rep["verdict_counts"]["REGRESSED"],
                           "verdict_counts": rep["verdict_counts"], "accuracy": c["accuracy"],
                           "accuracy_metric": c["accuracy_metric"], "passed_all": c["passed_all"],
                           "unstable_inputs": c["unstable_inputs"], "latency_p50_s": c["latency_p50_s"],
                           "cost_per_1k_calls_usd": c["cost_per_1k_calls_usd"], "report": f"compare_{model}.json"})
    if not reports:
        sys.exit("No candidate produced results.")
    ranked = sorted(reports, key=lambda m: rank_key(reports[m]))
    for i, m in enumerate(ranked, 1):
        next(cd for cd in candidates if cd["model"] == m)["rank"] = i
    chosen = ranked[0]
    cand_rep = reports[chosen]
    print(f"[2/5] Ranked: " + ", ".join(f"{m} ({reports[m]['verdict_counts']['REGRESSED']} regressed, "
                                        f"acc {reports[m]['candidate']['accuracy']:.2f})" for m in ranked))
    print(f"      chosen: {chosen}")

    # 3. cause ------------------------------------------------------------------------------
    causes = find_causes(cand_rep, components)
    print(f"[3/5] Cause: {len(causes)} regressed input(s)")
    for cs in causes:
        print(f"      {cs['input_id']}: {'; '.join(cs['evidence'])}")
        print(f"        suspects: {[s['component'] for s in cs['suspects']]}")
    if causes and not args.no_ablate:
        from ablation import ablate
        print(f"      ablation: removing one sentence at a time, {len(causes)} input(s) x {args.runs} runs on {chosen}")
        ablate(args.task, base_path, components, causes, variant=args.variant, name=chosen,
               model_config=args.model_config, runs=args.runs, results_dir=Path(args.results),
               patch_dir=out / "ablation_patches", api_base=args.api_base)
        for cs in causes:
            print(f"      {cs['input_id']}: {cs['status']}"
                  + "".join(f"\n        line: {u['component']}: \"{u['text']}\"" for u in cs["confirmed_lines"]))

    # 4. fix ------------------------------------------------------------------------------
    attempts, accepted, final_rep = [], None, None
    fixable = causes and any(s["editable"] for cs in causes for s in cs["suspects"])
    if not causes:
        print("[4/5] Fix: nothing to fix (no regressions)")
    elif not fixable:
        print("[4/5] Fix: regressions trace to built-in template instructions; no config-editable component")
    else:
        fixer = args.fixer_model or chosen
        history = []
        # A prior run's migration.json carries the full edit (component, new_text, rationale,
        # old_text) it got from the fixer for each attempt. Reuse it instead of calling the
        # fixer again, so a replay with all results cached makes zero API calls.
        prev_attempts_by_n = {}
        prev_path = out / "migration.json"
        if prev_path.exists():
            try:
                prev_summary = json.loads(prev_path.read_text())
            except json.JSONDecodeError:
                prev_summary = {}
            if prev_summary.get("task") == args.task and prev_summary.get("chosen") == chosen:
                prev_attempts_by_n = {a["attempt"]: a["edit"] for a in prev_summary.get("fix_attempts", [])
                                      if a.get("edit")}
        for n in range(1, args.max_fix_attempts + 1):
            pfile = out / f"fix{n}_patch.json"
            cached_edit = prev_attempts_by_n.get(n)
            if cached_edit and pfile.exists():
                print(f"[4/5] Fix attempt {n}: reusing edit cached in migration.json (no fixer call)")
                edit = cached_edit
            else:
                print(f"[4/5] Fix attempt {n} (fixer: {fixer})")
                try:
                    edit = parse_json(call_llm(fixer, FIXER_SYSTEM,
                                               fixer_prompt(args.task, components, causes, cand_rep, history), args.api_base))
                except (ValueError, RuntimeError, KeyError) as e:
                    attempts.append({"attempt": n, "error": str(e)})
                    print(f"      fixer error: {e}")
                    continue
                edit["old_text"] = components.get(edit["component"])
            try:
                patch = patch_from_edit(edit)
            except ValueError as e:
                attempts.append({"attempt": n, "edit": edit, "error": str(e)})
                print(f"      {e}")
                continue
            pfile.write_text(json.dumps(patch, indent=2))
            print(f"      edit {edit['component']}: {json.dumps(edit['new_text'])[:110]}")
            path = record(args, chosen, pfile, f"fix{n}")
            if not n_ok_rows(path):
                attempts.append({"attempt": n, "edit": edit, "error": "re-run produced no results"})
                continue
            rep = build_report(base_path, path)
            (out / f"compare_{chosen}_fix{n}.json").write_text(json.dumps(rep, indent=2, default=list))
            target_ids = {cs["input_id"] for cs in causes}
            still = [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED" and it["input_id"] in target_ids]
            new = [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED" and it["input_id"] not in target_ids]
            ok = rep["verdict_counts"]["REGRESSED"] == 0
            outcome = ("accepted: 0 regressions" if ok else
                       f"still regressed {still}" + (f"; newly regressed {new}" if new else ""))
            print(f"      result: {rep['verdict_counts']}  -> {outcome}")
            attempts.append({"attempt": n, "edit": edit, "patch_file": pfile.name, "report": f"compare_{chosen}_fix{n}.json",
                             "verdict_counts": rep["verdict_counts"], "still_regressed": still, "newly_regressed": new,
                             "accepted": ok, "outcome": outcome})
            if ok:
                accepted, final_rep = edit, rep
                for cs in causes:
                    if cs["input_id"] not in still:
                        cs["confirmed_by"] = "ablation+fix" if cs.get("confirmed_by") == "ablation" else "fix"
                        cs["status"] = "confirmed"
                        cs["confirmed_component"] = cs["confirmed_component"] or edit["component"]
                break
            history.append({"component": edit["component"], "new_text": edit["new_text"], "outcome": outcome})

    # 5. PR ---------------------------------------------------------------------------------
    ready = bool(accepted) or not causes
    pr = None
    if ready:
        cfg_text = patched_config_text(args.task, chosen, args.model_config, accepted)
        (out / Path(UPSTREAM_PATH[args.task]).name).write_text(cfg_text)
        diff = "".join(difflib.unified_diff(TASK_CONFIG[args.task].read_text().splitlines(True), cfg_text.splitlines(True),
                                            fromfile="a/" + UPSTREAM_PATH[args.task], tofile="b/" + UPSTREAM_PATH[args.task]))
        (out / "pr.diff").write_text(diff)
        body = pr_body(args.task, chosen, cand_rep, cand_rep, final_rep, causes, accepted, candidates, args.model_config, args)
        (out / "PR.md").write_text(body)
        pr = {"title": body.splitlines()[0].lstrip("# "), "body_file": "PR.md", "diff_file": "pr.diff",
              "config_file": Path(UPSTREAM_PATH[args.task]).name, "target_repo": "explosion/spacy-llm",
              "target_path": UPSTREAM_PATH[args.task], "diff": diff}
        print(f"[5/5] PR written: {out / 'PR.md'}, {out / 'pr.diff'}")
    else:
        print("[5/5] PR not written: regressions remain unfixed (see migration.json)")

    summary = {
        "task": args.task, "started": started, "finished": datetime.now(timezone.utc).isoformat(),
        "baseline": {"file": base_path.name, **cand_rep["baseline"]},
        "candidates": sorted(candidates, key=lambda c: c.get("rank", 99)),
        "chosen": chosen, "model_config": args.model_config,
        "editable_components": components,
        "causes": causes, "fix_attempts": attempts,
        "final": {"ready_to_merge": ready, "model": chosen, "prompt_edit": accepted,
                  "report": (f"compare_{chosen}_fix{attempts[-1]['attempt']}.json" if accepted else f"compare_{chosen}.json"),
                  "verdict_counts": (final_rep or cand_rep)["verdict_counts"],
                  "candidate": (final_rep or cand_rep)["candidate"]},
        "pr": pr,
        "comparison": final_rep or cand_rep,
    }
    (out / "migration.json").write_text(json.dumps(summary, indent=2, default=list))
    print(f"\nReady to merge: {'YES' if ready else 'NO'}   ->  {out / 'migration.json'}")


if __name__ == "__main__":
    main()
