"""Record the baseline: run spacy-llm's own NER and text-classification examples on the
models they use today, and save every prompt, raw response and parsed result.

Runs the real library (spacy-llm, pinned below) with the upstream example configs, so the
baseline is exactly what a user of those examples gets. A thin wrapper around `requests`
captures the resolved model ID, token usage and latency, which spacy-llm itself discards.

  pip install "spacy-llm==0.7.4"
  export OPENAI_API_KEY=sk-...
  python record_baseline.py --dry-run          # render one prompt per task, no API calls
  python record_baseline.py                    # full baseline (both tasks, both variants)
  python record_baseline.py --task ner --variant gpt-4 --limit 2 --runs 1   # smoke test

Variants (both retire Oct 23, 2026):
  shipped  the example configs as published: spacy.GPT-3-5.v1 / .v2 -> "gpt-3.5-turbo"
  gpt-4    spacy.GPT-4.v3, whose default model name is "gpt-4"
Later, the same harness runs a candidate: --variant gpt-4 --name gpt-5.6-sol
"""
import argparse
import copy
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
UPSTREAM = {"repo": "explosion/spacy-llm", "commit": "a399273acda231c65fcb2b2cc2ad0f580efb9318",
            "version": "0.7.4"}
TASKS = {
    "ner": {"config": HERE / "upstream" / "ner_fewshot.cfg",
            "examples": HERE / "upstream" / "ner_examples.json",
            "inputs": HERE / "inputs" / "ner.jsonl"},
    "textcat": {"config": HERE / "upstream" / "textcat_zeroshot.cfg",
                "examples": None,
                "inputs": HERE / "inputs" / "textcat.jsonl"},
}
VARIANTS = ["shipped", "gpt-4"]
# USD per 1M tokens (input, output). Verify on OpenAI's pricing page before quoting numbers.
PRICES = {"gpt-4": (30.0, 60.0), "gpt-4-0613": (30.0, 60.0),
          "gpt-3.5-turbo": (0.5, 1.5), "gpt-3.5-turbo-0125": (0.5, 1.5),
          "gpt-5.6-sol": (5.0, 30.0), "gpt-5.6-terra": (2.5, 15.0)}  # rates from the user, Oct 6
OPENAI_BASE = "https://api.openai.com/v1"
PROTECTED = {f"{t}__{v}.jsonl" for t in ("ner", "textcat") for v in ("gpt-4__gpt-4", "shipped__gpt-3.5-turbo")}

# ---------------------------------------------------------------------------------------
# Capture layer: wrap requests.get/post (spacy-llm looks these up at call time).
# ---------------------------------------------------------------------------------------
_local = threading.local()
_api_base = None  # set by --api-base (testing / proxies)


def _install_capture():
    import requests
    real_get, real_post = requests.get, requests.post

    def rewrite(url):
        if _api_base and url.startswith(OPENAI_BASE):
            return _api_base.rstrip("/") + url[len(OPENAI_BASE):]
        return url

    def get(url, *a, **kw):
        return real_get(rewrite(url), *a, **kw)

    def post(url, *a, **kw):
        t0 = time.perf_counter()
        resp = real_post(rewrite(url), *a, **kw)
        rec = {"status": resp.status_code, "latency_s": round(time.perf_counter() - t0, 3),
               "request": kw.get("json")}
        try:
            body = resp.json()
            rec.update({"resolved_model": body.get("model"), "usage": body.get("usage"),
                        "system_fingerprint": body.get("system_fingerprint"),
                        "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason"),
                        "response_text": ((body.get("choices") or [{}])[0].get("message") or {}).get("content"),
                        "error": body.get("error")})
        except ValueError:
            rec["error"] = resp.text[:300]
        calls = getattr(_local, "calls", None)
        if calls is not None:
            calls.append(rec)
        return resp

    requests.get, requests.post = get, post


