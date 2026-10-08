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
import hashlib
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
import pr_report  # noqa: E402

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
# Parts of speech the NER description explicitly rules out ("Adjectives, verbs, adverbs are not
# entities. Pronouns are not entities."). A spurious span with one of these points at that rule in
# the description, not at a label definition.
NON_ENTITY_POS = {"VERB", "AUX", "ADV", "ADJ", "PRON"}
_POS_NLP = []


def span_pos(text, span):
    """Coarse POS of a span's head word, or None if no tagger is installed (en_core_web_sm)."""
    if not _POS_NLP:
        try:
            import spacy
            _POS_NLP.append(spacy.load("en_core_web_sm", disable=["ner", "lemmatizer"]))
        except (ImportError, OSError):
            _POS_NLP.append(None)
    nlp, i = _POS_NLP[0], text.lower().find(span.lower())
    if nlp is None or i < 0:
        return None
    if i == 0:  # sentence-initial imperatives ("Sear the steak") get mis-tagged as proper nouns
        text = text[0].lower() + text[1:]
    sp = nlp(text).char_span(i, i + len(span), alignment_mode="expand")
    return sp.root.pos_ if sp is not None else None


def check_component(task, check, instruction):
    """Where a hard check's instruction lives: the editable component of the *upstream* prompt that
    contains it (e.g. "Pronouns are not entities." is in the description), else the fixed template.
    Upstream, because a prompt edit may have removed the sentence from the current prompt."""
    norm = lambda t: " ".join((t or "").split()).lower().rstrip(".")
    if instruction and task in TASK_CONFIG:
        for comp, text in editable_components(task).items():
            if norm(instruction) and norm(instruction) in norm(text):
                return comp
    return f"template:{check}"


def find_causes(report, components):
    causes = []
    for it in report["items"]:
        if it["verdict"] != "REGRESSED":
            continue
        suspects = Counter()
        evidence = []
        for r in it["regressions"]:
            if r["kind"] == "hard_check":
                suspects[check_component(report["task"], r["check"], r["instruction"])] += 5
                evidence.append(f"hard check '{r['check']}' fails {r['candidate_fail']} runs "
                                f"(baseline {r['baseline_fail']}); instruction: \"{r['instruction']}\"")
        for e in it["candidate"]["errors"]:
            if e["type"] in ("spurious", "wrong_label"):
                label = e["pred"][1] if isinstance(e["pred"], (list, tuple)) else e["pred"]
                pos = span_pos(it["text"], e["pred"][0]) if e["type"] == "spurious" else None
                if pos in NON_ENTITY_POS:  # e.g. a verb tagged as INGREDIENT: the description's rule
                    suspects["description"] += 3
                    suspects[f"label_definitions.{label}"] += 1
                else:
                    suspects[f"label_definitions.{label}"] += 2
                    suspects["description"] += 1
                evidence.append(f"{e['type']}: predicted {tuple(e['pred'])}" + (f" (a {pos})" if pos else "")
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
def patched_config_text(task, model, model_config, edits):
    """Upstream config with the new model block and every accepted prompt edit applied."""
    by_comp = {e["component"]: e for e in edits or []}
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
        if "description" in by_comp and line.startswith("description ="):
            words = by_comp["description"]["new_text"].split()
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
        m = re.match(r"^(\w+)\s*=", line)
        if m and f"label_definitions.{m.group(1)}" in by_comp:
            out.append(f"{m.group(1)} = {json.dumps(by_comp['label_definitions.' + m.group(1)]['new_text'])}")
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out).rstrip() + "\n"


