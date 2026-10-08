"""Per-step model assignment: for each agent step, the cheapest model that doesn't regress it, then the mix end to end.

  python assign.py                 # ladder gpt-5.6-terra -> gpt-5.6-sol -> gpt-4, every run cached

1. Per step k, cheapest candidate first: old model (gpt-4) everywhere except step k, which runs the candidate
   (steps before k replayed from gpt-4's own run). The step passes if the 24 dev tickets x 3 runs show 0
   regressions vs all-gpt-4 (compare.py's noise-floor rule). Fail -> next model up; gpt-4 itself always passes.
2. The chosen mix runs end to end, live, on the 24 dev tickets and on the 12 held-out hard tickets (never used to
   pick models). Accepted if held-out shows 0 regressions and its gold score >= gpt-4's minus the noise floor.
3. Per-step tokens and cost from real usage (non-replayed calls only), priced with config/prices.yml, for
   all-old (gpt-4), all-new (gpt-5.6-sol + tone patch) and the mix.
All runs use the clean agent (no planted line). New-model steps carry the tone patch from model_only.py, the
prompt that would ship; gpt-4 steps keep the original prompts. Writes reports/assignment/result.json.
"""
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import llm  # noqa: E402
import support_agent as A  # noqa: E402
from compare import build_report, load  # noqa: E402
from record_baseline import PRICES  # noqa: E402
from stepfinder import label_of, plan_of, run_config, spec  # noqa: E402

RESULTS = HERE / "results"
OUT = HERE / "reports" / "assignment"
LADDER = ["gpt-5.6-terra", "gpt-5.6-sol"]  # cheapest first (config/prices.yml); gpt-4 is the fallback
OLD = "gpt-4"
RUNS, WORKERS = 3, 6


def by_run(rows):
    return {tid: {r["run"]: r for r in rs} for tid, rs in rows.items()}


def new_prompts():
    patch = json.loads((HERE / "reports" / "model_only" / "result_gpt-5.6-sol.json").read_text())["patch"]
    lines = list(A.PROMPTS[patch["step"]])
    for e in patch["edits"]:
        lines[e["line_index"]] = e["new"]
    tag = hashlib.sha256(json.dumps(lines).encode()).hexdigest()[:8]  # model_only.py names its patch runs this way
    return {**A.unplanted_prompts(), patch["step"]: lines}, patch, tag


def usage_per_step(paths):
    """Mean prompt/completion tokens per ticket run, per step and model, over live (non-replayed) calls."""
    acc = {}
    for p in paths:
        for line in open(p):
            r = json.loads(line)
            if r.get("error"):
                continue
            for s in r["steps"]:
                if s.get("replayed") or not s.get("usage"):
                    continue
                a = acc.setdefault(s["step"], {"model": s["model"], "in": [], "out": []})
                a["in"].append(s["usage"].get("prompt_tokens") or 0)
                a["out"].append(s["usage"].get("completion_tokens") or 0)
    out = {}
    for step in A.STEPS:
        a = acc.get(step)
        if not a:
            continue
        tin, tout = statistics.mean(a["in"]), statistics.mean(a["out"])
        p = PRICES.get(a["model"])
        out[step] = {"model": a["model"], "calls": len(a["in"]), "prompt_tokens": round(tin, 1),
                     "completion_tokens": round(tout, 1),
                     "usd_per_1k": round((tin * p[0] + tout * p[1]) / 1e6 * 1000, 4) if p else None}
    return out


def total(per_step):
    vals = [v["usd_per_1k"] for v in per_step.values()]
    return None if not vals or any(v is None for v in vals) else round(sum(vals), 4)


