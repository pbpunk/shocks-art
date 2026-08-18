from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.indexing.embedding_service import (
    float32_blob_to_vector,
    index_visual_trace_embeddings,
)
from app.indexing.embeddings import EmbeddingBackendError
from app.library_models import Embedding, Media, Trace


class FakeEmbeddingBackend:
    def __init__(self, *, model_id: str = "fake-model@generation-a", dimension: int = 4):
        self.model_id = model_id
        self.dimension = dimension
        self.calls: list[list[Path]] = []

    def embed_text(self, texts):
        return [[1.0] + [0.0] * (self.dimension - 1) for _ in texts]

    def embed_images(self, image_paths):
        paths = [Path(path) for path in image_paths]
        self.calls.append(paths)
        vectors = []
        for index, _path in enumerate(paths, start=1):
            vector = [float(index), 2.0, 3.0, 4.0][: self.dimension]
            if len(vector) < self.dimension:
                vector.extend([1.0] * (self.dimension - len(vector)))
            vectors.append(vector)
        return vectors


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'embedding.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def add_visual_trace(db, tmp_path, *, filename: str, timestamp_ms: int) -> tuple[Trace, Path]:
    source = tmp_path / f"source-{filename}"
    source.write_bytes(b"source")
    media = Media(
        source_type="local",
        source_id=str(source),
        source_path=str(source),
        filename=filename,
        title=Path(filename).stem,
        media_kind="image",
        mime_type="image/jpeg",
        size_bytes=source.stat().st_size,
        source_modified_ns=source.stat().st_mtime_ns,
        checksum_sha256=(f"{timestamp_ms:064x}")[-64:],
    )
    db.add(media)
    db.flush()

    artifact_root = tmp_path / "library_index"
    artifact = artifact_root / "visual" / media.media_id / f"{timestamp_ms}.jpg"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"frame")
    trace = Trace(
        media_id=media.media_id,
        trace_type="visual",
        start_ms=timestamp_ms,
        end_ms=timestamp_ms,
        artifact_path=artifact.relative_to(artifact_root).as_posix(),
        extractor="fake",
        extractor_version="1",
        configuration_hash="c" * 64,
    )
    db.add(trace)
    db.commit()
    db.refresh(trace)
    return trace, artifact_root


def test_visual_embeddings_are_normalized_persisted_and_reused(tmp_path):
    db = make_session(tmp_path)
    try:
        first_trace, index_root = add_visual_trace(db, tmp_path, filename="first.jpg", timestamp_ms=1)
        second_trace, _ = add_visual_trace(db, tmp_path, filename="second.jpg", timestamp_ms=2)
        backend = FakeEmbeddingBackend()

        first = index_visual_trace_embeddings(db, index_root=index_root, backend=backend)
        second = index_visual_trace_embeddings(db, index_root=index_root, backend=backend)

        assert first.considered == 2
        assert first.created == 2
        assert first.reused == 0
        assert second.created == 0
        assert second.reused == 2
        assert len(backend.calls) == 1

        rows = list(db.scalars(select(Embedding).order_by(Embedding.trace_id)).all())
        assert len(rows) == 2
        assert {row.trace_id for row in rows} == {first_trace.trace_id, second_trace.trace_id}
        for row in rows:
            assert row.model_id == backend.model_id
            assert row.embedding_dimension == 4
            assert row.dtype == "float32"
            assert row.normalized is True
            vector = float32_blob_to_vector(row.vector_blob)
            assert len(vector) == 4
            norm = sum(value * value for value in vector) ** 0.5
            assert norm == pytest.approx(1.0, abs=1e-6)
    finally:
        db.close()


def test_different_embedding_generations_coexist_without_replacement(tmp_path):
    db = make_session(tmp_path)
    try:
        trace, index_root = add_visual_trace(db, tmp_path, filename="subject.jpg", timestamp_ms=3)
        generation_a = FakeEmbeddingBackend(model_id="qwen@generation-a")
        generation_b = FakeEmbeddingBackend(model_id="qwen@generation-b")

        result_a = index_visual_trace_embeddings(db, index_root=index_root, backend=generation_a)
        result_b = index_visual_trace_embeddings(db, index_root=index_root, backend=generation_b)

        assert result_a.created == 1
        assert result_b.created == 1
        rows = list(db.scalars(select(Embedding).where(Embedding.trace_id == trace.trace_id)).all())
        assert len(rows) == 2
        assert {row.model_id for row in rows} == {"qwen@generation-a", "qwen@generation-b"}
    finally:
        db.close()


def test_missing_or_escaping_trace_artifact_fails_before_embedding(tmp_path):
    db = make_session(tmp_path)
    try:
        trace, index_root = add_visual_trace(db, tmp_path, filename="missing.jpg", timestamp_ms=4)
        backend = FakeEmbeddingBackend()

        (index_root / trace.artifact_path).unlink()
        with pytest.raises(EmbeddingBackendError, match="artifact is missing"):
            index_visual_trace_embeddings(db, index_root=index_root, backend=backend)
        assert backend.calls == []

        outside = tmp_path / "outside.jpg"
        outside.write_bytes(b"outside")
        trace.artifact_path = "../outside.jpg"
        db.commit()
        with pytest.raises(EmbeddingBackendError, match="escapes Library index root"):
            index_visual_trace_embeddings(db, index_root=index_root, backend=backend)
        assert backend.calls == []
    finally:
        db.close()
