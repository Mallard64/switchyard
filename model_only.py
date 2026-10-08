"""Genuine model-only regression on the 4-step agent: localize it, fix it with a bounded patch, prove it on held-out tickets.

  python model_only.py                      # gpt-4 -> gpt-5.6-sol, every stage cached
  python model_only.py --new gpt-5.6-terra

Model-only means: same prompts (the clean agent, without the planted decide line), same fake tools, same
tickets, same 3 runs; only the model changes. (gpt-5.6-* reject temperature 0, so they run at their default
sampling; gpt-4 keeps its recorded temperature 0. That is the only setting that differs, and it can't be avoided.)

Stages (compare.py's noise-floor rule throughout):
  1. compare      old vs new on the 24 dev tickets -> regressed tickets
  2. step-finder  repair = old model swapped back at step k; reproduce = new model at step k only; 3 runs each
  3. lines        remove each line of the guilty step (new model everywhere); 3 runs each
  4. fix          an LLM fixer sees ONLY dev failures and may change at most MAX_EDITS lines of the guilty step;
                  a patch must clear the dev regressions on a full dev re-run, else it gets the evidence and retries
  5. held-out     the accepted patch, once, on the 12 hard tickets (written before any run; never shown to the fixer)
Writes reports/model_only/result_<new>.json.
"""
import argparse
import difflib
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import llm  # noqa: E402
import support_agent as A  # noqa: E402
from compare import build_report, compare_input, load, load_gold  # noqa: E402
from stepfinder import localize, plan_of, run_config, spec  # noqa: E402

RESULTS = HERE / "results"
OUT = HERE / "reports" / "model_only"
MAX_EDITS = 2
MAX_ATTEMPTS = 3
FIXER_SYSTEM = (
    "You repair one step of a multi-step LLM pipeline after a model migration. The new model breaks a "
    "requirement that the old model met. Change as few prompt lines as possible, only in the step given, at most "
    f"{MAX_EDITS} lines; keep every other instruction's intent. Respond with only a JSON object: "
    '{"edits": [{"line_index": <0-based int>, "new_text": "<full replacement line>"}], "rationale": "<one sentence>"}')


def by_run(rows):
    return {tid: {r["run"]: r for r in rs} for tid, rs in rows.items()}


def step_rec(row, step):
    return next(s for s in row["steps"] if s["step"] == step)


def regressions(old_path, new_path):
    rep = build_report(old_path, new_path)
    return rep, [it for it in rep["items"] if it["verdict"] == "REGRESSED"]


def ablate_lines(new, step, regressed, cand_path, base_path, runs, workers):
    """Remove one line at a time from the guilty step (new model everywhere, upstream replayed from its run)."""
    tickets = {t["id"]: t for t in A.load_tickets(sets="all")}
    base, cand, gold = load(base_path), load(cand_path), load_gold("agent")
    k, lines, out = A.STEPS.index(step), A.PROMPTS[step], []
    prompts = dict(new["prompts"])
    for i, line in enumerate(lines):
        p = {**prompts, step: lines[:i] + lines[i + 1:]}
        path = run_config([tickets[t] for t in regressed], f"{new['label']}__ablate-{step}-L{i}", plan_of(new, {}),
                          by_run(cand), k, runs, RESULTS, workers, prompts=p)
        rows = load(path)
        repaired = [t for t in regressed if t in rows and compare_input("agent", base[t], rows[t], gold[t])["verdict"] != "REGRESSED"]
        out.append({"line_index": i, "line": line, "repairs": len(repaired), "of": len(regressed), "tickets": repaired})
    return out


def fixer_prompt(step, dev_items, base, cand, lines_result, feedback):
    lines = "\n".join(f"{i}: {l}" for i, l in enumerate(A.PROMPTS[step]))
    ex = []
    for it in dev_items[:4]:
        tid = it["input_id"]
        reg = it["regressions"][0]
        ex.append(f"Ticket {tid}: {it['text']}\n"
                  f"Failed requirement: {reg.get('instruction') or reg['kind']} "
                  f"(old model failed {reg.get('baseline_fail', '?')} runs, new model {reg.get('candidate_fail', '?')})\n"
                  f"Old model, `{step}` output: {step_rec(base[tid][0], step)['raw'][:700]}\n"
                  f"New model, `{step}` output: {step_rec(cand[tid][0], step)['raw'][:700]}")
    abl = "\n".join(f"line {r['line_index']}: removing it alone repairs {r['repairs']}/{r['of']}" for r in lines_result)
    return (f"Step `{step}` prompt lines (0-based):\n{lines}\n\nFailing examples (dev set):\n\n" + "\n\n".join(ex)
            + f"\n\nLine ablation on the new model:\n{abl}" + (f"\n\nYour previous attempt failed:\n{feedback}" if feedback else ""))


