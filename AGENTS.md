# AGENTS.md

## Cursor Cloud specific instructions

### Overview

TicketAlert v2 is a ticket availability notifier for Indian ticketing platforms. It has three components:

- **Python Flask backend** (`backend/app.py`, entry point `start.py`) — API server + background monitor loop + auto-checkout worker. Serves the frontend.
- **Frontend** (`frontend/`) — Vanilla HTML/JS/CSS PWA served by Flask at `/`.
- **Node.js client** (`node-client/`) — Optional standalone API client for automation/checkout flows.

### Running the dev server

```
python3 start.py
# Serves at http://localhost:5000
```

The server requires a `.env` file with VAPID keys. Generate once with `python3 generate_keys.py` and copy the output into `.env` (see `.env.example` for the template). Without VAPID keys, push notifications won't work but the app still runs.

### Key gotchas

- **`USE_BROWSER` env var**: Set to `"false"` for dev unless you need Playwright-based scraping. When `"true"`, the monitor loop uses headless Chromium which is slower and requires `playwright install-deps chromium` on Linux.
- **No database required for local dev**: Without `DATABASE_URL`, the app falls back to `data.json` file storage automatically.
- **Google OAuth is optional**: Without `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`, the app runs in anonymous single-user mode. The login UI still shows but can be ignored for API-level testing.
- **No automated test suite exists**: There are no unit tests, integration tests, or linting configs in this repo. Validation is done via manual API calls and UI interaction.
- **Monitor loop starts automatically**: `start.py` calls `start_monitor()` which spawns a daemon thread. The `/health` endpoint reports monitor status.
- **Playwright system deps**: On Linux, `python3 -m playwright install-deps chromium` installs required system libraries. This is already done in the VM setup but may need re-running if the Playwright version changes.

### Useful API endpoints for testing

- `GET /health` — Server + monitor status
- `GET /api/stats` — Watcher counts
- `GET /api/watchers` — List watchers
- `POST /api/watchers` — Add a watcher (JSON body: `{"url": "...", "name": "...", "interval_seconds": 30}`)
- `DELETE /api/watchers/<id>` — Remove a watcher
- `POST /api/watchers/<id>/check-now` — Force an immediate check

### Supported domains for watchers

URLs must match one of: `bookmyshow.com`, `in.bookmyshow.com`, `district.in`, `insider.in`, `paytm.com/event`, `zomato.com/events`, `ticketnew.com`, `kyazoonga.com`.
