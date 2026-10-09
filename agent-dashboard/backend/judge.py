"""LLM judge for CHANGED items: a second opinion when the old and new model disagree but neither
the hard checks nor the gold labels say the new output is worse.

- Different provider from the candidates (they are OpenAI): Claude, via the Anthropic SDK.
- Both orders: every pair is judged as (old, new) and as (new, old). A preference counts only if
  it survives the swap; otherwise the verdict is "inconsistent" (position bias), not a regression.
- Old vs old: the same judge compares two of the old model's own runs. How often it "prefers" one
  of two outputs from the same model is the judge's noise floor, reported next to its verdicts.
- Optional: with no Anthropic credentials the judge is skipped and everything else still works.
Every call is cached in results/judge__<model>.jsonl, so re-runs and offline replays are free.

  python judge.py reports/migration_ner/compare_gpt-5.6-sol.json        # judge CHANGED items
  python judge.py REPORT --dry-run                                      # show a prompt + call count
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
DEFAULT_MODEL = "claude-opus-5-5"
SYSTEM = (
    "You review outputs from an automated pipeline during a model migration. You get the task "
    "instructions, one input, and two outputs (A and B) for that input. Decide which output follows "
    "the instructions better for this input. Judge only correctness against the instructions; ignore "
    "wording, order and formatting differences that the instructions don't care about. If both are "
    "equally correct, or equally wrong, answer \"tie\"."
)
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"winner": {"type": "string", "enum": ["A", "B", "tie"]},
                   "reason": {"type": "string"}},
    "required": ["winner", "reason"],
    "additionalProperties": False,
}
VERDICTS = ("old_better", "new_better", "tie", "inconsistent")


def has_credentials():
    """True if the Anthropic SDK can find credentials (env vars or an `ant auth login` profile)."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()


def instructions(task):
    """The task instructions the judge holds both outputs to."""
    if task == "ner":
        from migrate import editable_components
        comps = editable_components(task)
        defs = "\n".join(f"- {k.split('.', 1)[1]}: {v}" for k, v in comps.items() if k.startswith("label_"))
        return (f"Extract named entities with labels DISH, INGREDIENT, EQUIPMENT.\n{comps['description']}\n"
                f"Label definitions:\n{defs}")
    if task == "textcat":
        return "Classify the text as exactly one of: COMPLIMENT, INSULT."
    import support_agent as A
    return ("A customer-support pipeline classifies a ticket, applies the store policy to decide an action, "
            "and writes the reply. Decision step instructions:\n" + "\n".join(A.PROMPTS["decide"])
            + "\nReply instructions:\n" + "\n".join(A.PROMPTS["draft"]))


def render_output(task, row):
    if task == "ner":
        return ", ".join(f"{e['text']} -> {e['label']}" for e in row["output"]) or "(no entities)"
    if task == "textcat":
        return str(row["gold_score"]["pred"])
    d = row.get("decision") or {}
    return f"category: {row.get('category')}; action: {d.get('action')}\nreply:\n{row.get('final_reply')}"


def user_prompt(task, text, a, b):
    return (f"Task instructions:\n{instructions(task)}\n\nInput:\n{text}\n\n"
            f"Output A:\n{a}\n\nOutput B:\n{b}\n\nWhich output follows the instructions better for this input?")


def call_claude(model, system, user):
    """One verdict from Claude. Returns ({winner, reason}, usage dict)."""
    import anthropic
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # a classifier decline re-runs on Anthropic's recommended fallback model
    )
    if response.stop_reason == "refusal":
        return {"winner": None, "reason": "refused"}, response.usage.to_dict()
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text), response.usage.to_dict()


