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
