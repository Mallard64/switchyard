"""Compare a candidate model's run against the baseline, input by input.

  python compare.py results/ner__gpt-4__gpt-4.jsonl results/ner__gpt-4__gpt-5.6-sol.jsonl
  python compare.py BASE CAND --json report.json      # also write the machine-readable report

Verdict rules (per input, both sides run N times):
  REGRESSED  a hard check fails in most candidate runs but not most baseline runs, or the
             candidate's gold score is below the baseline's worst run in most candidate runs.
  IMPROVED   the reverse.
  CHANGED    output differs from every baseline output in most runs, score not worse -> review.
  SAME       otherwise.
Requiring "most runs" and "below the baseline's worst run" is what keeps run-to-run noise
from being reported as a regression.

Ready to merge = zero REGRESSED inputs.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from checks import NER_CHECKS, TEXTCAT_CHECKS, check_ner, check_textcat  # noqa: E402

from record_baseline import cost  # noqa: E402
from support_agent import AGENT_CHECKS  # noqa: E402

INSTRUCTION = {**NER_CHECKS, **TEXTCAT_CHECKS, **{k: v[1] for k, v in AGENT_CHECKS.items()}}


def load(path):
    """Rows per input from one results file, or several (a list): runs are renumbered so files
    stack, e.g. a protected baseline + an extension file, or a baseline + its noise-floor re-run."""
    if isinstance(path, (list, tuple)):
        merged = defaultdict(list)
        for p in path:
            for iid, rs in load(p).items():
                for r in rs:
                    merged[iid].append({**r, "run": len(merged[iid]) + 1})
        return merged
    rows = {}
    for line in open(path):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("error"):
            # Recompute checks from the saved raw output so older files get current checks.
            # Likewise fill in costs recorded before a model's price was known in PRICES.
            if r["task"] == "ner":
                r["checks"] = check_ner(r["text"], r["raw_output"], r["output"])
            elif r["task"] == "textcat":
                r["checks"] = check_textcat(r["raw_output"], r["output"])
            if r["task"] in ("ner", "textcat"):
                if r.get("cost_usd") is None:
                    r["cost_usd"] = cost(r.get("model"), r.get("usage"))
            else:  # agent: checks span all steps; flatten per-step fields to the row
                r["raw_output"] = r["final_reply"]
                r["latency_s"] = round(sum(s.get("latency_s") or 0 for s in r["steps"]), 3)
                costs = [s["cost_usd"] if s.get("cost_usd") is not None else cost(s["model"], s.get("usage"))
                         for s in r["steps"]]
                r["cost_usd"] = None if None in costs else round(sum(costs), 6)
                r["resolved_model"] = "+".join(sorted({s.get("resolved_model") or "?" for s in r["steps"]}))
                r["model"] = r["config"]
            r["passed_all"] = all(r["checks"].values())
            rows[(r["input_id"], r["run"])] = r
    by_input = defaultdict(list)
    for r in rows.values():
        by_input[r["input_id"]].append(r)
    return by_input


def load_gold(task):
    return {r["id"]: r for r in map(json.loads, open(HERE / "inputs" / f"{task}.jsonl"))}


def norm(s):
    return " ".join(s.lower().split())


# ---- task-specific views ---------------------------------------------------------------
def ner_key(r):
    return tuple(sorted((norm(e["text"]), e["label"]) for e in r["output"]))


def ner_errors(ents, gold):
    """Classify every disagreement: boundary, wrong label, missed, spurious."""
    pred = [(norm(e["text"]), e["label"]) for e in ents]
    ref = [(norm(g["text"]), g["label"]) for g in gold]
    out, used = [], set()
    for g in ref:
        if g in pred:
            used.add(g)
            continue
        same_span = [p for p in pred if p[0] == g[0] and p not in used]
        overlap = [p for p in pred if p not in used and p[1] == g[1] and (g[0] in p[0] or p[0] in g[0])]
        if same_span:
            used.add(same_span[0])
            out.append({"type": "wrong_label", "gold": g, "pred": same_span[0]})
        elif overlap:
            used.add(overlap[0])
            out.append({"type": "boundary", "gold": g, "pred": overlap[0]})
        else:
            out.append({"type": "missed", "gold": g})
    for p in pred:
        if p not in used and p not in ref:
            out.append({"type": "spurious", "pred": p})
    return out


def ner_scores(ents, gold):
    errs = ner_errors(ents, gold)
    n_pred, n_gold = len(ents), len(gold)
    strict_tp = n_gold - len([e for e in errs if "gold" in e])
    lenient_tp = strict_tp + len([e for e in errs if e["type"] == "boundary"])

    def f1(tp):
        p = tp / n_pred if n_pred else 0
        r = tp / n_gold if n_gold else 0
        return 2 * p * r / (p + r) if p + r else 0.0
    return {"f1": round(f1(strict_tp), 4), "f1_lenient": round(f1(lenient_tp), 4), "errors": errs}


def view(task, r, gold):
    if task == "agent":
        # Compare decisions, not reply wording (replies vary run to run without temperature 0).
        g = r["gold_score"]
        dec = (r["category"], (r["order_id"] or "").upper() or None, (r["decision"] or {}).get("action"))
        return {"key": dec, "score": g["score"], "strict": None,
                "errors": [{"type": f"wrong_{k}"} for k in ("category", "order_id", "action", "reply_content")
                           if not g[k]],
                "shown": f"{dec[0]}/{dec[2]}"}
    if task == "ner":
        s = ner_scores(r["output"], gold["gold"])
        return {"key": ner_key(r), "score": s["f1_lenient"], "strict": s["f1"], "errors": s["errors"],
                "shown": [f"{e['text']}/{e['label']}" for e in r["output"]]}
    pred = r["gold_score"]["pred"]
    return {"key": pred, "score": 1.0 if pred == gold["gold"] else 0.0, "strict": None,
            "errors": [] if pred == gold["gold"] else [{"type": "wrong_label", "gold": gold["gold"], "pred": pred}],
            "shown": pred}


# ---- comparison ---------------------------------------------------------------------------
def majority(n_true, n):
    return n_true * 2 > n


def compare_input(task, base_rows, cand_rows, gold):
    bv = [view(task, r, gold) for r in base_rows]
    cv = [view(task, r, gold) for r in cand_rows]
    nb, nc = len(bv), len(cv)
    reasons_bad, reasons_good = [], []

    for check in base_rows[0]["checks"]:
        b_fail = sum(not r["checks"][check] for r in base_rows)
        c_fail = sum(not r["checks"][check] for r in cand_rows)
        if majority(c_fail, nc) and not majority(b_fail, nb):
            reasons_bad.append({"kind": "hard_check", "check": check, "instruction": INSTRUCTION.get(check),
                                "baseline_fail": f"{b_fail}/{nb}", "candidate_fail": f"{c_fail}/{nc}"})
        elif majority(b_fail, nb) and not majority(c_fail, nc):
            reasons_good.append({"kind": "hard_check", "check": check, "instruction": INSTRUCTION.get(check),
                                 "baseline_fail": f"{b_fail}/{nb}", "candidate_fail": f"{c_fail}/{nc}"})

    b_scores = [v["score"] for v in bv]
    c_scores = [v["score"] for v in cv]
    worse = sum(s < min(b_scores) for s in c_scores)
    better = sum(s > max(b_scores) for s in c_scores)
    if majority(worse, nc):
        reasons_bad.append({"kind": "accuracy", "baseline": b_scores, "candidate": c_scores})
    elif majority(better, nc):
        reasons_good.append({"kind": "accuracy", "baseline": b_scores, "candidate": c_scores})

    base_keys = {v["key"] for v in bv}
    novel = sum(v["key"] not in base_keys for v in cv)
    if reasons_bad:
        verdict = "REGRESSED"
    elif reasons_good:
        verdict = "IMPROVED"
    elif majority(novel, nc):
        verdict = "CHANGED"
    else:
        verdict = "SAME"

    # Most common candidate output, with its errors, as the example to show.
    common = Counter(v["key"] for v in cv).most_common(1)[0][0]
    cex = next(v for v in cv if v["key"] == common)
    bcommon = Counter(v["key"] for v in bv).most_common(1)[0][0]
    bex = next(v for v in bv if v["key"] == bcommon)
    bad_raw = next((r["raw_output"] for r in cand_rows if not r["passed_all"]), None)
    flagged = {x["check"] for x in reasons_bad if x["kind"] == "hard_check"}
    intermittent = [{"check": c, "instruction": INSTRUCTION.get(c), "run": r["run"], "raw_output": r["raw_output"]}
                    for r in cand_rows for c, ok in r["checks"].items() if not ok and c not in flagged]
    return {
        "intermittent_failures": intermittent,
        "input_id": base_rows[0]["input_id"], "text": base_rows[0]["text"], "tricky": base_rows[0].get("tricky"),
        "verdict": verdict, "regressions": reasons_bad, "improvements": reasons_good,
        "baseline": {"output": bex["shown"], "score_runs": b_scores, "errors": bex["errors"],
                     "stable": len(base_keys) == 1},
        "candidate": {"output": cex["shown"], "score_runs": c_scores, "errors": cex["errors"],
                      "stable": len({v["key"] for v in cv}) == 1, "failing_raw_output": bad_raw},
    }


def side_summary(by_input, task, gold):
    rows = [r for rs in by_input.values() for r in rs]
    n = len(rows)
    checks = {c: round(sum(r["checks"][c] for r in rows) / n, 4) for c in rows[0]["checks"]}
    scores = [view(task, r, gold[r["input_id"]])["score"] for r in rows]
    lat = sorted(r["latency_s"] for r in rows)
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    return {
        "model": rows[0].get("model"), "resolved_model": sorted({r.get("resolved_model") or "?" for r in rows}),
        "outputs": n, "passed_all": round(sum(r["passed_all"] for r in rows) / n, 4), "checks": checks,
        "accuracy": round(sum(scores) / n, 4),
        "accuracy_metric": {"ner": "lenient entity F1", "agent": "gold score"}.get(task, "label accuracy"),
        "unstable_inputs": sum(len({json.dumps(view(task, r, gold[r['input_id']])['key']) for r in rs}) > 1
                               for rs in by_input.values()),
        "latency_p50_s": lat[n // 2], "latency_p95_s": lat[min(n - 1, int(n * .95))],
        "cost_per_1k_calls_usd": round(sum(costs) / len(costs) * 1000, 2) if costs else None,
    }


def build_report(baseline_path, candidate_path):
    base, cand = load(baseline_path), load(candidate_path)
    if not base or not cand:
        raise ValueError("one of the files has no successful rows")
    task = next(iter(base.values()))[0]["task"]
    gold = load_gold(task)
    shared = sorted(set(base) & set(cand))
    if not shared:
        raise ValueError("no inputs in common between the two files")
    items = [compare_input(task, base[i], cand[i], gold[i]) for i in shared]
    counts = Counter(it["verdict"] for it in items)
    return {
        "task": task, "candidate_file": Path(candidate_path).name,
        "baseline_file": " + ".join(Path(p).name for p in baseline_path) if isinstance(baseline_path, (list, tuple))
                         else Path(baseline_path).name,
        "inputs_compared": len(shared),
        "verdict_counts": {k: counts.get(k, 0) for k in ["REGRESSED", "IMPROVED", "CHANGED", "SAME"]},
        "ready_to_merge": counts.get("REGRESSED", 0) == 0,
        "baseline": side_summary({i: base[i] for i in shared}, task, gold),
        "candidate": side_summary({i: cand[i] for i in shared}, task, gold),
        "instructions_implicated": dict(Counter(r["check"] for it in items for r in it["regressions"]
                                                if r["kind"] == "hard_check")),
        "items": items,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("baseline")
    ap.add_argument("candidate")
    ap.add_argument("--json", help="write the full report here")
    args = ap.parse_args()
    try:
        report = build_report(args.baseline, args.candidate)
    except ValueError as e:
        sys.exit(str(e))
    print_report(report)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=2, default=list))
        print(f"\nFull report -> {args.json}")


def print_report(rep):
    b, c = rep["baseline"], rep["candidate"]
    print(f"{rep['task']}: {b['model']} -> {c['model']}  ({rep['inputs_compared']} inputs)")
    vc = rep["verdict_counts"]
    print(f"  REGRESSED {vc['REGRESSED']}   IMPROVED {vc['IMPROVED']}   CHANGED {vc['CHANGED']}   SAME {vc['SAME']}")
    print(f"  ready to merge: {'YES' if rep['ready_to_merge'] else 'NO'}\n")
    fmt = "  {:<22}{:>14}{:>14}"
    print(fmt.format("", "baseline", "candidate"))
    print(fmt.format("resolved model", b["resolved_model"][0][:13], c["resolved_model"][0][:13]))
    print(fmt.format("passed all checks", f"{b['passed_all']:.0%}", f"{c['passed_all']:.0%}"))
    for k in b["checks"]:
        print(fmt.format(f"  {k}", f"{b['checks'][k]:.0%}", f"{c['checks'][k]:.0%}"))
    print(fmt.format(b["accuracy_metric"], f"{b['accuracy']:.2f}", f"{c['accuracy']:.2f}"))
    print(fmt.format("unstable inputs", b["unstable_inputs"], c["unstable_inputs"]))
    print(fmt.format("latency p50", f"{b['latency_p50_s']:.2f}s", f"{c['latency_p50_s']:.2f}s"))
    if b["cost_per_1k_calls_usd"] is not None and c["cost_per_1k_calls_usd"] is not None:
        unit = "tickets" if rep["task"] == "agent" else "calls"  # an agent row is a whole 4-step run
        print(fmt.format(f"cost / 1k {unit}", f"${b['cost_per_1k_calls_usd']:.2f}", f"${c['cost_per_1k_calls_usd']:.2f}"))
    for verdict in ["REGRESSED", "IMPROVED", "CHANGED"]:
        its = [it for it in rep["items"] if it["verdict"] == verdict]
        if not its:
            continue
        print(f"\n{verdict}")
        for it in its:
            print(f"  {it['input_id']}{' (tricky)' if it['tricky'] else ''}: {it['text'][:70]}")
            print(f"     baseline : {it['baseline']['output']}   score {it['baseline']['score_runs']}")
            print(f"     candidate: {it['candidate']['output']}   score {it['candidate']['score_runs']}")
            for r in it["regressions"] + it["improvements"]:
                if r["kind"] == "hard_check":
                    print(f"     -> {r['check']} fails {r['baseline_fail']} -> {r['candidate_fail']}  "
                          f"[instruction: \"{r['instruction']}\"]")
            errs = [e for e in it["candidate"]["errors"] if verdict != "IMPROVED"]
            if errs:
                print("     candidate errors: " + "; ".join(
                    f"{e['type']} {e.get('gold', '')}{' -> ' if 'gold' in e and 'pred' in e else ''}{e.get('pred', '')}"
                    for e in errs))
    inter = [(it, f) for it in rep["items"] for f in it["intermittent_failures"]]
    if inter:
        print("\nINTERMITTENT HARD-CHECK FAILURES (candidate, minority of runs; not counted as regressions)")
        for it, f in inter:
            raw = " / ".join(l.strip() for l in f["raw_output"].strip().splitlines() if l.strip())
            print(f"  {it['input_id']} run {f['run']}: {f['check']}  [instruction: \"{f['instruction']}\"]")
            print(f"     output: {raw[:160]}{'...' if len(raw) > 160 else ''}")


if __name__ == "__main__":
    main()
