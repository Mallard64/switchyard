# Move the `ner_v3_openai` example to `gpt-5.6-sol` before the Oct 23, 2026 model shutdowns

## Summary

- This example uses `spacy.GPT-3-5.v1` (`gpt-3.5-turbo`), and the obvious upgrade, `spacy.GPT-4.v3`, defaults to `gpt-4` (`gpt-4-0613`). OpenAI shuts down both on **Oct 23, 2026** ([deprecations](https://developers.openai.com/api/docs/deprecations)). spacy-llm checks `/v1/models` when a pipeline loads and raises `ValueError` for a missing model, so the example stops loading.
- Swapping `gpt-4` for `gpt-5.6-sol` regressed **1** of 20 inputs (beyond the old model's own run-to-run variation).
- Proposed fix `fix1` (reword `label_definitions.EQUIPMENT`) leaves **0** regressions on a full re-run of all 20 inputs.
- Model: `spacy.GPT-4.v3` with `name = "gpt-5.6-sol"` and `config = {}`.
- `spacy.GPT-4.v3` sends `temperature: 0.0` by default, which `gpt-5.6-sol` rejects; changing only the model name makes every request fail, hence the empty config.

## 1. Where it broke (step)

This pipeline has one LLM step (spacy-llm `ner_v3_openai` example, one LLM call per text), so every regression is in that step.

## 2. What broke (prompt lines)

**ner-16**: "Nothing beats a bowl of pho on a cold day."
- No single line's removal repairs it (likely a *missing* instruction rather than a wrong one). Proven at component level: `label_definitions.EQUIPMENT`, because editing only that component removes the regression (fix).

## 3. Evidence

### Before / after

- `ner-16`: `gpt-4` → ['pho/DISH'] · `gpt-5.6-sol` → ['bowl/EQUIPMENT', 'pho/DISH'] · `gpt-5.6-sol` + fix → ['pho/DISH']

### Scores

| | `gpt-4` (baseline) | `gpt-5.6-sol`, model swap only | `gpt-5.6-sol` + fix |
|---|---|---|---|
| Passed all hard checks | 100% | 100% | 100% |
| lenient entity F1 | 0.97 | 0.97 | 0.98 |
| Inputs regressed vs baseline | – | 1 | 0 |
| Inputs whose output varies between runs | 0 | 2 | 3 |
| Latency p50 | 2.89s | 3.66s | 3.75s |
| Cost per 1k calls | $15.63 | $7.51 | $7.48 |

**Old-model noise floor:** `gpt-4` changed its output between its own runs on 0/20 inputs. An input counts as regressed only if most new-model runs fail a hard check the old model passes, or score below the old model's worst run. A fresh `gpt-4` re-run judged by the same rule flags 0 regressions (false alarms).

## 4. Proposed fixes

Each fix is separate. Review each and accept, edit or reject it; only accepted or edited fixes are applied. An edit is re-verified on the full suite before it counts.

### `fix1`: reword `label_definitions.EQUIPMENT` (awaiting review)

Where: step `llm`, `label_definitions.EQUIPMENT`. Source: fixer LLM (`gpt-5.6-sol`), attempt 1. Rationale: This excludes serving or portion containers while preserving bowls and other vessels actively used in food preparation as equipment.

```diff
-Any kind of cooking equipment. e.g. oven, cooking pot, grill
+Any kind of cooking equipment. e.g. oven, cooking pot, grill. Containers mentioned only as serving vessels or quantity expressions are not equipment.
```

Full re-run with this fix (20 inputs × 3 runs on `gpt-5.6-sol`): REGRESSED 0, IMPROVED 1, CHANGED 2, SAME 17.

Accept: `python migrate.py --task ner --candidates gpt-5.6-sol gpt-5.6-terra --model-config '{}' --accept fix1` · Reject: `python migrate.py --task ner --candidates gpt-5.6-sol gpt-5.6-terra --model-config '{}' --reject fix1` · Edit: `python migrate.py --task ner --candidates gpt-5.6-sol gpt-5.6-terra --model-config '{}' --apply-edit my_edit.json` (see migration.json `edit_format`)

## Other differences (not regressions; worth a look)

- improved: "Sear the steak in a cast-iron skillet, then baste it with butter and thyme.": `gpt-4` ['steak/DISH', 'cast-iron skillet/EQUIPMENT', 'butter/INGREDIENT', 'thyme/INGREDIENT'] → `gpt-5.6-sol` ['steak/INGREDIENT', 'cast-iron skillet/EQUIPMENT', 'butter/INGREDIENT', 'thyme/INGREDIENT']
- changed: "My grandmother's lasagna needs ricotta, mozzarella and a good marinara.": `gpt-4` ["grandmother's lasagna/DISH", 'ricotta/INGREDIENT', 'mozzarella/INGREDIENT', 'marinara/INGREDIENT'] → `gpt-5.6-sol` ['lasagna/DISH', 'ricotta/INGREDIENT', 'mozzarella/INGREDIENT', 'marinara/INGREDIENT']
- changed: "Toast the cumin and coriander seeds in a dry pan, then grind them with a mortar and pestle.": `gpt-4` ['cumin/INGREDIENT', 'coriander seeds/INGREDIENT', 'dry pan/EQUIPMENT', 'mortar and pestle/EQUIPMENT'] → `gpt-5.6-sol` ['cumin/INGREDIENT', 'coriander seeds/INGREDIENT', 'pan/EQUIPMENT', 'mortar and pestle/EQUIPMENT']
- improved: "Microwave the leftover curry for two minutes.": `gpt-4` ['Microwave/EQUIPMENT', 'leftover curry/DISH'] → `gpt-5.6-sol` ['curry/DISH']
- Other candidate `gpt-5.6-terra`: 3 regressed, lenient entity F1 0.96

## Limits

- 20 hand-labelled inputs; accuracy figures depend on those labels.
- `gpt-5.6-sol` only accepts the default temperature, so its outputs vary between runs; an input counts as regressed only if most runs fail.
- Line-level proof only finds lines that are present and over-applied; a missing instruction is proven at component level by the fix instead.
