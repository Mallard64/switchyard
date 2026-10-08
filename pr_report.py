"""Render a migration PR description from a migration dict (the same data as migration.json).

Order is fixed, so a reviewer can approve in ~5 minutes:
  summary -> 1. where (step) -> 2. what (lines) -> 3. evidence -> 4. fixes (each separate,
  with its own diff, verification, and accept / edit / reject status) -> other differences -> limits.
Only fixes with decision "accepted" or "edited" go into the final diff; "pending" ones are shown
as proposals and "rejected" ones are listed but excluded.
"""
import difflib

from pipeline_view import matrix_markdown

DECISION_LABEL = {"pending": "awaiting review", "accepted": "accepted", "edited": "accepted with engineer edits",
                  "rejected": "rejected"}


def fix_diff(fix):
    old = (fix.get("old_text") or "").splitlines() or [""]
    new = (fix.get("new_text") or "").splitlines() if fix.get("new_text") else []
    if fix.get("kind") == "remove_line":
        return [f"- {l}" for l in old]
    body = [l for l in difflib.unified_diff(old, new, lineterm="", n=0)][2:]
    return [l for l in body if not l.startswith("@@")] or [f"- {l}" for l in old]


def live_fixes(m):
    return [f for f in m["fixes"] if f["decision"] in ("accepted", "edited")]


