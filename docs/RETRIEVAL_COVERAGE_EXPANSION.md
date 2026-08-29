# Retrieval coverage expansion

The temporal-fusion evaluator established that retrieval fusion cannot help until Language and visual evidence exist on the same canonical `Media` identity.

## Measured structural result

On live revision `ac1addd6e1f29442732612c93225702d1bbc60c0`, the exact-generation corpus had:

- 44 Media with Language Traces
- 26 Media with exact-generation visual embeddings
- 0 shared Media identities

All five fixed retrieval queries therefore returned zero temporal-fusion matches. This was a corpus-coverage failure, not evidence that lexical weights or fusion weights needed tuning.

A bounded proof then visually indexed and embedded canonical YouTube Media `media_66612c0710ad4b8ba78e3653256af2fe`, selected from the existing grounded Language results for `sanding axes`.

On live revision `5d740809d31f96eae68b524664c33f2202c656cc`, that Media changed from:

- 1,511 Language Traces
- 0 visual Traces
- 0 exact-generation visual embeddings

to:

- 1,511 Language Traces
- 75 visual Traces
- 75 exact-generation visual embeddings

The fixed `sanding axes` query then produced five same-Media temporal-fusion matches. Its visual result ranked that Media first, Language ranked it third, the closest observed same-Media gap was 10.6 seconds, and Library scratch returned to zero bytes. Filename, title, source path, and source URL did not participate in scoring.

The normal five-query retrieval-quality evaluator immediately reflected the new coverage:

- corpus shared-Media count increased from 0 to 1
- `sanding axes` returned five fused matches
- `mixing and pouring epoxy` returned three fused matches on the newly shared Media
- the unresolved `fractal burning setup`, `finished staffs`, and `gluing letters onto a sign` queries still pointed primarily at Language Media without corresponding visual coverage

This is direct evidence that archive modality coverage is currently the gating variable for these misses.

## Next bounded expansion

`retrieval-coverage-expand` is a fixed, `main-only` host profile. It targets exactly three canonical YouTube Media selected from the existing grounded Language Trace evidence:

- `fractal-burning-setup` -> `media_4a2b9b61b1cd44e7bd820ed68dbf207d`
- `finished-staffs` -> `media_0a571dc5e48942fc9b9d98e27609eeb0`
- `gluing-sign` -> `media_53c498d982c14ec680bacf2be2f4dfa0`

The `finished-staffs` target is the transcript candidate explicitly discussing pictures of wizard staffs already made for customers, rather than simply choosing the highest lexical rank.

For each target, the profile:

1. requires canonical `source_type=youtube` Media with existing Language Traces;
2. requires the live singleton indexing worker and refuses to compete with queued/running indexing work;
3. reuses existing visual Traces and exact-generation embeddings when already present;
4. otherwise enqueues exactly one `visual-media` job for that Media;
5. then enqueues exactly one Media-scoped `visual-embeddings` job if needed;
6. requires exact-generation embedding coverage for every resulting visual Trace;
7. requires Library scratch to return to its pre-run level.

It never runs `visual-pending`, never opts into bulk remote indexing, and accepts no Sheet-supplied Media ID, URL, query, path, model, command, or indexing scope.

After the expansion, the ordinary `retrieval-quality` profile remains the evaluator. Semantic success or failure should be judged from the resulting Language/visual candidate overlap and temporal-fusion evidence rather than by adding filename/title/path metadata or lexical heuristics.
