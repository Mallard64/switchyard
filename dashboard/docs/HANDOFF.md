# Frontend handoff

## Current scope

The frontend is complete as a local report viewer. It renders supplied JSON, provides animated dashboard storytelling and exposes detailed evidence in Full report. Backend integration is a separate step.

There is no framework, package dependency, authentication system, database, live evaluation job or GitHub-write connection in this version.

## Entry points

- `index.html` loads `styles.css` and `app.js`.
- The final block in `app.js` fetches `./results.json`, selects a normalizer, validates the normalized report and renders it.
- `normalizeMigration` adapts a full pipeline report; `normalizeNER` adapts a flat NER comparison.
- `renderVisualDashboard` and `installDashboardMotion` control the main dashboard.
- `render` and `renderMigration` produce Full report.
- `server.mjs` serves only the page, script, stylesheet and active JSON on localhost.

## Full migration data mapping

| JSON field | UI use |
| --- | --- |
| `baseline` | Reference model metrics |
| `candidates` | Initial candidate comparison and ranking |
| `chosen` | Selected candidate |
| `causes` | Diagnosed regression and supporting evidence |
| `fix_attempts` | Prompt edits, acceptance and retest outcomes |
| `final` | Post-repair summary and readiness reported by the evaluator |
| `comparison` | Final per-input NER results used in detailed evidence |
| `pr` | Proposed repository/path, title and diff |

Keep the full migration fixture as the reference for exact nested fields and units. Cost is USD per 1,000 calls; latency is seconds; accuracy is a fraction. Use `null` for unavailable metrics rather than inventing zeroes.

## Suggested integration sequence

1. Agree on a report endpoint that returns the migration fixture shape.
2. Replace the local JSON fetch with that endpoint, retaining normalization and validation.
3. Add real loading, failure and job-status handling if evaluations are asynchronous.
4. Connect any real run or pull-request actions separately. The current replay and diff preview do not perform these operations.
5. Check the integrated response against the fixture, including missing metrics and final-versus-initial values.

Keep provider credentials and GitHub credentials on the server. Treat model output as untrusted content and preserve the current escaping and HTTP(S)-only link handling.

## Important interpretation rules

- Do not substitute final post-repair values into the initial candidate comparison.
- Do not interpret SAME as proof that an output is correct; it means unchanged.
- Preserve unavailable metrics and distinguish recorded recommendations from verified production readiness.
- The full migration fixture does not provide every initial candidate's per-input report. Do not fabricate those details.
- Legacy dashboard-schema Fix actions are simulated and apply to matching shared cause IDs. Missing after-fix outputs remain unavailable.

## Visual reference

Root files contain the approved black / warm silver / champagne design, including Full report. `versions/graphite/` preserves an earlier HTML/CSS/JS snapshot; it relies on report data from the root and is not a separate deployable app.
