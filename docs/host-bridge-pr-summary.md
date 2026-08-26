# PR Summary: Autonomous Host Bridge

This branch introduces the bounded Shock's Art GPT Bridge host verifier so workstation-only evidence can be requested autonomously without exposing arbitrary shell execution.

Key changes:

- exact-SHA detached verification for current main or eligible autonomous branch tips;
- fixed profiles for Whisper benchmarking, private YouTube probing, and indexer soak evidence;
- durable local status/journaling and Google Verification/State tabs;
- worker lifecycle managed alongside the app but isolated from creator-facing app health;
- execution-location backlog schema (`github`, `host`, `both`, `user`);
- tests and docs covering request validation and security invariants.

Real workstation receipts remain required before host-backed backlog items can be marked done.