def parse_patch(raw, step):
    obj, _ = A.parse_json(raw)
    edits = (obj or {}).get("edits") or []
    n = len(A.PROMPTS[step])
    ok = (0 < len(edits) <= MAX_EDITS and len({e.get("line_index") for e in edits}) == len(edits)
          and all(isinstance(e.get("line_index"), int) and 0 <= e["line_index"] < n
                  and isinstance(e.get("new_text"), str) and 0 < len(e["new_text"]) <= 400 for e in edits))
    return (edits, (obj or {}).get("rationale")) if ok else (None, None)


def patched(step, edits):
    lines = list(A.PROMPTS[step])
    for e in edits:
        lines[e["line_index"]] = e["new_text"]
    return lines


def run_full(new, prompts, ticket_set, source_path, label, runs, workers):
    """End-to-end run of every ticket in a set with patched prompts (nothing replayed)."""
    tickets = A.load_tickets(sets=(ticket_set,))
    return run_config(tickets, label, plan_of(new, {}), by_run(load(source_path)), 0, runs, RESULTS, workers, prompts=prompts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", default="gpt-4")
    ap.add_argument("--new", default="gpt-5.6-sol")
    ap.add_argument("--fixer", default="gpt-5.6-sol")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    clean = A.unplanted_prompts()
    old = spec(args.old, {"temperature": 0.0}, clean, f"{args.old}-np")
    new = spec(args.new, {}, clean, f"{args.new}-np")
    f = lambda m, tag="": RESULTS / f"agent__{m}__noplant{tag}.jsonl"
    dev_old, dev_new, ho_old, ho_new = f(args.old), f(args.new), f(args.old, "__hard"), f(args.new, "__hard")

    # 1. compare
    dev_rep, dev_reg = regressions(dev_old, dev_new)
    ho_rep, ho_reg = regressions(ho_old, ho_new)
    print(f"[1/5] model-only {args.old} -> {args.new}: dev {dev_rep['verdict_counts']}, held-out {ho_rep['verdict_counts']}")
    out = {"pair": {"old": args.old, "new": args.new, "prompts": "clean agent (planted decide line removed) on both sides",
                    "sampling": {"old": {"temperature": 0.0}, "new": "model default (temperature 0 rejected)"}},
           "synthetic": {"tickets": True, "regression": False, "note": "hand-written tickets; the regression is the "
                                                                       "new model's own behavior, nothing planted"},
           "dev": {"tickets": dev_rep["inputs_compared"], "runs": args.runs, "verdict_counts": dev_rep["verdict_counts"],
                   "regressed": [{"input_id": it["input_id"], "regressions": it["regressions"]} for it in dev_reg]},
           "heldout_before": {"tickets": ho_rep["inputs_compared"], "verdict_counts": ho_rep["verdict_counts"],
                              "regressed": [{"input_id": it["input_id"], "regressions": it["regressions"]} for it in ho_reg]}}
    if not dev_reg:
        print("No genuine model-only regression on the dev set; nothing to localize.")
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"result_{args.new}.json").write_text(json.dumps(out, indent=2, default=list) + "\n")
        return

    # 2. step-finder
    sf = localize(old, new, dev_old, dev_new, args.runs, RESULTS, args.workers)
    ranked = sorted(sf["summary"].items(), key=lambda kv: -kv[1]["explains"])
    step = ranked[0][0]
    reg_ids = [it["input_id"] for it in dev_reg]
    per_step = {s: {"repair": sum(t["necessity"][s]["verdict"] != "REGRESSED" for t in sf["tickets"] if t["necessity"][s]),
                    "reproduce": sum(1 for t in sf["tickets"] if s in t["causal_steps"]
                                     and next(iter(t["sufficiency"].values()))["verdict"] == "REGRESSED"),
                    "of": len(sf["tickets"])} for s in A.STEPS}
    out["stepfinder"] = {"runs": args.runs, "per_step": per_step, "guilty_step": step,
                         "tickets": [{"input_id": t["input_id"], "causal_steps": t["causal_steps"], "status": t["status"],
                                      "repair": {s: (v or {}).get("verdict") for s, v in t["necessity"].items()},
                                      "reproduce": {k: v["verdict"] for k, v in t["sufficiency"].items()}}
                                     for t in sf["tickets"]],
                         "live_calls": sf["live_calls"]}
    print(f"[2/5] step-finder: " + ", ".join(f"{s} repair {v['repair']}/{v['of']} reproduce {v['reproduce']}/{v['of']}"
                                             for s, v in per_step.items()))

    # 3. lines
    lines_result = ablate_lines(new, step, reg_ids, dev_new, dev_old, args.runs, args.workers)
    out["lines"] = {"step": step, "results": lines_result}
    print(f"[3/5] lines in {step}: " + ", ".join(f"L{r['line_index'] + 1} {r['repairs']}/{r['of']}" for r in lines_result))

    # 4. fix (dev only)
    base, cand = load(dev_old), load(dev_new)
    feedback, attempts, accepted = None, [], None
    for n in range(1, MAX_ATTEMPTS + 1):
        llm.set_run(n)  # each attempt is its own cache key
        raw = llm.call_llm("fixer", args.fixer, [{"role": "system", "content": FIXER_SYSTEM},
                                                 {"role": "user", "content": fixer_prompt(step, dev_reg, base, cand, lines_result, feedback)}],
                           {}, api_base=A.OPENAI_BASE)["raw"]
        edits, rationale = parse_patch(raw, step)
        if not edits:
            feedback = f"Your reply was not a valid patch (JSON with 1-{MAX_EDITS} edits to existing lines of `{step}`): {raw[:300]}"
            attempts.append({"attempt": n, "valid": False, "raw": raw[:1000]})
            print(f"[4/5] attempt {n}: invalid patch")
            continue
        lines = patched(step, edits)
        tag = hashlib.sha256(json.dumps(lines).encode()).hexdigest()[:8]
        path = run_full(new, {**clean, step: lines}, "agent", dev_new, f"{new['label']}__patch-{tag}", args.runs, args.workers)
        rep = build_report(dev_old, path)
        still = [it for it in rep["items"] if it["verdict"] == "REGRESSED"]
        attempts.append({"attempt": n, "valid": True, "edits": edits, "rationale": rationale, "dev_file": path.name,
                         "dev_verdict_counts": rep["verdict_counts"], "dev_still_regressed": [it["input_id"] for it in still]})
        print(f"[4/5] attempt {n}: {len(edits)} line(s) -> dev {rep['verdict_counts']}")
        if not still:
            accepted = (edits, rationale, lines, tag, rep)
            break
        feedback = "After your patch these dev tickets still regress: " + "; ".join(
            f"{it['input_id']}: {it['regressions'][0].get('check') or it['regressions'][0]['kind']}" for it in still)
    out["fix"] = {"fixer_model": args.fixer, "max_edits": MAX_EDITS, "saw": "dev tickets only", "attempts": attempts}
    if not accepted:
        print("[4/5] no patch cleared the dev regressions; held-out not run")
    else:
        edits, rationale, lines, tag, dev_rep2 = accepted
        diff = "\n".join(difflib.unified_diff(A.PROMPTS[step], lines, f"a/PROMPTS['{step}']", f"b/PROMPTS['{step}']", lineterm=""))
        out["patch"] = {"step": step, "rationale": rationale, "diff": diff,
                        "edits": [{"line_index": e["line_index"], "old": A.PROMPTS[step][e["line_index"]], "new": e["new_text"]}
                                  for e in edits], "dev_verdict_counts": dev_rep2["verdict_counts"]}
        # 5. held-out, once
        path = run_full(new, {**clean, step: lines}, "agent_hard", ho_new, f"{new['label']}__patch-{tag}__hard", args.runs, args.workers)
        rep = build_report(ho_old, path)
        out["heldout_after"] = {"tickets": rep["inputs_compared"], "file": path.name, "verdict_counts": rep["verdict_counts"],
                                "regressed": [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED"],
                                "baseline_passed_all": rep["baseline"]["passed_all"], "patched_passed_all": rep["candidate"]["passed_all"]}
        print(f"[5/5] held-out (never shown to the fixer): before {ho_rep['verdict_counts']} -> after {rep['verdict_counts']}")
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"result_{args.new}.json").write_text(json.dumps(out, indent=2, default=list) + "\n")
    print(f"-> {OUT / f'result_{args.new}.json'}   spent so far ${llm.spent_usd():.2f}")


if __name__ == "__main__":
    main()
