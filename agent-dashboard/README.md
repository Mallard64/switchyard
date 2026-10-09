# Switchyard Agent Dashboard

This review branch adds a standalone dashboard in `agent-dashboard/`. The existing root backend and `dashboard/` are preserved. This folder includes the combined frontend, a backend snapshot, saved reports, and recorded runs, so it can be tried independently.

## Start the dashboard

From this folder, run:

```sh
npm start
```

Open **http://localhost:4176/**. The page reads reports from `backend/reports/`, lets you switch among saved migration/evaluation reports, and can replay recorded results without making model calls. It can also show new rows while a backend run is writing them.

## Run the backend

The backend source, input examples, cached results, report fixtures, and dependency list are in `backend/`. To install its Python dependencies:

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Cached runs can be replayed without credentials. New model calls require an `OPENAI_API_KEY` in your shell and may incur API charges. See [backend/README.md](backend/README.md) for the available commands and evaluation details. To watch a run in the dashboard, keep `npm start` running in a second terminal while running the backend command from `backend/`.

## Project boundaries

`backend/` is a copied project snapshot used by this standalone preview. The repository's root backend and original `dashboard/` are separate. Do not commit a real `.env` file or provider keys. The separate Cloudflare site includes the report picker and recorded-run replay controls, and bundles the saved reports plus the run rows needed for replay. New model evaluations still run through the Python backend; rebuild and redeploy this folder to publish their updated reports. The original dashboard remains separate. Hosted preview: https://switchyard-agent-dashboard.switchyard-dashboard.workers.dev/ . Deploying this folder requires access to that Cloudflare account.
