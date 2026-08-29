# Retrieval Target Diagnostics

This profile exists to diagnose the fixed retrieval misses after canonical YouTube Media gained both Language and visual coverage. It is an evaluation tool, not production retrieval logic.

## Fixed targets

The Sheet cannot supply a query, Media ID, candidate depth, model, path, URL, or command. The repository owns three fixed query/Media pairs:

- `fractal-burning-setup` / `fractal burning setup` / `media_4a2b9b61b1cd44e7bd820ed68dbf207d`
- `finished-staffs` / `finished staffs` / `media_0a571dc5e48942fc9b9d98e27609eeb0`
- `gluing-sign-control` / `gluing letters onto a sign` / `media_53c498d982c14ec680bacf2be2f4dfa0`

The first two remain diagnostically unresolved after targeted visual coverage expansion. The sign query is retained as a control because the normal fixed evaluator produced a same-Media fused result with a 7.64-second gap after coverage expansion.

## What the profile measures

For each fixed target, the profile performs deep read-only retrieval using the exact persistent Qwen generation:

- Language search depth: 500 windows.
- Visual search depth: all currently persisted vectors (requested depth 5000).
- The target Media's global Language and visual ranks.
- The highest-ranked Language anchor in the target Media and the temporally nearest visual frame in that same Media.
- The highest-ranked visual anchor in the target Media and the temporally nearest Language window in that same Media.
- Explicit top-25, top-50, and top-100 inclusion flags for each side of those anchored pairs.
- The closest temporal pair available anywhere among the target Media's query-visible Language and visual matches.

Temporal-nearest diagnostics choose smallest same-Media temporal gap before global retrieval rank. This is intentional: the point is to reveal whether relevant evidence exists near a strong anchor but is excluded by candidate depth or semantic ranking.

## Interpretation

A strong Language anchor whose nearest visual frame is close in time but ranks outside the production candidate pool is evidence for candidate-depth or cross-modal propagation work. A strong Language anchor whose nearest visual frame ranks adequately but is temporally distant indicates that direct visual query semantics are selecting a different moment; widening the temporal join would risk false grounding and is not an acceptable shortcut.

Likewise, a strong visual hit whose nearest Language window is semantically weak or low-ranked exposes the opposite asymmetry. These measurements are intended to inform the existing `Media -> Traces -> Associations -> Entities` roadmap, especially whether temporal Associations should propagate evidence across modalities.

The profile does not modify production data, enqueue indexing jobs, invoke `visual-pending`, perform bulk remote indexing, or use title/filename/path metadata for selection or scoring.
