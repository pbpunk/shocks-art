# Persistent Library Indexing Jobs

IDX-040 introduces a durable offline job boundary for Library indexing. FastAPI may inspect or enqueue work in later backlog items, but it does not execute GPU inference in request handlers.

## Durable state

`app.indexing.job_queue.IndexJobQueue` stores queue state in an ignored SQLite database at `data/indexing_jobs.sqlite3` by default. `SHOCKS_INDEX_JOB_DB` may override that host-owned path.

Each job records:

- a generated job ID;
- one fixed repository-owned job type;
- optional Media identity;
- a small validated payload;
- priority and retry budget;
- queued/running/completed/failed state;
- attempt count;
- worker lease owner/expiry;
- progress, result, and error JSON/text;
- timestamps.

The queue accepts only these current job types:

- `visual-media`
- `visual-pending`
- `visual-embeddings`
- `sync-stream-media`

Payload fields are allowlisted per job type. Arbitrary command, script, path, URL, model, or test-selector fields are rejected.

## Singleton ownership

`python -m app.indexing.worker` is the only execution process. It acquires the single `library-indexer` lease before claiming jobs. SQLite `BEGIN IMMEDIATE` transactions serialize lease and claim mutations, preventing two local workers from intentionally owning the queue at the same time.

A heartbeat thread renews both the singleton worker lease and the active job lease while long-running inference executes. If a process dies and the lease expires, the next worker can acquire ownership and recover the stale job.

Stale jobs are requeued while retry budget remains. Once the recorded attempt count reaches `max_attempts`, stale recovery marks the job failed instead of looping forever. Failed/cancelled jobs can be explicitly reset through the queue API for a fresh manual retry.

## Isolation from FastAPI

`app.indexing.job_queue` imports only the Python standard library. The worker imports normal application models before initializing the main database, then imports extraction/Qwen code only inside the fixed dispatch branch that needs it.

This keeps Torch/Qwen and future Whisper runtimes out of web-app startup and request handlers.

## Current scope versus IDX-041

IDX-040 provides persistent queue/state, retry/recovery semantics, and singleton worker ownership.

IDX-041 will add the creator-facing Library controls and diagnostics that enqueue pending/reindex work, expose progress/errors, and implement user-facing retry/cancel behavior. Until then, queue manipulation is an internal application API rather than a public remote-execution surface.
