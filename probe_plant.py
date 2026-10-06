"""Choose the planted break for the demo agent, honestly: try candidate prompt lines on the
decide step only (upstream fixed to gold step-1 output) on both models, and keep a line that
breaks the new model but not the old one. Results -> reports/agent/plant_probe.json.

  python probe_plant.py --candidate gpt-5.6-sol --candidate-config '{}'
"""
import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import support_agent as A

HERE = Path(__file__).parent
# Each plant: (index to insert at in PROMPTS["decide"], line). Both are plausible prompt drift.
PLANTS = {
    "none": None,
    "A_final_sale": (4, "Final-sale items are not eligible for refunds or replacements."),
    "B_keep_happy": (2, "Keep customers happy: when a case is borderline or the customer is upset, "
                        "lean toward the customer."),
}
PROBE_TICKETS = ["t02", "t03", "t05", "t07", "t14", "t19"]


def decide_once(ticket, lines, model, params):
    g = ticket["gold"]
    ctx = {"ticket": ticket}
    A.advance("classify", ctx, json.dumps({"category": g["category"], "order_id": g["order_id"]}))
    out = A.call_openai(model, params, A.render("decide", ctx, lines))
    parsed, _ = A.parse_json(out["raw"])
    return (parsed or {}).get("action"), out["usage"], out["raw"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", default="gpt-5.6-sol")
    ap.add_argument("--candidate-config", type=json.loads, default={})
    ap.add_argument("--runs", type=int, default=2)
    args = ap.parse_args()
    tickets = {t["id"]: t for t in A.load_tickets()}
    models = {"gpt-4": {"temperature": 0.0}, args.candidate: args.candidate_config}
    jobs = []
    for plant, spec in PLANTS.items():
        lines = list(A.PROMPTS["decide"])
        if spec:
            lines.insert(*spec)
        for tid in PROBE_TICKETS:
            for model, params in models.items():
                for r in range(args.runs):
                    jobs.append((plant, tid, model, r, lines, params))
    with ThreadPoolExecutor(6) as pool:
        res = list(pool.map(lambda j: (j[:4], decide_once(tickets[j[1]], j[4], j[2], j[5])), jobs))
    rows = [{"plant": p, "ticket": t, "model": m, "run": r, "action": a, "gold": tickets[t]["gold"]["action"],
             "usage": u, "raw": raw} for (p, t, m, r), (a, u, raw) in res]
    cost = sum(A.cost(r["model"], r["usage"]) or 0 for r in rows)
    print(f"{len(rows)} calls, gpt-4 cost ${cost:.2f}\n")
    print(f"{'plant':<14}{'model':<14}" + "".join(f"{t:>9}" for t in PROBE_TICKETS) + "   correct")
    for plant in PLANTS:
        for model in models:
            sub = [r for r in rows if r["plant"] == plant and r["model"] == model]
            cells = []
            for tid in PROBE_TICKETS:
                acts = Counter(r["action"] for r in sub if r["ticket"] == tid)
                ok = sum(n for a, n in acts.items() if a == tickets[tid]["gold"]["action"])
                cells.append(f"{ok}/{sum(acts.values())}")
            print(f"{plant:<14}{model:<14}" + "".join(f"{c:>9}" for c in cells)
                  + f"   {sum(r['action'] == r['gold'] for r in sub)}/{len(sub)}")
    out = HERE / "reports" / "agent" / "plant_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"plants": {k: v[1] if v else None for k, v in PLANTS.items()},
                               "tickets": PROBE_TICKETS, "rows": rows}, indent=2))
    print(f"\n-> {out}")


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
