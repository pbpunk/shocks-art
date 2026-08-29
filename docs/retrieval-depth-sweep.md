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

- `finished staffs`: the strongest semantic target Language window is global rank 3 and its temporally overlapping visual frame is global rank 90 with a 360 ms gap. Its fixed Language Trace is `trace_881afad9478944918286f370a9aa1721`.
- `fractal burning setup`: the strongest setup Language window is global rank 1 and has an overlapping visual frame, but that exact nearby frame is global visual rank 400. Its fixed Language Trace is `trace_1a81f877bcba4a4aa04d647745424d14`.
- `gluing letters onto a sign`: the recovered control has Language rank 1 paired with visual rank 23 at a 7.64 second gap. Its fixed Language Trace is `trace_15f39d71d81241cbb22519233b4e347e`.

## Live receipt history

The first exact-main live sweep on `f509e96d195d42be66c2db6a2be4ae0b384974a3` completed, but its verbose result was cut at the bridge's 30,000-character persistence boundary. Its preserved portion established that axes remained stable and that fractal first entered the global top five at depth 50 by pairing the rank-1 Language setup window with a global visual-rank-47 candidate at a 109.56-second gap.

A first compact revision on `5ecfdbaa7a1e57cedde0ae9e1ab6e40621544fab` then failed closed before persistence because its encoded receipt was still 51,267 characters against the profile's 28,000-character budget. This confirmed the new guard works, but no complete decision receipt was produced.

## Decision-only receipt

The profile now records only evidence required for the candidate-depth decision:

- global fused count and top-five Media identities at each depth;
- whether the expected target Media reaches the global top five, plus its best global match;
- for the fixed semantic Language anchors above, whether the anchor is present at the depth, whether it can fuse with target visual evidence, its best grounded pair with true global Language/visual ranks, and whether that exact anchor reaches the global top five;
- compact global top matches for the epoxy control, which has no single repository-fixed target Media.

This avoids repeated target top-five dumps while preserving trace identity, rank, gap, and a short Language snippet. The profile still returns nonzero instead of reporting success if the successful JSON receipt exceeds 28,000 characters.

## Interpretation

A deeper candidate pool is a plausible production tuning choice only if it brings the intended fixed semantic anchor into the global result set while preserving the already-working axes, epoxy, and sign controls.

For `finished staffs`, the key question is specifically whether the wizard-staffs Language Trace—not the lexical `workbench finished` false hit—becomes globally competitive at depth 100.

For `fractal burning setup`, a technically valid 109.56-second match is not equivalent to the exact caption-window frame. The receipt therefore keeps the fixed setup Language anchor and its exact visual Trace pairing visible so candidate depth can be separated from future temporal Association/propagation work.
