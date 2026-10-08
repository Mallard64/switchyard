"""Every agent model call goes through call_llm(): LiteLLM transport plus a per-call hash cache.

The cache key is sha256 of (model, params, messages, run). The run number is part of the key on
purpose: models are sampled several times per ticket to measure run-to-run noise, so identical
messages in run 1 and run 2 must stay separate samples. support_agent.record_row sets the run.

  results/llm_calls.jsonl   one row per paid call: {"key", "step", "model", "run", "cost_usd", "ts", "response"}

Spend is the sum of cost_usd in that file; past SPEND_CAP_USD every uncached call raises instead.
"""
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# LiteLLM fetches its model-price map over the network at import unless told not to; replays stay offline.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

CACHE = Path(__file__).parent / "results" / "llm_calls.jsonl"
# Paid calls stop once the calls logged here cost this much (USD). Override with LLM_SPEND_CAP_USD.
SPEND_CAP_USD = float(os.environ.get("LLM_SPEND_CAP_USD", "15"))
_lock = threading.Lock()
_cache = None
_spent = 0.0
_local = threading.local()


def set_run(run):
    """Called once per (ticket, run) before its steps run, on that worker thread."""
    _local.run = run


def call_key(model, params, messages, run):
    blob = json.dumps([model, params or {}, messages, run], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _load():
    global _cache, _spent
    if _cache is None:
        _cache = {}
        if CACHE.exists():
            for line in open(CACHE):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn last line from an interrupted run
                _cache[row["key"]] = row["response"]
                _spent += row.get("cost_usd") or 0.0
    return _cache


def spent_usd():
    with _lock:
        _load()
        return round(_spent, 4)


def price(model, usage):
    from record_baseline import PRICES
    p = PRICES.get(model)
    if not p or not usage:
        return None
    return (usage.get("prompt_tokens") or 0) / 1e6 * p[0] + (usage.get("completion_tokens") or 0) / 1e6 * p[1]


OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://localhost:11434")


def _completion(model, params, messages, api_base):
    import litellm
    litellm.telemetry = False
    if model.startswith("ollama/"):
        # Open-weight models served locally by Ollama: no key, no per-token price (config/prices.yml has null).
        return litellm.completion(model="ollama_chat/" + model.split("/", 1)[1], messages=messages,
                                  api_base=OLLAMA_BASE, num_retries=1, timeout=300, **(params or {}))
    # "openai/" pins the provider: LiteLLM's model map doesn't know gpt-5.6-* names yet.
    return litellm.completion(model=f"openai/{model}", messages=messages, api_base=api_base,
                              api_key=os.environ.get("OPENAI_API_KEY"), num_retries=3, timeout=120, **(params or {}))


def call_llm(step, model, messages, params=None, api_base=None, completion=_completion):
    """One chat call. Returns {"raw", "usage", "resolved_model", "system_fingerprint", "finish_reason",
    "latency_s", "cached"}. A cache hit costs nothing and makes no network call."""
    global _spent
    run = getattr(_local, "run", None)
    key = call_key(model, params, messages, run)
    with _lock:
        hit = _load().get(key)
        over = _spent >= SPEND_CAP_USD
    if hit is not None:
        return {**hit, "cached": True}
    if over:
        raise RuntimeError(f"spend cap reached: ${_spent:.2f} logged in {CACHE.name} >= ${SPEND_CAP_USD:.2f}")
    t0 = time.perf_counter()
    resp = completion(model, params, messages, api_base)
    choice = resp.choices[0]
    usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage or {})
    out = {"raw": choice.message.content or "",
           "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
           "resolved_model": getattr(resp, "model", None),
           "system_fingerprint": getattr(resp, "system_fingerprint", None),
           "finish_reason": choice.finish_reason, "latency_s": round(time.perf_counter() - t0, 3)}
    usd = price(model, out["usage"])
    row = {"key": key, "step": step, "model": model, "run": run, "cost_usd": usd,
           "ts": datetime.now(timezone.utc).isoformat(), "response": out}
    with _lock:
        _load()[key] = out
        _spent += usd or 0.0
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE, "a") as f:
            f.write(json.dumps(row) + "\n")
    return {**out, "cached": False}
