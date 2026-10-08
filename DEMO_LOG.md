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

## Task 4: dashboard views (done; existing frontend extended, not rebuilt)
- `dashboard/demo-views.js` (new) is imported by `app.js`. `render()` gets one inserted section plus one bind call. `server.mjs` lists `reports/<name>/results.json` when it's v1 with a `demo` block, opens the demo report first and serves `demo-views.js`. The styles are an appended `.dm-*` block in `styles.css` using the current black/silver edition's colors.
- Shown only when a report has `demo`. Three tabs, deep-linkable with `#savings`, `#evidence` and `#pr`:
  - **Savings (buyer):** per-step cost before/after, an editable tickets-per-month calculator (default 100,000, labelled as an assumption), a quality bar for gpt-4 vs sol without and with the fix, and the rejected cheaper mix explained.
  - **Evidence (engineer):** repair/reproduce per step, line ablation inside `tone`, before/after replies for a dev ticket (t21) and a held-out ticket (t36), and the synthetic benchmark (34/39 held-out exact, 38/39 two-way), labelled synthetic.
  - **PR card:** summary (where / what / proof / cost) and 2 changes, the model swap and tone line 3, each with Accept / Edit / Reject. An edit is marked "re-run needed" and is never shown as verified.
- Fixed while checking: the demo cards were white on the dark theme, and `p95 NaN` appeared in the existing candidate cards (`latency_p95_ms` added, additively).
- Screenshots: `docs/demo/savings.png`, `evidence.png`, `pr.png` (headless Chrome).

## Task 5: demo mode (done; works offline)
- `python demo.py --demo` narrates the real recorded migration in 5 stages: switch, which step, which line, fix, held-out. It replays the cached rows with their recorded relative timing, squeezed into about 1 minute (`--speed` changes that). It starts the dashboard on :4173 unless one is already running.
- It imports no model client, reads only `results/*.jsonl` and `reports/*.json`, and exits with a clear message if a cached file is missing.
- **Offline check:** I ran it with `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` unset and `http(s)_proxy` pointed at a dead port, and it completed (13 s at `--speed 4`). With the server path, `/api/reports` lists `demo` first. The dashboard page loads no external URLs.

