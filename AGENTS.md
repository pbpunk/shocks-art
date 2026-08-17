# Agent Instructions

- Keep Gemini bounded to video interpretation and repair.
- Do not add recursive agent strategy loops to the MVP.
- Preserve `Stream -> AnalysisRun -> CandidateWindow -> DerivedAsset -> PublishingRecord -> PerformanceRecord` lineage.
- Update schema versions when changing structured output.
- Use tests with mocked external APIs by default.
- Do not reanalyze completed streams unless the user explicitly asks for that feature.

