# Model-migration tool: hackathon project

## What this is
This is a tool that migrates an LLM pipeline off a retiring model and proves nothing broke. It works in five steps:

1. Replay real inputs on the old model and on candidate models.
2. Score both with hard checks and gold labels.
3. Find the prompt component behind each regression.
4. Have an LLM fix only that component, then re-run everything to prove the fix.
5. Write a pull request with the evidence.

Pitch line: **"Migration tools exist. They rewrite your prompts and hope. We prove what broke and change only that."**

## Positioning (decided Oct 6; build to this)
- **Scope: migration only.** Not an eval platform, not a router. We sit alongside both: evals detect *that* quality dropped, routers pick models, we make the switch safe and explain *why* it broke.
- **Migration is contested.** Not Diamond (prompt adaptation, GA Jan 2026, explicitly targets model upgrades), AWS Bedrock's migration tool, and Braintrust Loop all do "new model → better prompt". They rewrite the **whole** prompt via search and score the app end to end. Never write "nobody does this" in the PR, README or demo copy.
- **Our two differentiators — every feature should serve these:**
  1. **Step-finder:** in a multi-step agent, swap the model one step at a time to *prove* which step the new model broke. (LLMs asked "which step failed?" are right ~14–29% of the time on Who&When.)
  2. **Line-level causal fix:** prove which prompt lines broke (ablation, not heuristics) and ship a **minimal diff**, not a rewrite.
- **Human-in-the-loop, not autopilot.** The tool investigates; an engineer decides. The PR/report is the product: *where* (step), *what* (lines), *proof* (before/after, old-model noise floor), *fix* (smallest diff). Per-fix accept / edit / reject.
- **Demo order:** lead with the step-finder on a multi-step agent (single-prompt apps are where competitors are strongest), then the line-level fix, then the PR.

