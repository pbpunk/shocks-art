from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.indexing.refinement import RefinementConfig, build_refinement_plan, refine_visual_trace
from app.library_models import Embedding, Media, Trace


class FakeFrameBackend:
    name = "fake-frames"
    version = "1"

    def __init__(self):
        self.timestamps = []

    def extract_frame(self, source_path, timestamp_ms, output_path, *, still_image):
        assert still_image is False
        self.timestamps.append(timestamp_ms)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"frame-{timestamp_ms}".encode("utf-8"))


class FakeMixedBackend:
    model_id = "fake@generation"
    dimension = 2

    def __init__(self, best_timestamp_ms):
        self.best_timestamp_ms = best_timestamp_ms
        self.calls = []

    def embed_text_and_images(self, texts, image_paths):
        texts = list(texts)
        image_paths = list(image_paths)
        self.calls.append((texts, image_paths))
        text_vectors = [[1.0, 0.0] for _ in texts]
        image_vectors = []
        for path in image_paths:
            timestamp_ms = int(Path(path).stem)
            if timestamp_ms == self.best_timestamp_ms:
                image_vectors.append([1.0, 0.0])
            else:
                distance = abs(timestamp_ms - self.best_timestamp_ms)
                image_vectors.append([max(0.05, 1.0 - distance / 10000.0), 1.0])
        return text_vectors, image_vectors


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'refinement.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def add_video_trace(db, tmp_path, *, duration_seconds=30.0, coarse_ms=10000, interval_seconds=5.0):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake-video")
    media = Media(
        source_type="local",
        source_id=str(source),
        source_path=str(source),
        title="Video",
        filename="clip.mp4",
        media_kind="video",
        mime_type="video/mp4",
        size_bytes=10,
        source_modified_ns=1,
        checksum_sha256="a" * 64,
        duration_seconds=duration_seconds,
    )
    db.add(media)
    db.flush()
    trace = Trace(
        media_id=media.media_id,
        trace_type="visual",
        start_ms=coarse_ms,
        end_ms=coarse_ms,
        artifact_path=f"visual/{media.media_id}/coarse.jpg",
        extractor="ffmpeg",
        extractor_version="fake",
        configuration_hash="c" * 64,
        metadata_json={"sampleIntervalSeconds": interval_seconds},
    )
    db.add(trace)
    db.commit()
    return media, trace


def test_short_cadence_plan_refines_five_seconds_to_half_second_samples(tmp_path):
    db = make_session(tmp_path)
    try:
        media, trace = add_video_trace(db, tmp_path, duration_seconds=30.0, coarse_ms=10000, interval_seconds=5.0)
        plan = build_refinement_plan(media, trace)

        assert plan.coarse_timestamp_ms == 10000
        assert plan.radius_seconds == 2.5
        assert plan.step_seconds == 0.5
        assert plan.window_start_ms == 7500
        assert plan.window_end_ms == 12500
        assert plan.sample_count == 11
        assert plan.timestamps_ms[0] == 7500
        assert plan.timestamps_ms[-1] == 12500
        assert 10000 in plan.timestamps_ms
    finally:
        db.close()


def test_long_cadence_plan_is_bounded_at_thirty_one_samples(tmp_path):
    db = make_session(tmp_path)
    try:
        media, trace = add_video_trace(db, tmp_path, duration_seconds=300.0, coarse_ms=120000, interval_seconds=60.0)
        plan = build_refinement_plan(media, trace)

        assert plan.radius_seconds == 30.0
        assert plan.step_seconds == 2.0
        assert plan.window_start_ms == 90000
        assert plan.window_end_ms == 150000
        assert plan.sample_count == 31
        assert 120000 in plan.timestamps_ms
    finally:
        db.close()


def test_refinement_handles_start_boundary_and_keeps_coarse_timestamp(tmp_path):
    db = make_session(tmp_path)
    try:
        media, trace = add_video_trace(db, tmp_path, duration_seconds=8.0, coarse_ms=0, interval_seconds=5.0)
        plan = build_refinement_plan(media, trace)

        assert plan.window_start_ms == 0
        assert plan.window_end_ms == 2500
        assert plan.timestamps_ms[0] == 0
        assert plan.sample_count == 6
    finally:
        db.close()


def test_refinement_ranks_dense_frames_and_does_not_mutate_index(tmp_path):
    db = make_session(tmp_path)
    try:
        media, trace = add_video_trace(db, tmp_path, duration_seconds=30.0, coarse_ms=10000, interval_seconds=5.0)
        frame_backend = FakeFrameBackend()
        embedding_backend = FakeMixedBackend(best_timestamp_ms=10500)
        output_dir = tmp_path / "review"

        trace_count_before = db.scalar(select(func.count()).select_from(Trace))
        embedding_count_before = db.scalar(select(func.count()).select_from(Embedding))

        result = refine_visual_trace(
            db,
            trace_id=trace.trace_id,
            query="man playing guitar",
            embedding_backend=embedding_backend,
            frame_backend=frame_backend,
            config=RefinementConfig(top_k=3),
            output_directory=output_dir,
        )

        assert result.trace_id == trace.trace_id
        assert result.media_id == media.media_id
        assert result.model_id == embedding_backend.model_id
        assert result.dimension == 2
        assert result.plan.sample_count == 11
        assert len(frame_backend.timestamps) == 11
        assert len(embedding_backend.calls) == 1
        texts, image_paths = embedding_backend.calls[0]
        assert texts == ["man playing guitar"]
        assert len(image_paths) == 11
        assert result.matches[0].timestamp_ms == 10500
        assert result.matches[0].offset_ms == 500
        assert result.matches[0].score == pytest.approx(1.0)
        assert len(result.matches) == 3
        assert all(match.artifact_file for match in result.matches)
        assert len(list(output_dir.glob("*.jpg"))) == 3
        assert result.output_directory == str(output_dir)

        assert db.scalar(select(func.count()).select_from(Trace)) == trace_count_before
        assert db.scalar(select(func.count()).select_from(Embedding)) == embedding_count_before
    finally:
        db.close()


def test_refinement_rejects_still_image_trace(tmp_path):
    db = make_session(tmp_path)
    try:
        source = tmp_path / "still.jpg"
        source.write_bytes(b"image")
        media = Media(
            source_type="local",
            source_id=str(source),
            source_path=str(source),
            title="Still",
            filename="still.jpg",
            media_kind="image",
            mime_type="image/jpeg",
            size_bytes=5,
            source_modified_ns=1,
            checksum_sha256="b" * 64,
        )
        db.add(media)
        db.flush()
        trace = Trace(
            media_id=media.media_id,
            trace_type="visual",
            start_ms=0,
            end_ms=0,
            artifact_path="visual/still.jpg",
            extractor="fake",
            extractor_version="1",
            configuration_hash="d" * 64,
        )
        db.add(trace)
        db.commit()

        with pytest.raises(ValueError, match="video Trace"):
            refine_visual_trace(
                db,
                trace_id=trace.trace_id,
                query="object",
                embedding_backend=FakeMixedBackend(best_timestamp_ms=0),
                frame_backend=FakeFrameBackend(),
            )
    finally:
        db.close()
