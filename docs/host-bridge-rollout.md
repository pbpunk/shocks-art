# Host Bridge Rollout Checklist

The autonomous host bridge code can be merged independently of the real workstation proofs. Do not mark the host-backed backlog items done until their receipts exist.

## One-time workstation setup

1. Update the local Shock's Art checkout to the merged revision.
2. Put the Google spreadsheet ID in local `.env` as `SHOCKS_GOOGLE_SPREADSHEET_ID`.
3. Confirm `SHOCKS_GOOGLE_CREDENTIALS` points to the local service-account JSON already used for the private GPT bridge.
4. Share the Shock's Art GPT Bridge Google Sheet with that service account.
5. Configure profile-specific host inputs only for profiles that will be used:
   - `SHOCKS_WHISPER_BENCHMARK_MANIFEST`
   - `SHOCKS_PRIVATE_YOUTUBE_TEST_URL`
   - `SHOCKS_YTDLP_COOKIES_FROM_BROWSER`
   - `SHOCKS_INDEXER_SOAK_SECONDS`
6. Run `Restart App.cmd` once. The web app must remain healthy even if bridge startup reports an error.
7. Confirm `data/google_host_worker_status.json` reports `idle` and the `State` tab heartbeat advances.

## First proof sequence

### Safety proof

Submit deliberately invalid rows and confirm rejection without execution:

- abbreviated SHA;
- branch name instead of SHA;
- unknown profile;
- command-looking profile string;
- reused `request_id` with a changed SHA/profile.

### Exact-SHA candidate proof

1. Use the exact tip SHA of an `autonomous/*` branch whose base includes current `main`.
2. Request one fixed profile.
3. Confirm receipt `tested_revision` exactly matches the requested SHA.
4. Confirm production checkout never changes branch or SHA.
5. Add another commit to the candidate branch and confirm the old PASS does not apply to the new SHA.

### Host profile proofs

Run, in order:

1. `whisper-benchmark` for IDX-025.
2. `private-youtube-probe` for IDX-038.
3. `indexer-soak` after IDX-041 is ready for the fuller IDX-042 soak/restart/recovery proof.

## Backlog migration

The schema now supports execution locations `github`, `host`, `both`, and `user`.

The authoritative backlog migration should be structural and should preserve all existing item text/evidence. Reclassify the pure workstation evidence items from `codex` to `host`; reserve `user` for acceptance that genuinely needs a person. Do not perform a blind text replacement in the backlog because completed historical items may intentionally retain old evidence wording.