The demo target is [explosion/spacy-llm](https://github.com/explosion/spacy-llm) (about 1.4k stars, actively maintained). Its example configs use `gpt-3.5-turbo` (`spacy.GPT-3-5.v1`), and `spacy.GPT-4.v3` defaults to `"gpt-4"`. Both shut down on **Oct 23, 2026**.

## Deadlines and team
- **Hackathon submission:** Sat Oct 10, 11:59 PM. It needs the repo, a demo video and a one-paragraph thesis. Aim for early evening.
- **Office hours:** Wed and Thu, 6–8 PM.
- **Table pitch:** Sun Oct 11, 1:30 PM. It must work offline from cached results.
- **gpt-4 shuts down Oct 23.** The baseline files in `results/` can't be regenerated after that, so never edit or delete them.
- **Team:**
  - The user owns all backend and technical work.
  - Iris builds the dashboard, which reads `reports/migration_ner/migration.json`.
  - Claire owns the pitch and market analysis.
  - Nash runs customer interviews.

## Update (Oct 6, evening)
- Original tasks 1–4 are **done and committed**: git repo + `.gitignore`, offline replay from cached `fixN_patch.json`, `reports/SCHEMA.md`, and `benchmark.py` → `reports/benchmark_ner/RESULTS.md`.
- **Benchmark (NER, gpt-4, 10 planted breaks, $14.18):** catch 4/10, right component named 4/4, **top-ranked only 2/4**, single-attempt fix 1/4, false alarms 0/20.
- **Biggest gap vs. the pitch:** `find_causes()` in `migrate.py` ranks suspects with a **heuristic score** (+5/+2/+1), and it picked EQUIPMENT over the real `description` break twice. The pitch says "we *prove* what broke", so ranking must become causal (task 2).
- `.claude/` is untracked — leave it out of git.

## Earlier state (Oct 6, 1 AM)
Everything below has run for real against the OpenAI API.

**Baseline:** 20 inputs per task, 3 runs each.

| Task | Model | Accuracy | Hard checks passed | Output changed between runs |
|---|---|---|---|---|
| NER | gpt-4 | F1 0.92 strict, 0.97 lenient | 60/60 | 0/20 |
| NER | gpt-3.5 | F1 0.89 | 57/60 | 10/20 |
| Textcat | gpt-4 | 60/60 | 60/60 | – |
| Textcat | gpt-3.5 | 54/60 (misses sarcasm) | 60/60 | – |

**Real finding 1:** switching only the model name to `gpt-5.6-sol` fails all 120 calls. `spacy.GPT-4.v3` sends `temperature: 0.0`, and the new model accepts only the default. The fix is `--model-config '{}'`.

**Migration run** (`python migrate.py --task ner --candidates gpt-5.6-sol gpt-5.6-terra --model-config '{}'`):
- `gpt-5.6-sol` ranked first with 1 regression. `gpt-5.6-terra` ranked second with 2.
- The regression was ner-16: "Nothing beats a bowl of pho", where `bowl` was tagged EQUIPMENT.
- The cause was confirmed as `label_definitions.EQUIPMENT`.
- Fix attempt 1 was accepted. It clarifies that serving and quantity containers aren't equipment. Result: 0 regressions, lenient F1 0.97 → 0.98.
- Output is in `reports/migration_ner/`: `PR.md`, `pr.diff`, a patched `fewshot.cfg`, and `migration.json`.

**Textcat:** `gpt-5.6-sol` is identical to `gpt-4` (20/20 SAME).

**Not posted anywhere.** No PR has been opened on explosion/spacy-llm. That's the user's decision.

## Files
| File | Purpose |
|---|---|
| `record_baseline.py` | Runs the real spacy-llm (0.7.4) on the upstream example configs and caches every call to `results/<task>__<variant>__<model>[__tag].jsonl`. It resumes after interruptions. Flags: `--name`, `--model-config`, `--patch` (prompt patch JSON), `--tag`, `--dry-run`, `--api-base`. |
| `checks.py` | Hard checks, each tied to one prompt instruction: NER `format`, `valid_labels`, `spans_in_text`, `no_pronouns`, `nonempty`, `in_order`; textcat `valid_label`, `bare_answer`, `single_label`. Also gold scoring. |
| `compare.py` | Baseline vs one candidate. Verdicts: REGRESSED, IMPROVED, CHANGED or SAME, using a majority-of-runs rule against the baseline's worst run. Recomputes checks from the saved `raw_output`. `build_report()` is importable. |
| `migrate.py` | Orchestrator: candidates → rank → cause → fix → verify → PR. Writes `reports/migration_<task>/`. |
| `benchmark.py` | Planted-bug benchmark: patches the gpt-4 prompt, re-runs, scores catch / right-component / fix / false-alarm rates. Writes `reports/benchmark_ner/`. |
| `upstream/` | Upstream example configs, copied unchanged. Don't edit them. |
| `support_agent.py` | 4-step support agent (classify → decide → draft → tone), fake deterministic tools, line-list prompts, resumable cache `results/agent__<config>.jsonl`. `decide` contains one **planted** line (chosen by `probe_plant.py`). |
| `stepfinder.py` / `linefinder.py` | Prove which agent step, then which prompt line, broke (swap / ablation, 3 runs, compare.py's rule). Reports in `reports/agent/`. |
| `ablation.py` | Sentence-level ablation for NER causes, with a no-ablation control; used by `migrate.py` and `benchmark.py --ablate`. |
| `dashboard_export.py` | Writes `reports/<dir>/dashboard.json` (Switchyard fixture shape) for agent reports; called by `migrate_agent.py` and `demo_e2e.py`. The dashboard (`../switchyard-dashboard`, branch `samegrade-integration`) serves these plus live rows from `results/*.jsonl`. |
| `judge.py` | Claude judge for CHANGED items: both orders, old-vs-old noise floor, cached in `results/judge__<model>.jsonl`; skipped without Anthropic credentials. `migrate.py` calls it (`--no-judge` to skip). |
| `pr_report.py` | Renders PR.md in fixed order: step → lines → evidence → fixes (each accept / edit / reject). |
| `migrate_agent.py` | Agent migration report + review flow (`--accept`, `--reject`, `--apply-edit`); writes `reports/migration_agent/`. `migrate.py` has the same flags for NER. |
| `inputs/` | Hand-written inputs with gold labels and `tricky` flags. A teammate still needs to review the gold labels. |

Setup: `source .venv/bin/activate`. `OPENAI_API_KEY` must be set in the shell. In zsh, don't paste commands that have inline `#` comments.

## Rules
- Never print, log or commit `OPENAI_API_KEY`.
- Ask the user before any run likely to cost more than about $5. For scale, the full baseline cost $1.18, and one fix attempt is 60 calls.
- Never edit or delete `results/*__gpt-4__gpt-4.jsonl`, `results/*__gpt-4__gpt-4__ext.jsonl` (gpt-4 on the 30 probe inputs per task, Oct 7), `results/*__shipped__gpt-3.5-turbo.jsonl` or `results/agent__gpt-4.jsonl`.
- Don't open PRs or issues on public repos, or post anywhere, without the user's explicit OK.
- Don't make up prices. `PRICES` in `record_baseline.py` holds rates the user supplied (gpt-5.6-sol $5/$30, gpt-5.6-terra $2.50/$15 per 1M, Oct 6). For agent reports, `cost_per_1k_calls_usd` is per 1k 4-step ticket runs.
- Report real numbers only. If a result is weak, say so. Don't tune the inputs to make the demo look better.

## Next tasks, in priority order
Ask before any run likely over ~$5 and give an estimate (the benchmark came in at $14 — estimate carefully). Commit after each task.

1. **Multi-step demo agent + step-finder (the demo lead).** gpt-4 retires Oct 23, so record its baseline first.
   - Build a small 4-step support agent in this repo: classify ticket → look up policy → draft reply → check tone. Each step is its own prompt + model setting.
   - Tools are fake and deterministic (canned policy lookups), so differences come only from the model. Run live, never replay logs: each step's output feeds the next.
   - 20–30 tickets with expected outcomes. Record the gpt-4 baseline: 3 runs per ticket, every step's inputs/outputs cached (same resumable cache style as `record_baseline.py`).
   - Run all steps on the candidate; mark a ticket regressed only beyond the old model's own run-to-run noise (reuse `compare.py`'s rule).
   - **Step-finder:** for each regressed ticket, run "new model everywhere except step k (old model)". If it passes again, step k is causal. 3 runs each; binary search when steps > 4; try pairs if no single step explains it. Output: "step 2 explains X% of failures".
   - Plant one realistic break that only shows on the new model in step 2, so the demo has a clear answer — but also report whatever the real candidate breaks, honestly.
   - Show me the cost estimate before recording.
2. **Make cause-finding causal, at line level.**
   - Split each component (esp. `description`) into individual sentences/lines.
   - On failing inputs, on the *new* model: ablate or neutralize each suspect line, re-run 3×, rank lines by measured effect on the failure. Keep the heuristic only to order which lines to test first.
   - Mark each cause in `migration.json` as `confirmed` (by ablation) or `suspected`; update `SCHEMA.md` and tell me so Iris can show it.
   - Re-run the benchmark on the 4 caught breaks (reuse caches) and report top-ranked rate before vs. after.
3. **PR/report = the product (human-in-the-loop).**
   - `PR.md` order: step → lines → evidence (before/after examples, scores, old-model noise floor, cost/latency) → minimal diff. Plain language a reviewer can approve in 5 minutes.
   - Each fix is separate and individually accept / edit / reject. Add `migrate.py --apply-edit <file>`: takes the engineer's edited lines, re-runs the full suite, re-verifies, regenerates the PR.
   - Mirror the same data in `migration.json` for Iris's report page (it's her main screen).
