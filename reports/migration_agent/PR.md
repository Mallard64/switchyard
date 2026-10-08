# Move the support agent from `gpt-4` to `gpt-5.6-sol` before the Oct 23, 2026 shutdown

## Summary

- OpenAI shuts down `gpt-4` (`gpt-4-0613`) on Oct 23, 2026; after that every step of this agent fails.
- Swapping `gpt-4` for `gpt-5.6-sol` regressed **6** of 24 inputs (beyond the old model's own run-to-run variation).
- Step **`tone`** explains 5/6 regressions (83%), proven by swapping one step at a time.
- Proposed fix `edit1` (engineer edit of the `decide + draft + tone` prompts) leaves **0** regressions on a full re-run of all 24 inputs.
- `gpt-5.6-sol` only accepts its default temperature, so steps run with `{}` instead of `temperature: 0`, and outputs can vary between runs.

## 1. Where it broke (step)

For each regressed ticket, each step was swapped back to `gpt-4` one at a time (3 runs each, earlier steps replayed from the candidate's own run). A step is causal when swapping it alone makes the ticket stop regressing; it is then checked the other way round (only that step on `gpt-5.6-sol`). How reliable this is (synthetic benchmark, 9 planted breaks, 39 tickets, held-out): the step-finder named exactly the planted step(s) on 34/39, confirmed both ways on 38/39, and named a wrong step on 0 (`reports/agent_bench/RESULTS.md`).

| Step | Regressions it explains |
|---|---|
| `classify` | 0/6 (0%) |
| `decide` | 1/6 (17%) |
| `draft` | 0/6 (0%) |
| `tone` | 5/6 (83%) |

● = new model ran that step, ○ = old model. ✓ = the ticket came out like the old pipeline.

**`t01`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ refund/refund |
| New pipeline | ● new | ● new | ● new | ● new | ✗ refund/refund |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✗ refund/refund |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ refund/refund |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ refund/refund |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✓ refund/refund |
| Only tone new | ○ old | ○ old | ○ old | ● new | ✗ refund/refund |

<details><summary>t04: same experiments</summary>

**`t04`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ refund/refund |
| New pipeline | ● new | ● new | ● new | ● new | ✗ refund/refund |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✗ refund/refund |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ refund/refund |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ refund/refund |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✓ refund/refund |
| Only tone new | ○ old | ○ old | ○ old | ● new | ✓ refund/refund |

</details>

<details><summary>t07: same experiments</summary>

**`t07`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ damaged/replace |
| New pipeline | ● new | ● new | ● new | ● new | ✗ damaged/deny |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✗ damaged/deny |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✓ damaged/replace |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ damaged/deny |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✗ damaged/deny |
| Only decide new | ○ old | ● new | ○ old | ○ old | ✗ damaged/deny |

</details>

<details><summary>t11: same experiments</summary>

**`t11`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ cancel/cancel |
| New pipeline | ● new | ● new | ● new | ● new | ✗ cancel/cancel |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✗ cancel/cancel |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ cancel/cancel |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ cancel/cancel |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✓ cancel/cancel |
| Only tone new | ○ old | ○ old | ○ old | ● new | ✓ cancel/cancel |

</details>

<details><summary>t21: same experiments</summary>

**`t21`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ refund/refund |
| New pipeline | ● new | ● new | ● new | ● new | ✗ refund/refund |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✗ refund/refund |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ refund/refund |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ refund/refund |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✓ refund/refund |
| Only tone new | ○ old | ○ old | ○ old | ● new | ✓ refund/refund |

</details>

<details><summary>t24: same experiments</summary>

**`t24`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ cancel/cancel |
| New pipeline | ● new | ● new | ● new | ● new | ✗ cancel/cancel |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✗ cancel/cancel |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ cancel/cancel |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✓ cancel/cancel |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✓ cancel/cancel |
| Only tone new | ○ old | ○ old | ○ old | ● new | ✗ cancel/cancel |

</details>

## 2. What broke (prompt lines)

**t01**: "Hi, I bought the wireless earbuds (order A1001) but I've changed my mind, they're still in the box. Can I get my money back?"
- **Proven line** in `tone prompt, line 3`: "It must not promise anything beyond the decision."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (6 lines tested, 3 runs each).

**t04**: "Return request for my backpack, order A1007. Never used it."
- **Proven line** in `tone prompt, line 3`: "It must not promise anything beyond the decision."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (6 lines tested, 3 runs each).

**t07**: "The clearance lamp from order A1003 arrived with the base cracked in half. It won't stand up."
- **Proven line** in `decide prompt, line 5`: "Final-sale items are not eligible for refunds or replacements."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (8 lines tested, 3 runs each).

**t11**: "Please cancel order A1006, I ordered the wrong chair."
- **Proven line** in `tone prompt, line 3`: "It must not promise anything beyond the decision."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (6 lines tested, 3 runs each).

**t21**: "Hello, the scarf from order A1011 was a gift and they already have one. Unopened. Can I return it?"
- **Proven line** in `tone prompt, line 3`: "It must not promise anything beyond the decision."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (6 lines tested, 3 runs each).

**t24**: "Cancel A1006. Your site is a mess and I shouldn't have to email you for this."
- **Proven line** in `tone prompt, line 4`: "If the draft passes, set verdict to PASS and final_reply to null."
- **Proven line** in `tone prompt, line 3`: "It must not promise anything beyond the decision."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (6 lines tested, 3 runs each).

## 3. Evidence

### Before / after

- `t01`: `gpt-4` → refund/refund · `gpt-5.6-sol` → refund/refund · `gpt-5.6-sol` + fix → refund/refund
- `t04`: `gpt-4` → refund/refund · `gpt-5.6-sol` → refund/refund · `gpt-5.6-sol` + fix → refund/refund
- `t07`: `gpt-4` → damaged/replace · `gpt-5.6-sol` → damaged/deny · `gpt-5.6-sol` + fix → damaged/replace
- `t11`: `gpt-4` → cancel/cancel · `gpt-5.6-sol` → cancel/cancel · `gpt-5.6-sol` + fix → cancel/cancel
- `t21`: `gpt-4` → refund/refund · `gpt-5.6-sol` → refund/refund · `gpt-5.6-sol` + fix → refund/refund
- `t24`: `gpt-4` → cancel/cancel · `gpt-5.6-sol` → cancel/cancel · `gpt-5.6-sol` + fix → cancel/cancel

### Scores

| | `gpt-4` (baseline) | `gpt-5.6-sol`, model swap only | `gpt-5.6-sol` + fix |
|---|---|---|---|
| Passed all hard checks | 96% | 76% | 96% |
| Gold score (category, order ID, action, reply content) | 1.00 | 0.99 | 1.00 |
| Tickets regressed vs baseline | – | 6 | 0 |
| Tickets whose decision varies between runs | 0 | 0 | 0 |
| Pipeline latency p50 (4 steps) | 6.67s | 7.13s | 6.30s |
| Cost per 1k tickets | $38.88 | $12.04 | $10.80 |

**Old-model noise floor:** `gpt-4` made the same decision in all 3 runs on 24/24 tickets. A ticket counts as regressed only if most `gpt-5.6-sol` runs fail a hard check `gpt-4` passes, or score below `gpt-4`'s worst run.

## 4. Proposed fixes

Each fix is separate. Review each and accept, edit or reject it; only accepted or edited fixes are applied. An edit is re-verified on the full suite before it counts.

### `edit1`: engineer edit of the `decide + draft + tone` prompts (accepted with engineer edits)

Where: step `decide + draft + tone`, `decide + draft + tone prompts (3 lines)`. Source: engineer edit.

```diff
-[decide line 5] Final-sale items are not eligible for refunds or replacements.
-[draft line 8] Never promise anything the decision does not include.
-[tone line 3] It must not promise anything beyond the decision.
+[decide line 5] Final-sale items are not eligible for refunds or replacements unless the supplied policy explicitly says it also applies to final-sale items.
+[draft line 8] Never promise anything beyond the decision, except the required standard refund or cancellation details above: the exact amount, original payment method, and 5-7 business day timeline.
+[tone line 3] It must not promise anything beyond the decision, except standard refund or cancellation details already in the draft: the exact amount, original payment method, and 5-7 business day timeline. Keep those details.
```

Full re-run with this fix (24 inputs × 3 runs on `gpt-5.6-sol`): REGRESSED 0, IMPROVED 0, CHANGED 0, SAME 24.

## Limits

- 24 synthetic tickets with hand-written expected outcomes; scores depend on those labels.
- **The `decide` prompt line "Final-sale items are not eligible for refunds or replacements." was planted** for this demo, chosen because it breaks only the new model (see `reports/agent/plant_probe.json`). Apart from it, this candidate showed no regressions.
- Line-level proof works when the new model over-applies a line that is present. It cannot find a missing instruction; those are proven at component level by the fix instead.
