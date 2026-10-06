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
    base_path = outdir / f"agent__{args.old}.jsonl"
    cand_path = outdir / f"agent__{args.candidate}.jsonl"
    report = build_report(base_path, cand_path)
    regressed = [it["input_id"] for it in report["items"] if it["verdict"] == "REGRESSED"]
    print(f"{args.old} -> {args.candidate}: {len(regressed)} regressed ticket(s): {regressed}")
    if not regressed:
        return
    tickets = [t for t in A.load_tickets() if t["id"] in regressed]
    base, cand, gold = load(base_path), load(cand_path), load_gold("agent")
    by_run = lambda rows: {tid: {r["run"]: r for r in rs} for tid, rs in rows.items()}
    cand_src, base_src = by_run(cand), by_run(base)
    old, new = (args.old, args.old_config), (args.candidate, args.candidate_config)
    files = []

    # Necessity: swap one step back to the old model.
    singles = {}
    for k, step in enumerate(A.STEPS):
        label = A.config_label(args.candidate, {step: args.old})
        files.append(run_config(tickets, label, make_plan(new, {step: old}), cand_src, k, args.runs, outdir,
                                args.workers))
        singles[step] = {tid: judge(files[-1], tid, base, gold) for tid in regressed}

    # Pairs, only for tickets no single step repairs.
    stuck = [tid for tid in regressed if all(singles[s][tid]["verdict"] == "REGRESSED" for s in A.STEPS)]
    pairs = {}
    for a, b in combinations(A.STEPS, 2) if stuck else []:
        label = A.config_label(args.candidate, {a: args.old, b: args.old})
        files.append(run_config([t for t in tickets if t["id"] in stuck], label,
                                make_plan(new, {a: old, b: old}), cand_src, A.STEPS.index(a), args.runs,
                                outdir, args.workers))
        pairs[f"{a}+{b}"] = {tid: judge(files[-1], tid, base, gold) for tid in stuck}

    # Sufficiency: for each causal step, the new model on that step alone.
    causal = {tid: [s for s in A.STEPS if singles[s][tid]["verdict"] != "REGRESSED"] for tid in regressed}
    suff = {}
    for step in sorted({s for ss in causal.values() for s in ss}, key=A.STEPS.index):
        label = A.config_label(args.old, {step: args.candidate})
        tids = [tid for tid in regressed if step in causal[tid]]
        files.append(run_config([t for t in tickets if t["id"] in tids], label, make_plan(old, {step: new}),
                                base_src, A.STEPS.index(step), args.runs, outdir, args.workers))
        suff[step] = {tid: judge(files[-1], tid, base, gold) for tid in tids}

    out_tickets = []
    for t in tickets:
        tid = t["id"]
        steps = causal[tid]
        out_tickets.append({
            "input_id": tid, "text": t["text"], "tricky": t.get("tricky", False),
            "baseline_output": next(it for it in report["items"] if it["input_id"] == tid)["baseline"]["output"],
            "candidate_output": next(it for it in report["items"] if it["input_id"] == tid)["candidate"]["output"],
            "causal_steps": steps,
            "status": "confirmed" if steps and all(suff[s][tid]["verdict"] == "REGRESSED" for s in steps)
                      else "necessary_only" if steps else "unexplained",
            "necessity": {s: singles[s][tid] for s in A.STEPS},
            "pairs": {p: v[tid] for p, v in pairs.items() if tid in v},
            "sufficiency": {s: suff[s][tid] for s in steps},
            # What the causal step actually said, on each model (first run).
            "step_outputs": {s: {"new": step_raw(cand[tid], s), "old": step_raw(base[tid], s)} for s in steps},
        })
    n = len(regressed)
    summary = {s: {"explains": sum(s in causal[tid] for tid in regressed), "of": n} for s in A.STEPS}
    for v in summary.values():
        v["pct"] = round(100 * v["explains"] / n)
    result = {"old_model": args.old, "candidate": args.candidate, "runs": args.runs,
              "baseline_file": base_path.name, "candidate_file": cand_path.name,
              "regressed": regressed, "summary": summary,
              "unexplained": [tid for tid in regressed if not causal[tid]],
              "tickets": out_tickets, "live_calls": new_calls_cost(files)}
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
