# Move the `ner_v3_openai` example to `gpt-5.6-sol` before the Oct 23, 2026 model shutdowns

## Why

This example uses `spacy.GPT-3-5.v1` (`gpt-3.5-turbo`, which resolves to `gpt-3.5-turbo-0125`), and the obvious upgrade, `spacy.GPT-4.v3`, defaults to `gpt-4` (`gpt-4-0613`). OpenAI shuts down both on **Oct 23, 2026** ([deprecations](https://developers.openai.com/api/docs/deprecations)). spacy-llm checks `/v1/models` when a pipeline loads and raises `ValueError` for a model that isn't listed, so after that date this example stops loading rather than degrading.

## What changes

- Model: `spacy.GPT-4.v3` with `name = "gpt-5.6-sol"`.
- `spacy.GPT-4.v3` sends `temperature: 0.0` by default, which `gpt-5.6-sol` rejects ("Only the default (1) value is supported"). Changing only the model name makes every request fail, so the config sets `config = {}`.
- Prompt: `label_definitions.EQUIPMENT` reworded (diff below). This excludes serving or portion containers while preserving bowls and other vessels actively used in food preparation as equipment.

## Evidence

Baseline: this example's prompt on `spacy.GPT-4.v3` (`gpt-4`), the stronger of the two retiring models. 20 inputs, each run 3 times per model. An input counts as regressed only if most new-model runs fail a hard check the old model passes, or score below the old model's worst run.

| | Baseline (`gpt-4-0613`) | `gpt-5.6-sol`, model swap only | `gpt-5.6-sol` + prompt fix |
|---|---|---|---|
| Passed all hard checks | 100% | 100% | 100% |
| lenient entity F1 | 0.97 | 0.97 | 0.98 |
| Inputs regressed vs baseline | - | 1 | 0 |
| Inputs whose output varies between runs | 0 | 2 | 3 |
| Latency p50 | 2.89s | 3.66s | 3.75s |
| Cost per 1k calls | $15.63 | $7.51 | $7.48 |

Other candidates tested:

- `gpt-5.6-terra`: 2 regressed, lenient entity F1 0.96

## Regressions found, and their cause

**"Nothing beats a bowl of pho on a cold day."**

- `gpt-4`: ['pho/DISH']
- `gpt-5.6-sol`: ['bowl/EQUIPMENT', 'pho/DISH']
- Evidence: spurious: predicted ('bowl', 'EQUIPMENT')
- Cause (confirmed by fix): `label_definitions.EQUIPMENT`. Editing only that component removes the regression, with no new regressions elsewhere.

### Prompt change

```diff
- Any kind of cooking equipment. e.g. oven, cooking pot, grill
+ Any kind of cooking equipment. e.g. oven, cooking pot, grill. Containers mentioned only as serving vessels or quantity expressions are not equipment.
```

## Other differences (not regressions; worth a look)

- changed: "My grandmother's lasagna needs ricotta, mozzarella and a good marinara." — `gpt-4` ["grandmother's lasagna/DISH", 'ricotta/INGREDIENT', 'mozzarella/INGREDIENT', 'marinara/INGREDIENT'] → `gpt-5.6-sol` ['lasagna/DISH', 'ricotta/INGREDIENT', 'mozzarella/INGREDIENT', 'marinara/INGREDIENT']
- changed: "Toast the cumin and coriander seeds in a dry pan, then grind them with a mortar and pestle." — `gpt-4` ['cumin/INGREDIENT', 'coriander seeds/INGREDIENT', 'dry pan/EQUIPMENT', 'mortar and pestle/EQUIPMENT'] → `gpt-5.6-sol` ['cumin/INGREDIENT', 'coriander seeds/INGREDIENT', 'pan/EQUIPMENT', 'mortar and pestle/EQUIPMENT']
- improved: "Microwave the leftover curry for two minutes." — `gpt-4` ['Microwave/EQUIPMENT', 'leftover curry/DISH'] → `gpt-5.6-sol` ['curry/DISH']

## Limits

- 20 hand-labelled inputs; accuracy figures depend on those labels.
- `gpt-5.6-sol` only accepts the default temperature, so its outputs can vary between runs; the regression rule above requires a majority of runs to fail.
