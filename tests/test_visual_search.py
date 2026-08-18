from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import pytest

from app.core.database import Base
from app.indexing.embedding_service import normalize_vector, vector_to_float32_blob
from app.indexing.embeddings import EmbeddingBackendError
from app.indexing.visual_search import search_visual_embeddings
from app.library_models import Embedding, Media, Trace


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'visual-search.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def add_media_with_trace(db, *, media_id: str, trace_id: str, start_ms: int) -> Trace:
    media = db.get(Media, media_id)
    if media is None:
        media = Media(
            media_id=media_id,
            source_type="test",
            source_id=media_id,
            source_path="",
            filename=f"{media_id}.jpg",
            title="fixture",
            media_kind="image",
            mime_type="image/jpeg",
            size_bytes=1,
            source_modified_ns=1,
            checksum_sha256=(media_id.encode("utf-8").hex() + "0" * 64)[:64],
        )
        db.add(media)
        db.flush()
    trace = Trace(
        trace_id=trace_id,
        media_id=media_id,
        trace_type="visual",
        start_ms=start_ms,
        end_ms=start_ms,
        artifact_path=f"visual/{media_id}/{start_ms}.jpg",
        extractor="fixture",
        extractor_version="1",
        configuration_hash="c" * 64,
    )
    db.add(trace)
    db.flush()
    return trace


def add_embedding(db, trace: Trace, *, model_id: str, vector: list[float]):
    normalized = normalize_vector(vector)
    db.add(
        Embedding(
            trace_id=trace.trace_id,
            model_id=model_id,
            embedding_dimension=len(normalized),
            dtype="float32",
            vector_blob=vector_to_float32_blob(normalized),
            normalized=True,
        )
    )
    db.flush()


def test_search_returns_deterministic_top_k_and_timings(tmp_path):
    db = make_session(tmp_path)
    try:
        model_id = "qwen@generation-a"
        first = add_media_with_trace(db, media_id="media_a", trace_id="trace_b", start_ms=0)
        tied = add_media_with_trace(db, media_id="media_a", trace_id="trace_a", start_ms=1000)
        third = add_media_with_trace(db, media_id="media_b", trace_id="trace_c", start_ms=0)

        add_embedding(db, first, model_id=model_id, vector=[1.0, 0.0])
        add_embedding(db, tied, model_id=model_id, vector=[1.0, 0.0])
        add_embedding(db, third, model_id=model_id, vector=[0.8, 0.6])
        db.commit()

        result = search_visual_embeddings(
            db,
            query_vector=[10.0, 0.0],
            model_id=model_id,
            dimension=2,
            top_k=2,
        )

        assert result.vector_count == 3
        assert [match.trace_id for match in result.matches] == ["trace_a", "trace_b"]
        assert [match.score for match in result.matches] == pytest.approx([1.0, 1.0], abs=1e-6)
        assert result.database_ms >= 0
        assert result.scoring_ms >= 0
        assert result.elapsed_ms >= result.database_ms
        payload = result.as_dict()
        assert payload["vectorCount"] == 3
        assert payload["returned"] == 2
        assert payload["matches"][0]["traceId"] == "trace_a"
    finally:
        db.close()


def test_search_filters_exact_embedding_generation(tmp_path):
    db = make_session(tmp_path)
    try:
        trace = add_media_with_trace(db, media_id="media_a", trace_id="trace_a", start_ms=0)
        add_embedding(db, trace, model_id="generation-a", vector=[1.0, 0.0])
        add_embedding(db, trace, model_id="generation-b", vector=[0.0, 1.0])
        db.commit()

        first = search_visual_embeddings(
            db,
            query_vector=[1.0, 0.0],
            model_id="generation-a",
            dimension=2,
        )
        second = search_visual_embeddings(
            db,
            query_vector=[1.0, 0.0],
            model_id="generation-b",
            dimension=2,
        )

        assert first.vector_count == 1
        assert first.matches[0].score == pytest.approx(1.0, abs=1e-6)
        assert second.vector_count == 1
        assert second.matches[0].score == pytest.approx(0.0, abs=1e-6)
    finally:
        db.close()


def test_search_rejects_wrong_query_dimension_and_corrupt_blob(tmp_path):
    db = make_session(tmp_path)
    try:
        trace = add_media_with_trace(db, media_id="media_a", trace_id="trace_a", start_ms=0)
        add_embedding(db, trace, model_id="generation-a", vector=[1.0, 0.0])
        db.commit()

        with pytest.raises(EmbeddingBackendError, match="query embedding has dimension"):
            search_visual_embeddings(
                db,
                query_vector=[1.0, 0.0, 0.0],
                model_id="generation-a",
                dimension=2,
            )

        embedding = trace.embeddings[0]
        embedding.vector_blob = b"bad"
        db.commit()
        with pytest.raises(EmbeddingBackendError, match="expected 8"):
            search_visual_embeddings(
                db,
                query_vector=[1.0, 0.0],
                model_id="generation-a",
                dimension=2,
            )
    finally:
        db.close()


def test_search_empty_generation_is_fast_and_empty(tmp_path):
    db = make_session(tmp_path)
    try:
        result = search_visual_embeddings(
            db,
            query_vector=[1.0, 0.0],
            model_id="missing-generation",
            dimension=2,
            top_k=5,
        )
        assert result.vector_count == 0
        assert result.matches == ()
        assert result.as_dict()["returned"] == 0
    finally:
        db.close()