def main():
    prompts, patch, tag = new_prompts()
    clean = A.unplanted_prompts()
    old = spec(OLD, {"temperature": 0.0}, clean, f"{OLD}-np")
    cands = {m: spec(m, {}, prompts, f"{m}-npp") for m in LADDER}
    base_dev = RESULTS / f"agent__{OLD}__noplant.jsonl"
    base_ho = RESULTS / f"agent__{OLD}__noplant__hard.jsonl"
    dev_tickets = A.load_tickets(sets=("agent",))
    src = by_run(load(base_dev))

    # 1. per-step ladder
    per_step, chosen = {}, {}
    for k, step in enumerate(A.STEPS):
        tried = []
        for m in LADDER:
            ov = {step: cands[m]}
            path = run_config(dev_tickets, label_of(old, ov), plan_of(old, ov), src, k, RUNS, RESULTS, WORKERS)
            rep = build_report(base_dev, path)
            ok = rep["verdict_counts"]["REGRESSED"] == 0 and rep["inputs_compared"] == len(dev_tickets)
            tried.append({"model": m, "file": path.name, "verdict_counts": rep["verdict_counts"], "passed": ok,
                          "regressed": [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED"]})
            print(f"  {step}: {m} -> {rep['verdict_counts']} {'PASS' if ok else 'fail'}")
            if ok:
                break
        pick = next((t["model"] for t in tried if t["passed"]), OLD)
        chosen[step] = pick
        per_step[step] = {"tried": tried, "chosen": pick}
    print("assignment:", chosen)

    # 2. the mix end to end
    mix_spec = {s: cands[m] if m != OLD else old for s, m in chosen.items()}
    default = mix_spec[A.STEPS[0]]
    overrides = {s: v for s, v in mix_spec.items() if v is not default}
    label = "mix-" + "-".join(f"{s[:2]}{m.split('-')[-1] if m != OLD else '4'}" for s, m in chosen.items())
    plan = plan_of(default, overrides)
    mixed = {}
    for name, ticket_set, base in (("dev", "agent", base_dev), ("heldout", "agent_hard", base_ho)):
        tickets = A.load_tickets(sets=(ticket_set,))
        path = run_config(tickets, label + ("__hard" if name == "heldout" else ""), plan, by_run(load(base)), 0, RUNS,
                          RESULTS, WORKERS)
        rep = build_report(base, path)
        mixed[name] = {"file": path.name, "tickets": rep["inputs_compared"], "runs": RUNS,
                       "verdict_counts": rep["verdict_counts"],
                       "regressed": [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED"],
                       "gold_old": rep["baseline"]["accuracy"], "gold_new": rep["candidate"]["accuracy"],
                       "passed_all_old": rep["baseline"]["passed_all"], "passed_all_new": rep["candidate"]["passed_all"],
                       "old_unstable_inputs": rep["baseline"]["unstable_inputs"]}
        print(f"  mix on {name}: {rep['verdict_counts']} gold {rep['baseline']['accuracy']} -> {rep['candidate']['accuracy']}")
    ho = mixed["heldout"]
    noise = ho["old_unstable_inputs"] / max(1, ho["tickets"])
    accepted = ho["verdict_counts"]["REGRESSED"] == 0 and ho["gold_new"] >= ho["gold_old"] - noise

    # 3. per-step cost from real usage
    def files(*names):
        return [RESULTS / n for n in names if (RESULTS / n).exists()]
    costs = {
        "all_old": usage_per_step(files(f"agent__{OLD}__noplant.jsonl", f"agent__{OLD}__noplant__hard.jsonl")),
        "all_new": usage_per_step(files(f"agent__gpt-5.6-sol-np__patch-{tag}.jsonl",
                                        f"agent__gpt-5.6-sol-np__patch-{tag}__hard.jsonl")),
        "mix": usage_per_step(files(mixed["dev"]["file"], mixed["heldout"]["file"])),
    }
    totals = {k: total(v) for k, v in costs.items()}
    savings = {"vs_all_old_pct": round((1 - totals["mix"] / totals["all_old"]) * 100, 1) if totals["mix"] and totals["all_old"] else None,
               "vs_all_new_pct": round((1 - totals["mix"] / totals["all_new"]) * 100, 1) if totals["mix"] and totals["all_new"] else None}
    # The mix is shipped only if held-out accepts it; otherwise the recommendation falls back to all-new + patch,
    # which model_only.py validated on the same held-out tickets (and was never tuned on them).
    mo = json.loads((HERE / "reports" / "model_only" / "result_gpt-5.6-sol.json").read_text())
    all_new_ok = mo.get("heldout_after", {}).get("verdict_counts", {}).get("REGRESSED") == 0
    recommended = ({"config": "mix", "assignment": chosen, "usd_per_1k": totals["mix"]} if accepted else
                   {"config": "all_new", "assignment": {s: "gpt-5.6-sol" for s in A.STEPS}, "usd_per_1k": totals["all_new"],
                    "why": "the per-step mix regressed on held-out; all gpt-5.6-sol + the tone patch had 0 held-out "
                           "regressions (model_only.py)"} if all_new_ok else
                   {"config": "all_old", "assignment": {s: OLD for s in A.STEPS}, "usd_per_1k": totals["all_old"]})
    recommended["savings_vs_all_old_pct"] = (round((1 - recommended["usd_per_1k"] / totals["all_old"]) * 100, 1)
                                             if recommended["usd_per_1k"] and totals["all_old"] else None)
    out = {"ladder": LADDER + [OLD], "rule": "a step passes when gpt-4-everywhere-else with this model at the step shows "
           "0 regressions on 24 dev tickets x 3 runs (compare.py noise-floor rule)",
           "synthetic": {"tickets": True, "regression": False},
           "per_step": per_step, "assignment": chosen, "mixed": mixed, "accepted": accepted,
           "noise_floor": noise, "costs_per_1k_ticket_runs": costs, "totals_usd_per_1k": totals, "savings": savings,
           "recommended": recommended,
           "prices": "config/prices.yml", "generated_at": datetime.now(timezone.utc).isoformat()}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"accepted={accepted} totals/1k={totals} savings={savings}  spent ${llm.spent_usd():.2f}")


if __name__ == "__main__":
    main()