# ---------------------------------------------------------------------------------------
# Pipeline construction
# ---------------------------------------------------------------------------------------
def model_block(task, variant, name=None, model_config=None):
    """The [components.llm.model] block for a task/variant (None = keep upstream as-is)."""
    if variant == "shipped" and not name and model_config is None:
        return None
    # Keep the variant's sampling config so candidate runs are like-for-like,
    # unless --model-config overrides it (e.g. '{}' for models that reject temperature=0).
    shipped_cfg = {"ner": {}, "textcat": {"temperature": 0.0}}[task]  # v1 sends none; v2 sends 0.0
    cfg = shipped_cfg if variant == "shipped" else {"temperature": 0.0}
    if model_config is not None:
        cfg = model_config
    block = {"@llm_models": "spacy.GPT-4.v3", "config": cfg}
    if name:
        block["name"] = name
    return block


def apply_patch(config, patch):
    """Apply a prompt patch: {"description": str, "label_definitions": {LABEL: str}}."""
    task_cfg = config["components"]["llm"]["task"]
    if patch.get("description") is not None:
        task_cfg["description"] = patch["description"]
    for label, text in (patch.get("label_definitions") or {}).items():
        task_cfg.setdefault("label_definitions", {})[label] = text


def build_nlp(task, variant, name=None, model_config=None, patch=None):
    from spacy.util import load_config
    from spacy_llm.util import assemble_from_config
    spec = TASKS[task]
    overrides = {}
    if spec["examples"]:
        overrides["paths.examples"] = str(spec["examples"])
    config = load_config(spec["config"], overrides=overrides, interpolate=False)
    block = model_block(task, variant, name, model_config)
    if block is not None:
        config["components"]["llm"]["model"] = block
    if patch:
        apply_patch(config, patch)
    config = config.interpolate()
    return assemble_from_config(config), copy.deepcopy(dict(config["components"]["llm"]["model"]))


def model_label(task, variant, name):
    if name:
        return name
    return "gpt-4" if variant == "gpt-4" else "gpt-3.5-turbo"


