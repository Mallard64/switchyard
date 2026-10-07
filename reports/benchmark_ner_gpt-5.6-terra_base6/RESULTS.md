# Planted-break benchmark on 50 inputs (re-based on gpt-5.6-terra), Oct 7

**Why terra:** gpt-4 costs about 5× more per call, and the benchmark tests our detection, not gpt-4. The baseline
is terra on the unbroken prompt; each break re-runs terra with one prompt component changed. Terra only runs at
its default temperature, so it is noisy: 12/50 inputs change output between its own runs (gpt-4: 0/20).

**Inputs:** the original 20 plus 30 probes (Oct 7), each written to probe one rule the breaks change
(`probes` field in `inputs/ner.jsonl`). Gold labels written before any run; none dropped. They still
need a teammate's review.

## Side by side

| | gpt-4, 20 inputs (Oct 6) | terra, first 20, 3-run baseline | terra, 50, 3-run baseline | terra, first 20, **6-run baseline** | terra, 50, **6-run baseline** |
|---|---|---|---|---|---|
| Breaks caught | 4/10 | 2/10 | 4/10 | 1/10 | **3/10** |
| Right component named | 4/4 | 2/2 | 4/4 | 1/1 | 3/3 |
| Right component ranked top (old heuristic) | 2/4 | 1/2 | 3/4 | 0/1 | 2/3 |
| Ranked top (POS-aware, Oct 7) | **4/4** | – | – | 1/1 | 2/3 |
| False alarms, unbroken re-run | 0 | – | **1** | – | **0** (held-out re-run) |
| Fix accepted, 1st attempt / within 3 | invalid (see below) | – | 4/4 / 4/4 | – | 3/3 / 3/3 |

*3-run baseline:* "regressed" = most candidate runs below the baseline's worst of 3 runs. *6-run baseline:* the
noise-floor re-run is stacked onto the baseline (worst of 6), and false alarms are measured on a separate
held-out re-run (`rerun2`).

## What changed the numbers, and why

1. **The probes work, but each catch rests on one input.** break06 is caught only by ner-42 ("side of sliced
   tomatoes") and break08 only by ner-48 ("steak came with mashed potatoes"). Both are probes written for those
   breaks, and both fail in 3/3 runs.
2. **With 3 baseline runs, a noisy model produces false alarms.** The terra re-run flagged ner-18, and break05's
   only "catch" was that same input: noise, not detection. With the 6-run baseline, both disappear (0 false
   alarms on a held-out re-run).
3. **7 of 10 breaks don't change terra's behavior.** For those breaks, F1 stays within ±0.015 of the baseline
   (often higher) with at most 1 CHANGED input, the same as an unbroken re-run. There's nothing to catch: the
   breaks were written for gpt-4. On terra, the catch rate measures break strength more than detection.

   | | break01 | 02 | 03 | 04 | 05 | 06 | 07 | 08 | 09 | 10 | no break |
   |---|---|---|---|---|---|---|---|---|---|---|---|
   | REGRESSED (6-run) | 0 | 0 | 14 | 0 | 0 | 1 | 0 | 1 | 0 | 0 | 0 |
   | CHANGED | 0 | 1 | 1 | 1 | 0 | 1 | 1 | 0 | 0 | 1 | 0 |
   | lenient F1 (baseline 0.957) | 0.969 | 0.972 | 0.881 | 0.961 | 0.965 | 0.962 | 0.959 | 0.941 | 0.971 | 0.952 | 0.958 |
4. **The old fix rate (gpt-4, "1/4") was not valid.** The old benchmark showed the fixer the *original* prompt
   instead of the broken one and re-ran its fix *without* the break. Both are fixed: the fixer sees the broken
   prompt and the fix is applied on top of the break. Fix runs are now cached by patch content, and earlier
   fixer edits are reused, so re-running never mixes rows from different edits.
5. **POS-aware cause ranking.** On break03 terra tags verbs as entities ("sear", "baste", "thinly"); the old
   heuristic blamed the INGREDIENT definition. Now a spurious span whose head is a verb, adverb, adjective or
   pronoun points to the description's rule ("Adjectives, verbs, adverbs are not entities"); nouns keep the old
   weights. **Caveat:** designed after seeing these failures and scored on the same breaks, so it's a dev-set
   number. The real sol case (`bowl`, a noun) is unaffected.

## Costs
About $5.86 (first terra run) + $0.45 (held-out re-run) + $0.26 (terra baseline extension). No gpt-4 calls.
Re-scoring (`--rescore`) and re-runs from cache cost $0.

## Next, for eval
- **Breaks that terra actually reacts to,** checked with an unbroken re-run as the reference, so the catch rate
  measures detection rather than break strength. Hold them out from tuning.
- More probes per definition, so a catch doesn't rest on a single input.
