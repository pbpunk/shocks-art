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

- `finished staffs`: the strongest target Language window is global rank 3 and its temporally overlapping visual frame is global rank 90 with a 360 ms gap. This predicts recovery at depth 100 without any temporal widening.
- `fractal burning setup`: the strongest setup Language window is global rank 1 and has an overlapping visual frame, but that frame is global visual rank 400. The strongest visual query matches occur much later in the stream. This predicts that ordinary top-100 depth will not recover the intended setup moment.
- `gluing letters onto a sign`: the recovered control has Language rank 1 paired with visual rank 23 at a 7.64 second gap, demonstrating that the current depth-25 policy works when both modalities rank the same moment strongly enough.

## Interpretation

If depth 100 recovers the intended `finished staffs` target while preserving the existing axes/sign controls, candidate depth is a sufficient explanation for that miss and can be evaluated separately as a production tuning choice.

If `fractal burning setup` appears only at depth 500, that is not treated as evidence to ship a 500-wide pool automatically. Its target-local diagnostic shows the nearby frame has weak direct visual-query rank; a large pool would brute-force around a semantic asymmetry. That case should instead inform the planned temporal Association/propagation work while preserving the existing fail-closed temporal grounding rule.
