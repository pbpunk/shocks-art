# Data Model

## Media

Generic Library source record independent of livestream-specific models. Stores source identity/location, title/filename, MIME/media kind, size and modification metadata, SHA-256 content identity, duration/dimensions, processing status, and source metadata. Local files are the current validation adapter; future archive-backed Media can use other source types without changing downstream indexing concepts.

## Trace

Timestamped evidence extracted from a Media item. A Trace has an extensible `trace_type` (initially visual; later language/OCR/metadata), millisecond start/end positions, optional text or generated artifact path, extractor/version/configuration identity, confidence, provenance, and metadata. Extraction identity is unique per Media/type/time/extractor/version/configuration so interrupted jobs can safely resume without duplicating evidence.

## Embedding

Regenerable vector representation of a Trace. Stores the Trace link, model ID, vector dimension, dtype, normalized flag, and raw vector bytes. A Trace/model/dimension tuple is unique so different embedding generations are never silently mixed.

## IndexRun

One observable indexing-stage attempt for a Media item. Stores stage, configuration hash, status, timing, error text, and stage statistics. Runs are historical attempts rather than canonical evidence; Trace/Embedding rows remain the durable derived output.

## Stream

Root livestream asset. Stores YouTube identity, title, URL, duration, thumbnail, processing status, and schema version.

## StreamTranscript

Searchable transcript text captured from available YouTube captions. Stores language, source, raw caption file location, and timestamped plain text for later library search and quote verification.

## StreamAnalysisArtifact

Searchable upstream analysis material for a stream. Stores structured native Ask prompts, outlines, opportunities, drilldowns, final rankings, and local review artifacts with links back to the originating analysis run.

## AnalysisRun

One Gemini request lifecycle. Stores model, prompt version, schema version, status, retry count, raw response location, validation errors, usage, cost, and timing.

## CandidateWindow

Child source segment selected from a stream. Stores timestamps, pillar, tags, transcript excerpt, visual description, component scores, confidence, review status, and weighted score.

## DerivedAsset

Placeholder lineage entity for future shorts, reels, compilations, voiceovers, or manually edited outputs.

## PublishingRecord

Placeholder entity for where a derived asset is published.

## PerformanceRecord

Placeholder entity for platform metrics connected to publishing records.

## YouTubeOAuthCredential

Encrypted OAuth credential for YouTube Analytics and Reporting access. Stores channel identity, granted scopes, token expiry, connection status, and reconnect errors.

## YouTubeAnalyticsSync

One analytics ingestion attempt. Stores sync mode, status, fetched date range, row/update counts, last successful analytics date, and visible error messages.

## YouTubeVideo

Normalized YouTube video metadata used by Analytics. Joins to `Stream` through `source_video_id` whenever an existing livestream archive record is present. Stores title, thumbnail, duration, content type, and API metadata needed for display.

## YouTubeDailyMetric

Daily per-video YouTube Analytics metrics. Stores content type and live/on-demand split plus views, engaged views, watch time, average view duration, likes, subscribers, revenue, ad monetization metrics, and additional normalized metrics.

## YouTubeLivestreamMetric

Per-livestream rollup. Stores scheduled/actual timing, duration, live views, replay views, total views, watch time, likes, subscribers, revenue, and average/peak concurrency.

## YouTubeLivestreamTimeseries

Position-based livestream analytics. Stores video ID, stream position, derivable timestamp, and concurrent-viewer metrics so future content-intelligence overlays can join by video ID and stream position.

## YouTubeLiveEventPlaceholder

Forward-compatible event table for prospective live chat, Super Chats, memberships, and related events. Historical chat or Super Chat data is not fabricated when unavailable from YouTube APIs.
