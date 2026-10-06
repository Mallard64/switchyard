# Switchyard dashboard

A local, dependency-free dashboard for replaying model migration reports. Built with HTML, CSS and JavaScript. It makes no live model calls and creates no real PRs.

## Run

Install Node.js if needed, open this folder in a terminal, and run:

```sh
npm start
```

Open http://localhost:4173. No npm install is needed. An alternative is `python3 -m http.server 4173` from this folder. Do not open index.html directly: browsers may block JSON loading on file URLs.

## Demo

The dashboard opens in a single-page visual overview: model quality bars on a 0–100% scale, before/after verdict distribution, one repair example, and clickable final input results. Click a candidate bar for its initial regression count and latency. Prompt changes expand inline; the proposed code change opens in a dialog. Full report retains all underlying metrics and replay controls.

Full report:

1. Review the initial candidate rankings and the recorded diagnosis / prompt repair.
2. Click Run comparison to replay the final, post-repair evaluation (about 15 seconds).
3. Click a result tile to inspect entities, per-run scores and errors.
4. Expand View proposed diff to inspect the recorded PR proposal. No PR is created.
5. Reset clears replay state.

## Replace data

The active results.json uses migration.json. Replace it with another report and reload.

Supported formats:
- Full migration report: candidates, chosen, causes, fix_attempts, final, pr, and comparison. Initial model summaries are separate from the embedded final per-input comparison. Missing candidate report files are not fabricated. Null costs display as unavailable. PR diffs are previews, with links only when a URL is supplied.
- Flat NER comparison (ner_sol.json): baseline, candidate, items and verdict_counts.
- Original dashboard schema: model grids and simulated cause-level fixes.

All assets are local. The dashboard makes no model calls and works offline while served locally.

## Implementation decisions to confirm with Felix

- The Oct 4 mockup was not provided. This first version uses an original desktop layout.
- A Fix applies to all regressed results across candidates sharing its cause_id, because causes are global in the supplied schema.
- Retirement days are calculated relative to run_id, making a recorded replay stable. The UI labels this explicitly.
- Summary cards initially show recorded totals, while tiles replay. After each fix, pass counts increase only for results associated with that applied fix.
- Missing after_fix_output is shown as unavailable; it is never fabricated. Cause-level retest totals are not presented as individual input runs.
- Some sample results contain placeholder output strings or values that do not agree semantically with status. The dashboard honors supplied status and does not rescore outputs.
- The sample's retirement date, model results and recommendation are fictional. They are not verified provider facts.
- PR link is hidden when null; external links accept only HTTP(S).

## Files

- index.html — page shell
- styles.css — layout and visual styling
- app.js — JSON rendering, replay state, details and fixes
- results.json — active full migration report
- results.migration.json — copy of supplied migration report
- results.ner.json — copy of supplied NER report
- results.sample.json — original dashboard-schema sample
- server.mjs — local development server

## Handoff

Share this source folder or add it to the team repository. Include results.json and this README. No dependencies or node_modules folder are required. Felix can retain this static frontend or port the components into the team's preferred framework.

## Scroll interactions

Comparison bars animate once when visible; exact values remain static. A sticky repair panel follows scroll position through Detect, Repair, and Verify, with clickable stage shortcuts. Final input tiles appear in a short stagger, and the code-review banner fades in. All states represent supplied recorded results, not live model execution. Reduced-motion preference shows all repair stages in normal document flow and disables entrance motion.

## Cinematic visual edition

The overview now has an original model-card hero inspired by Apple's product-page presentation: oversized typography, metallic CSS surfaces, black backgrounds and scroll-linked perspective. Explore the results skips directly to the comparison table. No Apple graphics or remote assets are required. The prior graphite HTML/CSS/JS are retained under versions/graphite for reference (the active results.json remains at project root).
