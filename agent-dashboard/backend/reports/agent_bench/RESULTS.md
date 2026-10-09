# Step-finder benchmark: 4-step support agent, Oct 7

**Question:** when a new model breaks one step (or two) of a multi-step agent, does the step-finder name
exactly that step and prove it?

**Setup** (written before any run): old model `gpt-5.6-terra`, new model `gpt-5.6-sol`, both at their default
sampling (gpt-4 not needed to test localization). Each break changes only the **new model's** prompt for the
planted step(s), simulating "the new model misreads step k"; the old model keeps the clean prompt. All 24
tickets × 3 runs per break. Steps before the broken one are replayed from sol's clean run. t07 already regresses
with clean sol vs terra (the planted final-sale line), so it's excluded as background. Up to 8 regressed
tickets per break are localized (the first 8 by ID). Cost ≈ $12.85 (sol $11.49, terra $1.09, LLM baseline
$0.27).

| Break | Planted in | Effect | Tickets localized |
|---|---|---|---|
| c1: money-back requests routed to billing | classify | 4 regressed | 4 |
| c2: order ID extracted without its letter | classify | 19 | 8 |
| d1: upset/urgent customers get what they ask | decide | **0** (sol ignored it) | – |
| d2: damaged items refunded, not replaced | decide | 2 | 2 |
| r1: no order IDs/amounts/timelines in the reply | draft | 11 | 8 |
| r2: always promise a refund if still unhappy | draft | **0** | – |
| t1: tone step shortens every reply to 20 words | tone | 1 | 1 |
| t2: tone step wraps its JSON in a code block | tone | 23 | 8 |
| p1: c2 + r1 together | classify + draft | 19 | 8 |

## Results (39 localized tickets)

| | Step-finder, held-out | Step-finder, after narrowing fix (dev-set) | LLM "which step failed?" (gpt-5.6-sol) |
|---|---|---|---|
| Exactly the planted step(s) | 34 | **37** | 31 |
| Confirmed subset of a two-step plant | 2 | 2 | – |
| Over-attributed (planted step + extra steps) | 3 | 0 | – |
| Wrong step | **0** | **0** | 0 |
| Confirmed both ways (swap back repairs; new model on that step alone reproduces) | 38 | 39 | n/a (no proof) |
| Two-step break p1: both steps found where both mattered | 6/8 | 6/8 | **0/8** (names one step) |

- **Correct subset:** on 2 p1 tickets, fixing classify alone repairs the ticket and the classify break alone
  reproduces it. The draft break didn't matter for those tickets, so a single cause is the right answer.
- **Over-attribution (held-out):** on t08, t13 and t15, several single-step swaps "repaired" the ticket, partly
  from run-to-run noise downstream of the real cause, and the joint check couldn't separate them. **Fix (after
  seeing this):** when more than one step repairs a ticket, each is tested alone (new model only at that step),
  and only the steps that reproduce the failure are kept. All three narrowed to the planted step.

## What this does and doesn't show
- **The step-finder localizes and proves.** 0 wrong steps; every answer is backed by two experiments.
- **This benchmark does *not* reproduce the "LLM attribution is right only 14–29%" claim.** Our planted breaks
  leave visible fingerprints in the trace (an order ID without its letter, JSON in a code block), and the
  attributor saw the step instructions and the expected outcome. It named a planted step on 39/39 tickets
  (31/39 exact, failing only where two steps were planted). Who&When failures are much subtler. The defensible
  difference here: the step-finder **proves** its answer and finds **multi-step** causes; an LLM guess does neither.
- 2 of 9 breaks had no effect on sol; terra/sol are robust to some prompt edits.
- Synthetic setup: breaks are prompt changes standing in for model behavior changes; 24 synthetic tickets.
