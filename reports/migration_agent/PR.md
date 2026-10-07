# Move the support agent from `gpt-4` to `gpt-5.6-sol` before the Oct 23, 2026 shutdown

## Summary

- OpenAI shuts down `gpt-4` (`gpt-4-0613`) on Oct 23, 2026; after that every step of this agent fails.
- Swapping `gpt-4` for `gpt-5.6-sol` regressed **1** of 24 inputs (beyond the old model's own run-to-run variation).
- Step **`decide`** explains 1/1 regressions (100%), proven by swapping one step at a time.
- Proposed fix `fix1` (remove line 5 of the `decide` prompt) leaves **0** regressions on a full re-run of all 24 inputs.
- `gpt-5.6-sol` only accepts its default temperature, so steps run with `{}` instead of `temperature: 0`, and outputs can vary between runs.

## 1. Where it broke (step)

For each regressed ticket, each step was swapped back to `gpt-4` one at a time (3 runs each, earlier steps replayed from the candidate's own run). A step is causal when swapping it alone makes the ticket stop regressing; it is then checked the other way round (only that step on `gpt-5.6-sol`). How reliable this is: on 9 planted breaks (7 with an effect, 39 tickets), the step-finder named the planted step(s) exactly on 37/39, a correct subset of a two-step break on 2, and a wrong step on 0 (`reports/agent_bench/RESULTS.md`).

| Step | Regressions it explains |
|---|---|
| `classify` | 0/1 (0%) |
| `decide` | 1/1 (100%) |
| `draft` | 0/1 (0%) |
| `tone` | 0/1 (0%) |

`t07`:
- `gpt-5.6-sol` everywhere except `classify` (on `gpt-4`) → REGRESSED (damaged/deny)
- `gpt-5.6-sol` everywhere except `decide` (on `gpt-4`) → SAME (damaged/replace)
- `gpt-5.6-sol` everywhere except `draft` (on `gpt-4`) → REGRESSED (damaged/deny)
- `gpt-5.6-sol` everywhere except `tone` (on `gpt-4`) → REGRESSED (damaged/deny)
- `gpt-4` everywhere except `decide` (on `gpt-5.6-sol`) → REGRESSED (damaged/deny)

## 2. What broke (prompt lines)

**t07**: "The clearance lamp from order A1003 arrived with the base cracked in half. It won't stand up."
- **Proven line** in `decide prompt, line 5`: "Final-sale items are not eligible for refunds or replacements."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (8 lines tested, 3 runs each).

## 3. Evidence

### Before / after

- `t07`: `gpt-4` → damaged/replace · `gpt-5.6-sol` → damaged/deny · `gpt-5.6-sol` + fix → damaged/replace

### Scores

| | `gpt-4` (baseline) | `gpt-5.6-sol`, model swap only | `gpt-5.6-sol` + fix |
|---|---|---|---|
| Passed all hard checks | 100% | 100% | 100% |
| Gold score (category, order ID, action, reply content) | 1.00 | 0.99 | 1.00 |
| Tickets regressed vs baseline | – | 1 | 0 |
| Tickets whose decision varies between runs | 0 | 0 | 0 |
| Pipeline latency p50 (4 steps) | 6.67s | 7.13s | 7.78s |
| Cost per 1k tickets | $38.88 | $12.04 | $11.84 |

**Old-model noise floor:** `gpt-4` made the same decision in all 3 runs on 24/24 tickets. A ticket counts as regressed only if most `gpt-5.6-sol` runs fail a hard check `gpt-4` passes, or score below `gpt-4`'s worst run.

## 4. Proposed fixes

Each fix is separate. Review each and accept, edit or reject it; only accepted or edited fixes are applied. An edit is re-verified on the full suite before it counts.

### `fix1`: remove line 5 of the `decide` prompt (awaiting review)

Where: step `decide`, `decide prompt, line 5`. Source: line-finder (removing this line alone repaired the regression).

```diff
- Final-sale items are not eligible for refunds or replacements.
```

_Removing a line can also drop intent the tests don't cover. If this line states a rule you need, edit it instead of removing it._

Full re-run with this fix (24 inputs × 3 runs on `gpt-5.6-sol`): REGRESSED 0, IMPROVED 0, CHANGED 0, SAME 24.

Accept: `python migrate_agent.py --candidate gpt-5.6-sol --candidate-config '{}' --accept fix1` · Reject: `python migrate_agent.py --candidate gpt-5.6-sol --candidate-config '{}' --reject fix1` · Edit: `python migrate_agent.py --candidate gpt-5.6-sol --candidate-config '{}' --apply-edit my_edit.json` (see migration.json `edit_format`)

## Limits

- 24 synthetic tickets with hand-written expected outcomes; scores depend on those labels.
- **The `decide` prompt line "Final-sale items are not eligible for refunds or replacements." was planted** for this demo, chosen because it breaks only the new model (see `reports/agent/plant_probe.json`). Apart from it, this candidate showed no regressions.
- Line-level proof works when the new model over-applies a line that is present. It cannot find a missing instruction; those are proven at component level by the fix instead.
