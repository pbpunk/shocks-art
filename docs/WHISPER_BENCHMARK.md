# Creator-speech Whisper benchmark

IDX-025 needs a small **labeled local corpus**, not a generic Whisper smoke test. The goal is to choose the cheapest fixed faster-whisper configuration that is accurate enough for Shock's normal speech and unusual project/object names.

The Google Sheet never supplies media paths, model IDs, or arguments. The workstation owns this trusted local benchmark input.

## Default location

Unless `SHOCKS_WHISPER_BENCHMARK_MANIFEST` intentionally overrides it, the fixed `whisper-benchmark` host profile reads:

`data/whisper_benchmark/manifest.json`

That directory is under ignored runtime data and must not be committed. Media paths may be absolute or relative to the manifest directory.

## Prepare a review corpus from repaired native-Ask lineage

The fixed main-only `whisper-benchmark-prepare` host profile removes the previous manual corpus-construction dead end without weakening the ground-truth requirement.

When a reviewed `manifest.json` does not already exist, the profile:

- selects repaired native-Ask CandidateWindows whose speech claims were grounded back to stored timestamped JSON3 captions;
- caps the package at 8 cases, at most 2 per stream;
- creates bounded 10-45 second mono 16 kHz WAV excerpts under `data/whisper_benchmark/clips/`;
- prefers existing derived-clip or source-video caches and otherwise requests only the bounded YouTube time section;
- writes `data/whisper_benchmark/manifest.draft.json` with source lineage, source-backed `caption_text`, and suggested project terms.

The draft is **not benchmark ground truth**. It deliberately omits the runnable manifest's required `reference_text` and `project_terms`. A human must listen to each WAV, correct the exact spoken text, choose the unusual terms that truly matter, and create `manifest.json`. The normal manifest validator therefore rejects the draft if it is accidentally supplied to `whisper-benchmark`.

The prep profile never overwrites an existing reviewed `manifest.json` and is main-only because it writes ignored workstation corpus files.

## What to label

Use a small representative set rather than hours of footage. A practical first pass is 6-12 speech excerpts of roughly 10-60 seconds each, with a mix of:

- normal conversational creator speech;
- noisier or faster speech when it is representative;
- several examples containing project/object names that ordinary speech models may miss;
- repeated important names across more than one excerpt where possible.

Avoid silent B-roll or clips where the reference transcript itself is uncertain. The benchmark is intended to measure speech recognition, not infer what was said from context.

## Manifest schema

```json
{
  "cases": [
    {
      "id": "dragon-staff-01",
      "media_path": "clips/dragon-staff-01.wav",
      "language": "en",
      "reference_text": "This is the Dragon Staff I was working on yesterday.",
      "project_terms": ["Dragon Staff"]
    }
  ]
}
```

Every case requires:

- a unique `id`;
- `media_path` pointing to an existing local audio/video file;
- an exact human-checked `reference_text`;
- at least one labeled `project_terms` entry;
- optional `language` (defaults to `en`).

Project terms should be the unusual names we actually care about preserving in retrieval, not a list of common words.

## Validate before running

The dependency-light validator uses the same parser as the host profile:

`py -3 tools/validate_whisper_benchmark_manifest.py`

It checks structure, duplicate IDs, required labels, and media-file existence without importing faster-whisper or loading a model.

## Host benchmark behavior

The fixed profile compares repository-owned candidates only:

- `small.en / float16`
- `medium.en / float16`
- `medium.en / int8_float16`

For each candidate it records load/runtime/audio duration, real-time factor, general word accuracy, project-term recall, timestamp validity, and observed process GPU memory. The acceptance thresholds are repository-owned; the Sheet cannot change them.

A successful IDX-025 receipt must come from the real workstation at the exact tested SHA. A missing/invalid manifest or unavailable faster-whisper runtime is a configuration result, not a benchmark PASS or model-quality FAIL.
