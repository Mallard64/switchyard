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

## Task 1: genuine model-only regression (done; REAL, nothing planted)
**Setup:**
- Both sides run the clean agent: the planted decide line is removed (`--no-plant`) on old and new alike. Tools, the 24 dev tickets, the 12 held-out hard tickets and the 3 runs are all identical.
- The only other difference is sampling. gpt-5.6-* reject temperature 0, so they run at their default, while gpt-4 keeps temperature 0. That can't be avoided.
- New recordings, $5.0: gpt-4, terra and sol on clean prompts, dev + hard (sol dev was already cached).

**Found (compare.py noise-floor rule):**

| Model | Dev (24) | Held-out (12) | Failing check |
|---|---|---|---|
| gpt-5.6-sol | **4 regressed** (t01, t11, t21, t24) | **3** (t27, t29, t36) | `draft.states_timeline`: the final reply omits "5–7 business days". gpt-4 failed it 0/3 on every one of those tickets, and gpt-4's runs were stable on 24/24 + 12/12. |
| gpt-5.6-terra | 2 (t04, t21) | 0 | same check |

**Mechanism (sol, t21 run 1):** the draft contains the timeline. Sol's tone step returns REVISED with the issue "The draft promises a refund timeline and payment method that are not included in the decision" and deletes it. gpt-4's tone step returns PASS.

**Repair / reproduce (3 runs each, `reports/model_only/result_gpt-5.6-sol.json`):**

| Step | Repair (old model swapped back here) | Reproduce (new model here only) |
|---|---|---|
| classify | 0/4 | – |
| decide | 0/4 | – |
| draft | 0/4 | – |
| **tone** | **4/4** | **2/4** (t01, t11). t21 and t24 did not reproduce with gpt-4-written drafts. |

**Lines:** removing tone line 3, "It must not promise anything beyond the decision.", repairs 4/4. Every other tone line repairs 0/4.

**Bounded patch:** the fixer (gpt-5.6-sol) saw only the dev failures and was allowed at most 2 lines of `tone`. Its first attempt changed 1 line, and the dev re-run (24 × 3) gave 0 regressions.
```
-It must not promise anything beyond the decision.
+It must not add promises beyond the decision; however, standard refund details already present in the draft, including return to the original payment method within 5-7 business days, are allowed and must be preserved.
```
**Held-out (12 hard tickets, never shown to the fixer, run once):** 3 regressed → **0 regressed** (2 improved, 10 same). Hard checks passed: 100% for the patched sol and 100% for gpt-4.

**Real app (spacy-llm NER, from earlier cached runs, model-only apart from the forced `{}` sampling):** sol regresses on ner-16 ("bowl" tagged EQUIPMENT), and terra on 3 of 50 inputs. The fix is in `reports/migration_ner/`. Textcat: no regressions.

Spend after task 1: **$7.11** (logged in `results/llm_calls.jsonl`).
