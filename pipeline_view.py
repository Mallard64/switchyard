"""Human-readable views of the support agent's pipeline, shared by the PR text, the dashboard export
and the demo script: what each step does, what it said for a ticket, and the step-swap experiments
as a grid (which model ran each step, and whether the ticket came out right)."""

STEP_INFO = {
    "classify": "Reads the ticket; picks a category and the order ID",
    "decide": "Looks up the order and policy; picks the action",
    "draft": "Writes the reply to the customer",
    "tone": "Checks the reply's tone; may rewrite it",
}
STEPS = list(STEP_INFO)


def step_summary(step, rec):
    """One short line for what a step produced (from a cached step record)."""
    p = rec.get("parsed")
    if step == "classify" and isinstance(p, dict):
        return f"category: {p.get('category')} · order {p.get('order_id') or 'none'}"
    if step == "decide" and isinstance(p, dict):
        amount = f" · ${p['amount']:.2f}" if isinstance(p.get("amount"), (int, float)) else ""
        return f"{p.get('action')}{amount} — {str(p.get('reason') or '').strip()[:90]}"
    if step == "draft":
        text = " ".join(str(rec.get("raw") or "").split())
        return text[:140] + ("…" if len(text) > 140 else "")
    if step == "tone" and isinstance(p, dict):
        return f"{p.get('verdict')}" + (f": {'; '.join(p.get('issues') or [])[:90]}" if p.get("issues") else "")
    return " ".join(str(rec.get("raw") or "").split())[:120]


def trace(row):
    """{step: summary} for one cached pipeline run (one row)."""
    return {s["step"]: step_summary(s["step"], s) for s in row["steps"]}


def first_divergence(old, new):
    """First of classify/decide whose decision differs between two traces (reply wording always differs)."""
    for s in STEPS[:2]:
        if old.get(s) and new.get(s) and old[s].split(" — ")[0] != new[s].split(" — ")[0]:
            return s
    return None


def experiments(tk, baseline_output, candidate_output):
    """The step-finder's experiments for one ticket as rows: {label, models: {step: old|new}, output, ok}.
    `ok` = the ticket came out like the old pipeline (not REGRESSED)."""
    rows = [{"label": "Old pipeline", "models": {s: "old" for s in STEPS}, "output": baseline_output, "ok": True,
             "kind": "reference"},
            {"label": "New pipeline", "models": {s: "new" for s in STEPS}, "output": candidate_output, "ok": False,
             "kind": "reference"}]
    for s, j in tk.get("necessity", {}).items():
        rows.append({"label": f"Swap {s} back to old", "models": {x: "old" if x == s else "new" for x in STEPS},
                     "output": j["output"], "ok": j["verdict"] != "REGRESSED", "kind": "swap_back"})
    for pair, j in tk.get("pairs", {}).items():
        both = pair.split("+")
        rows.append({"label": f"Swap {' + '.join(both)} back to old", "kind": "swap_back",
                     "models": {x: "old" if x in both else "new" for x in STEPS},
                     "output": j["output"], "ok": j["verdict"] != "REGRESSED"})
    for key, j in tk.get("sufficiency", {}).items():
        only = key.split("+")
        rows.append({"label": f"Only {' + '.join(only)} new", "kind": "only_new",
                     "models": {x: "new" if x in only else "old" for x in STEPS},
                     "output": j["output"], "ok": j["verdict"] != "REGRESSED"})
    return rows


def matrix_markdown(rows):
    """A compact markdown grid of the experiments (● new model, ○ old model)."""
    out = ["| Experiment | " + " | ".join(STEPS) + " | Result |", "|---|" + "---|" * len(STEPS) + "---|"]
    for r in rows:
        cells = " | ".join("● new" if r["models"][s] == "new" else "○ old" for s in STEPS)
        out.append(f"| {r['label']} | {cells} | {'✓' if r['ok'] else '✗'} {r['output']} |")
    return out


def readable(pattern):
    """A must_include regex as plain words: "order (number|id)" -> "order number/id", "\\$79\\.00" -> "$79.00"."""
    import re
    text = re.sub(r"\[([^\]])[^\]]*\]", r"\1", pattern)                 # [.,] -> .
    text = re.sub(r"\(([^)]*)\)", lambda m: m.group(1).replace("|", "/"), text)
    return f"\"{text.replace(chr(92), '')}\""


def expected(step, ticket):
    """What a step should produce for this ticket, from its gold labels."""
    g = ticket["gold"]
    if step == "classify":
        return f"category: {g['category']} · order {g['order_id'] or 'none'}"
    if step == "decide":
        return f"action: {g['action']}"
    if step == "draft":
        must = ", ".join(map(readable, ticket.get("must_include") or [])) or "(nothing specific)"
        never = ", ".join(map(readable, ticket.get("must_not_include") or [])) or "(nothing specific)"
        return f"A reply that states the {g['action']} decision. Must mention: {must}. Must not say: {never}."
    return "PASS, or a rewrite that keeps every fact (order ID, amounts, timelines)."


def cell(rec, old_rec=None):
    """Hover detail for one step of one run: the prompt it got, its raw output, and which system-prompt
    lines differ from the old pipeline's prompt for the same step."""
    msgs = rec.get("messages") or []
    system = next((m["content"] for m in msgs if m["role"] == "system"), "")
    user = next((m["content"] for m in msgs if m["role"] == "user"), "")
    old_lines = set()
    if old_rec:
        old_lines = set(next((m["content"] for m in old_rec.get("messages") or [] if m["role"] == "system"), "").split("\n"))
    lines = system.split("\n")
    return {"model": rec.get("model"), "system": lines, "user": user, "output": rec.get("raw") or "",
            "changed": [i for i, l in enumerate(lines) if old_rec and l not in old_lines]}