def render(m):
    old, new = m["old_model"], m["new_model"]
    fixes, causes = m["fixes"], m["causes"]
    out = [f"# {m['title']}", ""]

    # Summary ------------------------------------------------------------------------------
    n_reg = len(causes)
    best = next((f for f in fixes if f["decision"] in ("accepted", "edited")), None) or \
        next((f for f in fixes if f["decision"] == "pending" and f["verification"]["passes"]), None)
    out += ["## Summary", ""] + [f"- {w}" for w in m["why"]]
    out.append(f"- Swapping `{old}` for `{new}` regressed **{n_reg}** of {m['n_inputs']} inputs"
               + (" (beyond the old model's own run-to-run variation)." if n_reg else "."))
    if m.get("steps") and n_reg:
        top = max(m["steps"]["summary"].items(), key=lambda kv: kv[1]["explains"])
        out.append(f"- Step **`{top[0]}`** explains {top[1]['explains']}/{top[1]['of']} regressions "
                   f"({top[1]['pct']}%), proven by swapping one step at a time.")
    if best:
        v = best["verification"]
        out.append(f"- Proposed fix `{best['id']}` ({best['summary']}) leaves **{v['verdict_counts']['REGRESSED']}** "
                   f"regressions on a full re-run of all {m['n_inputs']} inputs.")
    out += [f"- {b}" for b in m.get("model_change", [])] + [""]

    # 1. Where -----------------------------------------------------------------------------
    out += ["## 1. Where it broke (step)", ""]
    if m.get("steps"):
        st = m["steps"]
        out += [st["method"], "", "| Step | Regressions it explains |", "|---|---|"]
        out += [f"| `{s}` | {v['explains']}/{v['of']} ({v['pct']}%) |" for s, v in st["summary"].items()]
        out += ["", "● = new model ran that step, ○ = old model. ✓ = the ticket came out like the old pipeline."]
        for n, c in enumerate(causes):
            if c.get("experiments"):
                table = ["", f"**`{c['input_id']}`**", ""] + matrix_markdown(c["experiments"])
                # First ticket in full; the rest collapsed so the PR stays scannable.
                out += table if n == 0 else ["", f"<details><summary>{c['input_id']}: same experiments</summary>"] + \
                    table + ["", "</details>"]
            elif c.get("step_evidence"):
                out += ["", f"`{c['input_id']}`:"] + [f"- {row}" for row in c["step_evidence"]]
        out.append("")
    else:
        out += [f"This pipeline has one LLM step ({m['pipeline']}), so every regression is in that step.", ""]

    # 2. What ------------------------------------------------------------------------------
    out += ["## 2. What broke (prompt lines)", ""]
    for c in causes:
        out.append(f"**{c['input_id']}**: \"{c['text']}\"")
        if c.get("confirmed_lines"):
            for u in c["confirmed_lines"]:
                out.append(f"- **Proven line** in `{u['component']}`: \"{u['text']}\"")
            out.append(f"  - Removing only this line makes the input stop regressing on `{new}`; removing any other "
                       f"line of the same prompt does not ({c['lines_tested']} lines tested, 3 runs each).")
        elif c["status"] == "confirmed":
            out.append(f"- No single line's removal repairs it (likely a *missing* instruction rather than a wrong one). "
                       f"Proven at component level: `{c['confirmed_component']}`, because editing only that component "
                       f"removes the regression ({c['confirmed_by']}).")
        else:
            sus = ", ".join(f"`{s['component']}`" for s in c.get("suspects", [])[:2])
            out.append(f"- **Not proven.** Suspected: {sus or 'unknown'}. Neither line removal nor a fix has confirmed it.")
        out.append("")

    # 3. Evidence --------------------------------------------------------------------------
    out += ["## 3. Evidence", "", "### Before / after", ""]
    for c in causes:
        out.append(f"- `{c['input_id']}`: `{old}` → {c['baseline_output']} · `{new}` → {c['candidate_output']}"
                   + (f" · `{new}` + fix → {c['fixed_output']}" if c.get("fixed_output") else ""))
    out += ["", "### Scores", "", f"| | `{old}` (baseline) | `{new}`, model swap only | `{new}` + fix |", "|---|---|---|---|"]
    out += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |" for r in m["evidence"]["table"]]
    out += ["", f"**How much the old model varies on its own:** {m['evidence']['noise_floor']}", ""]

    # 4. Fixes -----------------------------------------------------------------------------
    out += ["## 4. Proposed fixes", "",
            "Each fix is separate. Review each and accept, edit or reject it; only accepted or edited fixes are "
            "applied. An edit is re-verified on the full suite before it counts.", ""]
    if not fixes:
        out += ["No fix proposed.", ""]
    for f in fixes:
        v = f["verification"]
        out += [f"### `{f['id']}`: {f['summary']} ({DECISION_LABEL[f['decision']]})", "",
                f"Where: step `{f['step']}`, `{f['component']}`. Source: {f['source']}."
                + (f" Rationale: {f['rationale']}" if f.get("rationale") else ""), "",
                "```diff", *fix_diff(f), "```", ""] + ([f"_{f['caution']}_", ""] if f.get("caution") else []) + [
                f"Full re-run with this fix ({v['inputs']} inputs × {v['runs']} runs on `{new}`): "
                f"REGRESSED {v['verdict_counts']['REGRESSED']}, IMPROVED {v['verdict_counts']['IMPROVED']}, "
                f"CHANGED {v['verdict_counts']['CHANGED']}, SAME {v['verdict_counts']['SAME']}."
                + (f" Still regressed: {v['still_regressed']}." if v["still_regressed"] else "")
                + (f" **Newly regressed: {v['newly_regressed']}.**" if v["newly_regressed"] else ""), ""]
        if f["decision"] == "pending":
            out += [f"Accept: `{m['cli']} --accept {f['id']}` · Reject: `{m['cli']} --reject {f['id']}` · "
                    f"Edit: `{m['cli']} --apply-edit my_edit.json` (see migration.json `edit_format`)", ""]
    rejected = [f["id"] for f in fixes if f["decision"] == "rejected"]
    if rejected:
        out += [f"Rejected and not applied: {', '.join(f'`{r}`' for r in rejected)}.", ""]

    if m.get("other_differences"):
        out += ["## Other differences (not regressions; worth a look)", ""] + [f"- {d}" for d in m["other_differences"]] + [""]
    out += ["## Limits", ""] + [f"- {l}" for l in m["limits"]]
    return "\n".join(out).rstrip() + "\n"
