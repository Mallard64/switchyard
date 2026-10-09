# Held-out planted breaks (v2) on gpt-5.6-terra, 50 inputs, Oct 7

10 new breaks, written before any run and not used to tune anything before scoring. Each break first gets a
**screen**: one run on all 50 inputs. It counts as *effective* only if its lenient F1 or hard-check pass rate is
below **all 9 unbroken terra runs** (F1 0.946–0.976, pass rate 1.00). That test doesn't use the regression rule.
Detection, cause ranking and fixes are then scored on the effective breaks, with 3 fresh runs against the 6-run
baseline. Cost $6.67 (terra only).

## Screen

| Break | Change | Screen F1 | Pass rate | Effective |
|---|---|---|---|---|
| v2b01 | description: "Pronouns **that refer to food are** entities." | 0.916 | 0.76 | yes |
| v2b02 | description: + "Cooking methods such as grilling, roasting or frying count as EQUIPMENT." | 0.913 | 1.00 | yes |
| v2b03 | description → "Entities are any words related to food or cooking." | 0.960 | 1.00 | no |
| v2b04 | DISH → "Any food item, including single ingredients." | 0.934 | 1.00 | yes |
| v2b05 | DISH → "Restaurant menu items only; home-cooked food is not a dish." | 0.841 | 0.90 | yes |
| v2b06 | INGREDIENT → "Raw, unprocessed foods only; sauces, cheeses and doughs are not ingredients." | 0.822 | 0.96 | yes |
| v2b07 | INGREDIENT → "Spices and seasonings." | 0.973 | 1.00 | no |
| v2b08 | EQUIPMENT → "Electric appliances only, e.g. blender, microwave, toaster." | 0.890 | 0.98 | yes |
| v2b09 | EQUIPMENT → "Any physical object in the kitchen, e.g. plates, cups, bowls, napkins, countertops." | 0.963 | 1.00 | no |
| v2b10 | EQUIPMENT definition deleted (blank) | 0.959 | 1.00 | no |

Terra ignores 4 of 10 edits, including a blank EQUIPMENT definition: it labels from the label names alone.

## Detection on the 6 effective breaks

| | Held-out result | After the Oct 7 mapping fix (dev-set) |
|---|---|---|
| Caught (all 50 inputs / first 20 only) | **6/6 / 6/6** | same |
| Right component named | 6/6 | 6/6 |
| Right component ranked first | **5/6** | 6/6 |
| Fix accepted on attempt 1 / within 3 | **5/6 / 5/6** | – |
| False alarms (held-out unbroken re-run) | **0** | – |

Regressed inputs per caught break: 13, 8, 5, 12, 16, 13. These are not single-input catches.

**Reading these numbers honestly:** the screen and the regression rule both look at accuracy against gold, so
when a break is strong, "caught" is expected. What the 6/6 shows is that the noise-robust rule (worst of 6 runs)
doesn't suppress real effects, while still producing 0 false alarms. The more informative numbers are component
ranking (5/6) and fixes (5/6).

## The two misses
- **v2b01, ranking.** A failed `no_pronouns` hard check was always blamed on spacy-llm's fixed template. The
  sentence "Pronouns are not entities." actually lives in the editable description. Fixed after this run: a
  check's instruction now maps to the upstream component that contains it (`migrate.check_component`). The fixer
  had already worked around it: it edited the description, and that fix was accepted.
- **v2b05, fix.** Attempts 1 and 2 repaired all 12 regressions but newly broke ner-42 ("The side of sliced
  tomatoes…"): the reworded DISH definition made a raw side count as a dish. Attempt 3 edited the wrong
  component. Full-suite re-verification caught both failures, which is why it exists.
