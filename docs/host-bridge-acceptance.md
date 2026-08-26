# Host Bridge Acceptance Gates

The bridge infrastructure is acceptable for merge when repository review confirms:

- only fixed profile names are accepted;
- only full commit SHAs are accepted;
- candidate revisions are current main or eligible autonomous branch tips;
- candidate runs use detached worktrees;
- Sheet inputs cannot become commands/paths/URLs/profile arguments;
- terminal request IDs cannot be mutated and rerun;
- worker state is durable under ignored `data/`;
- bridge startup failure cannot take down the creator-facing app.

A separate live host proof is required after merge/update to validate Google connectivity and real workstation execution.