4. **Raise catch rate honestly.** Add inputs that probe each label definition (aim 50 per task). Re-run the benchmark; report old and new numbers side by side. Don't drop inputs that hurt results.
5. **LLM judge for CHANGED items** (unchanged from before): different provider than candidates, both orders, old-vs-old to measure noise, optional when no judge key.
6. **Costs + per-step model choice.** Get gpt-5.6 prices from me for `PRICES`. Then, from the step-finder runs, report which steps can run on a cheaper model with no quality loss and the resulting cost change. Report only; don't build a router.

Dropped: second real example (nasa-petal/bidara). Don't build an eval dashboard, router, or whole-prompt optimizer.

## Pitch facts (verified)
- **Shutdown dates:** OpenAI's deprecations page lists Oct 23, 2026 for `gpt-4-0613`, `gpt-4-turbo` and `gpt-3.5-turbo-0125`. Recommended replacements are `gpt-5.6-sol` and `gpt-5.6-terra`.
- **Hard failure:** spacy-llm checks `/v1/models` when it loads and raises `ValueError` if the model is gone. Pipelines stop working outright rather than degrading.
- **Real migration costs found:**
  - The temperature parameter is rejected.
  - The model can't be made deterministic.
  - Latency goes up about 27% (2.89s → 3.66s p50).
  - One label definition needed a one-line clarification.
- **Silent failure mode:** when the model lists entities out of order, spacy-llm silently drops the rest. The `in_order` check catches it.
- **Benchmark (real):** 0 false alarms on a fresh gpt-4 re-run; when a break is caught, the real component is in the suspect list 4/4.
- **Step-finder benchmark (Oct 7, `reports/agent_bench/`, terra -> sol, 9 planted breaks):** on 39 localized tickets, 0 wrong steps; 34/39 exact held-out (37/39 after the narrowing fix), all confirmed both ways; two-step break found as two steps 6/8. An LLM asked "which step failed?" also named a planted step 39/39 (31/39 exact), so **don't cite the 14–29% figure as something we reproduced**: our edge here is proof and multi-step causes.
- **Don't claim** "nobody has built migration tooling". Claim "nobody proves which step and which lines broke".
