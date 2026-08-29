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

- `finished staffs`: the strongest target Language window is global rank 3 and its temporally overlapping visual frame is global rank 90 with a 360 ms gap.
- `fractal burning setup`: the strongest setup Language window is global rank 1 and has an overlapping visual frame, but that exact nearby frame is global visual rank 400. The strongest direct visual-query matches occur much later in the stream.
- `gluing letters onto a sign`: the recovered control has Language rank 1 paired with visual rank 23 at a 7.64 second gap, demonstrating that the current depth-25 policy works when both modalities rank the same moment strongly enough.

## First live sweep

The first exact-main live sweep on `f509e96d195d42be66c2db6a2be4ae0b384974a3` completed successfully, but its verbose result exceeded the bridge's persisted `result_json` budget and was cut at 30,000 characters. The preserved portion is still useful:

- `sanding axes` remained stable from depth 25 through 500.
- `fractal burning setup` had no target fusion at depth 25, then entered the global top five at depth 50 and remained there at depth 100. That match pairs the rank-1 setup Language window with a visual candidate at global rank 47 and a **109.56 second** gap. This is inside the unchanged 120-second rule, but it is not the rank-400 frame that overlaps the exact caption window; the usefulness of this later frame must be evaluated separately rather than treating threshold compliance as proof of a strong setup shot.
- The persisted receipt was truncated during the `finished staffs` depth-100 section, so the experiment cannot yet support a complete production-depth decision for staffs, epoxy, or sign behavior.

## Compact decision receipt

The profile now emits a compact receipt capped below the bridge persistence limit. It keeps:

- global fused count and top-five Media identities at each depth;
- expected-Media hits that actually make the global top five;
- target-only top fused matches for fixed target Media;
- Language/visual trace IDs, ranks, timestamps, temporal gap, and short Language snippets needed to distinguish a relevant staff/setup moment from a lexical false positive;
- compact global top matches for the epoxy query, which has no single repository-fixed target Media.

It intentionally omits the previous full duplicate match dumps. The profile returns a nonzero result instead of reporting success if its compact receipt still exceeds the fixed JSON budget.

## Interpretation

Candidate depth can be considered a production tuning choice only when the compact rerun shows that a deeper pool recovers the intended semantic moment while preserving the already-working axes, epoxy, and sign controls.

The fractal depth-50 result is specifically not enough by itself to justify a wider production pool: its 109.56-second pair sits close to the current temporal boundary, while the exact caption-window frame ranks much lower visually. That distinction should inform the planned temporal Association/propagation work rather than being hidden by simply increasing the time window.
