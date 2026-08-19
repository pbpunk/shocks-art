# Candidate temporal transcript-evidence gate

Direct Gemini candidate analysis can describe a real spoken moment while assigning it to the wrong source window. Schema validation alone cannot catch that: internally consistent start/end fields can still be semantically wrong.

Before `save_candidates` persists a new direct-analysis response, the application now checks the latest stored timestamped `StreamTranscript` when one is available:

- a non-empty spoken `transcript_excerpt` must have timestamped `transcript_evidence`;
- the schema already requires every evidence timestamp to be inside the candidate window;
- each declared transcript-evidence claim must be supported by stored JSON3 caption text somewhere inside the selected candidate window (plus a small boundary tolerance);
- long evidence quotes are split into caption-sized chunks, so an evidence timestamp may mark the beginning of a quote that legitimately spans much of the selected window;
- visual-only candidates can use `No verified in-window transcript evidence` and are not rejected for lacking speech evidence;
- when no timestamped raw transcript exists, this gate does not pretend verification is possible. Local transcription can fill that gap later.

A temporal evidence failure occurs before any `CandidateWindow` rows are added. The owning `AnalysisRun` and `Stream` are quarantined. Unlike schema-format errors, this failure does not go through the text-only Gemini repair path because that repair request does not have source video access and therefore cannot safely invent corrected timestamps.

## Historical audit semantics

Older candidates predate the timed `transcript_evidence` requirement, so the read-only audit does not automatically call every legacy row bad simply because the evidence array is empty.

For those rows, the audit uses the stored `transcript_excerpt` itself:

- if all auditable excerpt chunks are supported inside the candidate window, the row passes;
- if some excerpt chunks are supported in-window and other chunks are not, the row fails because that is strong evidence that multiple source windows were combined;
- if none of the excerpt can be matched lexically, the row is `unverifiable` rather than failed because the old field may contain a paraphrase instead of a quotation.

This distinction is only for auditing historical rows. New direct-analysis responses remain subject to the stricter timed-evidence requirement before persistence.

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
