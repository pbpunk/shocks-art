# Agent Instructions

- Keep Gemini bounded to video interpretation and repair.
- Do not add recursive agent strategy loops to the MVP.
- Preserve `Stream -> AnalysisRun -> CandidateWindow -> DerivedAsset -> PublishingRecord -> PerformanceRecord` lineage.
- Update schema versions when changing structured output.
- Use tests with mocked external APIs by default.
- Do not reanalyze completed streams unless the user explicitly asks for that feature.
- Treat host-only evidence as an execution-location problem, not as a reason to require an interactive Codex session.
- Autonomous workstation requests must use the Shock's Art GPT Bridge fixed-profile contract in `docs/google-host-control.md`.
- Never add a Google Sheet field that can provide arbitrary shell/Python/PowerShell, filesystem paths, URLs, Git remotes/branches/refspecs, model identifiers, test selectors, or profile arguments.
- Candidate host verification must be tied to an exact 40-character SHA and run from a detached worktree; a PASS applies only to the exact tested SHA.
- Do not mark host-backed backlog items complete merely because bridge code exists. Require the acceptance evidence from a real workstation receipt.
