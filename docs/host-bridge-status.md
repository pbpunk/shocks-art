# Autonomous Host Bridge Status

Repository implementation is present on the autonomous host-bridge branch.

Implemented:

- exact-SHA request contract;
- fixed profile allowlist;
- detached worktree candidate execution;
- moving-branch invalidation;
- durable local request journal/status;
- Google Verification/State tab bootstrap;
- managed Start/Stop worker lifecycle isolated from web-app health;
- Whisper benchmark, private YouTube probe, and bounded indexer soak profiles;
- backlog schema support for `host` and `user` execution locations;
- safety contract tests and operator documentation.

Not yet proven on the real workstation:

- Google Sheet/service-account connectivity for this Shock's Art bridge;
- a real exact-SHA candidate receipt;
- Whisper benchmark results;
- private YouTube authentication/retrieval results;
- long-duration restart/recovery/contention soak evidence.

Do not close IDX-025, IDX-038, or IDX-042 until those host receipts satisfy their acceptance criteria.
