# Shock's Art GPT Bridge: Autonomous Host Verification

The Shock's Art GPT Bridge is a narrow control plane that lets remote GitHub/ChatGPT work request bounded evidence from the Windows workstation without granting arbitrary shell access.

## Architecture

- **GitHub** owns code, tests, benchmark harnesses, and candidate revisions.
- **Shock's Art GPT Bridge (Google Sheet)** carries immutable requests and compact receipts.
- **Windows workstation** owns GPU/CUDA, private media, browser authentication, local performance measurements, and other host-only evidence.
- **The creator-facing web app** remains isolated from bridge failures.

The bridge does not make Google Sheets a general remote-execution surface.

## Verification request schema

The `Verification` tab has these columns:

1. `request_id`
2. `created_at`
3. `expected_revision`
4. `profile`
5. `requester_id`
6. `state`
7. `started_at`
8. `finished_at`
9. `tested_revision`
10. `outcome`
11. `exit_code`
12. `duration_seconds`
13. `summary`
14. `result_json`

Only the first five fields are requester inputs. `expected_revision` must be a full 40-character Git SHA. `profile` must be one of the hard-coded profiles below. The workstation owns all status/result fields.

The `State` tab contains a single worker heartbeat row with:

- `host_updated_at`
- `host_status`
- `host_revision`
- `host_last_request_id`
- `host_last_error`
- `host_active_request_id`

## Allowed profiles

### `whisper-benchmark`

Runs the repository-owned local Whisper/faster-whisper benchmark against a workstation-configured manifest. The request cannot provide media paths, model names, Python, or shell arguments. Host configuration controls the benchmark corpus and allowed local model matrix.

Receipt evidence includes measured runtime, real-time factor, project-term accuracy, segmentation counts, and available GPU-memory measurements.

### `private-youtube-probe`

Tests private-video authentication plus a bounded 30-second source retrieval against a workstation-configured private test video. The Sheet cannot provide a URL, cookies path, browser profile, yt-dlp argument, output path, or command.

Receipt evidence includes metadata latency, partial-retrieval latency/bytes/throughput, and a sanitized video ID.

### `indexer-soak`

Runs a bounded repeated indexing/retrieval validation profile for workstation soak evidence. Duration is workstation configuration and is capped by the repository profile. The Sheet cannot select tests, paths, commands, or duration.

The initial v1 profile records repeated indexing-related test passes, disk free-space delta, and available GPU-memory observations. IDX-042 still requires stronger restart/recovery/contention evidence before it can be closed.

## Exact-SHA safety

Before a request can run, the worker fetches `origin/main` and `origin/autonomous/*`.

A revision is eligible only when it is either:

- the exact current `origin/main` SHA; or
- the exact tip of an `origin/autonomous/*` branch for which current `origin/main` is an ancestor.

The worker creates a detached temporary Git worktree at that exact SHA and runs the fixed profile from the worktree. It verifies the worktree revision and tracked cleanliness after execution. For an autonomous candidate, refs are fetched again after the profile; a branch that moved during verification cannot retain a PASS for the old request.

Candidate verification never checks the PR branch into the production checkout.

## No arbitrary execution

A Sheet row cannot supply:

- a shell command or PowerShell fragment;
- Python source or module name;
- a repository, remote, branch, or refspec;
- a filesystem path;
- a media URL;
- a model identifier;
- a test selector;
- profile arguments.

Those decisions live in versioned repository code or trusted workstation environment configuration.

## Durable local state

Ignored runtime files live under `data/`:

- `google_host_journal.json` — immutable request fingerprint and terminal receipt cache;
- `google_host_worker_status.json` — heartbeat/status snapshot;
- `google_host_worker.pid` — managed worker PID.

Logs live under the app-owned logs directory.

A repeated request ID with changed SHA/profile is rejected. Terminal requests are replayed from the local journal rather than executed twice.

## Lifecycle

`Start App.cmd` starts the creator app first, validates its health, then attempts to start the host worker. If the bridge is not configured, the worker remains disabled. If Google or credentials fail, the web app remains available and startup reports a warning.

`Stop App.cmd` stops the host worker independently before stopping the web process.

## Workstation configuration

The worker reads `SHOCKS_*` values from the local `.env`; these values are never read from the Sheet. See `.env.example`.

Required to enable the worker:

- `SHOCKS_GOOGLE_SPREADSHEET_ID`
- `SHOCKS_GOOGLE_CREDENTIALS`

Profile-specific host inputs remain optional until that profile is requested.

## Backlog semantics

`host` means the work can execute autonomously on the workstation through this bounded bridge. It does **not** mean the task is remotely executable by GitHub Actions alone.

`both` means repository implementation plus definitive host evidence.

`user` is reserved for acceptance that genuinely requires subjective or real-user validation rather than machine evidence.

Codex is a tool that may be used to work on code, not an execution location, so it should not be used as a backlog execution category once host automation exists.
