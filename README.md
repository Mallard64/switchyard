# Switchyard

A frontend dashboard for reviewing AI model migrations: compare quality, cost and latency, inspect a prompt repair, and review the resulting code change.

The current edition combines a cinematic dashboard with a matching full report in black, warm silver and champagne. Built with plain HTML, CSS and JavaScript, with no external packages or build step.

## Quick start

Install Node.js (a current LTS release is recommended), then run from this folder:

```sh
npm start
```

Open **http://localhost:4173/**. No `npm install` is needed. Keep the terminal running while viewing the app; press Ctrl+C to stop it.

Do not open `index.html` directly: browsers may block loading the report JSON from a file URL. If port 4173 is occupied, use `PORT=4174 npm start` on macOS/Linux.

## What you can explore

- **Dashboard:** scroll-driven model cards, a comparison table with inline bars, a Detect → Repair → Verify story, and clickable evaluation results.
- **Model comparison:** quality/F1, cost per 1,000 calls, median latency, p95 latency and regressions. Missing values keep their position and display as unavailable.
- **Full report:** candidate details, prompt changes, evaluation replay controls and individual test evidence.
- **Code review:** inspect the proposed repository change in a dialog.
- **Accessibility:** reduced-motion preferences disable scroll effects and expose repair stages in normal document flow.

## Data and behavior

The app loads `results.json` when the page opens. Replace that file with a compatible report and reload to display another run.

| File | Purpose |
| --- | --- |
| `results.json` | Active report, currently the supplied migration report |
| `results.migration.json` | Preserved full migration fixture |
| `results.ner.json` | Flat named-entity-recognition comparison fixture |
| `results.sample.json` | Earlier dashboard-schema fixture |

The frontend supports the full migration schema, flat NER comparison schema, and original dashboard schema. See [the handoff guide](docs/HANDOFF.md) for data mapping and integration notes.

### Live backend (samegrade)

When the `samegrade` repo sits next to this folder (or `SAMEGRADE_DIR=/path/to/samegrade npm start`), the server also
reads the migration backend. Nothing is written to it.

| Endpoint | What it returns |
| --- | --- |
| `/api/reports` | Reports the backend has produced: `reports/<name>/dashboard.json` (agent migrations, exported by `dashboard_export.py`) and `reports/migration_ner/migration.json` |
| `/api/report?id=<name>` | One of those reports (ids are whitelisted from the list; no file paths) |
| `/api/live` | Server-Sent Events: every row a run appends to `samegrade/results/*.jsonl` after the server starts |

The page then shows a **report picker** in the header and a **Live** button. The button reads "Live · N runs active"
while runs are writing results; clicking it shows each run's progress (rows, hard checks passed/failed, errors) and the
latest rows. Start a run in samegrade, for example `python demo_e2e.py --break u3`, and tickets appear as they finish.
"Reload report" re-reads the report once a run has rewritten it. **▶ Replay a run** streams a recorded run behind the current report (baseline, candidate or fix re-run) back
through the live view, in recorded order and pacing, compressed to 20 s / 1 min or in real time. Replays make **no model
calls** and work offline (`POST /api/replay`, accepted only from the dashboard page itself). Without the backend, the page falls back to
`results.json` exactly as before.

**This version displays recorded results.** Replay controls animate those results; they do not call models, run evaluations, apply real code changes, or create GitHub pull requests. Model names, dates, scores and recommendations come from the supplied fixtures, not live provider information.

Initial candidate results and final post-repair results are distinct. Unknown costs are not treated as zero, and a report with no regressions can still contain changed or unstable outputs.

## Project structure

```text
switchyard-dashboard/
├── index.html              Page shell
├── styles.css              Dashboard and full-report styles
├── app.js                  Data normalization, rendering and interactions
├── server.mjs              Local development server
├── package.json            Start and syntax-check commands
├── results*.json           Active report and reference fixtures
├── docs/HANDOFF.md         Frontend/backend handoff notes
└── versions/graphite/      Previous visual edition (reference only)
```

The current version lives at the project root. The graphite snapshot is not served by the local server.

## Checks

```sh
npm run check
```

This checks JavaScript syntax. For a manual check, open the dashboard, inspect the comparison table, scroll through the repair story, open a result and the code diff, then switch to Full report and back. Check that missing metrics show as unavailable and that narrow screens remain usable.

## Sharing and scope

Clone this repository and follow Quick start to run the same version. The local server binds to this computer only; it is a development preview, not production hosting.

No API keys are needed for this frontend. Keep any future provider credentials on the backend. `.env` files, dependency folders and logs are excluded by `.gitignore`.
