# Product Spec

## Mission

Convert long Shocks Art livestream archives into categorized, searchable, traceable source footage for short-form editing while preserving human editorial control.

## MVP Users

- Nate or an editor reviewing candidate source segments.
- A producer exporting approved metadata to Vizard or manual editing.

## MVP Workflow

1. Configure the YouTube channel handle and API keys.
2. Discover public archived livestreams through the YouTube Data API.
3. Store each livestream as a root `Stream`.
4. Analyze one selected stream or the queued archive with Gemini.
5. Validate Gemini JSON against the versioned schema.
6. Save ranked `CandidateWindow` rows per stream.
7. Review, approve, reject, mark later, adjust timestamps, edit pillar and tags.
8. View ranked top-five candidates.
9. Export CSV or JSON for downstream editing.

## Exclusions

The MVP does not include automatic publishing, performance optimization, autonomous strategy, recursive agent loops, or full minute-by-minute indexing.
