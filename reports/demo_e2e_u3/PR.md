# Demo: the `u3` regression shows up downstream, but the cause is one `classify` line

## Summary

- Planted break for the demo: classify calls late or missing orders cancellations (symptom: decide denies or cancels). Old and new are both `gpt-5.6-terra`; only the new side's prompt changed, so the true cause is known.
- Swapping `gpt-5.6-terra (clean)` for `gpt-5.6-terra+u3` regressed **3** of 24 inputs (beyond the old model's own run-to-run variation).
- Step **`classify`** explains 3/3 regressions (100%), proven by swapping one step at a time.
- Proposed fix `fix1` (remove line 5 of the `classify` prompt) leaves **0** regressions on a full re-run of all 24 inputs.

## 1. Where it broke (step)

For 3 regressed tickets, each step was swapped back to the old side one at a time (1 run each, earlier steps replayed); a causal step is then checked the other way round (only that step on the new side).

| Step | Regressions it explains |
|---|---|
| `classify` | 3/3 (100%) |
| `decide` | 0/3 (0%) |
| `draft` | 0/3 (0%) |
| `tone` | 0/3 (0%) |

● = new model ran that step, ○ = old model. ✓ = the ticket came out like the old pipeline.

**`t09`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ shipping/info |
| New pipeline | ● new | ● new | ● new | ● new | ✗ cancel/deny |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✓ shipping/info |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ cancel/deny |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ cancel/deny |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✗ cancel/deny |
| Only classify new | ● new | ○ old | ○ old | ○ old | ✗ cancel/deny |

<details><summary>t10: same experiments</summary>

**`t10`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ shipping/replace |
| New pipeline | ● new | ● new | ● new | ● new | ✗ cancel/deny |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✓ shipping/replace |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ cancel/deny |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ cancel/deny |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✗ cancel/deny |
| Only classify new | ● new | ○ old | ○ old | ○ old | ✗ cancel/deny |

</details>

<details><summary>t23: same experiments</summary>

**`t23`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ shipping/replace |
| New pipeline | ● new | ● new | ● new | ● new | ✗ cancel/deny |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✓ shipping/replace |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ cancel/deny |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ cancel/deny |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✗ cancel/deny |
| Only classify new | ● new | ○ old | ○ old | ○ old | ✗ cancel/deny |

</details>

## 2. What broke (prompt lines)

**t09**: "Where is my blender? Order A1004 was supposed to arrive on the 4th."
- **Proven line** in `classify prompt, line 5`: "Tickets about late or missing orders are cancel requests."
  - Removing only this line makes the input stop regressing on `gpt-5.6-terra+u3`; removing any other line of the same prompt does not (6 lines tested, 3 runs each).

**t10**: "Order A1010 still hasn't shown up. The tracking page said September 26. That's over a week ago."
- **Proven line** in `classify prompt, line 5`: "Tickets about late or missing orders are cancel requests."
  - Removing only this line makes the input stop regressing on `gpt-5.6-terra+u3`; removing any other line of the same prompt does not (6 lines tested, 3 runs each).

**t23**: "my order a1010 never came?? its been forever"
- **Proven line** in `classify prompt, line 5`: "Tickets about late or missing orders are cancel requests."
  - Removing only this line makes the input stop regressing on `gpt-5.6-terra+u3`; removing any other line of the same prompt does not (6 lines tested, 3 runs each).

## 3. Evidence

### Before / after

- `t09`: `gpt-5.6-terra (clean)` → shipping/info · `gpt-5.6-terra+u3` → cancel/deny · `gpt-5.6-terra+u3` + fix → shipping/info
- `t10`: `gpt-5.6-terra (clean)` → shipping/replace · `gpt-5.6-terra+u3` → cancel/deny · `gpt-5.6-terra+u3` + fix → shipping/replace
- `t23`: `gpt-5.6-terra (clean)` → shipping/replace · `gpt-5.6-terra+u3` → cancel/deny · `gpt-5.6-terra+u3` + fix → shipping/replace

### Scores

| | `gpt-5.6-terra (clean)` (baseline) | `gpt-5.6-terra+u3`, model swap only | `gpt-5.6-terra+u3` + fix |
|---|---|---|---|
| Passed all hard checks | 100% | 100% | 100% |
| Gold score | 1.00 | 0.94 | 1.00 |
| Tickets regressed vs old | – | 3 | 0 |
| Cost per 1k tickets | $4.81 | $4.53 | $4.63 |

**Old-model noise floor:** 1 run per configuration (kept minimal for cost); the old side has 3 cached runs.

## 4. Proposed fixes

Each fix is separate. Review each and accept, edit or reject it; only accepted or edited fixes are applied. An edit is re-verified on the full suite before it counts.

### `fix1`: remove line 5 of the `classify` prompt (awaiting review)

Where: step `classify`, `classify prompt, line 5`. Source: line-finder (removing this line alone repaired the sampled regressions).

```diff
- Tickets about late or missing orders are cancel requests.
```

Full re-run with this fix (24 inputs × 1 runs on `gpt-5.6-terra+u3`): REGRESSED 0, IMPROVED 0, CHANGED 0, SAME 24.

Accept: `python demo_e2e.py --break u3 --accept fix1` · Reject: `python demo_e2e.py --break u3 --reject fix1` · Edit: `python demo_e2e.py --break u3 --apply-edit my_edit.json` (see migration.json `edit_format`)

## Limits

- 1 run per configuration and 3 tickets localized: enough to show the method, not a noise-robust measurement.
- Planted break; 24 synthetic tickets.
