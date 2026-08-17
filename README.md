# Shocks Art Livestream Content System

Local-first MVP for discovering Shocks Art archived YouTube livestreams, extracting ranked source-footage candidates, reviewing the results, ranking the archive, and exporting metadata.

## JARVIS app contract

Shocks Art owns its runtime and declares the external contract in `jarvis.app.json`.

Canonical values:

```text
App ID:       shocks-art
Route:        /shocks_art
Fixed port:   8000
Local app:    http://127.0.0.1:8000/shocks_art/
Health:       http://127.0.0.1:8000/shocks_art/health
API ping:     http://127.0.0.1:8000/shocks_art/api/ping
Public app:   https://desktop.tail27cee7.ts.net/shocks_art/
```

Lifecycle entrypoints:

- `Start App.cmd` — safe/idempotent start, fixed-port ownership check, health wait, runtime metadata, logs, and app-scoped Tailscale routes.
- `Stop App.cmd` — stops only the Shocks Art process that owns the declared runtime.
- `Restart App.cmd` — safe Stop + Start.
- `Update App.cmd` — verifies the repository boundary and clean worktree, fast-forwards from `origin/main`, then restarts and verifies readiness.

Runtime ownership is recorded in `data/runtime.json`; stdout/stderr logs live under `data/logs/`. The legacy `Start Shocks Art Server.cmd` and `Refresh Shocks Art Server.cmd` entrypoints delegate to the canonical JARVIS lifecycle.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
Copy-Item .env.example .env
```

Fill in `.env` with `YOUTUBE_API_KEY` and `GEMINI_API_KEY` for real discovery and analysis.

For Analytics, also configure:

```powershell
YOUTUBE_OAUTH_CLIENT_SECRETS_FILE=path\to\client_secret.json
YOUTUBE_OAUTH_REDIRECT_URI=http://127.0.0.1:8000/analytics/oauth2callback
YOUTUBE_TOKEN_ENCRYPTION_KEY=generate-a-long-local-secret
YOUTUBE_ANALYTICS_BACKFILL_START=2026-06-01
```

The Analytics OAuth flow requests YouTube Analytics readonly, monetary readonly, and YouTube readonly scopes so it can persist channel metrics, revenue metrics, and video/live metadata locally.

The Gemini API lane is useful for experiments, but native YouTube Ask is the preferred video-grounded lane because it has access to YouTube's own video context.

## Run

For normal use, run:

```powershell
.\Start App.cmd
```

The launcher uses the fixed JARVIS port and namespace and opens the canonical public URL when possible.

For direct development only, you can still run Uvicorn yourself on the declared port:

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000/shocks_art/`.

## Analytics Flow

1. Open `/shocks_art/analytics`.
2. Click `Connect YouTube` and finish the Google OAuth flow for the Shocks Art channel.
3. Click `Sync YouTube`.
4. The first sync backfills from `YOUTUBE_ANALYTICS_BACKFILL_START` through today. Later syncs re-fetch from the last successful date minus three days so late YouTube adjustments are captured without re-fetching all history.

Analytics dashboards render from stored SQLite rows, not live API requests. Revenue projections are pace-based calculations only: MTD run rate, trailing 7-day pace, and trailing 28-day pace.

Historical live chat, Super Chat, memberships, and similar event history are not estimated. The schema includes a future event table for prospective capture.

## Tailscale exposure

`Start App.cmd` owns Shocks Art's Tailscale namespace and configures:

```text
Public Funnel:  https://desktop.tail27cee7.ts.net/shocks_art/
Private Serve:  https://desktop.tail27cee7.ts.net:8443/shocks_art/
Upstream:       http://127.0.0.1:8000/shocks_art
```

The app genuinely serves `/shocks_art`; the gateway is not expected to strip the namespace. Shocks Art never claims the JARVIS root route.

## Local Fixture Flow

Use the dashboard button `Load Fixture` to create a sample queued livestream. Automated tests use mocked YouTube and Gemini behavior so they do not spend API quota.

## Real Single-Stream Smoke Test

1. Configure `.env`.
2. Start the server.
3. Click `Discover Streams`.
4. Open `/shocks_art/candidates` after processing, or call:

```powershell
Invoke-WebRequest -Method Post "http://127.0.0.1:8000/shocks_art/api/process?limit=1"
```

The app skips completed streams unless reanalysis is explicitly added later. Each new stream extraction asks Gemini for up to three ranked non-overlapping candidate windows.

## Native YouTube Ask Flow

Use this when you want the same video-grounded behavior as YouTube's Ask button:

1. Start the server.
2. Open `/shocks_art/native-ask`.
3. Pick a stream.
4. Click `Run Native Ask`.
5. Watch the status panel until it imports candidates or reports a browser failure.
6. Review imported candidates in `/shocks_art/candidates`.

The manual paste box on `/shocks_art/native-ask` is a fallback for comparing responses or recovering when YouTube changes the Ask UI.

Raw native Ask responses are stored under `data/native_youtube_responses/` and linked to the corresponding extraction run.

## Native Ask Automation Runner

The app also includes a browser automation runner that uses a persistent local Chromium profile:

```powershell
pip install -e ".[automation]"
playwright install chromium
python tools/native_youtube_ask_runner.py --app-url http://127.0.0.1:8000/shocks_art --stream-id stream_xxx
```

The first run may require signing in or dismissing YouTube UI in the opened browser. On failure, the runner writes screenshots and page HTML under `data/automation_failures/`.

## Tests

```powershell
pytest
```

`tests/test_framework_contract.py` covers the manifest, health identity, API ping, namespaced routes, and JARVIS shell behavior.

## Main Paths

- `jarvis.app.json`: JARVIS runtime/network/lifecycle contract.
- `tools/app_contract.ps1`: shared lifecycle ownership and verification helpers.
- `schemas/candidate_window.schema.v1.json`: Gemini output contract.
- `app/models.py`: asset lineage entities.
- `app/services/processing.py`: discovery, analysis, validation, retry, persistence.
- `app/services/gemini.py`: Gemini prompt and repair prompt boundary.
- `app/services/native_youtube.py`: native YouTube Ask prompt, parser, and persistence.
- `tools/native_youtube_ask_runner.py`: local browser runner for the YouTube Ask panel.
- `app/services/youtube.py`: official YouTube Data API adapter.
- `templates/`: review UI.
