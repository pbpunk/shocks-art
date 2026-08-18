from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.indexing.language_traces import import_stream_transcript_language_traces
from app.library_models import Media
from app.models import Stream, StreamTranscript


YOUTUBE_MEDIA_SOURCE_TYPE = "youtube"


@dataclass(frozen=True)
class StreamMediaSyncResult:
    streams_considered: int
    media_created: int
    media_updated: int
    transcripts_imported: int
    language_traces_created: int
    language_traces_reused: int
    transcript_errors: int

    def as_dict(self) -> dict:
        return asdict(self)


def youtube_source_fingerprint(source_video_id: str) -> str:
    """Stable catalog fingerprint for remote Media whose source bytes are not retained.

    Media.checksum_sha256 historically requires a unique non-null value. Remote
    YouTube Media deliberately has no permanent local bytes to hash, so this
    value is an explicit source-identity fingerprint rather than a content hash.
    The distinction is preserved in Media.metadata_json.
    """

    raw = f"youtube-source-id:{source_video_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _latest_existing_transcript(db: Session, stream_id: str) -> StreamTranscript | None:
    transcripts = list(
        db.scalars(
            select(StreamTranscript)
            .where(StreamTranscript.stream_id == stream_id)
            .order_by(StreamTranscript.updated_at.desc(), StreamTranscript.created_at.desc())
        ).all()
    )
    for transcript in transcripts:
        if transcript.raw_location and Path(transcript.raw_location).is_file():
            return transcript
    return transcripts[0] if transcripts else None


def upsert_stream_media(db: Session, stream: Stream) -> tuple[Media, bool]:
    """Represent one existing livestream as durable metadata-only Library Media."""

    media = db.scalar(
        select(Media).where(
            Media.source_type == YOUTUBE_MEDIA_SOURCE_TYPE,
            Media.source_id == stream.source_video_id,
        )
    )
    created = media is None
    if media is None:
        media = Media(
            source_type=YOUTUBE_MEDIA_SOURCE_TYPE,
            source_id=stream.source_video_id,
            checksum_sha256=youtube_source_fingerprint(stream.source_video_id),
        )
        db.add(media)

    existing_metadata = dict(media.metadata_json or {})
    media.source_url = stream.url
    media.source_path = ""
    media.title = stream.title
    media.filename = ""
    media.mime_type = "video/mp4"
    media.media_kind = "video"
    media.size_bytes = 0
    media.source_modified_ns = 0
    media.duration_seconds = float(stream.duration or 0) or None
    media.width = 0
    media.height = 0
    media.processing_status = "discovered"
    media.metadata_json = {
        **existing_metadata,
        "origin": "livestream",
        "stream_id": stream.stream_id,
        "source_video_id": stream.source_video_id,
        "published_at": stream.published_at,
        "remote_bytes_retained": False,
        "checksum_kind": "youtube_source_identity",
        "content_checksum_available": False,
    }
    db.flush()
    return media, created


def sync_stream_media(
    db: Session,
    stream: Stream,
    *,
    import_language: bool = True,
) -> tuple[Media, bool, int, int, bool]:
    """Sync one Stream into Media and optionally reuse its stored JSON3 captions."""

    media, created = upsert_stream_media(db, stream)
    language_created = 0
    language_reused = 0
    transcript_imported = False

    if import_language:
        transcript = _latest_existing_transcript(db, stream.stream_id)
        if transcript is not None and transcript.raw_location and Path(transcript.raw_location).is_file():
            result = import_stream_transcript_language_traces(
                db,
                media=media,
                stream=stream,
                transcript=transcript,
            )
            language_created = result.created
            language_reused = result.reused
            transcript_imported = True

    db.commit()
    return media, created, language_created, language_reused, transcript_imported


def sync_all_stream_media(
    db: Session,
    *,
    import_language: bool = True,
    limit: int | None = None,
) -> StreamMediaSyncResult:
    """Metadata-sync livestreams without downloading source video bytes."""

    query = select(Stream).order_by(Stream.published_at.desc(), Stream.created_at.desc())
    if limit is not None:
        query = query.limit(limit)
    streams = list(db.scalars(query).all())

    media_created = 0
    media_updated = 0
    transcripts_imported = 0
    language_created = 0
    language_reused = 0
    transcript_errors = 0

    for stream in streams:
        try:
            _, created, created_traces, reused_traces, imported = sync_stream_media(
                db,
                stream,
                import_language=import_language,
            )
        except Exception:
            db.rollback()
            transcript_errors += 1
            # The next pass can retry safely; do not let one bad transcript block
            # metadata ingestion of the rest of the livestream archive.
            try:
                _, created = upsert_stream_media(db, stream)
                db.commit()
            except Exception:
                db.rollback()
                continue
            created_traces = 0
            reused_traces = 0
            imported = False

        media_created += int(created)
        media_updated += int(not created)
        transcripts_imported += int(imported)
        language_created += created_traces
        language_reused += reused_traces

    return StreamMediaSyncResult(
        streams_considered=len(streams),
        media_created=media_created,
        media_updated=media_updated,
        transcripts_imported=transcripts_imported,
        language_traces_created=language_created,
        language_traces_reused=language_reused,
        transcript_errors=transcript_errors,
    )
