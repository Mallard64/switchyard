"""A bigger model pool for the support agent: screen every model, pick the cheapest model per step, prove the mix.

  python model_pool.py            # every run cached; uncached runs call OpenAI and the local Ollama server

Pool: three open-weight models served locally by Ollama (no list price: tokens are shown instead of dollars),
four cheap OpenAI models, gpt-5.6-terra, and gpt-5.6-sol (the validated config: sol + the tone patch).

1. Screen (model-only): each model on the clean agent vs gpt-4 on the dev (24), hard (12) and fresh held-out
   (12, holdout2) tickets, 3 runs, compare.py's noise-floor rule. Sampling matches gpt-4 (temperature 0) where
   the model accepts it; gpt-5.x models reject 0 and run at their default.
2. Ladder per step: background = the validated config (sol + patch) everywhere, candidate at step k only (steps
   before k replayed from sol + patch's own dev run). Self-hosted models first, then API models by measured cost
   per call at that step. Pass = 0 regressions vs gpt-4 on 24 dev tickets x 3 runs. Nobody passes -> sol stays.
3. Compose the mix and run it end to end on dev. If dev regresses, sol is put back at one swapped step at a time
   on the regressed tickets; the step whose swap repairs the most moves up one rung. Repeat (dev only, MAX_ROUNDS).
4. Once dev is clean: run the mix ONCE on holdout2, which no selection step has seen. Accepted only if 0
   regressions and gold >= gpt-4's minus the noise floor. sol + patch also runs once on holdout2 (pre-registered).
Writes reports/model_pool/result.json.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import llm  # noqa: E402
import support_agent as A  # noqa: E402
from assign import new_prompts, total, usage_per_step  # noqa: E402
from compare import build_report, load  # noqa: E402
from record_baseline import PRICES  # noqa: E402
from stepfinder import label_of, plan_of, run_config, spec  # noqa: E402

RESULTS = HERE / "results"
OUT = HERE / "reports" / "model_pool"
OLD, BASE = "gpt-4", "gpt-5.6-sol"
T0 = {"temperature": 0.0}
# (model, sampling params, hosting). Order inside a hosting group doesn't matter: the ladder sorts API models by cost.
POOL = [
    ("ollama/llama3.2:3b", T0, "self-hosted"), ("ollama/qwen2.5:7b", T0, "self-hosted"), ("ollama/llama3.1:8b", T0, "self-hosted"),
    ("gpt-5-nano", {}, "api"), ("gpt-4.1-nano", T0, "api"), ("gpt-5.6-luna", {}, "api"), ("gpt-5.4-mini", {}, "api"),
    ("gpt-5.6-terra", {}, "api"),
]
SETS = {"dev": "", "hard": "__hard", "holdout2": "__holdout2"}
RUNS, WORKERS, MAX_ROUNDS = 3, 6, 3


def by_run(rows):
    return {tid: {r["run"]: r for r in rs} for tid, rs in rows.items()}


def f(model, s):
    return RESULTS / f"agent__{A.safe(model)}__noplant{SETS[s]}.jsonl"


def summarize(old_path, path):
    rep = build_report(old_path, path)
    return {"verdict_counts": rep["verdict_counts"], "regressed": [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED"],
            "passed_all": rep["candidate"]["passed_all"], "gold": rep["candidate"]["accuracy"], "tickets": rep["inputs_compared"],
            "errors": sum(1 for l in open(path) if json.loads(l).get("error"))}


def screen():
    out = []
    for model, params, hosting in [(BASE, {}, "api")] + POOL:
        row = {"model": model, "hosting": hosting, "params": params, "sets": {}}
        for s in SETS:
            if f(model, s).exists() and f(OLD, s).exists():
                row["sets"][s] = summarize(f(OLD, s), f(model, s))
        per = usage_per_step([f(model, s) for s in SETS if f(model, s).exists()])
        row["per_step"] = per
        row["usd_per_1k"] = total(per)
        row["tokens_per_ticket"] = round(sum(v["prompt_tokens"] + v["completion_tokens"] for v in per.values()), 1)
        row["price"] = PRICES.get(model)
        out.append(row)
    return out


def rung_order(step, screening):
    """Self-hosted first (no API bill; tokens shown), then API models by measured $ per 1k calls at this step."""
    hosted = [m for m, _, h in POOL if h == "self-hosted"]
    cost = {r["model"]: (r["per_step"].get(step) or {}).get("usd_per_1k") for r in screening}
    api = sorted([m for m, _, h in POOL if h == "api"], key=lambda m: (cost.get(m) is None, cost.get(m) or 0))
    return hosted + api


def guilty_step(choice, regressed, mix_path, specs, base):
    """For a mix that regressed on dev: put sol (+ patch) back at one swapped step at a time on the regressed
    tickets; the step whose swap repairs the most tickets is guilty. Only steps not already on sol are tried."""
    tickets = [t for t in A.load_tickets(sets=("agent",)) if t["id"] in regressed]
    mix_rows = by_run(load(mix_path))
    best, best_n = None, 0
    swapped = [s for s, m in choice.items() if m != BASE]
    for step in swapped:
        ov = {s: specs[choice[s]] for s in swapped if s != step}
        label = Path(mix_path).stem.removeprefix("agent__") + f"__{step}-sol"
        path = run_config(tickets, label, plan_of(base, ov), mix_rows, A.STEPS.index(step), RUNS, RESULTS, WORKERS)
        rep = build_report(f(OLD, "dev"), path)
        n = sum(1 for it in rep["items"] if it["input_id"] in regressed and it["verdict"] != "REGRESSED")
        print(f"    sol back at {step}: repairs {n}/{len(regressed)}")
        if n > best_n:
            best, best_n = step, n
    return best


def main():
    prompts, patch, tag = new_prompts()
    params = {m: p for m, p, _ in POOL}
    specs = {m: spec(m, params[m], prompts, f"{A.safe(m)}-npp") for m, _, _ in POOL}
    base = spec(BASE, {}, prompts, f"{BASE}-npp")
    base_dev = RESULTS / f"agent__{BASE}-np__patch-{tag}.jsonl"
    old_dev, old_ho2 = f(OLD, "dev"), f(OLD, "holdout2")
    dev = A.load_tickets(sets=("agent",))
    src = by_run(load(base_dev))

    # 1. screen
    screening = screen()
    for r in screening:
        cost = f"${r['usd_per_1k']}/1k" if r["usd_per_1k"] is not None else f"{r['tokens_per_ticket']} tok/ticket (self-hosted)"
        print(f"  {r['model']:<20} " + "  ".join(f"{s}: {v['verdict_counts']['REGRESSED']} worse" for s, v in r["sets"].items())
              + f"   {cost}")

    # 2. ladder
    ladder, choice = {}, {}
    for k, step in enumerate(A.STEPS):
        order = rung_order(step, screening)
        tried = []
        for m in order:
            ov = {step: specs[m]}
            path = run_config(dev, label_of(base, ov), plan_of(base, ov), src, k, RUNS, RESULTS, WORKERS)
            s = summarize(old_dev, path)
            ok = s["verdict_counts"]["REGRESSED"] == 0 and s["tickets"] == len(dev) and s["errors"] == 0
            tried.append({"model": m, "file": path.name, **s, "passed": ok})
            print(f"  {step}: {m} -> {s['verdict_counts']['REGRESSED']} worse"
                  f"{', errors ' + str(s['errors']) if s['errors'] else ''} {'PASS' if ok else 'fail'}")
            if ok:
                break
        ladder[step] = {"order": order, "tried": tried}
        choice[step] = next((t["model"] for t in tried if t["passed"]), BASE)
    print("ladder choice:", choice)

    # 3. compose + dev end to end, upgrading the guilty step on failure
    rounds, final = [], None
    for rnd in range(1, MAX_ROUNDS + 1):
        ov = {s: specs[m] for s, m in choice.items() if m != BASE}
        label = "pool-mix-" + "-".join(f"{s[:2]}{A.safe(m).split('-')[-1]}" for s, m in choice.items())
        path = run_config(dev, label, plan_of(base, ov), src, 0, RUNS, RESULTS, WORKERS)
        s = summarize(old_dev, path)
        rounds.append({"round": rnd, "assignment": dict(choice), "file": path.name, **s})
        print(f"  mix round {rnd}: {choice} -> dev {s['verdict_counts']}")
        if s["verdict_counts"]["REGRESSED"] == 0 and s["errors"] == 0:
            final = (dict(choice), ov, label)
            break
        guilty = guilty_step(choice, s["regressed"], path, specs, base)
        rounds[-1]["guilty_step"] = guilty
        if not guilty:
            break
        order, cur = ladder[guilty]["order"], choice[guilty]
        nxt = order[order.index(cur) + 1:] if cur in order else []
        choice[guilty] = nxt[0] if nxt else BASE
    out = {"pool": [{"model": m, "params": p, "hosting": h} for m, p, h in POOL], "base": BASE, "patch_tag": tag,
           "screening": screening, "ladder": ladder, "rounds": rounds,
           "synthetic": {"tickets": True, "regression": False}}

    # 4. held-out, once
    ho2 = A.load_tickets(sets=("agent_holdout2",))
    ho2_src = by_run(load(f(BASE, "holdout2")))
    path = run_config(ho2, f"{BASE}-np__patch-{tag}__holdout2", plan_of(base, {}), ho2_src, 0, RUNS, RESULTS, WORKERS)
    out["sol_patch_holdout2"] = summarize(old_ho2, path) | {"file": path.name}
    print(f"  sol + patch on holdout2: {out['sol_patch_holdout2']['verdict_counts']}")
    if final:
        assignment, ov, label = final
        path = run_config(ho2, label + "__holdout2", plan_of(base, ov), ho2_src, 0, RUNS, RESULTS, WORKERS)
        rep = build_report(old_ho2, path)
        noise = rep["baseline"]["unstable_inputs"] / max(1, rep["inputs_compared"])
        ho = summarize(old_ho2, path) | {"file": path.name, "gold_old": rep["baseline"]["accuracy"], "noise_floor": noise}
        accepted = ho["verdict_counts"]["REGRESSED"] == 0 and ho["gold"] >= ho["gold_old"] - noise and ho["errors"] == 0
        per = usage_per_step([RESULTS / rounds[-1]["file"], path])
        out["mix"] = {"assignment": assignment, "holdout2": ho, "accepted": accepted, "per_step": per,
                      "api_usd_per_1k": round(sum(v["usd_per_1k"] or 0 for v in per.values()), 4),
                      "self_hosted_steps": [s for s, v in per.items() if v["usd_per_1k"] is None],
                      "self_hosted_tokens_per_ticket": round(sum(v["prompt_tokens"] + v["completion_tokens"]
                                                                 for v in per.values() if v["usd_per_1k"] is None), 1)}
        print(f"  mix on holdout2 (never seen): {ho['verdict_counts']} -> accepted={accepted}; "
              f"API ${out['mix']['api_usd_per_1k']}/1k + self-hosted steps {out['mix']['self_hosted_steps']}")
    else:
        out["mix"] = None
        print("  no mix cleared dev; holdout2 not used for a mix")
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"-> {OUT / 'result.json'}   spent ${llm.spent_usd():.2f}")


if __name__ == "__main__":
    main()
