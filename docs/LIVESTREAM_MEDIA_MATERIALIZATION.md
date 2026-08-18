# Livestream Media and on-demand materialization

Shock's Art Library treats an existing livestream as durable **Media metadata**, not as a permanently mirrored local video file.

## Durable state

`python -m app.indexing sync-stream-media` maps each existing `Stream` to one `Media` row using the YouTube `source_video_id` as canonical remote identity.

The Media row stores:

- `source_type = youtube`
- YouTube video ID and URL
- title, published time, and known duration
- explicit `stream_id` provenance
- no permanent `source_path`
- no retained source bytes

The existing `StreamTranscript.raw_location` JSON3 caption artifact can be converted immediately into `language` Traces. This requires no source-video download.

`Media.checksum_sha256` predates remote Media and is non-null/unique in the current SQLite schema. Until that schema is generalized, YouTube Media uses a deterministic SHA-256 **source identity fingerprint**, explicitly marked in `metadata_json.checksum_kind = youtube_source_identity`; it must not be interpreted as a content checksum.

## Temporary source lifecycle

When a stage actually needs video bytes, `DefaultMediaRetriever` resolves the Media source:

- local Media: lease the existing local source path; no cleanup ownership
- YouTube Media: yt-dlp downloads one source into `LIBRARY_SCRATCH_PATH/<media-job>/`

The temporary directory is removed in a `finally` block when the materialization lease closes, including extraction failures. Persistent derived artifacts such as visual Trace JPEGs remain under `LIBRARY_INDEX_PATH`; the full remote source does not.

One materialization lease covers the entire visual-extraction pass for that Media item, so a livestream is downloaded once for the job rather than once per sampled frame.

## Commands

Sync all currently known livestream metadata and reuse existing captions without downloading video:

```powershell
python -m app.indexing sync-stream-media
```

Repeat sync is safe; unchanged Media is updated in place and unchanged Language Traces are reused.

Prove one remote source can be downloaded and cleaned without indexing it:

```powershell
python -m app.indexing materialize-media <MEDIA_ID>
```

Expected remote result includes:

- `temporary: true`
- `existsDuringLease: true`
- `existsAfterLease: false`

Index one remote livestream visually on demand:

```powershell
python -m app.indexing index-media <MEDIA_ID>
```

The source is downloaded for that job, sparse/adaptive visual Trace artifacts are persisted, and the full source download is removed afterward.

Bulk `index-pending` remains local-only by default. Remote downloads require an explicit opt-in:

```powershell
python -m app.indexing index-pending --include-remote
```

Do not use that flag casually against a large archive. The later persistent job queue will control remote indexing deliberately and sequentially.

## Existing Clips behavior

This does not change `app/services/clip_download.py`. Clips may retain its existing source cache because editorial clip generation has different caching requirements. Library does not rely on that cache and does not create a permanent mirror of the livestream archive.

## Deferred work

Private-YouTube authentication/range retrieval, shared Clips/Library yt-dlp primitives, stale scratch recovery after hard process termination, and the durable worker queue remain later retrieval/operations work. This implementation establishes the lifecycle early because livestream Media is immediately useful as the organic Library corpus.
