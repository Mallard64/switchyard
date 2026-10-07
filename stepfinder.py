"""Step-finder: prove which step of the support agent the new model broke.

For each ticket the candidate REGRESSED on (compare.py's noise-floor rule vs the old model):
  necessity    new model everywhere except step k (old model). If the ticket stops
               regressing, step k is causal. Steps before k are replayed from the
               candidate's own cached run, so only step k (and what follows) changes.
  pairs        if no single step repairs it, try every pair of steps on the old model.
  sufficiency  old model everywhere except step k (new model), upstream replayed from the
               old model's run. If the regression comes back, step k alone reproduces it.
All hybrids run live, 3 runs each, cached and resumable in results/agent__<config>.jsonl.

  python stepfinder.py --candidate gpt-5.6-sol --candidate-config '{}'
"""
import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import support_agent as A  # noqa: E402
from compare import build_report, compare_input, load, load_gold  # noqa: E402

REPORTS = HERE / "reports" / "agent"


def make_plan(default, overrides):
    """default/overrides values are (model, params); overrides: {step: (model, params)}."""
    return {s: dict(zip(("model", "params"), overrides.get(s, default))) for s in A.STEPS}


def run_config(tickets, label, plan, source, first_live, runs, outdir, workers, prompts=None):
    """Run tickets under plan; steps before first_live are replayed from source[ticket][run]."""
    out = outdir / f"agent__{label}.jsonl"
    done = A.load_done(out)
    todo = [(t, r) for t in tickets for r in range(1, runs + 1)
            if (t["id"], r) not in done and r in source.get(t["id"], {})]
    lock = threading.Lock()

    def work(t, r):
        src = source[t["id"]][r]
        row = A.record_row(t, r, plan, label, reuse=src["steps"][:first_live], prompts=prompts,
                           replayed_from={"config": src["config"], "run": r, "steps": A.STEPS[:first_live]})
        with lock, open(out, "a") as f:
            f.write(json.dumps(row) + "\n")
        if row["error"]:
            print(f"   {label} {t['id']} r{r} ERROR {row['error'][:150]}")

    if todo:
        print(f"   {label}: {len(todo)} to run")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda a: work(*a), todo))
    return out


def judge(path, tid, base, gold):
    rows = load(path).get(tid)
    if not rows:
        return None
    item = compare_input("agent", base[tid], rows, gold[tid])
    return {"verdict": item["verdict"], "output": item["candidate"]["output"],
            "score_runs": item["candidate"]["score_runs"], "file": path.name}


def step_raw(rows, step):
    rec = next(s for s in rows[0]["steps"] if s["step"] == step)
    return {"model": rec["model"], "raw": rec["raw"]}


def new_calls_cost(paths):
    """Cost of live (non-replayed) calls in these files, per model; None where no price is known."""
    by_model = {}
    for p in paths:
        for line in open(p):
            r = json.loads(line)
            for s in r.get("steps") or []:
                if not s.get("replayed"):
                    c = by_model.setdefault(s["model"], {"calls": 0, "usd": 0.0})
                    c["calls"] += 1
                    usd = s["cost_usd"] if s.get("cost_usd") is not None else A.cost(s["model"], s.get("usage"))
                    c["usd"] = None if c["usd"] is None or usd is None else c["usd"] + usd
    return {m: {"calls": v["calls"], "usd": None if v["usd"] is None else round(v["usd"], 4)}
            for m, v in by_model.items()}


def spec(model, params, prompts=None, label=None):
    """A side of the comparison: model + sampling params, optionally its own prompts per step."""
    return {"model": model, "params": params, "prompts": prompts or {}, "label": label or model}


def plan_of(default, overrides):
    """Plan with `default` on every step and `overrides` ({step: spec}) on some; prompts follow the spec."""
    plan = {}
    for st in A.STEPS:
        sp = overrides.get(st, default)
        plan[st] = {"model": sp["model"], "params": sp["params"],
                    **({"prompt": sp["prompts"][st]} if st in sp["prompts"] else {})}
    return plan


