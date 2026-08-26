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

## Restart survival

The update worker launches `tools/google_update_helper.py` as a detached Windows process. `Update App.cmd` is allowed to stop the web app and both bridge workers. The helper survives that stop, waits for the canonical updater to finish, writes an ignored durable receipt under `data/google_update_receipts/`, and best-effort publishes the receipt to Google.

When the updated app starts, `tools/start_host_worker.ps1` starts both:

- `google_host_worker.py` for host verification;
- `google_update_worker.py` for exact-main updates.

The restarted update worker reconciles any durable helper receipt that was produced while the old worker was stopped.

## State visibility

`State!A:F` remains the host-verifier heartbeat. `State!G:J` is the autonomous-updater heartbeat:

- `update_updated_at`
- `update_status`
- `update_last_request_id`
- `update_last_error`

This makes a failed bootstrap remotely diagnosable without opening workstation log files.

## Exactness

A successful `updated_exact` receipt requires the running checkout after `Update App.cmd` to equal the requested SHA exactly. If `main` moves during the update window and the canonical updater advances farther than requested, the helper reports failure rather than falsely claiming the requested exact-SHA deployment was proven. The newer checkout is not rolled back automatically.
