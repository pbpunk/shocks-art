# Cross-modal overlap proof

`cross-modal-overlap-proof` is a bounded, `main-only` workstation verification profile for proving that one canonical YouTube `Media` item can carry both stored Language Traces and newly generated visual evidence under the exact deployed application revision.

The profile exists because the live retrieval-quality corpus currently has language evidence on 44 Media items and exact-generation visual embeddings on 26 Media items, but zero shared Media identities. Temporal fusion cannot be evaluated meaningfully until at least one real Media item has both modalities.

## Fixed target

The versioned profile targets only `media_66612c0710ad4b8ba78e3653256af2fe`, a canonical YouTube Media item already proven to contain timestamped Language Traces relevant to the fixed query `sanding axes`. The Google Sheet cannot supply or override a Media ID, query, URL, source path, model, command, or indexing breadth.

The fixed Media ID is a proof fixture for the current derived corpus. If a later explicit derived-data reinitialization replaces Media IDs, the repository fixture must be deliberately updated from grounded Language Trace evidence before this profile can run again.

## Execution contract

The profile:

1. requires the existing singleton Library indexer worker to be present;
2. refuses to run while unrelated indexing jobs are queued or running;
3. verifies the fixed target exists, is `source_type=youtube`, and already has Language Traces;
4. reuses existing visual Traces when present, otherwise enqueues exactly one scoped `visual-media` job for the target;
5. reuses exact-generation visual embeddings when present, otherwise enqueues exactly one scoped `visual-embeddings` job for the same target;
6. waits for each durable queue job to reach a terminal state;
7. evaluates the fixed `sanding axes` query using the persistent isolated Qwen query runtime, language retrieval, visual retrieval, and the evaluation-only temporal fusion primitive;
8. requires at least one target language match, target visual match, and target fused temporal match;
9. verifies temporary Library scratch bytes return to no more than the pre-run level.

It never runs `visual-pending`, never opts into bulk remote indexing, and never clears or reinitializes derived state.

## Scoring isolation

The proof does not use Media title, filename, source path, or URL in semantic scoring. Language search uses Language Trace text; visual search uses persisted exact-generation embeddings; the evaluation-only fusion layer uses ranks and temporal proximity on the same Media identity. Presentation/source metadata is not a relevance signal.

## Failure meaning

A failure is evidence, not permission to broaden the operation. In particular:

- remote extraction authentication/download failure should be carried forward to the private-YouTube retrieval work rather than bypassed with another downloader;
- worker contention should be resolved by the existing indexer lifecycle rather than spawning a competing worker;
- embedding failure should remain scoped to the one target Media rather than falling back to a global embedding pass;
- zero fused matches after successful extraction/embedding means the target's actual visual/language timing or semantic evidence needs investigation, not a filename/title heuristic.
