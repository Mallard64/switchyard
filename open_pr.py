"""Open the migration pull request in the app repo (config/target_repo.yml) with the changes a reviewer kept.

  echo '{"changes": [{"id": "model", "decision": "accepted"}, {"id": "line0", "decision": "accepted"}]}' | python open_pr.py

Called by the dashboard (POST /api/create-pr, slide 4). Change ids match the PR slide: "model" is the model swap
on every step, "line<i>" is the i-th prompt edit from reports/demo/results.json. Decisions: "accepted" applies the
change as tested, "edited" applies the reviewer's text (marked untested in the PR), anything else leaves it out.

Clones the repo into a temp folder, checks each prompt line still says what upshift tested, commits on
upshift/<model>-<hash of the applied changes>, pushes and runs `gh pr create`. The branch name depends only on
the applied changes, so asking again for the same changes returns the PR that is already open.
Prints JSON: {"url", "branch", "existed", "applied", "skipped"} or {"error"}.
"""
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).parent
INCLUDED = {"accepted", "edited"}


def sh(*cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"{cmd[0]} {cmd[1]} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout.strip()


def plan(changes, demo):
    """Which changes to apply, with the text to apply. Returns (applied, skipped)."""
    by_id = {c.get("id"): c for c in changes}
    defs = [{"id": "model", "kind": "model", "old": demo["pair"]["old"], "new": demo["pair"]["new"]}]
    defs += [{"id": f"line{i}", "kind": "line", "step": demo["patch"]["step"], "line_index": e["line_index"],
              "old": e["old"], "new": e["new"]} for i, e in enumerate(demo["patch"]["edits"])]
    applied, skipped = [], []
    for d in defs:
        c = by_id.get(d["id"], {})
        decision = c.get("decision")
        if decision not in INCLUDED:
            skipped.append({**d, "decision": decision or "pending"})
            continue
        text = d["new"]
        if decision == "edited":
            text = str(c.get("text") or "").strip()
            if d["kind"] == "model":
                m = re.search(r"model:\s*([\w.\-:/]+)", text)
                if not m:
                    raise ValueError("the edited model change must contain 'model: <name>'")
                text = m.group(1)
            if not text:
                raise ValueError(f"the edited text for {d['id']} is empty")
        applied.append({**d, "decision": decision, "apply": text})
    return applied, skipped


def apply(repo_dir, target, applied):
    for ch in applied:
        if ch["kind"] == "model":
            path = repo_dir / target["config_file"]
            text = path.read_text()
            if f"model: {ch['old']}" not in text:
                raise RuntimeError(f"{target['config_file']} no longer uses {ch['old']}; nothing to migrate")
            text = text.replace(f"model: {ch['old']}", f"model: {ch['apply']}")
            # The new models reject temperature 0 and run at their default sampling.
            text = re.sub(r"^\s*temperature:\s*0(\.0)?\s*\n", "", text, flags=re.M)
            path.write_text(text)
        else:
            path = repo_dir / target["prompts_dir"] / f"{ch['step']}.txt"
            lines = path.read_text().split("\n")
            i = ch["line_index"]
            if i >= len(lines) or lines[i] != ch["old"]:
                raise RuntimeError(f"{path.name} line {i + 1} has changed since upshift tested it; re-run upshift first")
            lines[i] = ch["apply"]
            path.write_text("\n".join(lines))


def body(data, target, applied, skipped):
    demo = data["demo"]
    c, p, t = demo["confidence"], demo["patch"], demo["totals_usd_per_1k"]
    after = data["candidates"][0]["summary"]["verdict_counts_after_fix"]["REGRESSED"]
    rows = []
    for ch in applied:
        what = (f"Model `{ch['old']}` → `{ch['apply']}` on all 4 steps (`temperature: 0` removed: the new model rejects it)"
                if ch["kind"] == "model" else f"`{target['prompts_dir']}/{ch['step']}.txt` line {ch['line_index'] + 1}")
        note = " **Edited by the reviewer; not re-tested.** Re-run upshift before merging." if ch["decision"] == "edited" else ""
        rows.append(f"- {what}.{note}")
        if ch["kind"] == "line":
            rows.append(f"  ```diff\n  - {ch['old']}\n  + {ch['apply']}\n  ```")
    left_out = "".join(f"\n- Not included ({ch['decision']}): {ch['id']}" for ch in skipped)
    pool = demo.get("pool") or {}
    later = [pool.get("sol_patch_holdout2"), ((pool.get("retest") or {}).get("fresh3") or {}).get("sol_patch")]
    later = ", ".join(f"{x['regressed']} of {x['of']}" for x in later if x)
    repaired = next(s["repair"]["n"] for s in demo["per_step"] if s["guilty"])
    return f"""## Why
`{demo['pair']['old']}` shuts down on Oct 23, 2026. This moves the agent to `{demo['pair']['new']}`.

## What changes
{chr(10).join(rows)}{left_out}

## What upshift found
- Switching only the model, the **tone** step started deleting the "5–7 business days" refund timeline from replies.
- Putting `{demo['pair']['old']}` back at the tone step alone fixes {repaired}/{c['regressed_dev']} broken tickets; no other step fixes any.
- Removing tone line {p['edits'][0]['line_index'] + 1} alone fixes them too; the prompt change above rewrites that line.

## Evidence
- Tickets that got worse: {c['regressed_dev'] + c['regressed_heldout_before']} → {after} of {c['dev_tickets'] + c['heldout_tickets']} ({c['runs_per_ticket']} runs each), including {p['heldout_before']['REGRESSED']} → {p['heldout_after']['REGRESSED']} on {c['heldout_tickets']} tickets the fix never saw.{f' Later fresh tests: {later}.' if later else ''}
- Replies passing every check: {demo['quality']['old']['passed_all']:.1%} before, {demo['quality']['new_fixed']['passed_all']:.1%} after.
- Model cost per 1,000 tickets: ${t['all_old']:.2f} → ${demo['recommended']['usd_per_1k']:.2f}.
- The tickets are hand-written test tickets; the model outputs are real recorded runs.

Opened by upshift from the reviewer's decisions on the dashboard.
"""


def main():
    req = json.load(sys.stdin)
    target = yaml.safe_load((HERE / "config" / "target_repo.yml").read_text())
    data = json.loads((HERE / "reports" / "demo" / "results.json").read_text())
    demo = data["demo"]
    applied, skipped = plan(req.get("changes") or [], demo)
    if not applied:
        raise ValueError("accept or edit at least one change first")
    digest = hashlib.sha256(json.dumps([[c["id"], c["apply"]] for c in applied]).encode()).hexdigest()[:8]
    branch = f"upshift/{demo['pair']['new']}-{digest}"
    repo = target["repo"]
    out = {"branch": branch, "repo": repo, "applied": [c["id"] for c in applied], "skipped": [c["id"] for c in skipped]}

    existing = json.loads(sh("gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "url") or "[]")
    if existing:
        print(json.dumps({**out, "url": existing[0]["url"], "existed": True}))
        return
    with tempfile.TemporaryDirectory(prefix="upshift-pr-") as tmp:
        d = Path(tmp) / "repo"
        sh("gh", "repo", "clone", repo, str(d), "--", "--depth", "1", "--branch", target["base"])
        sh("git", "checkout", "-b", branch, cwd=d)
        apply(d, target, applied)
        sh("git", "add", "-A", cwd=d)
        sh("git", "commit", "-m", f"Move the support agent from {demo['pair']['old']} to {demo['pair']['new']}", cwd=d)
        # upshift/* branches belong to upshift: overwrite one left behind by a closed PR.
        sh("git", "push", "--force", "-u", "origin", branch, cwd=d)
        title = (f"Move the support agent to {demo['pair']['new']}" +
                 ("; keep the refund timeline in the tone check" if any(c["kind"] == "line" for c in applied) else ""))
        bf = Path(tmp) / "body.md"
        bf.write_text(body(data, target, applied, skipped))
        url = sh("gh", "pr", "create", "--repo", repo, "--base", target["base"], "--head", branch,
                 "--title", title, "--body-file", str(bf), cwd=d).splitlines()[-1]
    print(json.dumps({**out, "url": url, "existed": False}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # the dashboard shows this message
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
