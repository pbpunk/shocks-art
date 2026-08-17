# Shocks Art Livestream Content System

Local-first MVP for discovering Shocks Art archived YouTube livestreams, extracting ranked source-footage candidates, reviewing the results, ranking the archive, and exporting metadata.

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

```powershell
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Analytics Flow

1. Open `/analytics`.
2. Click `Connect YouTube` and finish the Google OAuth flow for the Shocks Art channel.
3. Click `Sync YouTube`.
4. The first sync backfills from `YOUTUBE_ANALYTICS_BACKFILL_START` through today. Later syncs re-fetch from the last successful date minus three days so late YouTube adjustments are captured without re-fetching all history.

Analytics dashboards render from stored SQLite rows, not live API requests. Revenue projections are pace-based calculations only: MTD run rate, trailing 7-day pace, and trailing 28-day pace.

Historical live chat, Super Chat, memberships, and similar event history are not estimated. The schema includes a future event table for prospective capture.

## Run On Tailscale

Bind to all interfaces so other devices on your tailnet can reach the app:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Then open the desktop's Tailscale URL from another tailnet device:

```text
http://100.113.160.106:8001
```

## Local Fixture Flow

Use the dashboard button `Load Fixture` to create a sample queued livestream. Automated tests use mocked YouTube and Gemini behavior so they do not spend API quota.

## Real Single-Stream Smoke Test

1. Configure `.env`.
2. Start the server.
3. Click `Discover Streams`.
4. Open `/candidates` after processing, or call:

```powershell
Invoke-WebRequest -Method Post "http://127.0.0.1:8000/api/process?limit=1"
```

The app skips completed streams unless reanalysis is explicitly added later. Each new stream extraction asks Gemini for up to three ranked non-overlapping candidate windows.

## Native YouTube Ask Flow

Use this when you want the same video-grounded behavior as YouTube's Ask button:

1. Start the server.
2. Open `/native-ask`.
3. Pick a stream.
4. Click `Run Native Ask`.
5. Watch the status panel until it imports candidates or reports a browser failure.
6. Review imported candidates in `/candidates`.

The manual paste box on `/native-ask` is a fallback for comparing responses or recovering when YouTube changes the Ask UI.

Raw native Ask responses are stored under `data/native_youtube_responses/` and linked to the corresponding extraction run.

## Native Ask Automation Runner

The app also includes a browser automation runner that uses a persistent local Chromium profile:

```powershell
pip install -e ".[automation]"
playwright install chromium
python tools/native_youtube_ask_runner.py --app-url http://127.0.0.1:8001 --stream-id stream_xxx
```

The first run may require signing in or dismissing YouTube UI in the opened browser. On failure, the runner writes screenshots and page HTML under `data/automation_failures/`.

## Tests

```powershell
pytest
```

## Main Paths

- `schemas/candidate_window.schema.v1.json`: Gemini output contract.
- `app/models.py`: asset lineage entities.
- `app/services/processing.py`: discovery, analysis, validation, retry, persistence.
- `app/services/gemini.py`: Gemini prompt and repair prompt boundary.
- `app/services/native_youtube.py`: native YouTube Ask prompt, parser, and persistence.
- `tools/native_youtube_ask_runner.py`: local browser runner for the YouTube Ask panel.
- `app/services/youtube.py`: official YouTube Data API adapter.
- `templates/`: review UI.
