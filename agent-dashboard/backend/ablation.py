"""Line-level cause finding for spacy-llm prompts, by ablation.

Splits each editable prompt component into sentences ("units"). For the inputs that regressed,
removes one unit at a time (a single-sentence label definition is blanked), re-runs only those
inputs on the model that regressed (3 runs), and judges each input with compare.py's
noise-floor rule against the baseline. A unit is `confirmed` when removing it makes the input
stop regressing. The heuristic suspect ranking from migrate.find_causes only orders the units.

Control: the unablated prompt is re-run the same way first. If that alone stops an input from
regressing, the regression is unstable (noise or prompt-fragile), so no unit can be confirmed
for it: its status is "unstable".
"""
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from compare import compare_input, load, load_gold  # noqa: E402

# Unit boundaries: after a sentence end (not "e.g."/"i.e."), and before an "e.g." example list.
CUT = re.compile(r"(?<![ei]\.[gei]\.)(?<=[.!?])\s+|(?=\be\.g\.)")


def _spans(text):
    cuts = sorted({0, len(text), *(m.end() for m in CUT.finditer(text))})
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if text[a:b].strip(" ,\n")]


def split_units(components):
    """[{unit_id, component, index, text, start, end}] — one per sentence or example list."""
    units = []
    for comp, text in components.items():
        spans = _spans(text)
        for i, (a, b) in enumerate(spans):
            units.append({"unit_id": f"{comp}#{i}", "component": comp, "index": i,
                          "text": " ".join(text[a:b].split()).rstrip(","), "start": a, "end": b,
                          "only_sentence": len(spans) == 1})
    return units


def without(components, unit):
    """Component text with this unit removed (blank if it was the only unit)."""
    if unit["only_sentence"]:
        return ""
    text = components[unit["component"]]
    rest = text[:unit["start"]] + text[unit["end"]:]
    lines = [" ".join(l.split()) for l in rest.splitlines()]
    return "\n".join(l for l in lines if l).strip(" ,")


def patch_with(base_patch, component, new_text):
    patch = json.loads(json.dumps(base_patch or {}))
    if component == "description":
        patch["description"] = new_text
    else:
        patch.setdefault("label_definitions", {})[component.split(".", 1)[1]] = new_text
    return patch


def record(task, variant, name, model_config, patch, tag, ids, runs, results_dir, patch_dir, api_base=None):
    pfile = Path(patch_dir) / f"{tag}.json"
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(json.dumps(patch, indent=2))
    cmd = [sys.executable, str(HERE / "record_baseline.py"), "--task", task, "--variant", variant,
           "--patch", str(pfile), "--tag", tag, "--runs", str(runs), "--only", ",".join(ids),
           "--outdir", str(results_dir)]
    if name:
        cmd += ["--name", name]
    if model_config is not None:
        cmd += ["--model-config", json.dumps(model_config)]
    if api_base:
        cmd += ["--api-base", api_base]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    errs = [l for l in proc.stdout.splitlines() if "ERROR" in l or "Could not build" in l]
    if errs:
        print("     " + "\n     ".join(errs[:3]))
    label = name or ("gpt-4" if variant == "gpt-4" else "gpt-3.5-turbo")
    return Path(results_dir) / f"{task}__{variant}__{label}__{tag}.jsonl"


def _unit(u):
    return {"unit_id": u["unit_id"], "component": u["component"], "text": u["text"]}


def ablate(task, base_path, components, causes, *, variant, name=None, model_config=None, base_patch=None,
           tag_prefix="", runs=3, results_dir=HERE / "results", patch_dir, api_base=None, max_units=None):
    """Adds `lines`, `confirmed_lines` and (when a unit is confirmed) `status`, `confirmed_by`,
    `confirmed_component` to each cause, in place. Returns causes."""
    if not causes:
        return causes
    base, gold = load(base_path), load_gold(task)
    ids = [c["input_id"] for c in causes]
    # Heuristic order: units of the most-suspected components first (ordering only).
    weight = {}
    for c in causes:
        for s in c["suspects"]:
            weight[s["component"]] = weight.get(s["component"], 0) + s["score"]
    units = sorted(split_units(components), key=lambda u: (-weight.get(u["component"], 0), u["unit_id"]))
    units = units[:max_units] if max_units else units
    path = record(task, variant, name, model_config, base_patch or {}, f"{tag_prefix}abl-control", ids, runs,
                  results_dir, patch_dir, api_base)
    rows = load(path) if path.exists() else {}
    control = {}
    for i in ids:
        it = compare_input(task, base[i], rows[i], gold[i]) if rows.get(i) else None
        control[i] = {"verdict": it and it["verdict"], "score_runs": it and it["candidate"]["score_runs"],
                      "file": path.name}
    print(f"      {'control (no ablation)':<32} " + "  ".join(f"{i}:{control[i]['verdict']}" for i in ids))
    per_input = {i: [] for i in ids}
    for u in units:
        tag = f"{tag_prefix}abl-{u['unit_id'].replace('label_definitions.', '').replace('#', '-')}"
        path = record(task, variant, name, model_config, patch_with(base_patch, u["component"], without(components, u)),
                      tag, ids, runs, results_dir, patch_dir, api_base)
        rows = load(path) if path.exists() else {}
        for i in ids:
            if not rows.get(i):
                per_input[i].append({**_unit(u), "status": "error", "verdict": None, "repaired_runs": "0/0"})
                continue
            it = compare_input(task, base[i], rows[i], gold[i])
            worst = min(it["baseline"]["score_runs"])
            fixed = sum(s >= worst for s in it["candidate"]["score_runs"])
            per_input[i].append({**_unit(u), "verdict": it["verdict"], "output": it["candidate"]["output"],
                                 "repaired_runs": f"{fixed}/{len(it['candidate']['score_runs'])}",
                                 "status": "confirmed" if it["verdict"] != "REGRESSED" else "no_effect",
                                 "file": path.name})
        print(f"      {u['unit_id']:<32} " + "  ".join(f"{i}:{per_input[i][-1]['status']}" for i in ids))
    for c in causes:
        lines = sorted(per_input[c["input_id"]], key=lambda r: (r["status"] != "confirmed",
                                                                -int(r["repaired_runs"].split("/")[0])))
        stable = control[c["input_id"]]["verdict"] == "REGRESSED"
        c["ablation_control"] = {**control[c["input_id"]], "stable": stable}
        if not stable:
            for r in lines:
                r["status"] = "unstable" if r["status"] == "confirmed" else r["status"]
        conf = [r for r in lines if r["status"] == "confirmed"]
        c["lines"] = lines
        c["confirmed_lines"] = [_unit_from(r) for r in conf]
        if conf:
            c.update(status="confirmed", confirmed_by="ablation", confirmed_component=conf[0]["component"])
    return causes


def _unit_from(r):
    return {"unit_id": r["unit_id"], "component": r["component"], "text": r["text"]}
