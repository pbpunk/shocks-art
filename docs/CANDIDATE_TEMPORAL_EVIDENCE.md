# Candidate temporal transcript-evidence gate

Direct Gemini candidate analysis can describe a real spoken moment while assigning it to the wrong source window. Schema validation alone cannot catch that: internally consistent start/end fields can still be semantically wrong.

Before `save_candidates` persists a direct-analysis response, the application now checks the latest stored timestamped `StreamTranscript` when one is available:

- a non-empty spoken `transcript_excerpt` must have timestamped `transcript_evidence`;
- the schema already requires every evidence timestamp to be inside the candidate window;
- each declared transcript-evidence line must also fuzzy-match stored JSON3 caption text near its timestamp;
- visual-only candidates can use `No verified in-window transcript evidence` and are not rejected for lacking speech evidence;
- when no timestamped raw transcript exists, this gate does not pretend verification is possible. Local transcription can fill that gap later.

A temporal evidence failure occurs before any `CandidateWindow` rows are added. The owning `AnalysisRun` and `Stream` are quarantined. Unlike schema-format errors, this failure does not go through the text-only Gemini repair path because that repair request does not have source video access and therefore cannot safely invent corrected timestamps.

## Read-only audit

Audit the most recent direct `gemini-*` candidates against their stored captions:

```powershell
python tools/audit_candidate_evidence.py --limit 100
```

Show only failed or unverifiable rows:

```powershell
python tools/audit_candidate_evidence.py --limit 100 --fail-only
```

Audit one known candidate:

```powershell
python tools/audit_candidate_evidence.py --candidate-id <CANDIDATE_ID>
```

Include structured/native candidates too:

```powershell
python tools/audit_candidate_evidence.py --all-models --limit 100
```

The audit is read-only. It reports `pass`, `fail`, or `unverifiable`; it does not archive, regenerate, or edit candidates.
