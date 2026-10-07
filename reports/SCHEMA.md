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
| `fix_attempts` | array | one entry per fixer attempt, in order (including failed ones) |
| `fixes` | array | **the review list**: one entry per fix that passed full-suite verification, plus engineer edits; each with its own decision. See `fixes[]` |
| `edit_format` | object | the JSON shape `--apply-edit` expects |
| `pr_view` | object | the PR's prose pieces (`title`, `why`, `model_change`, `evidence.table`, `evidence.noise_floor`, `other_differences`, `limits`), for rendering the report page in PR order |
| `final` | object | the state proposed for merge: the model swap plus every *accepted or edited* fix |
| `pr` | object | the generated PR draft (always written; the diff contains the model swap plus accepted fixes only) |
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
| `suspects` | ranked list of `{component, score, editable, text}` from a heuristic. **Only used to order the ablation search; not proof.** |
| `status` | `"confirmed"` once ablation or an accepted fix proves the cause, otherwise `"suspected"` |
| `confirmed_by` | `"ablation"`, `"fix"`, `"ablation+fix"`, or `null` (still suspected) |
| `confirmed_component` | the component ID proven to cause it (from ablation if available, else from the fix) |
| `confirmed_lines` | `[{unit_id, component, text}]` — the sentences whose removal alone makes this input stop regressing. Show these as "the lines that broke". Empty if ablation found none (e.g. the cause is a *missing* instruction, which removal can't reveal) |
| `lines` | every sentence tested: `[{unit_id, component, text, status, verdict, repaired_runs, output, file}]`, confirmed first. `status` is `"confirmed"` / `"no_effect"` / `"error"`; `repaired_runs` like `"3/3"` = runs at or above the baseline's worst score |

`unit_id` is `<component>#<n>`: sentence (or "e.g." example list) number `n` of that component, e.g.
`label_definitions.EQUIPMENT#1` = `"e.g. oven, cooking pot, grill"`.

**Display rule:** say "proven" only when `status == "confirmed"`. For `"suspected"`, say "suspected" and show `suspects`.

## `fixes[]` (review list; Iris's main screen)

Each fix is reviewed on its own. **Passing verification makes a fix eligible; only an engineer's
decision applies it.**

| Field | Meaning |
|---|---|
| `id` | `fix1`, `fix2`, … (or `edit1` for an engineer edit not tied to a proposed fix) |
| `step` | `"llm"` for single-step pipelines; the agent step name (`"decide"`) in `migration_agent` |
| `component` | what it edits, e.g. `label_definitions.EQUIPMENT` or `decide prompt, line 5` |
| `kind` | `"edit"` (replace text) or `"remove_line"` (agent: delete a proven line) |
| `old_text`, `new_text` | before/after text (`new_text` null for `remove_line`) |
| `summary`, `source`, `rationale` | one-line description; who proposed it (fixer LLM attempt n / line-finder / engineer edit); why |
| `caution` | optional warning shown under the diff (e.g. removing a line can drop untested intent) |
| `verification` | `{inputs, runs, verdict_counts, still_regressed, newly_regressed, passes, report_file}` from a **full re-run of every input** with this fix |
| `decision` | `"pending"` (awaiting review), `"accepted"`, `"edited"` (engineer's own text, verified), `"rejected"` |
| `decided_at` | ISO timestamp of the decision, or null |
| `original`, `edit_file` | engineer edits only: the proposed fix it replaced, and the edit file name |

Commands (no re-run for accept/reject):
`migrate.py ... --accept fix1` · `--reject fix1` · `--apply-edit edit.json` (full re-run, re-verify, new PR).
An edit that still regresses stays `"pending"` with its failing `verification` shown.

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

The state proposed for merge: the model swap plus accepted/edited fixes. With no accepted fix it is
the unmodified candidate result.

| Field | Meaning |
|---|---|
| `ready_to_merge` | true if there were no regressions, or an **engineer-accepted** fix removes them all. A verified but `pending` fix does *not* make this true (changed Oct 7) |
| `model` | the chosen candidate name |
| `prompt_edits` | the accepted/edited `edit` objects (list) |
| `prompt_edit` | first of `prompt_edits`, or null (kept for older readers) |
| `report` | filename of the matching `compare_*.json` |
| `verdict_counts` | same shape as in `fix_attempts[]` |
| `candidate` | scorecard (see **baseline / candidate** above) for this final state |

## `pr`

Always written. The diff holds the model swap plus accepted/edited fixes only.

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

## Agent report: `reports/migration_agent/migration.json`

Written by `migrate_agent.py` for the 4-step support agent (the demo lead). It is the same dict that
`pr_report.render()` turns into `PR.md`, so the page can follow PR order:
`title`, `why`, `model_change` → `steps` → `causes[]` → `evidence` → `fixes[]` → `other_differences`, `limits`.

| Field | Meaning |
|---|---|
| `task`, `pipeline`, `old_model`, `new_model`, `candidate_config`, `n_inputs` | what was migrated |
| `steps` | `{names, summary: {step: {explains, of, pct}}, method}` from the step-finder: "step X explains N%" |
| `causes[]` | per regressed ticket: `text`, `baseline_output`, `candidate_output`, `fixed_output`, `causal_steps`, `step_status` (`confirmed` = proven both ways), `step_evidence` (one sentence per swap run), `confirmed_lines` `[{component, text, step, line_index}]`, `lines_tested`, `status`, `confirmed_by` (`ablation` / `step-swap`) |
| `evidence` | `{table: [[label, baseline, swap only, swap + fix]], noise_floor: sentence}` |
| `fixes[]` | same shape as above; `kind: "remove_line"` for line-finder proposals, plus `line_index` (0-based) and `new_lines` (the full new prompt for that step) |
| `ready_to_merge`, `cli`, `edit_format`, `comparison_file` | review state, the command prefix, edit JSON shape, full per-ticket report file |

Supporting files: `reports/agent/stepfinder_<model>.json`, `reports/agent/linefinder_<model>.json`.
Display rule as for NER: say "proven" only for `confirmed` causes, and say clearly that the `decide`
line in this demo was planted (it is in `limits`).