# ---------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------
def load_jsonl(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def load_done(path):
    done = set()
    if path.exists():
        for line in open(path):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not r.get("error"):
                done.add((r["input_id"], r["run"]))
    return done


def cost(model, usage):
    p = PRICES.get(model)
    if not p or not usage:
        return None
    return round(usage.get("prompt_tokens", 0) / 1e6 * p[0] + usage.get("completion_tokens", 0) / 1e6 * p[1], 6)


def run_one(nlp, task, inp, run, meta):
    from checks import check_ner, check_textcat, score_ner, score_textcat
    _local.calls = []
    row = {"task": task, "input_id": inp["id"], "run": run, **meta,
           "ts": datetime.now(timezone.utc).isoformat(), "text": inp["text"], "tricky": inp.get("tricky", False)}
    try:
        doc = nlp(inp["text"])
        ok = [c for c in _local.calls if c["status"] == 200 and not c.get("error")]
        if not ok:
            raise RuntimeError(f"no successful API call: {_local.calls[-1:] or 'none made'}")
        last = ok[-1]
        req = last["request"] or {}
        prompt = "\n".join(m.get("content", "") for m in req.get("messages", []))
        raw = last.get("response_text") or ""
        usage = last.get("usage") or {}
        row.update({
            "resolved_model": last.get("resolved_model"), "system_fingerprint": last.get("system_fingerprint"),
            "finish_reason": last.get("finish_reason"), "request_params": {k: v for k, v in req.items() if k != "messages"},
            "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:12], "prompt": prompt,
            "raw_output": raw, "usage": usage, "latency_s": last["latency_s"],
            "api_attempts": len(_local.calls), "cost_usd": cost(meta["model"], usage), "error": None,
        })
        if task == "ner":
            ents = [{"text": e.text, "label": e.label_, "start": e.start_char, "end": e.end_char} for e in doc.ents]
            row["output"] = ents
            row["checks"] = check_ner(inp["text"], raw, ents)
            row["gold_score"] = score_ner(ents, inp["gold"])
        else:
            row["output"] = dict(doc.cats)
            row["checks"] = check_textcat(raw, doc.cats)
            row["gold_score"] = score_textcat(doc.cats, inp["gold"])
        row["passed_all"] = all(row["checks"].values())
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"[:1000]
        row["api_calls"] = _local.calls
    finally:
        _local.calls = None
    return row


def summarize(path, task):
    rows = {}
    for line in open(path):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("error"):
            rows[(r["input_id"], r["run"])] = r
    rows = list(rows.values())
    if not rows:
        print(f"  {path.name}: no successful rows yet")
        return
    n = len(rows)
    models = sorted({r.get("resolved_model") or "?" for r in rows})
    print(f"\n{path.name}: {n} outputs, resolved model {', '.join(models)}")
    print(f"  passed all hard checks: {sum(r['passed_all'] for r in rows)}/{n}")
    for name in rows[0]["checks"]:
        print(f"    {name:<14} {sum(r['checks'][name] for r in rows)}/{n}")
    if task == "ner":
        tp = sum(r["gold_score"]["tp"] for r in rows)
        npred = sum(r["gold_score"]["n_pred"] for r in rows)
        ngold = sum(r["gold_score"]["n_gold"] for r in rows)
        p, rc = tp / npred if npred else 0, tp / ngold if ngold else 0
        print(f"  vs gold: micro P {p:.2f}  R {rc:.2f}  F1 {2 * p * rc / (p + rc) if p + rc else 0:.2f}")
    else:
        print(f"  vs gold: accuracy {sum(r['gold_score']['correct'] for r in rows)}/{n}")
    # Run-to-run instability of the old model = the noise floor for later comparisons.
    by_input = {}
    for r in rows:
        key = json.dumps(r["output"] if task == "textcat" else sorted((e["text"], e["label"]) for e in r["output"]),
                         sort_keys=True)
        by_input.setdefault(r["input_id"], set()).add(key)
    unstable = sum(len(v) > 1 for v in by_input.values())
    print(f"  inputs whose output changed between runs: {unstable}/{len(by_input)}")
    lat = sorted(r["latency_s"] for r in rows)
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    print(f"  latency p50 {lat[n // 2]:.2f}s  p95 {lat[min(n - 1, int(n * .95))]:.2f}s"
          + (f"   cost ${sum(costs):.3f}" if costs else ""))


def main():
    global _api_base
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", choices=["ner", "textcat", "all"], default="all")
    ap.add_argument("--variant", choices=VARIANTS + ["all"], default="all")
    ap.add_argument("--name", help="override the model name (candidate runs), e.g. gpt-5.6-sol")
    ap.add_argument("--model-config", type=json.loads, default=None,
                    help="JSON sampling config sent to the model, replacing the default; "
                         "use '{}' for models that reject temperature=0")
    ap.add_argument("--patch", help="JSON file with a prompt patch (description / label_definitions)")
    ap.add_argument("--tag", help="suffix for the results file, e.g. fix1 (keeps patched runs separate)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="comma-separated input IDs to run (e.g. failing inputs during ablation)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--outdir", default=str(HERE / "results"))
    ap.add_argument("--api-base", help="send OpenAI calls here instead (testing/proxy)")
    ap.add_argument("--dry-run", action="store_true", help="render prompts with a stub model; no API calls")
    args = ap.parse_args()
    _api_base = args.api_base
    patch = json.load(open(args.patch)) if args.patch else None

    try:
        import spacy_llm  # noqa: F401
    except ImportError:
        sys.exit('Install the library first:  pip install "spacy-llm==0.7.4"')
    if args.dry_run:
        return dry_run(args)
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY first.")
    _install_capture()

    tasks = list(TASKS) if args.task == "all" else [args.task]
    variants = VARIANTS if args.variant == "all" else [args.variant]
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for task in tasks:
        inputs = load_jsonl(TASKS[task]["inputs"])[: args.limit]
        if args.only:
            inputs = [i for i in inputs if i["id"] in args.only.split(",")]
        for variant in variants:
            label = model_label(task, variant, args.name)
            out = outdir / f"{task}__{variant}__{label}{'__' + args.tag if args.tag else ''}.jsonl"
            done = load_done(out)
            todo = [(i, r) for i in inputs for r in range(1, args.runs + 1) if (i["id"], r) not in done]
            if out.name in PROTECTED and done and todo:
                # These baselines can't be regenerated after Oct 23, 2026: never append to them.
                print(f"\n== {out.name} is a protected baseline; refusing to add {len(todo)} rows. "
                      f"Record new inputs with --only <ids> --tag <name> instead.")
                continue
            print(f"\n== {task} / {variant} ({label}): {len(done)} done, {len(todo)} to run -> {out.name}")
            if not todo:
                summarize(out, task)
                continue
            # One pipeline per worker thread; building one also checks the model exists.
            local = threading.local()
            block_holder = {}

            def get_nlp():
                if not hasattr(local, "nlp"):
                    local.nlp, block_holder["block"] = build_nlp(task, variant, args.name, args.model_config, patch)
                return local.nlp

            try:
                get_nlp()
            except Exception as e:
                print(f"   Could not build the pipeline: {type(e).__name__}: {e}")
                continue
            meta = {"variant": variant, "model": label, "model_block": block_holder["block"],
                    "patch": patch, "tag": args.tag,
                    "upstream": UPSTREAM, "config": TASKS[task]["config"].name}
            lock, errors = threading.Lock(), 0

            def work(inp, run):
                row = run_one(get_nlp(), task, inp, run, meta)
                with lock, open(out, "a") as f:
                    f.write(json.dumps(row) + "\n")
                return row

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futs = [pool.submit(work, i, r) for i, r in todo]
                for k, fut in enumerate(as_completed(futs), 1):
                    row = fut.result()
                    if row["error"]:
                        errors += 1
                        print(f"   [{k}/{len(todo)}] {row['input_id']} r{row['run']} ERROR {row['error'][:150]}")
                    else:
                        shown = ([f"{e['text']}/{e['label'][:4]}" for e in row["output"]] if task == "ner"
                                 else row["gold_score"]["pred"])
                        print(f"   [{k}/{len(todo)}] {'ok  ' if row['passed_all'] else 'FAIL'} "
                              f"{row['input_id']} r{row['run']}  {str(shown)[:90]}")
            summarize(out, task)
            if errors:
                print(f"   {errors} errored; re-run the same command to retry only those.")


def dry_run(args):
    """Build each pipeline against a stub API and print the exact prompt and request params."""
    import requests

    def Stub(body):  # spacy-llm asserts it gets a real requests.Response
        r = requests.models.Response()
        r.status_code, r._content, r.encoding = 200, json.dumps(body).encode(), "utf-8"
        r.headers["Content-Type"] = "application/json"
        return r

    os.environ.setdefault("OPENAI_API_KEY", "dry-run")
    seen = {}
    requests.get = lambda url, *a, **kw: Stub({"data": [{"id": m} for m in
                                                        ["gpt-4", "gpt-3.5-turbo", args.name or "gpt-4"]]})

    def post(url, *a, **kw):
        seen["req"] = kw.get("json")
        return Stub({"model": "stub", "choices": [{"message": {"content": "COMPLIMENT"}, "finish_reason": "stop"}]})
    requests.post = post
    tasks = list(TASKS) if args.task == "all" else [args.task]
    variants = VARIANTS if args.variant == "all" else [args.variant]
    for task in tasks:
        inp = load_jsonl(TASKS[task]["inputs"])[0]
        for variant in variants:
            nlp, block = build_nlp(task, variant, args.name, args.model_config, json.load(open(args.patch)) if args.patch else None)
            nlp(inp["text"])
            req = seen["req"]
            print(f"\n===== {task} / {variant}  model block: {block}")
            print(f"request params: { {k: v for k, v in req.items() if k != 'messages'} }")
            print("prompt:\n" + "\n".join(m["content"] for m in req["messages"]))
    n = sum(len(load_jsonl(TASKS[t]["inputs"])[: args.limit]) for t in tasks)
    print(f"\nFull run: {n} inputs x {len(variants)} variants x {args.runs} runs = {n * len(variants) * args.runs} calls")


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
