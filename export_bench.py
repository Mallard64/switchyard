"""Export every tested planted break (agent_bench.py runs) as a Switchyard dashboard report, so the
dashboard shows whatever has been tested, not a hand-picked few.

Each break becomes reports/bench_<set>_<id>/dashboard.json: all 24 tickets old vs new, and for the
localized tickets the step-swap experiments. Rebuilt from cached runs only: the API key is removed
from this process, so a missing cache entry stops with an error instead of making a call.

  python export_bench.py
"""
import json
import os
import sys
from pathlib import Path

os.environ.pop("OPENAI_API_KEY", None)  # cached runs only; never spend from an export
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import dashboard_export  # noqa: E402
from agent_bench import BREAK_SETS  # noqa: E402
from compare import build_report  # noqa: E402
from pipeline_view import STEPS, experiments  # noqa: E402
from stepfinder import localize, spec  # noqa: E402

RESULTS = HERE / "results"
BENCHES = [  # (results.json, break set, picker group)
    (HERE / "reports" / "agent_bench" / "results.json", "v1", "Step-finder benchmark (sol vs terra)"),
    (HERE / "reports" / "agent_bench_downstream_gpt-5.6-terra_gpt-5.6-terra" / "results.json", "downstream",
     "Downstream-symptom breaks (terra)"),
]


def export_break(res, brk, detail, group):
    old, new = spec(res["old"], {}), spec(res["new"], {}, brk["prompts"], f"{res['new']}+{brk['id']}")
    base_path, cand_path = RESULTS / f"agent__{old['label']}.jsonl", RESULTS / f"agent__{new['label']}.jsonl"
    rep = build_report(base_path, cand_path)
    sample = detail["localized"]
    tickets = []
    if sample:
        sub = cand_path.with_name(cand_path.stem + f"__sample{len(sample)}.jsonl")
        tickets = localize(old, new, base_path, sub, res["runs"], RESULTS, 4)["tickets"]
    items = {it["input_id"]: it for it in rep["items"]}
    causes = [{"input_id": t["input_id"], "text": items[t["input_id"]]["text"],
               "baseline_output": items[t["input_id"]]["baseline"]["output"],
               "candidate_output": items[t["input_id"]]["candidate"]["output"], "causal_steps": t["causal_steps"],
               "status": "confirmed" if t["status"] == "confirmed" else "suspected", "confirmed_lines": [],
               "experiments": experiments(t, items[t["input_id"]]["baseline"]["output"],
                                          items[t["input_id"]]["candidate"]["output"])} for t in tickets]
    m = {"title": f"Planted break {brk['id']}: {brk['desc']}", "group": group,
         "label": f"{brk['id']} · {brk['desc'].split(' (')[0]}", "old_model": old["label"], "new_model": new["label"],
         "planted_steps": sorted(brk["prompts"], key=STEPS.index), "causes": causes, "fixes": [], "lines": [],
         "steps": None, "started": None, "finished": None}
    out = HERE / "reports" / f"bench_{detail['set']}_{brk['id']}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "compare_candidate.json").write_text(json.dumps(rep, indent=2, default=list))
    (out / "migration.json").write_text(json.dumps({**m, "comparison_file": "compare_candidate.json"}, indent=2))
    dashboard_export.export(out, m, rep, {})
    return out, rep["verdict_counts"]["REGRESSED"], sorted({s for t in tickets for s in t["causal_steps"]})


def main():
    for path, set_name, group in BENCHES:
        if not path.exists():
            continue
        res = json.loads(path.read_text())
        for detail in res["breaks_detail"]:
            brk = next(b for b in BREAK_SETS[set_name] if b["id"] == detail["id"])
            out, n, found = export_break(res, brk, {**detail, "set": set_name}, group)
            print(f"{out.name}: {n} regressed; planted {detail['planted_steps']}, step-finder found {found}")


if __name__ == "__main__":
    main()