## Task 6: DEMO.md + screenshots (done)
- `DEMO.md` has the setup and offline commands, a timed 2-minute script, what each screen proves (and what it doesn't), and answers to likely questions.
- `docs/demo/`: `savings.png`, `evidence.png`, `pr.png` (headless Chrome, 1360 px wide) and `terminal.txt` (a transcript of `demo.py --demo`).
- **Held-out numbers everywhere:** `migrate_agent.bench_note()` quoted 37/39 (post-fix, dev-set) in `reports/migration_agent*/PR.md`. It now reads the held-out row of RESULTS.md (34/39 exact, 38/39 two-way, 0 wrong). I swapped that one sentence in the 6 committed legacy files instead of regenerating them; see the cost issue under Open issues.

## Summary

### Real vs synthetic
| Item | Real or synthetic |
|---|---|
| Model outputs, token counts, latencies (all `results/*.jsonl` rows) | **Real** recorded API calls |
| The tone-timeline regression, repair/reproduce, line ablation, 1-line patch, held-out 3 → 0 | **Real.** Nothing planted; only the model changed (plus sol's forced default sampling). |
| Per-step costs and savings | **Real** token counts × official list prices (`config/prices.yml`, OpenAI pricing page, Oct 8) |
| Monthly volume (100,000) | **Assumption**, labelled on the page and editable |
| The 36 tickets and expected outcomes | **Synthetic** (hand-written; the 12 hard tickets were written before any run) |
| Step-finder accuracy 34/39 held-out, 38/39 two-way | **Synthetic** planted-bug benchmark, labelled in the UI |
| The decide line "Final-sale items…" | **Planted**. Removed on both sides for every demo number. |

### Spend
**$10.59 total** logged in `results/llm_calls.jsonl`. That's $1.03 overnight + $9.56 for this brief: about $5.0 recording clean baselines and candidates, $1.5 for the step-finder, line ablation, fixer and patch runs, and $3.5 for the per-step ladder and mix. The cap was $20, and no paid calls were made for tasks 3–6.

### Open issues
1. **The per-step mix isn't validated.** terra×3 + gpt-4 tone regressed held-out t33, and the obvious next candidate (terra×3 + sol-tone) needs a *fresh* held-out set. I didn't tune on the current one.
2. **Reproduce is 2/4, not 4/4.** On t21 and t24 sol's tone step drops the timeline only when sol also wrote the draft. This is shown as-is.
3. **Small held-out set.** The patch was verified on 12 tickets (3 → 0). That's real but small; a design partner's traffic is the next test.
4. **Mixed prices in legacy reports.** Rows recorded before tonight stored `cost_usd` at the user-supplied Oct 6 rates (sol $5/$30, terra $2.50/$15), while compare.py fills in missing costs at the verified rates. Regenerating `reports/migration_agent*` would mix the two in one table ($8.70 next to $10.80), so I left those reports' numbers alone. The demo report recomputes every cost from tokens × `prices.yml` and is consistent. The fix is for compare.py to always recompute from usage.
5. **Phone width:** the existing dashboard layout overflows at 390 px (the intro and note box clip). The new demo views reflow, but the page as a whole doesn't. It's fine on a laptop.
6. **The design canvas from earlier** (claude.ai artifact "Switchyard dashboard redesign") says "37 times" for the benchmark. Fix it to the held-out 34/39 before sharing it.
7. **Not pushed.** `demo` is based on `overnight`, and neither branch is on GitHub.

### Reproduce
```
git checkout demo
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover tests
python demo.py --demo
```
Rebuilding the demo data from cache is free: `python results_demo.py`. Re-running the experiments costs money; everything is cached, so a repeat is free:
```
set -a; source .env; set +a
python model_only.py
python assign.py
python results_demo.py
```

## Follow-up (Oct 8): a bigger model pool, including open-weight models
**Added:**
- **Four cheap OpenAI models**, prices verified on the OpenAI pricing page and added to `config/prices.yml`:
  - gpt-5-nano $0.05/$0.40
  - gpt-4.1-nano $0.10/$0.40
  - gpt-5.6-luna $0.20/$1.20
  - gpt-5.4-mini $0.75/$4.50
- **Three open-weight models run locally** through Ollama (Q4, Apple M4 16 GB): Llama 3.2 3B, Qwen 2.5 7B and Llama 3.1 8B. There's no account, and there's no list price, so `prices.yml` has `null` for them and the UI shows tokens per ticket, not dollars. Hardware and power aren't counted.
- `llm.py` routes `ollama/<model>` to the local server. `support_agent.safe()` makes `/` and `:` file-safe; existing labels are unchanged.
- Sampling matches gpt-4 (temperature 0) wherever the model accepts it (gpt-4.1-nano and the local models). The gpt-5.x models reject 0 and run at their default.
- **A fresh held-out set:** `inputs/agent_holdout2.jsonl` has 12 tickets (t37–t48), committed (3323f4f) **before any model ran on them**. The old 12 hard tickets had already been used once for a mix decision.

**Screening, model-only** (clean prompts, regressed vs gpt-4, dev 24 / hard 12 / fresh 12, cost per 1k ticket runs):

| Model | dev | hard | fresh | cost |
|---|---|---|---|---|
| gpt-5.6-terra | 2 | 0 | 1 | $3.80 |
| gpt-5.6-sol | 4 | 3 | 3 | $8.76 |
| gpt-5.4-mini | 4 | 4 | 2 | $1.51 |
| gpt-5.6-luna | 5 | 3 | 3 | $0.53 |
| gpt-5-nano | 7 | 1 | 2 | $1.73 (about 5,200 tokens/ticket, mostly reasoning) |
| ollama/llama3.1:8b | 7 | 3 | 3 | self-hosted, 1,290 tokens/ticket |
| gpt-4.1-nano | 11 | 4 | 3 | $0.16 |
| ollama/llama3.2:3b | 14 | 7 | 7 | self-hosted, 1,309 tokens/ticket |
| ollama/qwen2.5:7b | 19 | 10 | 9 | self-hosted, 1,253 tokens/ticket |

None of them is safe alone, which is why the per-step ladder exists.

**Per-step ladder (`model_pool.py`):**
- Background: sol + patch everywhere, the candidate at one step. Self-hosted models are tried first, then API models by cost. Pass means 0 regressions on dev 24 × 3.

| Step | Result |
|---|---|
| classify | 3 local models and gpt-4.1-nano fail; **gpt-5.6-luna passes** |
| decide | **luna passes** (local: 11 / 4 / 3 worse) |
| draft | **luna passes** |
| tone | llama3.2:3b and qwen2.5:7b fail; **llama3.1:8b (local) passes** |

**Mix (luna, luna, luna, local Llama 3.1 8B on tone):**
- Dev end to end: 0 regressed.
- **Fresh held-out: 1 regressed (t42) → not accepted.**
- It would have cost $0.31 per 1k in API calls plus about 360 self-hosted tokens per ticket.
- **Cause, real:** t42 types the order ID in lowercase ("a1005"). gpt-5.6-luna's classify step returns `order_id: null` in 3/3 runs, both in the mix and running alone, because it reads "uppercase (e.g. A1234)" as a validity rule. gpt-4 normalizes it to A1005. The dev set has no lowercase IDs, so only the fresh held-out set could catch this.

**The recommendation stays gpt-5.6-sol + the 1-line tone patch.** It now has **0 regressions on a second, pre-registered fresh held-out set** (12/12 same), on top of the earlier 3 → 0.

**Spend:** $16.22 total (+$5.63 for this follow-up: holdout2 baselines about $1.6, ladder and mix about $3.3, cheap-model screening about $0.7). Local models cost $0 in API calls.

**Open:**
- A luna-based mix needs a classify fix for lowercase IDs (a one-line prompt clarification, or normalizing the ID in code), and then a *third* fresh held-out set.
- Self-hosted cost should be measured (latency × hardware cost) before it's quoted as a saving.

## Follow-up 2 (Oct 8): cheap retest of the luna + local Llama mix, plain wording, PR
**Fix:** one line of the classify prompt, used by the mix only:
> Extract the order ID if the ticket gives one: the letter A followed by 4 digits (e.g. A1234). Customers may type it in lowercase or with a #; return it in uppercase without the # (a1234 -> A1234). Use null if there is none.

This was an engineer edit, written from the t42 failure.

**Results (`mix_retest.py`, `reports/mix_retest/result.json`):**

| Ticket set | Fixed mix: got worse | sol + fix: got worse |
|---|---|---|
| Practice (24 × 3) | 0 | – |
| holdout2 (12 × 3; **not fresh any more**, it exposed the bug) | 0 (t42 fixed) | – |
| **fresh3: 8 new tickets** (committed in 6e101d4 before any run, 2 with lowercase IDs) × 3 | **0 → passes** | 0 |

- Cost of the mix: **$0.31 per 1k tickets in API calls** plus about 363 tokens per ticket on local Llama 3.1 8B (tone).
- Small on purpose to save cost: 8 tickets is encouraging, not proof. The recommendation stays sol + fix until the mix passes a larger fresh set and self-hosted cost is measured.
- Spend for the retest: $1.10. **Total: $17.32** (gpt-4 baseline on fresh3 about $0.95, sol about $0.10, luna about $0.05; Llama local).

**Plain wording:** replaced jargon in what people read: dashboard labels (`app.js`, `demo-views.js`), README, DEMO.md, and the PR-report text.

| Before | After |
|---|---|
| spurious | extra (not in the answer key) |
| boundary | span too long or too short |
| regressed / regressions | got worse |
| held-out / dev | fresh test / practice tickets |
| repair / reproduce | swap gpt-4 back here: fixed / new model here only: broke again |
| gold score | correct-answer score |
| intermittent | failed in some runs only |
| unstable inputs | inputs changed between runs |
| localize | trace to a step |
| hybrid | mixed |
| line ablation | removing one line at a time |
| noise floor | how much the old model varies on its own |
| synthetic benchmark | practice test with planted bugs |
| open weights | open-source model on your own computer |

Code names and JSON fields are unchanged, so the frontend contract holds. Committed older reports (`reports/*/PR.md`) keep their old wording until they're regenerated.
