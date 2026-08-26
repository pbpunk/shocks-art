# Shock's Art GPT Bridge: Autonomous Updates

The `Updates` tab is the narrow deployment lane for Shock's Art. It exists so a remote GitHub/ChatGPT workflow can deploy a merged `main` revision without opening JARVIS or exposing arbitrary workstation execution.

## Request schema

Requester-owned columns are only:

1. `request_id`
2. `created_at`
3. `expected_revision`
4. `requester_id`

All remaining columns are workstation-owned receipt fields:

- `state`
- `launched_at`
- `finished_at`
- `running_revision`
- `outcome`
- `error`

`expected_revision` must be a full 40-character Git SHA and must equal the exact current `origin/main` at worker preflight.

## Safety boundary

The Sheet cannot supply a command, script, branch, refspec, repository, path, URL, PowerShell argument, environment value, or updater option. The only executable is the repository-owned root `Update App.cmd`.

Before launch the worker:

- fetches `origin/main`;
- requires a clean tracked working tree;
- requires `expected_revision == origin/main` exactly;
- treats an already-running exact revision as a successful no-op;
- requires the current deployed revision to be an ancestor of the requested revision;
- refuses rollback or divergence.

The detached helper repeats the same preflight immediately before invoking the canonical updater.

## Bootstrap and restart survival

The update lane deliberately uses Node built-ins rather than Python Google client libraries. `tools/google_update_worker.mjs`, `tools/google_update_helper.mjs`, and `tools/google_update_common.mjs` use Node's built-in crypto/fetch support with the pinned service-account credential file. This keeps the rescue/deployment path independent of the verifier's Python runtime and packages.

`tools/start_host_worker.ps1` starts the dependency-free update worker first. Host verification is started independently afterward, so verifier or Python failures cannot prevent future exact-main repairs from being deployed.

The update worker launches `tools/google_update_helper.mjs` as a detached Windows process. `Update App.cmd` may stop the web app and both bridge workers while the helper survives that stop. The helper waits for the canonical updater to finish, writes an ignored durable receipt under `data/google_update_receipts/`, and best-effort publishes the receipt to Google.

When the updated app starts, `tools/start_host_worker.ps1` starts both:

- `google_update_worker.mjs` for exact-main updates;
- `google_host_worker.py` for host verification.

The restarted updater reconciles durable helper receipts after restart.

The shared JARVIS contract defines `$DataDir` as the repository `data` directory. A regression test guards this bootstrap requirement because bridge PID files, status files, receipts, and the managed verifier environment all depend on that path existing deterministically.

## State visibility

`State!A:F` is the host-verifier heartbeat. `State!G:J` is the autonomous-updater heartbeat:

- `update_updated_at`
- `update_status`
- `update_last_request_id`
- `update_last_error`

This makes normal worker state visible remotely through the Sheet. Startup failures that occur before either worker launches are surfaced by the JARVIS start command.

## Exactness

A successful `updated_exact` receipt requires the running checkout after `Update App.cmd` to equal the requested SHA exactly. If `main` moves during the update window and the canonical updater advances farther than requested, the helper reports failure rather than falsely claiming the requested exact-SHA deployment was proven. The newer checkout is not rolled back automatically.
