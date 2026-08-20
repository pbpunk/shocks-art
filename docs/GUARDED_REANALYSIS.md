# Guarded stream reanalysis

Use guarded reanalysis when an existing candidate generation is known to be unreliable but its historical AnalysisRun/raw response should be preserved.

The replacement flow is deliberately **replacement-first**:

1. inspect the currently active CandidateWindow set for the Stream;
2. refuse to proceed if the active set differs from the caller's expected IDs;
3. refuse to proceed if any active candidate is favorited, reviewed beyond `pending_review`, has reviewer notes, is not complete, or has any DerivedAsset;
4. create and run a fresh AnalysisRun through the current candidate validation/evidence gate;
5. if that replacement fails or is quarantined, restore the Stream's prior processing status and leave the old candidates active;
6. if the replacement succeeds, re-check the captured old candidates for concurrent changes;
7. archive only the captured old CandidateWindow rows and record supersession metadata linking them to the replacement AnalysisRun.

Historical AnalysisRun rows and raw responses are never deleted or relabeled as bad merely because some candidates were superseded.

## Dry-run

Dry-run is the default and does not call Gemini:

```powershell
python tools/reanalyze_stream.py <STREAM_ID> \
  --expected-candidate-id <CANDIDATE_1> \
  --expected-candidate-id <CANDIDATE_2> \
  --expected-candidate-id <CANDIDATE_3>
```

The JSON output includes the active candidate set, blockers, and `safe` status.

## Apply

`--apply` requires both the exact expected active candidate set and a reason:

```powershell
python tools/reanalyze_stream.py <STREAM_ID> \
  --expected-candidate-id <CANDIDATE_1> \
  --expected-candidate-id <CANDIDATE_2> \
  --expected-candidate-id <CANDIDATE_3> \
  --reason "temporal evidence cleanup" \
  --apply
```

Apply performs a real Gemini analysis. It should therefore be run only after the dry-run reports `safe: true`.

A successful result reports:

- replacement AnalysisRun ID;
- superseded CandidateWindow IDs;
- replacement CandidateWindow IDs;
- reason.

The old candidates remain in the database with `review_status="archived"` and `_supersession_history` metadata in `emergent_observations`. The previous AnalysisRun remains unchanged and inspectable.

## Safety behavior

If Gemini fails, validation rejects the replacement, or the run is quarantined, the previous candidates remain visible and the Stream's pre-reanalysis processing status is restored. The failed replacement AnalysisRun is preserved for diagnosis.

If a human changes one of the captured old candidates while replacement analysis is running, the replacement generation is archived instead of the old generation and the operation returns a blocker.
