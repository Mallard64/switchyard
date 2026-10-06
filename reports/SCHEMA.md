# `migration.json` schema

Written by `migrate.py` to `reports/migration_<task>/migration.json`. This is the dashboard's
input. Generated from the real run in `reports/migration_ner/migration.json` (task `ner`,
candidates `gpt-5.6-sol` / `gpt-5.6-terra`, chosen `gpt-5.6-sol`).

## Top level

| Field | Type | Meaning |
|---|---|---|
| `task` | string | `"ner"` or `"textcat"` |
| `started`, `finished` | ISO timestamp | wall-clock bounds of the run |
| `baseline` | object | old model's scores — see **baseline / candidate** below |
| `candidates` | array | every `--candidates` model tried, ranked best first |
| `chosen` | string | the candidate name that won the ranking |
| `model_config` | object or null | the `--model-config` override passed in, e.g. `{}` |
| `editable_components` | object | prompt pieces a fix could touch: `{component_id: current_text}` |
| `causes` | array | one entry per regressed input on the chosen candidate |
| `fix_attempts` | array | one entry per fixer attempt, in order |
| `final` | object | the state actually proposed for merge (post-fix if a fix was accepted) |
| `pr` | object or null | the generated PR, or `null` if regressions are unfixed |
| `comparison` | object | full per-input report for `final` (same shape `compare.py --json` writes) |

## `baseline` / each candidate's scorecard

`baseline` and `candidates[i]` (and `final.candidate`) share this shape:

| Field | Type | Meaning |
|---|---|---|
| `model` | string | name passed to the API |
| `resolved_model` | array[string] | actual model ID(s) the API reported back |
| `outputs` | int | successful calls scored (inputs × runs) |
| `passed_all` | float 0–1 | fraction of outputs passing every hard check |
| `checks` | object | pass rate per hard check, e.g. `{"format": 1.0, "valid_labels": 1.0, ...}` |
| `accuracy` | float | score against gold labels |
| `accuracy_metric` | string | what `accuracy` is, e.g. `"lenient entity F1"` |
| `unstable_inputs` | int | inputs whose output differed between the 3 runs |
| `latency_p50_s`, `latency_p95_s` | float | seconds |
| `cost_per_1k_calls_usd` | float or null | null until `PRICES` has a rate for this model |

`candidates[i]` additionally has `regressed`, `verdict_counts`, `rank`, and `report` (the
`compare_<model>.json` filename with the full per-input detail).

## `causes[]`

One entry per input that regressed on the chosen candidate.

| Field | Meaning |
|---|---|
| `input_id` | e.g. `"ner-16"` |
| `text` | the input text |
| `baseline_output`, `candidate_output` | human-readable outputs, e.g. `"bowl/EQUIPMENT"` |
| `evidence` | strings explaining what went wrong (failed check, wrong/spurious/missed label, boundary) |
| `suspects` | ranked list of `{component, score, editable, text}` — which prompt piece is implicated, and whether a config patch can edit it |
| `status` | `"suspected"` until a fix removes the regression, then `"confirmed"` |
| `confirmed_component` | the component ID that fixed it, once confirmed |

## `fix_attempts[]`

One entry per attempt the fixer LLM made (`--max-fix-attempts`, default 3).

| Field | Meaning |
|---|---|
| `attempt` | 1-based attempt number |
| `edit` | `{component, new_text, rationale, old_text}` — the fixer's proposed change |
| `patch_file` | the `fixN_patch.json` applied to the config for this attempt |
| `report` | the `compare_<model>_fixN.json` for this attempt's full re-run |
| `verdict_counts` | `{REGRESSED, IMPROVED, CHANGED, SAME}` counts after the patch |
| `still_regressed`, `newly_regressed` | input IDs that stayed broken / broke anew |
| `accepted` | true only if `verdict_counts.REGRESSED == 0` |
| `outcome` | one-line summary, e.g. `"accepted: 0 regressions"` |

An attempt can also be `{"attempt": n, "error": "..."}` if the fixer call or re-run failed outright.

## `final`

The state actually proposed for merge: the post-fix result if a fix was accepted, otherwise the
unmodified candidate result.

| Field | Meaning |
|---|---|
| `ready_to_merge` | true if there were no regressions, or a fix removed them all |
| `model` | the chosen candidate name |
| `prompt_edit` | the accepted `edit` object, or null if no fix was needed/accepted |
| `report` | filename of the matching `compare_*.json` |
| `verdict_counts` | same shape as in `fix_attempts[]` |
| `candidate` | scorecard (see **baseline / candidate** above) for this final state |

## `pr`

Null if `final.ready_to_merge` is false. Otherwise:

| Field | Meaning |
|---|---|
| `title` | PR title |
| `body_file`, `diff_file` | filenames, `PR.md` and `pr.diff`, written alongside `migration.json` |
| `config_file` | the patched config written to the same directory |
| `target_repo`, `target_path` | where this would land upstream, e.g. `explosion/spacy-llm`, `usage_examples/ner_v3_openai/fewshot.cfg` |
| `diff` | the unified diff as a string (same content as `diff_file`) |

**No PR is opened automatically.** This object only describes a draft; posting it is a human decision.

## `comparison`

The full per-input report for `final`, in the same shape `compare.py --json` writes.

| Field | Meaning |
|---|---|
| `task`, `baseline_file`, `candidate_file` | what was compared |
| `inputs_compared` | count |
| `verdict_counts` | `{REGRESSED, IMPROVED, CHANGED, SAME}` |
| `ready_to_merge` | bool |
| `baseline`, `candidate` | scorecards (see above) |
| `instructions_implicated` | hard-check instructions broken by any regression, `{}` if none |
| `items` | array, one per input — see below |

### `comparison.items[]`

| Field | Meaning |
|---|---|
| `input_id`, `text`, `tricky` | which input, and whether it's flagged as a hard case |
| `verdict` | `REGRESSED`, `IMPROVED`, `CHANGED`, or `SAME` |
| `regressions`, `improvements` | what changed and why, e.g. a failed hard check or an accuracy delta across the 3 runs |
| `intermittent_failures` | hard checks that failed in a minority of candidate runs (not enough to count as a regression) |
| `baseline`, `candidate` | each: `{output, score_runs (per-run accuracy), errors (NER only: boundary/wrong_label/missed/spurious), stable, failing_raw_output}` |

A verdict of `CHANGED` means the output differs but isn't worse — these need a human or LLM judge.
Nothing in `items[]` needs a judge to interpret `REGRESSED` or `SAME`.
