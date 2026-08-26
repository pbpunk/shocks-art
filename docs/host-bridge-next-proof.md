# First Live Host Bridge Proof

After merge/update and local `.env` configuration, the first live proof should be a harmless exact-SHA `indexer-soak` request against the deployed `origin/main` revision.

Success criteria:

- worker heartbeat is visible in `State`;
- request moves queued -> running -> completed;
- `tested_revision` equals the exact requested SHA;
- outcome is PASS;
- local production checkout SHA/branch does not change because of verification;
- a terminal receipt is persisted in `data/google_host_journal.json`;
- repeating the same request ID does not rerun it.

Then prove an `autonomous/*` candidate SHA in a detached worktree before using the bridge for IDX-025 or IDX-038 evidence.
