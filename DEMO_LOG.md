# Demo log (branch `demo`, from `overnight`, started Oct 8)

`demo` branches from `overnight`, not main: the LiteLLM call layer, the hash cache and the spend cap
(`llm.py`) only exist there. Neither branch has been pushed.

## What exists (read before any change)
- **README.md:** setup, offline replay commands, the NER baseline kit (spacy-llm 0.7.4 on upstream configs), and the NER `migrate.py` flow.
- **stepfinder.py:** proves which agent step broke. Necessity: new model everywhere except step k, which is old. Sufficiency: old everywhere except step k, which is new. 3 runs each, compare.py's noise-floor rule, pairs when no single step explains a failure, cached hybrids in `results/agent__<config>.jsonl`.
- **reports/agent_bench/RESULTS.md:** synthetic benchmark of 9 planted prompt breaks, terra as old and sol as new, 39 localized tickets.
  - **Held-out:** 34/39 exact, 38/39 confirmed both ways, 0 wrong steps.
  - The 37/39 figure is *after* a narrowing fix designed on the same tickets (dev-set), so it is **not** a held-out number.
  - An LLM asked "which step?" got 31/39 exact.
- **OVERNIGHT_LOG.md:**
  - `llm.py` sends all agent calls through LiteLLM, with a sha256 per-call cache and a spend cap.
  - `thin_e2e.py` writes `public/results.json` (v1 schema).
  - `stepswap.py` does rescue/break and line ablation using the brief's 2/3-vs-1/3 rule.
  - `support_agent.py --no-plant` removes the planted line; 18 unit tests.
- **Frontend sample (`dashboard/results.sample.json`, schema v1):** `repo`, `baseline`, `prompt`, `inputs[]`, `candidates[]` (`summary`, `results[]` with `status`/`runs`/`cause_id`), `causes[]` (`fix.old_line`/`new_line`/`retest`) and `pr`. `validate()` requires every `regressed` result to have a matching cause.
- **The dashboard** (`dashboard/`, Node, no deps) lists `reports/<name>/dashboard.json` or `migration.json`, falls back to `results.json`, has a live SSE feed of `results/*.jsonl` and replays recorded runs offline.
- **Planted vs real:** the `decide` line "Final-sale items are not eligible for refunds or replacements." is **planted**. `probe_plant.py` chose it because it breaks only the new model, and every cached gpt-4 agent run includes it.
- **Spend already logged in `results/llm_calls.jsonl`:** $1.03 (overnight). Earlier sessions spent more (the benchmarks, about $14 + $12.85) before the hash-cache log existed. I count the $20 cap from the $1.03 logged there.

## Prices (`config/prices.yml`)
Checked on Oct 8 against https://developers.openai.com/api/docs/pricing (Standard tier, short context, per 1M tokens):

| Model | Input | Output |
|---|---|---|
| gpt-5.6-sol | $4.00 | $20.00 |
| gpt-5.6-terra | $2.00 | $12.00 |
| gpt-4-0613 | $30.00 | $60.00 |
| gpt-3.5-turbo | $0.50 | $1.50 |

**These differ from the rates that were in `record_baseline.PRICES`** (user-supplied Oct 6: sol $5/$30, terra $2.50/$15). Those were higher, so older reports *overstate* new-model cost. Tonight's demo numbers use the official page, recomputed from logged token counts.
