# Move the support agent from `gpt-4` to `gpt-5.6-sol` before the Oct 23, 2026 shutdown

## Summary

- OpenAI shuts down `gpt-4` (`gpt-4-0613`) on Oct 23, 2026; after that every step of this agent fails.
- Swapping `gpt-4` for `gpt-5.6-sol` regressed **3** of 12 inputs (beyond the old model's own run-to-run variation).
- Step **`tone`** explains 2/3 regressions (67%), proven by swapping one step at a time.
- Proposed fix `edit1` (engineer edit of the `decide + draft + tone` prompts) leaves **0** regressions on a full re-run of all 12 inputs.
- `gpt-5.6-sol` only accepts its default temperature, so steps run with `{}` instead of `temperature: 0`, and outputs can vary between runs.

## 1. Where it broke (step)

For each regressed ticket, each step was swapped back to `gpt-4` one at a time (3 runs each, earlier steps replayed from the candidate's own run). A step is causal when swapping it alone makes the ticket stop regressing; it is then checked the other way round (only that step on `gpt-5.6-sol`). How reliable this is (synthetic benchmark, 9 planted breaks, 39 tickets, held-out): the step-finder named exactly the planted step(s) on 34/39, confirmed both ways on 38/39, and named a wrong step on 0 (`reports/agent_bench/RESULTS.md`).

| Step | Regressions it explains |
|---|---|
| `classify` | 1/3 (33%) |
| `decide` | 0/3 (0%) |
| `draft` | 1/3 (33%) |
| `tone` | 2/3 (67%) |

● = new model ran that step, ○ = old model. ✓ = the ticket came out like the old pipeline.

**`t27`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ refund/refund |
| New pipeline | ● new | ● new | ● new | ● new | ✗ refund/refund |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✓ refund/refund |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ refund/refund |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ refund/refund |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✓ refund/refund |
| Only classify + tone new | ● new | ○ old | ○ old | ● new | ✓ refund/refund |

<details><summary>t29: same experiments</summary>

**`t29`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ cancel/cancel |
| New pipeline | ● new | ● new | ● new | ● new | ✗ cancel/cancel |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✗ cancel/cancel |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ cancel/cancel |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✗ cancel/cancel |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✓ cancel/cancel |
| Only tone new | ○ old | ○ old | ○ old | ● new | ✗ cancel/cancel |

</details>

<details><summary>t36: same experiments</summary>

**`t36`**

| Experiment | classify | decide | draft | tone | Result |
|---|---|---|---|---|---|
| Old pipeline | ○ old | ○ old | ○ old | ○ old | ✓ refund/refund |
| New pipeline | ● new | ● new | ● new | ● new | ✗ refund/refund |
| Swap classify back to old | ○ old | ● new | ● new | ● new | ✗ refund/refund |
| Swap decide back to old | ● new | ○ old | ● new | ● new | ✗ refund/refund |
| Swap draft back to old | ● new | ● new | ○ old | ● new | ✓ refund/refund |
| Swap tone back to old | ● new | ● new | ● new | ○ old | ✓ refund/refund |
| Only draft new | ○ old | ○ old | ● new | ○ old | ✗ refund/refund |

</details>

## 2. What broke (prompt lines)

**t27**: "Two orders came this week, A1005 and A1011. The mugs are fine, but I'd like to send back the scarf from A1011, it's unopened."
- **Proven line** in `classify prompt, line 1`: "You are the triage step of a customer-support pipeline for an online store."
- **Proven line** in `tone prompt, line 3`: "It must not promise anything beyond the decision."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (11 lines tested, 3 runs each).

**t29**: "Please cancel order A1006. Also, can you refund the shipping fee I paid on it?"
- **Proven line** in `tone prompt, line 3`: "It must not promise anything beyond the decision."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (6 lines tested, 3 runs each).

**t36**: "I want to return the scarf from A1011, unopened. Please refund $60 to cover the shipping I paid too."
- **Proven line** in `draft prompt, line 8`: "Never promise anything the decision does not include."
- **Proven line** in `draft prompt, line 2`: "State the decision clearly and give the reason in plain words."
  - Removing only this line makes the input stop regressing on `gpt-5.6-sol`; removing any other line of the same prompt does not (11 lines tested, 3 runs each).

## 3. Evidence

### Before / after

- `t27`: `gpt-4` → refund/refund · `gpt-5.6-sol` → refund/refund · `gpt-5.6-sol` + fix → refund/refund
- `t29`: `gpt-4` → cancel/cancel · `gpt-5.6-sol` → cancel/cancel · `gpt-5.6-sol` + fix → cancel/cancel
- `t36`: `gpt-4` → refund/refund · `gpt-5.6-sol` → refund/refund · `gpt-5.6-sol` + fix → refund/refund

### Scores

| | `gpt-4` (baseline) | `gpt-5.6-sol`, model swap only | `gpt-5.6-sol` + fix |
|---|---|---|---|
| Passed all hard checks | 92% | 78% | 100% |
| Gold score (category, order ID, action, reply content) | 0.96 | 0.99 | 1.00 |
| Tickets regressed vs baseline | – | 3 | 0 |
| Tickets whose decision varies between runs | 0 | 0 | 0 |
| Pipeline latency p50 (4 steps) | 7.60s | 7.82s | 6.92s |
| Cost per 1k tickets | $41.27 | $14.67 | $12.52 |

**Old-model noise floor:** `gpt-4` made the same decision in all 3 runs on 12/12 tickets. A ticket counts as regressed only if most `gpt-5.6-sol` runs fail a hard check `gpt-4` passes, or score below `gpt-4`'s worst run.

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

Full re-run with this fix (12 inputs × 3 runs on `gpt-5.6-sol`): REGRESSED 0, IMPROVED 3, CHANGED 0, SAME 9.

## Other differences (not regressions; worth a look)

- improved: `t28` "I'd like to return the backpack from order A1007. I took it on one hik": refund/refund → refund/deny
- improved: `t33` "Two of the mugs in order A1005 arrived shattered. I don't want replace": damaged/refund → damaged/replace
- improved: `t35` "Hola, el pedido A1005 llegó con dos tazas rotas. ¿Me pueden ayudar?": damaged/replace → damaged/replace

## Limits

- 24 synthetic tickets with hand-written expected outcomes; scores depend on those labels.
- **The `decide` prompt line "Final-sale items are not eligible for refunds or replacements." was planted** for this demo, chosen because it breaks only the new model (see `reports/agent/plant_probe.json`). Apart from it, this candidate showed no regressions.
- Line-level proof works when the new model over-applies a line that is present. It cannot find a missing instruction; those are proven at component level by the fix instead.
