from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.indexing.media_retrieval import MaterializedMedia, MediaRetrievalError, YtDlpMediaRetriever
from app.indexing.service import VisualExtractionConfig, index_visual_media
from app.indexing.stream_media import sync_all_stream_media
from app.library_models import Media, Trace
from app.models import Stream, StreamTranscript
from app.services.ytdlp import YtDlpError


class FakeFrameBackend:
    name = "fake-remote-frame"
    version = "1"

    def extract_frame(self, source_path, timestamp_ms, output_path, *, still_image):
        assert source_path.is_file()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"{source_path.name}:{timestamp_ms}".encode())


class FakeRemoteRetriever:
    def __init__(self, source_path: Path):
        self.source_path = source_path
        self.calls = 0

    @contextmanager
    def materialize(self, media):
        self.calls += 1
        yield MaterializedMedia(
            path=self.source_path,
            temporary=True,
            source_type=media.source_type,
            size_bytes=self.source_path.stat().st_size,
        )


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stream-media.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def add_stream_with_transcript(db, tmp_path, *, video_id="yt123"):
    stream = Stream(
        channel_id="channel",
        source_video_id=video_id,
        title="Workshop livestream",
        url=f"https://www.youtube.com/watch?v={video_id}",
        published_at="2026-08-18T10:00:00Z",
        duration=125,
    )
    db.add(stream)
    db.flush()
    raw = tmp_path / f"{video_id}.en-orig.json3"
    raw.write_text(
        json.dumps(
            {
                "events": [
                    {"tStartMs": 1000, "dDurationMs": 1500, "segs": [{"utf8": "dragon staff"}]},
                    {"tStartMs": 4000, "dDurationMs": 1000, "segs": [{"utf8": "fractal burning"}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    transcript = StreamTranscript(
        stream_id=stream.stream_id,
        language="en-orig",
        source="youtube_auto_captions",
        format="plain_text",
        text="00:01 dragon staff\n00:04 fractal burning",
        raw_location=str(raw),
    )
    db.add(transcript)
    db.commit()
    return stream, transcript


def test_sync_stream_media_creates_metadata_only_media_and_language_traces(tmp_path):
    db = make_session(tmp_path)
    try:
        stream, _ = add_stream_with_transcript(db, tmp_path)
        first = sync_all_stream_media(db)
        assert first.streams_considered == 1
        assert first.media_created == 1
        assert first.transcripts_imported == 1
        assert first.language_traces_created == 2
        assert first.transcript_errors == 0

        media = db.scalar(select(Media).where(Media.source_type == "youtube"))
        assert media is not None
        assert media.source_id == stream.source_video_id
        assert media.source_url == stream.url
        assert media.source_path == ""
        assert media.size_bytes == 0
        assert media.duration_seconds == 125
        assert media.metadata_json["stream_id"] == stream.stream_id
        assert media.metadata_json["remote_bytes_retained"] is False
        assert media.metadata_json["checksum_kind"] == "youtube_source_identity"

        traces = list(
            db.scalars(
                select(Trace)
                .where(Trace.media_id == media.media_id, Trace.trace_type == "language")
                .order_by(Trace.start_ms)
            ).all()
        )
        assert [(trace.start_ms, trace.end_ms, trace.content_text) for trace in traces] == [
            (1000, 2500, "dragon staff"),
            (4000, 5000, "fractal burning"),
        ]

        second = sync_all_stream_media(db)
        assert second.media_created == 0
        assert second.media_updated == 1
        assert second.language_traces_created == 0
        assert second.language_traces_reused == 2
        assert db.scalar(select(func.count()).select_from(Media)) == 1
        assert db.scalar(select(func.count()).select_from(Trace)) == 2
    finally:
        db.close()


def test_ytdlp_retriever_cleans_successful_scratch_lease(tmp_path, monkeypatch):
    db = make_session(tmp_path)
    try:
        stream, _ = add_stream_with_transcript(db, tmp_path)
        sync_all_stream_media(db, import_language=False)
        media = db.scalar(select(Media).where(Media.source_id == stream.source_video_id))
        scratch = tmp_path / "scratch"
        calls = []

        def fake_fetch(**kwargs):
            calls.append(kwargs)
            kwargs["expected_path"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["expected_path"].write_bytes(b"temporary-video")
            return kwargs["expected_path"]

        monkeypatch.setattr("app.indexing.media_retrieval.download_youtube_source", fake_fetch)
        retriever = YtDlpMediaRetriever(scratch_root=scratch)
        with retriever.materialize(media) as materialized:
            leased_path = materialized.path
            assert leased_path.is_file()
            assert materialized.temporary is True
            assert materialized.size_bytes == len(b"temporary-video")
        assert len(calls) == 1
        assert calls[0]["url"] == media.source_url
        assert not leased_path.exists()
        assert scratch.exists()
        assert list(scratch.iterdir()) == []
    finally:
        db.close()


def test_ytdlp_retriever_cleans_scratch_after_failure(tmp_path, monkeypatch):
    db = make_session(tmp_path)
    try:
        stream, _ = add_stream_with_transcript(db, tmp_path)
        sync_all_stream_media(db, import_language=False)
        media = db.scalar(select(Media).where(Media.source_id == stream.source_video_id))
        scratch = tmp_path / "scratch"

        def fake_fetch(**kwargs):
            raise YtDlpError("network failed")

        monkeypatch.setattr("app.indexing.media_retrieval.download_youtube_source", fake_fetch)
        retriever = YtDlpMediaRetriever(scratch_root=scratch)
        with pytest.raises(MediaRetrievalError, match="network failed"):
            with retriever.materialize(media):
                pass
        assert scratch.exists()
        assert list(scratch.iterdir()) == []
    finally:
        db.close()


def test_remote_visual_index_uses_one_materialization_and_persists_only_artifacts(tmp_path):
    db = make_session(tmp_path)
    try:
        stream, _ = add_stream_with_transcript(db, tmp_path)
        sync_all_stream_media(db, import_language=False)
        media = db.scalar(select(Media).where(Media.source_id == stream.source_video_id))
        source = tmp_path / "leased-source.mp4"
        source.write_bytes(b"fake-video")
        retriever = FakeRemoteRetriever(source)
        index_root = tmp_path / "index"

        result = index_visual_media(
            db,
            media,
            index_root=index_root,
            backend=FakeFrameBackend(),
            retriever=retriever,
            config=VisualExtractionConfig(sample_interval_seconds=60),
        )
        assert result.status == "complete"
        assert result.created == 3
        assert retriever.calls == 1
        traces = list(db.scalars(select(Trace).where(Trace.trace_type == "visual")).all())
        assert len(traces) == 3
        assert all(trace.provenance_json["sourceMaterialization"] == "temporary" for trace in traces)
        assert all((index_root / trace.artifact_path).is_file() for trace in traces)
        assert media.source_path == ""
    finally:
        db.close()
