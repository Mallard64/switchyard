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

## Task 2: per-step model assignment (done; the mix is NOT accepted, and the log says so)
**Method (`assign.py`):**
- Ladder per step, cheapest first: terra ($2/$12), then sol ($4/$20), with gpt-4 as the fallback.
- Test for step k: gpt-4 everywhere, the candidate at step k only, 24 dev tickets × 3 runs, steps before k replayed from gpt-4's run. Pass means 0 regressions (compare.py's rule).
- New-model steps carry the task-1 tone patch. The chosen mix then runs end to end on dev **and** on the 12 held-out tickets.

| Step | terra | sol | Chosen |
|---|---|---|---|
| classify | PASS (0/24 regressed) | – | terra |
| decide | PASS (0/24) | – | terra |
| draft | PASS (0/24) | – | terra |
| tone | fail (t07, t20) | fail (t07) | gpt-4 |

**Mix end to end (terra, terra, terra, gpt-4):**
- Dev: 0 regressed.
- **Held-out: 1 regressed (t33)**. gpt-4's tone step, given terra drafts, returned non-JSON on 2/3 runs. Gold score went from 0.958 to 1.000.
- Under the brief's rule (≥ old minus noise, 0 regressions) **the mix is not accepted**. Steps that each pass on their own did not compose.

**Per-step cost from real usage** (mean tokens per ticket run, `config/prices.yml`, $ per 1k ticket runs):

| Step | all gpt-4 | all sol + patch | mix |
|---|---|---|---|
| classify | $6.58 (190 in / 15 out) | $1.13 | $0.57 (terra) |
| decide | $10.83 | $2.05 | $1.11 (terra) |
| draft | $12.78 | $2.48 | $1.15 (terra) |
| tone | $9.13 | $2.17 | $8.68 (gpt-4) |
| **Total** | **$39.32** | **$7.84** | $11.50 |

**Recommendation:** all gpt-5.6-sol + the 1-line tone patch. It's validated on held-out in task 1 (0 regressions) and costs **$7.84 per 1k ticket runs, −80.1% vs gpt-4**. The mix saves 70.7% vs gpt-4 but costs 46.6% more than all-sol, because tone stays on gpt-4, and it isn't accepted anyway.

**Not done, on purpose:** I didn't try terra×3 + sol-tone on held-out after seeing the mix fail there, because that would tune on the held-out set. It's the obvious next candidate; it needs a fresh held-out set.

Spend after task 2: **$10.59**.

## Task 3: results.json additions (done)
- `python results_demo.py` writes `reports/demo/results.json`. The v1 fields keep the sample's exact shape and pass the dashboard's `validate()`. Everything new is additive:
  - Per result: `synthetic`, `split` (dev/heldout), `verdict`, `failed_checks`.
  - Per cause: `synthetic`, `step`.
  - Per input: `split`, `synthetic: true` (the tickets are hand-written).
  - Top level: `synthetic: false`.
  - A `demo` block with:
    - `per_step`: ladder, chosen model, cost per step for all-old / all-new / mix, repair and reproduce n/of.
    - `totals_usd_per_1k`, `savings`, `recommended`, `mix` (accepted: false, plus why).
    - `confidence`: 3 runs, 24 dev + 12 held-out tickets, regression counts, old-model unstable tickets.
    - `patch` (diff, edits, fixer, what it saw, dev/held-out before/after), `lines`.
    - `examples`: t21 dev and t36 held-out, with old / new / fixed tone output and final reply.
    - `benchmark`: **synthetic**, held-out 34/39 exact and 38/39 two-way, asserted against RESULTS.md at build time.
    - `prices` with source and date.
    - `monthly_requests_default: 100000`, labelled as an assumption the buyer edits.
- Numbers: sol as-is has 7/36 regressed (4 dev + 3 held-out) and 0/36 after the patch. Terra has 2/36. Cost per 1k ticket runs: gpt-4 $39.32, sol as-is $9.05, sol + patch $7.84.