def run_judge(args, cand_rep, base_path, out, chosen):
    """Judge CHANGED items on the chosen candidate (Anthropic; skipped without credentials). In place."""
    import judge
    n = sum(it["verdict"] == "CHANGED" for it in cand_rep["items"])
    if args.no_judge or not n:
        return {"skipped": "--no-judge" if args.no_judge else "no CHANGED items"}
    if not judge.has_credentials():
        print(f"      judge: skipped ({n} CHANGED items; no Anthropic credentials)")
        return {"skipped": "no Anthropic credentials", "changed_items": n}
    j = judge.Judge(args.judge_model or judge.DEFAULT_MODEL, cache_dir=args.results)
    judge.judge_report(cand_rep, j, *judge.load_rows(cand_rep, args.results))
    (out / f"compare_{chosen}.json").write_text(json.dumps(cand_rep, indent=2, default=list))
    s = cand_rep["judge_summary"]
    print(f"      judge ({s['model']}): {s['verdicts']}; noise floor {s['noise_floor']['verdicts']}; {j.calls} new calls")
    return {k: v for k, v in s.items() if k != "noise_floor"} | {"noise_floor": {
        k: v for k, v in s["noise_floor"].items() if k != "items"}}


EDIT_FORMAT = {"fix": "fix id this edit replaces (optional)",
               "component": "description | label_definitions.<LABEL>", "new_text": "full new text of that component"}


