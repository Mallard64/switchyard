# Migration baseline: spacy-llm examples on retiring OpenAI models

This kit records how [explosion/spacy-llm](https://github.com/explosion/spacy-llm) behaves on the models it uses today, before they shut down on **Oct 23, 2026**. Those models are `gpt-4` (→ `gpt-4-0613`) and `gpt-3.5-turbo` (→ `gpt-3.5-turbo-0125`). After that date, these outputs can't be regenerated.

## Why this repo

- It is actively maintained, with maintainer commits on Sept 27–28, 2026, and about 1.4k stars.
- `spacy.GPT-4.v3` defaults to `name="gpt-4"`, and the `spacy.GPT-3-5.*` models default to `"gpt-3.5-turbo"`. The library's own example configs use these.
- When a pipeline loads, spacy-llm checks `/v1/models` and raises `ValueError` if the model isn't listed. So after Oct 23, these pipelines will fail to load at all, not just get worse.

## What runs

The kit uses the real library (pinned at `spacy-llm==0.7.4`, the same as repo commit `a399273`) and the upstream example configs in `upstream/`, copied unchanged.

| Task | Upstream config | Labels |
|---|---|---|
| `ner` | `usage_examples/ner_v3_openai/fewshot.cfg` with its 2 few-shot examples | DISH, INGREDIENT, EQUIPMENT |
| `textcat` | `usage_examples/textcat_openai/zeroshot.cfg` | COMPLIMENT, INSULT |

There are two variants per task:

- **`shipped`** runs the configs as published: `spacy.GPT-3-5.v1` for NER (no temperature set) and `spacy.GPT-3-5.v2` for textcat (temperature 0).
- **`gpt-4`** runs `spacy.GPT-4.v3` at its default model, `"gpt-4"`.

The inputs in `inputs/` are 20 texts per task, each with a hand-written correct answer. About a third are flagged `tricky`: sarcasm, backhanded compliments, flavor vs. ingredient, a verb that looks like equipment ("Microwave the curry"). Those are where models tend to diverge.

## Setup (any machine)

Requirements: Python 3.11+ (tested on 3.11 and 3.14) and Node.js 18+ for the dashboard. No npm install is needed.

```bash
git clone <this repo> samegrade && cd samegrade
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` only if you will make new model calls.

Everything in `results/` is a cache of real model calls, so the commands below replay **with no API key and no cost**:

| Command | What it replays |
|---|---|
| `python demo_e2e.py --break u3` | Step-finder → line-finder → fix → PR on one planted break |
| `python migrate_agent.py --candidate gpt-5.6-sol --candidate-config '{}'` | Real 4-step agent migration, gpt-4 → gpt-5.6-sol |
| `python migrate_agent.py --candidate gpt-5.6-sol --candidate-config '{}' --tag hard` | The same on the 12 harder tickets |
| `python export_bench.py` | Dashboard reports for every planted break |
| `cd dashboard && npm start` | Dashboard at http://localhost:4173, reading `reports/` and `results/` |

The NER migration (`migrate.py`) checks for a key even when fully cached. To replay it offline, give it a placeholder key and an unreachable API base so nothing can be billed:

```bash
OPENAI_API_KEY=offline python migrate.py --task ner --candidates gpt-5.6-sol gpt-5.6-terra --model-config '{}' --api-base http://127.0.0.1:9/v1
```

It prints a "Could not build the pipeline" connection error for the fix re-run, then reads the cached rows. For new runs, load your keys with `set -a; source .env; set +a` (plain `source` doesn't export them to subprocesses).

## Record a baseline (about $1.50–2.50 total, a few minutes)

```bash
export OPENAI_API_KEY=sk-...

python record_baseline.py --dry-run                                  # shows the exact prompts; no API calls
python record_baseline.py --task textcat --variant gpt-4 --limit 2 --runs 1   # smoke test, 2 calls
python record_baseline.py                                            # full: 40 inputs x 2 variants x 3 runs = 240 calls
```

- Results go to `results/<task>__<variant>__<model>.jsonl`.
- If a run is interrupted or a call errors, run the same command again. It skips calls that already succeeded.
- spacy-llm retries rate-limited calls itself, and `api_attempts` records how many tries each call took.

Each row holds:

- the exact prompt and raw response
- the parsed result, meaning entities with character offsets, or category scores
- the resolved model ID, token usage, latency and cost
- the hard checks and a score against the correct answers

## Hard checks

Each hard check enforces one line of the prompt, so a failure points straight at an instruction.

| Task | Check | Instruction it enforces |
|---|---|---|
| ner | `format` | "Only use this output format … Do not output anything besides entities" |
| ner | `valid_labels` | the label list |
| ner | `spans_in_text` | entities must come from the paragraph |
| ner | `no_pronouns` | "Pronouns are not entities." |
| ner | `nonempty` | spaCy kept at least one entity |
| textcat | `valid_label` | the answer is one of COMPLIMENT or INSULT |
| textcat | `bare_answer` | "Do not put any other text in your answer" |
| textcat | `single_label` | "The task is exclusive, so only choose one label" |

Correct-answer scores (F1, a standard match score, for NER; accuracy for textcat) are reported separately.

The summary also reports **how many inputs changed output between runs**. That run-to-run variation in the old model is the baseline wobble: a candidate model only counts as worse on an input if it differs from the baseline by more than that.

## After it runs

1. Commit `results/` to the team repo right away. It is the irreplaceable artifact.
2. Check that the summaries show `gpt-4-0613` and `gpt-3.5-turbo-0125` as the resolved models.
3. Later, run a candidate with the same harness, for example `--variant gpt-4 --name gpt-5.6-sol` or `--variant shipped --name gpt-5.6-terra`. That keeps the task, prompt and sampling settings the same and changes only the model.

Prices in `PRICES` are list rates as I understand them. Verify them on your OpenAI billing page before quoting cost numbers.

## Compare a candidate against the baseline

```bash
python compare.py results/ner__gpt-4__gpt-4.jsonl results/ner__gpt-4__gpt-5.6-sol.jsonl --json reports/ner_sol.json
```

Each input gets one result:

- **REGRESSED** (got worse) means a hard check fails in most candidate runs, or accuracy falls below the baseline's worst run in most candidate runs.
- **IMPROVED** is the reverse.
- **CHANGED** means the output differs from the baseline but isn't worse. These need a human or LLM judge to review.
- **SAME** covers everything else.

The candidate is ready to merge when no input got worse. Comparing the baseline against itself finds 0 worse inputs, so the method doesn't raise false alarms.

The report also lists:

- **Checks that failed in some runs only:** a check failed, but in fewer than half the runs. Each one names the prompt instruction it broke.
- **NER errors by type:** span too long or too short (`dry pan` vs `pan`), wrong label, missed, or extra (found something that isn't in the answer key). Accuracy uses a forgiving match score that counts length differences as correct, so a longer or shorter span isn't treated as a wrong answer.

The `--json` report is the format the dashboard reads. See `reports/` for examples, which compare gpt-4 against gpt-3.5-turbo as a stand-in for a bad migration.

## Full migration: candidates → cause → fix → PR

```bash
python migrate.py --task ner --candidates gpt-5.6-sol gpt-5.6-terra --model-config '{}'
```

1. **Record** each candidate on the baseline inputs. Results are cached, so a re-run only does missing work. A model your key can't use is skipped and marked unavailable.
2. **Compare and rank** candidates by how many inputs got worse, then accuracy, then check pass rate, then cost, then latency. The best one is chosen.
3. **Cause.** Each input that got worse is traced to the prompt parts it points to:
   - a failed hard check points to its instruction in the template
   - a wrong, extra or missed label points to that label's definition
   - a span-boundary error points to the task description
4. **Fix.** An LLM (by default the chosen model; change it with `--fixer-model`) rewrites one implicated component. The full suite is re-run with that patch (`results/*__fixN.jsonl`), and the fix is accepted only if no input regresses against the baseline. A rejected attempt is fed back to the fixer, up to `--max-fix-attempts` (default 3). A cause is marked **confirmed** when editing that component alone removes the regression.
5. **PR.** The script writes a patched copy of the upstream example config, `pr.diff`, and `PR.md` with evidence, causes and limits. It doesn't open a PR. Posting to explosion/spacy-llm is your call: `gh pr create --repo explosion/spacy-llm --body-file reports/migration_ner/PR.md`.

Everything goes to `reports/migration_<task>/`. **`migration.json` is the dashboard's input.** It contains:

- `candidates[]` (ranked)
- `chosen`
- `causes[]`
- `fix_attempts[]`
- `final`
- `pr`
- `comparison`, the per-input report for the final setup, in the same shape as `compare.py --json`

A fix costs 60 candidate calls per attempt.
