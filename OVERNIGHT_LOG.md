# Overnight log (branch `overnight`, started Oct 8)

## Task 1: what exists (read before any change)
1. `record_baseline.py` runs real spacy-llm 0.7.4 (NER + textcat) and caches every call in `results/*.jsonl`. The gpt-4 and gpt-3.5 baselines are protected and never edited.
2. `checks.py` and `compare.py` hold the hard checks and the noise rule. The rule is majority-of-runs against the old model's worst run, and verdicts are REGRESSED / IMPROVED / CHANGED / SAME.
3. `support_agent.py` is already a 4-step agent: classify → decide (policy lookup) → draft → tone. Its tools are fake and deterministic, prompts are line lists, there's a per-step model plan, and a resumable cache.
4. The gpt-4 agent baseline is cached (24 tickets × 3 runs), plus the gpt-5.6-sol and gpt-5.6-terra full runs.
5. `stepfinder.py` swaps one step at a time. "Necessity" (new everywhere, old at step k) is the brief's *rescue*. "Sufficiency" (old everywhere, new at step k) is the brief's *break*. If no single step explains a failure it tries pairs.
6. `linefinder.py` and `ablation.py` do line-level ablation inside the guilty step (agent and NER).
7. `agent_bench.py` holds `BREAK_SETS`, planted bugs per step (`u2` is in step 2, `decide`). `demo_e2e.py --break <id>` runs the whole flow on one break.
8. `migrate.py` / `migrate_agent.py` produce the PR. `pr_report.py` renders PR.md. `judge.py` is a Claude judge (a different provider; skipped without a key).
9. `dashboard/` is the frontend (plain Node, no deps). It reads `reports/*/dashboard.json` and falls back to `results.json`. `results.sample.json` defines the v1 schema.
10. Not there before tonight: LiteLLM (calls went over raw HTTP), a per-call hash cache, `public/results.json`, the brief's 2/3-vs-1/3 rule, and unit tests.

## Task 2: thin end-to-end (done)
- `llm.py`: every agent call now goes through LiteLLM (`litellm==1.104.1`, pinned; the compromised 1.82.7/1.82.8 releases aren't on the index). It adds a per-call sha256 cache (`results/llm_calls.jsonl`) keyed on model+params+messages+run, and a hard spend cap (`LLM_SPEND_CAP_USD`, default 15) that refuses uncached calls once the logged spend reaches it.
- `support_agent.call_openai` delegates to `llm.call_llm`, so `run_ticket`, the step-finder, `agent_bench` and `probe_plant` all use it. A live smoke test (t01, terra, 4 calls, $0.0046) passed, and a re-run under a new tag was served fully from the hash cache with no key.
- `thin_e2e.py` writes `public/results.json` (v1 sample schema; it passes the dashboard's own `validate()`). The hard checks are valid_json, required_fields and label_match. The noise rule is: broken iff old ≥2/3 and new ≤1/3.
- **Result (cached runs, $0):** gpt-4 vs gpt-5.6-sol: **1 broken (t07)**, 23 same. gpt-4 vs gpt-5.6-terra: **0 broken**. Baseline noise under these checks is 0%. Cost per 1k ticket runs: gpt-4 $38.88, sol $12.04, terra $4.81. p50 latency: sol 7.1 s, terra 4.7 s.
- Honest note: compare.py's richer checks find 6 sol regressions. Most are draft/tone instructions (timeline, word limit) that the brief's three checks don't cover. The real migration still has those failures, and they're documented in `reports/migration_agent/`.
- Deviations: 24 tickets (the cached set), not 20. Re-recording to cut 4 would cost money and change nothing. NER/textcat calls still go through spacy-llm's own HTTP client, because routing them through LiteLLM means replacing spacy-llm's backend. The NER fixer (`migrate.py`) and the Claude judge are also unchanged. None of those were used tonight.