class Judge:
    def __init__(self, model=DEFAULT_MODEL, call=call_claude, cache_dir=HERE / "results"):
        self.model, self.call = model, call
        self.cache_path = Path(cache_dir) / f"judge__{model}.jsonl"
        self.cache = {}
        if self.cache_path.exists():
            for line in open(self.cache_path):
                r = json.loads(line)
                self.cache[r["key"]] = r
        self.calls = 0

    def ask(self, task, text, a, b):
        user = user_prompt(task, text, a, b)
        key = hashlib.sha256(json.dumps([self.model, SYSTEM, user]).encode()).hexdigest()[:16]
        if key not in self.cache:
            verdict, usage = self.call(self.model, SYSTEM, user)
            rec = {"key": key, "model": self.model, "winner": verdict.get("winner"), "reason": verdict.get("reason"),
                   "usage": usage, "ts": datetime.now(timezone.utc).isoformat()}
            with open(self.cache_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            self.cache[key] = rec
            self.calls += 1
        return self.cache[key]

    def compare(self, task, text, old, new):
        """Judge (old, new) in both orders. Returns old_better / new_better / tie / inconsistent."""
        first = self.ask(task, text, old, new)    # A = old, B = new
        second = self.ask(task, text, new, old)   # A = new, B = old
        pick = {("A", "B"): "old_better", ("B", "A"): "new_better", ("tie", "tie"): "tie"}
        return {"verdict": pick.get((first["winner"], second["winner"]), "inconsistent"),
                "order_old_first": first["winner"], "order_new_first": second["winner"],
                "reasons": [first["reason"], second["reason"]]}


def most_common_output(task, rows):
    return Counter(render_output(task, r) for r in rows).most_common(1)[0][0]


def judge_report(report, judge, base_rows, cand_rows, noise_pairs=5):
    """Annotate CHANGED items with judge verdicts and add an old-vs-old noise floor. In place."""
    task = report["task"]
    changed = [it for it in report["items"] if it["verdict"] == "CHANGED"]
    for it in changed:
        iid = it["input_id"]
        it["judge"] = {"model": judge.model, **judge.compare(task, it["text"], most_common_output(task, base_rows[iid]),
                                                             most_common_output(task, cand_rows[iid]))}
    # Noise floor: every pair of differing old-model runs, plus a few identical pairs (a judge that
    # "prefers" one of two identical outputs is showing pure position bias).
    noise, identical = [], 0
    for iid, rows in sorted(base_rows.items()):
        outs = list(dict.fromkeys(render_output(task, r) for r in rows))
        if len(outs) == 1:
            if identical >= noise_pairs:
                continue
            identical += 1
            outs = outs * 2
        noise.append({"input_id": iid, "identical": outs[0] == outs[1],
                      **judge.compare(task, rows[0]["text"], outs[0], outs[1])})
    counts = lambda xs: {v: sum(x["verdict"] == v for x in xs) for v in VERDICTS}
    report["judge_summary"] = {
        "model": judge.model, "provider": "anthropic", "changed_items": len(changed),
        "verdicts": counts([it["judge"] for it in changed]),
        "noise_floor": {"pairs": len(noise), "verdicts": counts(noise),
                        "false_preference_rate": (sum(x["verdict"] in ("old_better", "new_better") for x in noise)
                                                  / len(noise)) if noise else None,
                        "items": noise},
        "rule": "A preference counts only if it holds in both orders; old-vs-old pairs measure how often "
                "the judge prefers one of two outputs from the same model.",
    }
    return report


def load_rows(report, results):
    from compare import load
    base_files = [Path(results) / f.strip() for f in report["baseline_file"].split(" + ")]
    return (load(base_files if len(base_files) > 1 else base_files[0]),
            load(Path(results) / report["candidate_file"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="a compare_*.json report (compare.py --json / migrate.py output)")
    ap.add_argument("--judge-model", default=DEFAULT_MODEL)
    ap.add_argument("--results", default=str(HERE / "results"))
    ap.add_argument("--dry-run", action="store_true", help="print one prompt and the call count; no API calls")
    args = ap.parse_args()
    report = json.loads(Path(args.report).read_text())
    changed = [it for it in report["items"] if it["verdict"] == "CHANGED"]
    if args.dry_run:
        sample = changed[0]["text"] if changed else report["items"][0]["text"]
        print(f"--- system ---\n{SYSTEM}\n--- user ---\n{user_prompt(report['task'], sample, '<old output>', '<new output>')}")
        print(f"\n{len(changed)} CHANGED items -> {2 * len(changed)} calls, plus 2 per old-vs-old pair (cached after)")
        return
    if not has_credentials():
        print("Judge skipped: no Anthropic credentials (set ANTHROPIC_API_KEY or run `ant auth login`).")
        return
    judge = Judge(args.judge_model, cache_dir=args.results)
    judge_report(report, judge, *load_rows(report, args.results))
    out = Path(args.report).with_name(Path(args.report).stem + "_judged.json")
    out.write_text(json.dumps(report, indent=2, default=list))
    s = report["judge_summary"]
    print(f"{s['changed_items']} CHANGED items judged by {s['model']}: {s['verdicts']}")
    print(f"noise floor ({s['noise_floor']['pairs']} old-vs-old pairs): {s['noise_floor']['verdicts']}")
    print(f"{judge.calls} new API calls -> {out}")


if __name__ == "__main__":
    main()
