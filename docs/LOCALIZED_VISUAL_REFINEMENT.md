# Localized visual refinement

IDX-022 improves timestamp precision after coarse semantic visual search without dense-indexing entire videos.

## Command

```powershell
python -m app.indexing refine-visual <trace_id> "man playing guitar"
```

To preserve the returned top review frames under ignored runtime data:

```powershell
python -m app.indexing refine-visual <trace_id> "man playing guitar" --output-dir data/idx022_refinement
```

Optional overrides:

```text
--radius SECONDS
--step SECONDS
--max-samples N
--top-k N
```

## Default plan

The local window derives from the coarse Trace's recorded sampling interval:

- radius defaults to half the coarse interval, clamped to 2.5-30 seconds;
- dense step defaults to one tenth of the coarse interval, clamped to 0.5-2 seconds;
- a hard ceiling of 31 dense frames prevents a local refinement from expanding into a whole-video scan;
- the original coarse timestamp is always included in the local candidate set.

Examples:

- a 5-second coarse cadence becomes a +/-2.5 second window sampled every 0.5 seconds (11 frames);
- a 60-second coarse cadence becomes a +/-30 second window sampled every 2 seconds (31 frames).

## Runtime isolation

The refinement operation:

1. extracts only the planned local frames with FFmpeg;
2. sends the text query and dense images through one mixed Qwen subprocess request so the model loads once;
3. L2-normalizes the returned vectors and ranks local frames by cosine similarity;
4. returns refined timestamps and scores;
5. deletes dense scratch frames unless an explicit review output directory was supplied.

It does not create or modify persistent Media, Trace, Embedding, or IndexRun rows. The existing coarse index remains the durable retrieval layer; refinement is a local second-stage operation.

## Validation target

IDX-022 is complete when a strong coarse video match can be refined on the workstation and pixel review shows that the dense local result gives a more useful timestamp without changing persistent index counts or scanning the entire source video.
