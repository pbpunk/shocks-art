from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library_models import Media, Trace
from app.models import Stream, StreamTranscript


class LanguageTraceImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class LanguageSegment:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class LanguageTraceImportResult:
    media_id: str
    stream_id: str
    stream_transcript_id: str
    configuration_hash: str
    considered: int
    created: int
    reused: int

    def as_dict(self) -> dict:
        return asdict(self)


def _clean_segment_text(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def parse_youtube_json3_segments(path: Path) -> tuple[LanguageSegment, ...]:
    """Parse timestamped spoken-text events from a yt-dlp YouTube JSON3 caption file."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LanguageTraceImportError(f"cannot read JSON3 caption file {path.name}: {exc}") from exc

    events = payload.get("events", [])
    if not isinstance(events, list):
        raise LanguageTraceImportError("JSON3 caption payload has a non-array events field")

    segments: list[LanguageSegment] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        pieces = []
        for segment in event.get("segs", []) or []:
            if not isinstance(segment, dict):
                continue
            text = _clean_segment_text(segment.get("utf8"))
            if text:
                pieces.append(text)
        text = " ".join(pieces).strip()
        if not text:
            continue

        try:
            start_ms = max(0, int(event.get("tStartMs", 0) or 0))
            duration_ms = max(0, int(event.get("dDurationMs", 0) or 0))
        except (TypeError, ValueError) as exc:
            raise LanguageTraceImportError("JSON3 caption event has an invalid timestamp") from exc
        end_ms = start_ms + duration_ms
        segments.append(LanguageSegment(start_ms=start_ms, end_ms=end_ms, text=text))

    return tuple(segments)


def transcript_configuration_hash(transcript: StreamTranscript, raw_bytes: bytes) -> str:
    payload = {
        "schema": "language-trace-youtube-json3-v1",
        "streamTranscriptId": transcript.stream_transcript_id,
        "source": transcript.source,
        "language": transcript.language,
        "rawSha256": hashlib.sha256(raw_bytes).hexdigest(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _existing_language_trace(
    db: Session,
    *,
    media_id: str,
    segment: LanguageSegment,
    configuration_hash: str,
) -> Trace | None:
    return db.scalar(
        select(Trace).where(
            Trace.media_id == media_id,
            Trace.trace_type == "language",
            Trace.start_ms == segment.start_ms,
            Trace.end_ms == segment.end_ms,
            Trace.extractor == "youtube-json3-captions",
            Trace.extractor_version == "1",
            Trace.configuration_hash == configuration_hash,
        )
    )


def import_stream_transcript_language_traces(
    db: Session,
    *,
    media: Media,
    stream: Stream,
    transcript: StreamTranscript,
) -> LanguageTraceImportResult:
    """Convert one existing raw YouTube JSON3 transcript into durable Language Traces.

    This importer intentionally does not fetch captions or transcribe media. It only
    reuses an already-stored StreamTranscript raw JSON3 artifact and preserves its
    original event timestamps and provenance in the shared Trace model.
    """

    if transcript.stream_id != stream.stream_id:
        raise LanguageTraceImportError("transcript does not belong to the supplied Stream")
    raw_path = Path(transcript.raw_location)
    if not raw_path.is_file():
        raise LanguageTraceImportError(
            f"existing transcript raw JSON3 artifact is unavailable: {raw_path.name or transcript.stream_transcript_id}"
        )

    try:
        raw_bytes = raw_path.read_bytes()
    except OSError as exc:
        raise LanguageTraceImportError(f"cannot read transcript artifact {raw_path.name}: {exc}") from exc
    configuration_hash = transcript_configuration_hash(transcript, raw_bytes)
    segments = parse_youtube_json3_segments(raw_path)

    created = 0
    reused = 0
    for segment in segments:
        existing = _existing_language_trace(
            db,
            media_id=media.media_id,
            segment=segment,
            configuration_hash=configuration_hash,
        )
        if existing is not None:
            reused += 1
            continue

        db.add(
            Trace(
                media_id=media.media_id,
                trace_type="language",
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                content_text=segment.text,
                artifact_path="",
                extractor="youtube-json3-captions",
                extractor_version="1",
                configuration_hash=configuration_hash,
                confidence=None,
                provenance_json={
                    "streamId": stream.stream_id,
                    "sourceVideoId": stream.source_video_id,
                    "streamTranscriptId": transcript.stream_transcript_id,
                    "transcriptSource": transcript.source,
                    "language": transcript.language,
                    "rawCaptionFile": raw_path.name,
                },
                metadata_json={
                    "captionFormat": "youtube-json3",
                    "sourceTranscriptFormat": transcript.format,
                },
            )
        )
        created += 1

    db.commit()
    return LanguageTraceImportResult(
        media_id=media.media_id,
        stream_id=stream.stream_id,
        stream_transcript_id=transcript.stream_transcript_id,
        configuration_hash=configuration_hash,
        considered=len(segments),
        created=created,
        reused=reused,
    )


def import_existing_stream_transcript(
    db: Session,
    *,
    media_id: str,
    stream_id: str,
) -> LanguageTraceImportResult:
    media = db.get(Media, media_id)
    if media is None:
        raise LanguageTraceImportError(f"Media not found: {media_id}")
    stream = db.get(Stream, stream_id)
    if stream is None:
        raise LanguageTraceImportError(f"Stream not found: {stream_id}")
    transcript = db.scalar(
        select(StreamTranscript)
        .where(StreamTranscript.stream_id == stream_id)
        .order_by(StreamTranscript.updated_at.desc(), StreamTranscript.created_at.desc())
    )
    if transcript is None:
        raise LanguageTraceImportError(f"no existing StreamTranscript found for Stream {stream_id}")
    return import_stream_transcript_language_traces(
        db,
        media=media,
        stream=stream,
        transcript=transcript,
    )
