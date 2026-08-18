from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.indexing.service import (
    VisualExtractionConfig,
    effective_sample_interval_seconds,
    index_visual_media,
    visual_sample_timestamps_ms,
    visual_sampling_plan,
)
from app.library_models import Embedding, IndexRun, Media, Trace


class FakeFrameBackend:
    name = "fake-frame-backend"
    version = "1.0-test"

    def __init__(self, fail_once_at: int | None = None):
        self.fail_once_at = fail_once_at
        self.failed = False
        self.calls: list[tuple[int, bool]] = []

    def extract_frame(
        self,
        source_path: Path,
        timestamp_ms: int,
        output_path: Path,
        *,
        still_image: bool,
    ) -> None:
        self.calls.append((timestamp_ms, still_image))
        if self.fail_once_at == timestamp_ms and not self.failed:
            self.failed = True
            raise RuntimeError(f"simulated extraction failure at {timestamp_ms}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"frame:{source_path.name}:{timestamp_ms}".encode("utf-8"))


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'indexing.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def add_media(db, tmp_path, *, filename: str, kind: str, duration: float | None = None) -> Media:
    source = tmp_path / filename
    source.write_bytes(f"source:{filename}".encode("utf-8"))
    media = Media(
        source_type="local",
        source_id=str(source),
        source_path=str(source),
        title=source.stem,
        filename=source.name,
        mime_type="video/mp4" if kind == "video" else "image/jpeg",
        media_kind=kind,
        size_bytes=source.stat().st_size,
        source_modified_ns=source.stat().st_mtime_ns,
        checksum_sha256=("a" if kind == "video" else "b") * 64,
        duration_seconds=duration,
        width=1920,
        height=1080,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def test_trace_embedding_and_index_run_schema_round_trip(tmp_path):
    db = make_session(tmp_path)
    try:
        media = add_media(db, tmp_path, filename="schema.jpg", kind="image")
        trace = Trace(
            media_id=media.media_id,
            trace_type="visual",
            start_ms=0,
            end_ms=0,
            artifact_path="visual/example.jpg",
            extractor="fake",
            extractor_version="1",
            configuration_hash="c" * 64,
            confidence=0.9,
            provenance_json={"sourceSha256": media.checksum_sha256},
            metadata_json={"note": "schema-test"},
        )
        db.add(trace)
        db.flush()
        embedding = Embedding(
            trace_id=trace.trace_id,
            model_id="fake-embedding-model",
            embedding_dimension=4,
            dtype="float32",
            vector_blob=b"0123456789abcdef",
            normalized=True,
        )
        run = IndexRun(
            media_id=media.media_id,
            stage="visual_extract",
            configuration_hash="c" * 64,
            status="complete",
            statistics_json={"created": 1},
        )
        db.add_all([embedding, run])
        db.commit()

        stored_trace = db.get(Trace, trace.trace_id)
        assert stored_trace is not None
        assert stored_trace.media.media_id == media.media_id
        assert stored_trace.provenance_json["sourceSha256"] == media.checksum_sha256
        assert stored_trace.embeddings[0].model_id == "fake-embedding-model"
        assert stored_trace.embeddings[0].embedding_dimension == 4
        assert media.traces[0].trace_id == trace.trace_id
        assert media.index_runs[0].stage == "visual_extract"
    finally:
        db.close()


def test_visual_extraction_creates_still_and_sparse_video_traces(tmp_path):
    db = make_session(tmp_path)
    try:
        image = add_media(db, tmp_path, filename="photo.jpg", kind="image")
        # Avoid the Media checksum uniqueness constraint in this same fixture DB.
        video_source = tmp_path / "clip.mp4"
        video_source.write_bytes(b"source:clip.mp4")
        video = Media(
            source_type="local",
            source_id=str(video_source),
            source_path=str(video_source),
            title="clip",
            filename="clip.mp4",
            mime_type="video/mp4",
            media_kind="video",
            size_bytes=video_source.stat().st_size,
            source_modified_ns=video_source.stat().st_mtime_ns,
            checksum_sha256="d" * 64,
            duration_seconds=12.2,
            width=1920,
            height=1080,
        )
        db.add(video)
        db.commit()
        db.refresh(video)

        backend = FakeFrameBackend()
        index_root = tmp_path / "library_index"
        config = VisualExtractionConfig(sample_interval_seconds=5.0)

        image_result = index_visual_media(db, image, index_root=index_root, backend=backend, config=config)
        video_result = index_visual_media(db, video, index_root=index_root, backend=backend, config=config)

        assert image_result.expected == 1
        assert image_result.created == 1
        assert video_result.expected == 3
        assert video_result.created == 3

        image_traces = list(db.scalars(select(Trace).where(Trace.media_id == image.media_id)).all())
        video_traces = list(
            db.scalars(select(Trace).where(Trace.media_id == video.media_id).order_by(Trace.start_ms)).all()
        )
        assert [trace.start_ms for trace in image_traces] == [0]
        assert [trace.start_ms for trace in video_traces] == [0, 5000, 10000]
        for trace in image_traces + video_traces:
            assert trace.trace_type == "visual"
            assert trace.extractor == backend.name
            assert trace.extractor_version == backend.version
            assert (index_root / trace.artifact_path).is_file()
    finally:
        db.close()


def test_adaptive_sampling_policy_is_duration_aware_and_bounded():
    config = VisualExtractionConfig()
    cases = [
        (30.0, 5.0, 6),
        (60.0, 5.0, 12),
        (61.0, 10.0, 7),
        (600.0, 10.0, 60),
        (601.0, 30.0, 21),
        (3600.0, 30.0, 120),
        (3601.0, 60.0, 61),
        (10800.0, 60.0, 180),
        (28800.0, 120.0, 240),
    ]

    for duration, expected_interval, expected_count in cases:
        media = Media(media_kind="video", duration_seconds=duration, filename=f"{int(duration)}.mp4")
        timestamps = visual_sample_timestamps_ms(media, config)
        assert effective_sample_interval_seconds(duration, config) == expected_interval
        assert len(timestamps) == expected_count
        assert len(timestamps) <= config.max_video_samples
        assert timestamps == sorted(set(timestamps))

    image = Media(media_kind="image", filename="still.jpg")
    image_plan = visual_sampling_plan(image, config)
    assert image_plan["samplingPolicy"] == "still-image"
    assert image_plan["sampleCount"] == 1
    assert image_plan["timestampsMs"] == [0]


def test_fixed_sampling_override_remains_available():
    config = VisualExtractionConfig(sample_interval_seconds=7.5)
    media = Media(media_kind="video", duration_seconds=31.0, filename="override.mp4")

    plan = visual_sampling_plan(media, config)

    assert config.sampling_policy == "fixed"
    assert plan["intervalSeconds"] == 7.5
    assert plan["timestampsMs"] == [0, 7500, 15000, 22500, 30000]


def test_visual_extraction_rerun_reuses_existing_traces(tmp_path):
    db = make_session(tmp_path)
    try:
        media = add_media(db, tmp_path, filename="reuse.mp4", kind="video", duration=12.2)
        backend = FakeFrameBackend()
        index_root = tmp_path / "library_index"
        config = VisualExtractionConfig(sample_interval_seconds=5.0)

        first = index_visual_media(db, media, index_root=index_root, backend=backend, config=config)
        calls_after_first = len(backend.calls)
        second = index_visual_media(db, media, index_root=index_root, backend=backend, config=config)

        assert first.created == 3
        assert second.created == 0
        assert second.reused == 3
        assert second.repaired == 0
        assert len(backend.calls) == calls_after_first
        traces = list(db.scalars(select(Trace).where(Trace.media_id == media.media_id)).all())
        assert len(traces) == 3
    finally:
        db.close()


def test_failed_visual_run_resumes_from_persisted_progress(tmp_path):
    db = make_session(tmp_path)
    try:
        media = add_media(db, tmp_path, filename="resume.mp4", kind="video", duration=12.2)
        backend = FakeFrameBackend(fail_once_at=5000)
        index_root = tmp_path / "library_index"
        config = VisualExtractionConfig(sample_interval_seconds=5.0)

        with pytest.raises(RuntimeError, match="simulated extraction failure"):
            index_visual_media(db, media, index_root=index_root, backend=backend, config=config)

        traces_after_failure = list(
            db.scalars(select(Trace).where(Trace.media_id == media.media_id).order_by(Trace.start_ms)).all()
        )
        assert [trace.start_ms for trace in traces_after_failure] == [0]
        failed_run = db.scalar(
            select(IndexRun).where(IndexRun.media_id == media.media_id, IndexRun.status == "failed")
        )
        assert failed_run is not None
        assert "simulated extraction failure" in failed_run.error_message

        resumed = index_visual_media(db, media, index_root=index_root, backend=backend, config=config)
        assert resumed.status == "complete"
        assert resumed.reused == 1
        assert resumed.created == 2
        final_traces = list(
            db.scalars(select(Trace).where(Trace.media_id == media.media_id).order_by(Trace.start_ms)).all()
        )
        assert [trace.start_ms for trace in final_traces] == [0, 5000, 10000]
    finally:
        db.close()


def test_missing_visual_artifact_is_repaired_without_duplicate_trace(tmp_path):
    db = make_session(tmp_path)
    try:
        media = add_media(db, tmp_path, filename="repair.mp4", kind="video", duration=6.0)
        backend = FakeFrameBackend()
        index_root = tmp_path / "library_index"
        config = VisualExtractionConfig(sample_interval_seconds=5.0)

        index_visual_media(db, media, index_root=index_root, backend=backend, config=config)
        traces = list(
            db.scalars(select(Trace).where(Trace.media_id == media.media_id).order_by(Trace.start_ms)).all()
        )
        assert len(traces) == 2
        missing = index_root / traces[1].artifact_path
        missing.unlink()

        rerun = index_visual_media(db, media, index_root=index_root, backend=backend, config=config)
        assert rerun.created == 0
        assert rerun.reused == 1
        assert rerun.repaired == 1
        assert missing.is_file()
        assert len(list(db.scalars(select(Trace).where(Trace.media_id == media.media_id)).all())) == 2
    finally:
        db.close()
