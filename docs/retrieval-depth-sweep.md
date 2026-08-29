# Retrieval Candidate-Depth Sweep

This profile tests whether the fixed cross-modal retrieval misses are caused by the current candidate pool rather than by coverage or temporal policy. It is evaluation-only and does not change production ranking.

## Fixed experiment

The profile owns the same five evaluation queries used by `retrieval-quality`:

- `sanding axes`
- `fractal burning setup`
- `finished staffs`
- `mixing and pouring epoxy`
- `gluing letters onto a sign`

Candidate depths are fixed at **25, 50, 100, and 500**. A single deep Language/visual retrieval pass is reused for every depth so the only changed variable is how many ranked candidates reach same-Media temporal fusion.

The fusion temporal rule is not widened. Filename, title, and path metadata remain excluded from semantic scoring and selection. The profile performs no indexing and no production-state mutation.

## Target diagnostics context

The preceding live target diagnostic established:

- `finished staffs`: the strongest semantic target Language window is global rank 3 and its temporally overlapping visual frame is near global rank 90 with a 360 ms gap. Its fixed Language Trace is `trace_881afad9478944918286f370a9aa1721`.
- `fractal burning setup`: the strongest setup Language window is global rank 1 and has an overlapping visual frame, but that exact nearby frame is around global visual rank 400. Its fixed Language Trace is `trace_1a81f877bcba4a4aa04d647745424d14`.
- `gluing letters onto a sign`: the recovered control has Language rank 1 paired with a visual result inside the top 25 at a 7.64 second gap. Its fixed Language Trace is `trace_15f39d71d81241cbb22519233b4e347e`.

## Live receipt history

The first exact-main live sweep on `f509e96d195d42be66c2db6a2be4ae0b384974a3` completed, but its verbose result was cut at the bridge's 30,000-character persistence boundary. Its preserved portion established that axes remained stable and that fractal first entered the global top five at depth 50 by pairing the rank-1 Language setup window with a global visual-rank-47 candidate at a 109.56-second gap.

A first compact revision on `5ecfdbaa7a1e57cedde0ae9e1ab6e40621544fab` then failed closed before persistence because its encoded receipt was still 51,267 characters against the profile's 28,000-character budget. This confirmed the new guard works, but no complete decision receipt was produced.

The decision-focused sweep on exact deployed main `cd207a99ac1946218505080b7d39750b5a92eaac` passed with a complete receipt in 20.379 seconds. The experiment kept the existing 120,000 ms temporal ceiling, Qwen generation `Qwen/Qwen3-VL-Embedding-2B@9f2f7e710d6d#cfg-de309bcbc2df`, metadata isolation, and read-only execution.

## Decision result

### Sanding axes

The existing control is stable at every measured depth. The expected axes Media owns all five global fused results at 25, 50, 100, and 500. Its representative best pair remains visual rank 1 with a 40.28-second gap.

### Finished staffs

Depth **100** is the first measured pool that recovers the intended semantic moment rather than only generic lexical `finished` evidence.

The fixed wizard-staffs Language Trace `trace_881afad9478944918286f370a9aa1721` is Language rank 3. It cannot fuse at depth 25 or 50. At depth 100 it pairs with visual Trace `trace_d3631c9d9f2c403ea95ee1f159a5b272`, global visual rank 91, at a **360 ms** gap, and that exact semantic anchor reaches the global top five. The same best pair remains present at depth 500.

This is a genuine candidate-pool miss: no temporal widening is required to recover the intended moment.

### Fractal burning setup

The fixed setup Language Trace `trace_1a81f877bcba4a4aa04d647745424d14` is Language rank 1. It has no target fusion at depth 25. At depth 50 it reaches the global top five by pairing with visual Trace `trace_5f024328fd384828a81fed35c5f08366`, global visual rank 47, at a **109,560 ms** gap. The same primary pair remains at depth 100.

That pair is technically inside the unchanged 120-second temporal rule, but it sits close to the boundary and is not equivalent to the much lower-ranked frame that overlaps the exact caption window. Candidate depth therefore does not make this a strong setup proof. This case remains useful evidence for later temporal Association/propagation work and must not be used to justify widening the time window.

### Epoxy control

The global top five is unchanged from depth 50 through 100 and 500. A strong pair remains available with a 480 ms gap. Raising the evaluation pool from 50 to 100 does not perturb this control in the current corpus.

### Gluing-sign control

The fixed sign Language anchor already works at depth 25 and remains stable at every measured depth. Its representative pair has a 7,640 ms gap and a visual result inside the top 25.

## Baseline policy after the sweep

The `retrieval-quality` evaluation baseline now uses **100 candidates per modality**. This is the smallest measured depth that recovers the intended finished-staffs semantic anchor while preserving the already-working axes, epoxy, and sign controls.

The baseline receipt explicitly reports the unchanged **120,000 ms** temporal ceiling so near-boundary matches such as fractal remain inspectable. Its structured receipt is versioned independently from production search.

This is an **evaluation baseline change only**. The production Library endpoint remains visual-only; `retrieval_fusion.py` remains an evaluation primitive and is not promoted to production relevance policy by this result. No Association, Entity, hybrid-language, or workflow backlog item is completed by this depth decision.
