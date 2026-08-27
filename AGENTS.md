# Agent Instructions

- For production livestream Clips, Gemini interaction must occur only through the native Ask interaction on the YouTube page. Never wire direct Gemini API video analysis or repair into Clips Update, production routes, autonomous refresh, or reanalysis.
- Preserve historical direct-Gemini AnalysisRun/CandidateWindow lineage for diagnosis during normal operation, but do not surface non-Ask candidates in the production Clips feed and do not let them suppress a replacement native-Ask import. A separately implemented destructive derived-data reinitialization may remove that historical working-database lineage only after explicit user approval, with a safety backup and canonical-source rebuild receipt.
- During an explicitly approved derived-data reinitialization, unpublished/orphan `DerivedAsset` database rows may be cleared after a safety backup while referenced output files remain untouched; any `PublishingRecord` or `PerformanceRecord` lineage must still fail closed.
- Keep Gemini bounded to the YouTube-native editorial interpretation path; local Searchable Media Memory indexing remains local-first.
- Do not add recursive agent strategy loops to the MVP.
- Preserve `Stream -> AnalysisRun -> CandidateWindow -> DerivedAsset -> PublishingRecord -> PerformanceRecord` lineage.
- Update schema versions when changing structured output.
- Use tests with mocked external APIs by default.
- Do not reanalyze completed native-Ask streams unless the user explicitly asks for that feature.
- Treat host-only evidence as an execution-location problem, not as a reason to require an interactive Codex session.
- Autonomous workstation requests must use the Shock's Art GPT Bridge fixed-profile contract in `docs/google-host-control.md`.
- Autonomous production updates must use the exact-`origin/main` `Updates` contract in `docs/google-update-control.md`; never invent another remote execution path.
- Never add a Google Sheet field that can provide arbitrary shell/Python/PowerShell, filesystem paths, URLs, Git remotes/branches/refspecs, model identifiers, test selectors, or profile arguments.
- Candidate host verification must be tied to an exact 40-character SHA and run from a detached worktree; a PASS applies only to the exact tested SHA.
- A successful autonomous update receipt applies only when the running checkout exactly equals the requested SHA; do not claim success from a merely newer or related revision.
- Do not mark host-backed backlog items complete merely because bridge code exists. Require the acceptance evidence from a real workstation receipt.