def label_of(default, overrides):
    return default["label"] + "".join(f"__{st}-{overrides[st]['label']}" for st in A.STEPS if st in overrides)


def localize(old, new, base_path, cand_path, runs, outdir, workers, max_pairs=True):
    """The step-finder. old/new are spec() dicts; base_path/cand_path their cached full runs."""
    report = build_report(base_path, cand_path)
    regressed = [it["input_id"] for it in report["items"] if it["verdict"] == "REGRESSED"]
    if not regressed:
        return {"old_model": old["label"], "candidate": new["label"], "runs": runs, "regressed": [],
                "summary": {}, "unexplained": [], "tickets": [], "live_calls": {}}
    tickets = [t for t in A.load_tickets() if t["id"] in regressed]
    base, cand, gold = load(base_path), load(cand_path), load_gold("agent")
    by_run = lambda rows: {tid: {r["run"]: r for r in rs} for tid, rs in rows.items()}
    cand_src, base_src = by_run(cand), by_run(base)
    files = []

    # Necessity: swap one step back to the old model.
    singles = {}
    for k, step in enumerate(A.STEPS):
        files.append(run_config(tickets, label_of(new, {step: old}), plan_of(new, {step: old}), cand_src, k, runs,
                                outdir, workers))
        singles[step] = {tid: judge(files[-1], tid, base, gold) for tid in regressed}

    # Pairs, only for tickets no single step repairs.
    stuck = [tid for tid in regressed if all(singles[s][tid]["verdict"] == "REGRESSED" for s in A.STEPS)]
    pairs = {}
    for a, b in combinations(A.STEPS, 2) if stuck and max_pairs else []:
        files.append(run_config([t for t in tickets if t["id"] in stuck], label_of(new, {a: old, b: old}),
                                plan_of(new, {a: old, b: old}), cand_src, A.STEPS.index(a), runs, outdir, workers))
        pairs[f"{a}+{b}"] = {tid: judge(files[-1], tid, base, gold) for tid in stuck}

    # Causal steps: single steps that repair it; else the pairs that do.
    causal = {}
    for tid in regressed:
        steps = [s for s in A.STEPS if singles[s][tid]["verdict"] != "REGRESSED"]
        if not steps:
            fixing = [p for p, v in pairs.items() if tid in v and v[tid]["verdict"] != "REGRESSED"]
            steps = sorted({s for p in fixing for s in p.split("+")}, key=A.STEPS.index) if fixing else []
        causal[tid] = steps

    # Several single steps "repair" it (often run-to-run noise downstream of the real cause): keep
    # the steps that reproduce the regression on their own (new model only at that step).
    ambiguous = [tid for tid in regressed if len(causal[tid]) > 1
                 and all(singles[s][tid]["verdict"] != "REGRESSED" for s in causal[tid])]
    alone, narrowed = {}, {}
    for step in sorted({s for tid in ambiguous for s in causal[tid]}, key=A.STEPS.index):
        tids = [t for t in ambiguous if step in causal[t]]
        files.append(run_config([t for t in tickets if t["id"] in tids], label_of(old, {step: new}),
                                plan_of(old, {step: new}), base_src, A.STEPS.index(step), runs, outdir, workers))
        alone[step] = {t: judge(files[-1], t, base, gold) for t in tids}
    for tid in ambiguous:
        keep = [s for s in causal[tid] if alone[s][tid]["verdict"] == "REGRESSED"]
        if keep:
            narrowed[tid] = {"from": causal[tid], "to": keep}
            causal[tid] = keep

    # Sufficiency: the new model on the causal step(s) only.
    suff = {}
    for tid in regressed:
        if not causal[tid]:
            continue
        key = "+".join(causal[tid])
        if key not in suff:
            ov = {s: new for s in causal[tid]}
            tids = [t for t in regressed if causal[t] == causal[tid]]
            files.append(run_config([t for t in tickets if t["id"] in tids], label_of(old, ov), plan_of(old, ov),
                                    base_src, min(A.STEPS.index(s) for s in causal[tid]), runs, outdir, workers))
            suff[key] = {t: judge(files[-1], t, base, gold) for t in tids}

    out_tickets = []
    for t in tickets:
        tid = t["id"]
        steps = causal[tid]
        key = "+".join(steps)
        item = next(it for it in report["items"] if it["input_id"] == tid)
        out_tickets.append({
            "input_id": tid, "text": t["text"], "tricky": t.get("tricky", False),
            "baseline_output": item["baseline"]["output"], "candidate_output": item["candidate"]["output"],
            "causal_steps": steps,
            "status": ("confirmed" if steps and suff[key][tid]["verdict"] == "REGRESSED"
                       else "necessary_only" if steps else "unexplained"),
            "necessity": {s: singles[s][tid] for s in A.STEPS},
            "pairs": {p: v[tid] for p, v in pairs.items() if tid in v},
            "sufficiency": {key: suff[key][tid]} if steps else {},
            "narrowed": narrowed.get(tid),
            # What the causal step(s) actually said, on each model (first run).
            "step_outputs": {s: {"new": step_raw(cand[tid], s), "old": step_raw(base[tid], s)} for s in steps},
        })
    n = len(regressed)
    summary = {s: {"explains": sum(s in causal[tid] for tid in regressed), "of": n} for s in A.STEPS}
    for v in summary.values():
        v["pct"] = round(100 * v["explains"] / n)
    return {"old_model": old["label"], "candidate": new["label"], "runs": runs,
            "baseline_file": Path(base_path).name, "candidate_file": Path(cand_path).name,
            "regressed": regressed, "summary": summary,
            "unexplained": [tid for tid in regressed if not causal[tid]],
            "tickets": out_tickets, "live_calls": new_calls_cost(files)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", default="gpt-4")
    ap.add_argument("--old-config", type=json.loads, default={"temperature": 0.0})
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--candidate-config", type=json.loads, default={})
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--outdir", default=str(HERE / "results"))
    args = ap.parse_args()
    if len(A.STEPS) > 4:
        sys.exit("more than 4 steps: add the binary-search mode before using this")
    outdir = Path(args.outdir)
    result = localize(spec(args.old, args.old_config), spec(args.candidate, args.candidate_config),
                      outdir / f"agent__{args.old}.jsonl", outdir / f"agent__{args.candidate}.jsonl",
                      args.runs, outdir, args.workers)
    print(f"{args.old} -> {args.candidate}: {len(result['regressed'])} regressed ticket(s): {result['regressed']}")
    if not result["regressed"]:
        return
    summary, out_tickets, n = result["summary"], result["tickets"], len(result["regressed"])
    out_path = REPORTS / f"stepfinder_{args.candidate}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))

    print("\nStep-finder")
    for s, v in summary.items():
        print(f"  {s:<10} explains {v['explains']}/{n} regressed tickets ({v['pct']}%)")
    for tk in out_tickets:
        print(f"\n  {tk['input_id']}: {tk['baseline_output']} -> {tk['candidate_output']}   [{tk['status']}]")
        for s, j in tk["necessity"].items():
            print(f"     {args.candidate} except {s:<9}-> {j['verdict']:<10} {j['output']}  {j['score_runs']}")
        for p, j in tk["pairs"].items():
            print(f"     {args.candidate} except {p:<9}-> {j['verdict']:<10} {j['output']}  {j['score_runs']}")
        for s, j in tk["sufficiency"].items():
            print(f"     {args.old} except {s} ({args.candidate}) -> {j['verdict']}  {j['output']}  {j['score_runs']}")
        for s, o in tk["step_outputs"].items():
            print(f"     {s} on {o['new']['model']}: {o['new']['raw'][:150]}")
            print(f"     {s} on {o['old']['model']}: {o['old']['raw'][:150]}")
    print(f"\nlive calls: {result['live_calls']}\n-> {out_path}")


if __name__ == "__main__":
    main()
