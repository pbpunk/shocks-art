import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.indexing.language_traces import (
    LanguageTraceImportError,
    import_stream_transcript_language_traces,
    parse_youtube_json3_segments,
)
from app.library_models import Media, Trace
from app.models import Stream, StreamTranscript


def make_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'language.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def write_json3(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "tStartMs": 1000,
                        "dDurationMs": 2200,
                        "segs": [{"utf8": "hello "}, {"utf8": "world\n"}],
                    },
                    {"tStartMs": 4000, "dDurationMs": 500, "segs": [{"utf8": "   "}]},
                    {
                        "tStartMs": 5000,
                        "dDurationMs": 1000,
                        "segs": [{"utf8": "dragon   staff"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def add_fixture(db, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    media = Media(
        source_type="local",
        source_id=str(source),
        source_path=str(source),
        title="presentation metadata",
        filename="presentation.mp4",
        mime_type="video/mp4",
        media_kind="video",
        size_bytes=5,
        source_modified_ns=1,
        checksum_sha256="a" * 64,
        duration_seconds=10.0,
    )
    stream = Stream(
        channel_id="channel",
        source_video_id="youtube-video-123",
        title="Existing stream",
        url="https://www.youtube.com/watch?v=youtube-video-123",
        duration=10,
    )
    db.add_all([media, stream])
    db.flush()

    raw = tmp_path / "youtube-video-123.en-orig.json3"
    write_json3(raw)
    transcript = StreamTranscript(
        stream_id=stream.stream_id,
        language="en-orig",
        source="youtube_auto_captions",
        format="plain_text",
        text="00:01 hello world\n00:05 dragon staff",
        raw_location=str(raw),
    )
    db.add(transcript)
    db.commit()
    return media, stream, transcript, raw


def test_parse_youtube_json3_segments_preserves_timestamps_and_text(tmp_path):
    path = tmp_path / "captions.json3"
    write_json3(path)

    segments = parse_youtube_json3_segments(path)

    assert [(segment.start_ms, segment.end_ms, segment.text) for segment in segments] == [
        (1000, 3200, "hello world"),
        (5000, 6000, "dragon staff"),
    ]


def test_import_existing_json3_is_idempotent_and_preserves_provenance(tmp_path):
    db = make_session(tmp_path)
    try:
        media, stream, transcript, _ = add_fixture(db, tmp_path)

        first = import_stream_transcript_language_traces(
            db,
            media=media,
            stream=stream,
            transcript=transcript,
        )
        second = import_stream_transcript_language_traces(
            db,
            media=media,
            stream=stream,
            transcript=transcript,
        )

        assert first.considered == 2
        assert first.created == 2
        assert first.reused == 0
        assert second.created == 0
        assert second.reused == 2
        assert second.configuration_hash == first.configuration_hash

        traces = list(
            db.scalars(
                select(Trace)
                .where(Trace.media_id == media.media_id, Trace.trace_type == "language")
                .order_by(Trace.start_ms)
            ).all()
        )
        assert len(traces) == 2
        assert traces[0].content_text == "hello world"
        assert (traces[0].start_ms, traces[0].end_ms) == (1000, 3200)
        assert traces[1].content_text == "dragon staff"
        assert traces[1].provenance_json == {
            "streamId": stream.stream_id,
            "sourceVideoId": "youtube-video-123",
            "streamTranscriptId": transcript.stream_transcript_id,
            "transcriptSource": "youtube_auto_captions",
            "language": "en-orig",
            "rawCaptionFile": "youtube-video-123.en-orig.json3",
        }
        assert traces[1].metadata_json == {
            "captionFormat": "youtube-json3",
            "sourceTranscriptFormat": "plain_text",
        }
        assert str(tmp_path) not in json.dumps(traces[1].provenance_json)
    finally:
        db.close()


def test_changed_raw_caption_generation_does_not_overwrite_old_traces(tmp_path):
    db = make_session(tmp_path)
    try:
        media, stream, transcript, raw = add_fixture(db, tmp_path)
        first = import_stream_transcript_language_traces(
            db,
            media=media,
            stream=stream,
            transcript=transcript,
        )

        payload = json.loads(raw.read_text(encoding="utf-8"))
        payload["events"][0]["segs"] = [{"utf8": "corrected words"}]
        raw.write_text(json.dumps(payload), encoding="utf-8")
        second = import_stream_transcript_language_traces(
            db,
            media=media,
            stream=stream,
            transcript=transcript,
        )

        assert second.configuration_hash != first.configuration_hash
        assert second.created == 2
        traces = list(
            db.scalars(select(Trace).where(Trace.media_id == media.media_id, Trace.trace_type == "language")).all()
        )
        assert len(traces) == 4
        assert {trace.content_text for trace in traces} >= {"hello world", "corrected words"}
    finally:
        db.close()


def test_import_rejects_missing_raw_caption_artifact(tmp_path):
    db = make_session(tmp_path)
    try:
        media, stream, transcript, raw = add_fixture(db, tmp_path)
        raw.unlink()
        with pytest.raises(LanguageTraceImportError, match="raw JSON3 artifact is unavailable"):
            import_stream_transcript_language_traces(
                db,
                media=media,
                stream=stream,
                transcript=transcript,
            )
    finally:
        db.close()
