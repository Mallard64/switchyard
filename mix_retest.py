"""Cheap retest of the luna + local-llama mix after fixing the one thing that broke it.

  python mix_retest.py             # every run cached

model_pool.py's mix (gpt-5.6-luna on classify, decide, draft; local llama3.1:8b on tone) was clean on dev but
failed fresh ticket t42: luna's classify step drops order IDs typed in lowercase ("a1005"). Here:
  1. fix: one line of the classify prompt (mix only) says lowercase / "#" IDs are valid and must come back uppercase
  2. dev: the fixed mix on the 24 dev tickets x 3 runs (must stay at 0 got-worse)
  3. holdout2: the fixed mix on the 12 tickets that exposed the bug; reported, but no longer a fair test
  4. fresh3: 8 new tickets (committed before any run, 2 with lowercase IDs) x 3 runs: gpt-4 baseline,
     sol + patch (the current recommendation) and the fixed mix. This is the real test, kept small to save cost.
Writes reports/mix_retest/result.json.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import llm  # noqa: E402
import support_agent as A  # noqa: E402
from assign import new_prompts, usage_per_step  # noqa: E402
from compare import build_report  # noqa: E402
from stepfinder import plan_of, run_config, spec  # noqa: E402

RESULTS = HERE / "results"
OUT = HERE / "reports" / "mix_retest"
RUNS, WORKERS = 3, 6
FIX_LINE = 3  # classify: "Extract the order ID ..."
FIXED = ("Extract the order ID if the ticket gives one: the letter A followed by 4 digits (e.g. A1234). Customers "
         "may type it in lowercase or with a #; return it in uppercase without the # (a1234 -> A1234). "
         "Use null if there is none.")


def blank_source(tickets):
    """run_config wants a source row per (ticket, run); with first_live=0 nothing is replayed from it."""
    return {t["id"]: {r: {"steps": [], "config": "none"} for r in range(1, RUNS + 1)} for t in tickets}


def summary(old_path, path):
    rep = build_report(old_path, path)
    return {"file": Path(path).name, "verdict_counts": rep["verdict_counts"], "tickets": rep["inputs_compared"],
            "regressed": [it["input_id"] for it in rep["items"] if it["verdict"] == "REGRESSED"],
            "passed_all": rep["candidate"]["passed_all"], "gold": rep["candidate"]["accuracy"],
            "gold_old": rep["baseline"]["accuracy"], "old_unstable": rep["baseline"]["unstable_inputs"],
            "errors": sum(1 for l in open(path) if json.loads(l).get("error"))}


def main():
    prompts, patch, tag = new_prompts()
    old_line = A.PROMPTS["classify"][FIX_LINE]
    assert old_line.startswith("Extract the order ID"), old_line
    classify = list(A.PROMPTS["classify"])
    classify[FIX_LINE] = FIXED
    mix_prompts = {**prompts, "classify": classify}
    luna = spec("gpt-5.6-luna", {}, mix_prompts, "gpt-5.6-luna-nppc")
    llama = spec("ollama/llama3.1:8b", {"temperature": 0.0}, mix_prompts, "ollama-llama3.1-8b-nppc")
    sol = spec("gpt-5.6-sol", {}, prompts, "gpt-5.6-sol-npp")
    gpt4 = spec("gpt-4", {"temperature": 0.0}, A.unplanted_prompts(), "gpt-4-np")
    mix_plan = plan_of(luna, {"tone": llama})
    out = {"fix": {"step": "classify", "line_index": FIX_LINE, "old": old_line, "new": FIXED,
                   "written_by": "engineer edit, from the t42 failure"},
           "mix": {"classify": luna["model"], "decide": luna["model"], "draft": luna["model"], "tone": llama["model"]},
           "synthetic": {"tickets": True, "regression": False}}

    # 2. dev
    dev = A.load_tickets(sets=("agent",))
    path = run_config(dev, "pool-mix-fixed", mix_plan, blank_source(dev), 0, RUNS, RESULTS, WORKERS)
    out["dev"] = summary(RESULTS / "agent__gpt-4__noplant.jsonl", path)
    print(f"[dev] fixed mix: {out['dev']['verdict_counts']}")

    # 3. holdout2 (seen: it exposed the bug)
    ho2 = A.load_tickets(sets=("agent_holdout2",))
    path = run_config(ho2, "pool-mix-fixed__holdout2", mix_plan, blank_source(ho2), 0, RUNS, RESULTS, WORKERS)
    out["holdout2_seen"] = summary(RESULTS / "agent__gpt-4__noplant__holdout2.jsonl", path)
    print(f"[holdout2, no longer fresh] fixed mix: {out['holdout2_seen']['verdict_counts']}")

    # 4. fresh3
    fr = A.load_tickets(sets=("agent_fresh3",))
    g4 = run_config(fr, "gpt-4__noplant__fresh3", plan_of(gpt4, {}), blank_source(fr), 0, RUNS, RESULTS, WORKERS)
    sp = run_config(fr, f"gpt-5.6-sol-np__patch-{tag}__fresh3", plan_of(sol, {}), blank_source(fr), 0, RUNS, RESULTS, WORKERS)
    mx = run_config(fr, "pool-mix-fixed__fresh3", mix_plan, blank_source(fr), 0, RUNS, RESULTS, WORKERS)
    out["fresh3"] = {"tickets": len(fr), "runs": RUNS, "gpt4_file": g4.name,
                     "sol_patch": summary(g4, sp), "mix": summary(g4, mx)}
    m = out["fresh3"]["mix"]
    noise = m["old_unstable"] / max(1, m["tickets"])
    m["accepted"] = m["verdict_counts"]["REGRESSED"] == 0 and m["errors"] == 0 and m["gold"] >= m["gold_old"] - noise
    per = usage_per_step([RESULTS / out["dev"]["file"], mx])
    out["cost"] = {"per_step": per, "api_usd_per_1k": round(sum(v["usd_per_1k"] or 0 for v in per.values()), 4),
                   "self_hosted_tokens_per_ticket": round(sum(v["prompt_tokens"] + v["completion_tokens"]
                                                              for v in per.values() if v["usd_per_1k"] is None), 1)}
    print(f"[fresh3] sol + patch: {out['fresh3']['sol_patch']['verdict_counts']}   "
          f"fixed mix: {m['verdict_counts']} accepted={m['accepted']}   "
          f"mix API ${out['cost']['api_usd_per_1k']}/1k + {out['cost']['self_hosted_tokens_per_ticket']} self-hosted tokens/ticket")
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"-> {OUT / 'result.json'}   spent ${llm.spent_usd():.2f}")


if __name__ == "__main__":
    main()