def verification(rep, target_ids, runs, report_file):
    reg = [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED"]
    return {"inputs": rep["inputs_compared"], "runs": runs, "verdict_counts": rep["verdict_counts"],
            "still_regressed": [i for i in reg if i in target_ids],
            "newly_regressed": [i for i in reg if i not in target_ids],
            "passes": rep["verdict_counts"]["REGRESSED"] == 0, "report_file": report_file}


def migration_view(task, chosen, cand_rep, shown_rep, causes, fixes, candidates, args, noise):
    """The shared migration dict pr_report.render() turns into PR.md (mirrored in migration.json)."""
    b, c = cand_rep["baseline"], cand_rep["candidate"]
    f = shown_rep["candidate"] if shown_rep else None
    fx = lambda fn: fn(f) if f else "–"
    cost = lambda s: f"${s['cost_per_1k_calls_usd']:.2f}" if s and s["cost_per_1k_calls_usd"] is not None else "n/a"
    table = [("Passed all hard checks", f"{b['passed_all']:.0%}", f"{c['passed_all']:.0%}", fx(lambda s: f"{s['passed_all']:.0%}")),
             (b["accuracy_metric"], f"{b['accuracy']:.2f}", f"{c['accuracy']:.2f}", fx(lambda s: f"{s['accuracy']:.2f}")),
             ("Inputs regressed vs baseline", "–", cand_rep["verdict_counts"]["REGRESSED"],
              shown_rep["verdict_counts"]["REGRESSED"] if shown_rep else "–"),
             ("Inputs whose output varies between runs", b["unstable_inputs"], c["unstable_inputs"],
              fx(lambda s: s["unstable_inputs"])),
             ("Latency p50", f"{b['latency_p50_s']:.2f}s", f"{c['latency_p50_s']:.2f}s", fx(lambda s: f"{s['latency_p50_s']:.2f}s")),
             ("Cost per 1k calls", cost(b), cost(c), fx(cost))]
    for cs in causes:
        item = next((x for x in shown_rep["items"] if x["input_id"] == cs["input_id"]), None) if shown_rep else None
        cs["fixed_output"] = item["candidate"]["output"] if item else None
        cs["lines_tested"] = len(cs.get("lines", []))
    judged = {"old_better": "judge: old output better (both orders)", "new_better": "judge: new output better (both orders)",
              "tie": "judge: equivalent", "inconsistent": "judge: no consistent preference"}
    other = [f"{it['verdict'].lower()}: \"{it['text']}\": `gpt-4` {it['baseline']['output']} → `{chosen}` "
             f"{it['candidate']['output']}" + (f" ({judged[it['judge']['verdict']]})" if it.get("judge") else "")
             for it in cand_rep["items"] if it["verdict"] in ("CHANGED", "IMPROVED")]
    js = cand_rep.get("judge_summary") or {}
    if js.get("noise_floor"):
        nf = js["noise_floor"]
        other.append(f"Judge `{js['model']}` (Anthropic, a different provider from the candidates) compared each CHANGED "
                     f"output with the old one in both orders. On {nf['pairs']} pairs of the old model's own outputs it "
                     f"preferred one side {nf['false_preference_rate']:.0%} of the time (how often it disagrees with itself).")
    other += [f"Other candidate `{cd['model']}`: {cd['regressed']} regressed, {cd['accuracy_metric']} {cd['accuracy']:.2f}"
              for cd in candidates if cd.get("model") != chosen and "regressed" in cd]
    cfg = json.dumps(args.model_config if args.model_config is not None else {"temperature": 0.0})
    cli = (f"python migrate.py --task {task} --candidates {' '.join(cd['model'] for cd in candidates)}"
           + (f" --model-config '{json.dumps(args.model_config)}'" if args.model_config is not None else ""))
    return {
        "pipeline": f"spacy-llm `{UPSTREAM_PATH[task].split('/')[1]}` example, one LLM call per text",
        "old_model": "gpt-4", "new_model": chosen, "n_inputs": cand_rep["inputs_compared"], "cli": cli,
        "title": f"Move the `{UPSTREAM_PATH[task].split('/')[1]}` example to `{chosen}` before the Oct 23, 2026 model shutdowns",
        "why": ["This example uses `spacy.GPT-3-5.v1` (`gpt-3.5-turbo`), and the obvious upgrade, `spacy.GPT-4.v3`, "
                "defaults to `gpt-4` (`gpt-4-0613`). OpenAI shuts down both on **Oct 23, 2026** "
                "([deprecations](https://developers.openai.com/api/docs/deprecations)). spacy-llm checks `/v1/models` "
                "when a pipeline loads and raises `ValueError` for a missing model, so the example stops loading."],
        "model_change": [f"Model: `spacy.GPT-4.v3` with `name = \"{chosen}\"` and `config = {cfg}`."]
                        + ([f"`spacy.GPT-4.v3` sends `temperature: 0.0` by default, which `{chosen}` rejects; changing "
                            "only the model name makes every request fail, hence the empty config."]
                           if args.model_config is not None and "temperature" not in args.model_config else []),
        "steps": None, "causes": causes, "fixes": fixes, "edit_format": EDIT_FORMAT, "other_differences": other,
        "evidence": {"table": table, "noise_floor": noise},
        "limits": [f"{cand_rep['inputs_compared']} hand-labelled inputs; accuracy figures depend on those labels.",
                   f"`{chosen}` only accepts the default temperature, so its outputs vary between runs; an input "
                   "counts as regressed only if most runs fail.",
                   "Line-level proof only finds lines that are present and over-applied; a missing instruction is "
                   "proven at component level by the fix instead."],
    }


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
    ap.add_argument("--no-judge", action="store_true", help="skip the LLM judge for CHANGED items")
    ap.add_argument("--judge-model", default=None, help="Claude model for the judge (default: judge.DEFAULT_MODEL)")
    ap.add_argument("--accept", action="append", default=[], help="engineer accepts this fix id (no re-run)")
    ap.add_argument("--reject", action="append", default=[], help="engineer rejects this fix id (no re-run)")
    ap.add_argument("--apply-edit", help="JSON {fix?, component, new_text}: re-run the full suite with the "
                                         "engineer's text, re-verify, regenerate the PR")
    args = ap.parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY first.")
    out = Path(args.out or HERE / "reports" / f"migration_{args.task}")
    out.mkdir(parents=True, exist_ok=True)
    base_main = Path(args.baseline or results_path(args.results, args.task, args.variant, "gpt-4"))
    if not n_ok_rows(base_main):
        sys.exit(f"No baseline results at {base_main}. Run record_baseline.py first.")
    # Inputs added after the protected baseline was recorded live in a separate __ext file.
    base_ext = results_path(args.results, args.task, args.variant, "gpt-4", "ext")
    base_path = [base_main, base_ext] if not args.baseline and n_ok_rows(base_ext) else base_main
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
    judge_summary = run_judge(args, cand_rep, base_path, out, chosen)

    # 3. cause ------------------------------------------------------------------------------
    causes = find_causes(cand_rep, components)
    print(f"[3/5] Cause: {len(causes)} regressed input(s)")
    for cs in causes:
        print(f"      {cs['input_id']}: {'; '.join(cs['evidence'])}")
        print(f"        suspects: {[s['component'] for s in cs['suspects']]}")
    if causes and not args.no_ablate:
        from ablation import ablate
        print(f"      line test: removing one sentence at a time, {len(causes)} input(s) x {args.runs} runs on {chosen}")
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

    # 5. review: fixes, engineer decisions and edits -----------------------------------------
    target_ids = {cs["input_id"] for cs in causes}
    prev_path = out / "migration.json"
    prev = json.loads(prev_path.read_text()) if prev_path.exists() else {}
    prev_fixes = ({f["id"]: f for f in prev.get("fixes", [])}
                  if prev.get("task") == args.task and prev.get("chosen") == chosen else {})
    fixes = {}
    for a in attempts:
        if not a.get("accepted"):
            continue  # failed attempts stay in fix_attempts; only verified ones are proposed
        fid = f"fix{a['attempt']}"
        p = prev_fixes.get(fid, {})
        if p.get("source", "").startswith("engineer"):
            fixes[fid] = p
            continue
        e = a["edit"]
        fixes[fid] = {"id": fid, "step": "llm", "component": e["component"], "kind": "edit", "edit": e,
                      "old_text": e.get("old_text"), "new_text": e["new_text"], "rationale": e.get("rationale"),
                      "summary": f"reword `{e['component']}`",
                      "source": f"fixer LLM (`{args.fixer_model or chosen}`), attempt {a['attempt']}",
                      "verification": verification(json.loads((out / a["report"]).read_text()), target_ids,
                                                   args.runs, a["report"]),
                      "decision": p.get("decision", "pending"), "decided_at": p.get("decided_at")}
    for fid, p in prev_fixes.items():
        if fid not in fixes and p.get("source", "").startswith("engineer"):
            fixes[fid] = p
    if args.apply_edit:
        e = json.loads(Path(args.apply_edit).read_text())
        if e["component"] not in components:
            sys.exit(f"not an editable component: {e['component']}; have {sorted(components)}")
        e["old_text"] = components[e["component"]]
        tag = "edit-" + hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest()[:8]
        pfile = out / f"{tag}_patch.json"
        pfile.write_text(json.dumps(patch_from_edit(e), indent=2))
        path = record(args, chosen, pfile, tag)
        if not n_ok_rows(path):
            sys.exit("the edit's re-run produced no results")
        rep = build_report(base_path, path)
        (out / f"compare_{chosen}_{tag}.json").write_text(json.dumps(rep, indent=2, default=list))
        v = verification(rep, target_ids, args.runs, f"compare_{chosen}_{tag}.json")
        fid = e.get("fix") or f"edit{sum(k.startswith('edit') for k in fixes) + 1}"
        fixes[fid] = {"id": fid, "step": "llm", "component": e["component"], "kind": "edit", "edit": e,
                      "old_text": e["old_text"], "new_text": e["new_text"], "summary": f"engineer edit of `{e['component']}`",
                      "source": "engineer edit" + (f" replacing proposed {fid}" if fid in fixes else ""),
                      "original": fixes.get(fid), "edit_file": Path(args.apply_edit).name, "verification": v,
                      "decision": "edited" if v["passes"] else "pending", "decided_at": started if v["passes"] else None}
        print(f"[5/6] {fid}: engineer edit -> {v['verdict_counts']} -> "
              f"{'edited (accepted)' if v['passes'] else 'still regressing; left pending'}")
    for fid in args.accept + args.reject:
        if fid not in fixes:
            sys.exit(f"no fix {fid}; have {sorted(fixes)}")
    for fid, decision in [(f, "accepted") for f in args.accept] + [(f, "rejected") for f in args.reject]:
        fixes[fid].update(decision=decision, decided_at=started)
    fix_list = sorted(fixes.values(), key=lambda f: f["id"])
    live = pr_report.live_fixes({"fixes": fix_list})
    if len({f["component"] for f in live}) < len(live):
        sys.exit("two accepted fixes edit the same component; reject one")
    shown = live[0] if live else next((f for f in fix_list if f["decision"] == "pending"
                                       and f["verification"]["passes"]), None)
    shown_rep = json.loads((out / shown["verification"]["report_file"]).read_text()) if shown else None
    for f in fix_list:
        print(f"[5/6] {f['id']} ({f['component']}): verified {f['verification']['passes']}, decision {f['decision']}")

    # 6. PR -----------------------------------------------------------------------------------
    rerun = results_path(args.results, args.task, args.variant, "gpt-4", "rerun")
    noise = (f"`gpt-4` changed its output between its own runs on {cand_rep['baseline']['unstable_inputs']}/"
             f"{cand_rep['inputs_compared']} inputs. An input counts as regressed only if most new-model runs fail a "
             f"hard check the old model passes, or score below the old model's worst run.")
    if n_ok_rows(rerun):
        fa = build_report(base_path, rerun)["verdict_counts"]["REGRESSED"]
        noise += f" A fresh `gpt-4` re-run judged by the same rule flags {fa} regressions (false alarms)."
    ready = not causes or (bool(live) and all(f["verification"]["passes"] for f in live))
    edits = [f["edit"] for f in live]
    cfg_text = patched_config_text(args.task, chosen, args.model_config, edits)
    (out / Path(UPSTREAM_PATH[args.task]).name).write_text(cfg_text)
    diff = "".join(difflib.unified_diff(TASK_CONFIG[args.task].read_text().splitlines(True), cfg_text.splitlines(True),
                                        fromfile="a/" + UPSTREAM_PATH[args.task], tofile="b/" + UPSTREAM_PATH[args.task]))
    (out / "pr.diff").write_text(diff)
    view = migration_view(args.task, chosen, cand_rep, shown_rep, causes, fix_list, candidates, args, noise)
    (out / "PR.md").write_text(pr_report.render(view))
    pr = {"title": view["title"], "body_file": "PR.md", "diff_file": "pr.diff",
          "config_file": Path(UPSTREAM_PATH[args.task]).name, "target_repo": "explosion/spacy-llm",
          "target_path": UPSTREAM_PATH[args.task], "diff": diff}
    print(f"[6/6] PR written: {out / 'PR.md'} (model swap + {len(live)} accepted prompt fix(es))")

    summary = {
        "task": args.task, "started": started, "finished": datetime.now(timezone.utc).isoformat(),
        "baseline": {"file": cand_rep["baseline_file"], **cand_rep["baseline"]},
        "candidates": sorted(candidates, key=lambda c: c.get("rank", 99)),
        "chosen": chosen, "model_config": args.model_config,
        "editable_components": components,
        "causes": causes, "fix_attempts": attempts, "fixes": fix_list, "edit_format": EDIT_FORMAT,
        "judge": judge_summary,
        "pr_view": {k: view[k] for k in ("title", "why", "model_change", "evidence", "other_differences", "limits")},
        "final": {"ready_to_merge": ready, "model": chosen, "prompt_edits": edits,
                  "prompt_edit": edits[0] if edits else None,
                  "report": shown["verification"]["report_file"] if live else f"compare_{chosen}.json",
                  "verdict_counts": (shown_rep if live else cand_rep)["verdict_counts"],
                  "candidate": (shown_rep if live else cand_rep)["candidate"]},
        "pr": pr,
        "comparison": shown_rep if live else cand_rep,
    }
    (out / "migration.json").write_text(json.dumps(summary, indent=2, default=list))
    print(f"\nReady to merge: {'YES' if ready else 'NO (needs an accepted fix)' if causes else 'NO'}   ->  "
          f"{out / 'migration.json'}")

if __name__ == "__main__":
    main()
