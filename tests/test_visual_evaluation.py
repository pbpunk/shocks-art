import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.indexing.embedding_service import normalize_vector, vector_to_float32_blob
from app.indexing.evaluation import EvaluationQuery, EvaluationSpec, evaluate_visual_search, load_evaluation_spec
from app.library_models import Embedding, Media, Trace


class FakeEvaluationBackend:
    model_id = "fake@generation-a"
    dimension = 4

    def __init__(self):
        self.text_calls = []

    def embed_text(self, texts):
        values = list(texts)
        self.text_calls.append(values)
        mapping = {
            "guitar": [1.0, 0.9, 0.0, 0.0],
            "sports car": [0.0, 0.0, 1.0, 1.0],
        }
        return [mapping[value] for value in values]

    def embed_images(self, image_paths):
        raise AssertionError("evaluation must reuse persisted image embeddings")


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'evaluation.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def add_candidate(db, tmp_path, *, label: str, vector, model_id="fake@generation-a"):
    source = tmp_path / f"{label}.jpg"
    source.write_bytes(b"source")
    media = Media(
        source_type="local",
        source_id=str(source),
        source_path=str(source),
        title=f"SECRET TITLE {label}",
        filename=f"SECRET FILENAME {label}.jpg",
        media_kind="image",
        mime_type="image/jpeg",
        size_bytes=6,
        source_modified_ns=1,
        checksum_sha256=(label.encode("utf-8").hex() + "0" * 64)[:64],
    )
    db.add(media)
    db.flush()
    trace = Trace(
        media_id=media.media_id,
        trace_type="visual",
        start_ms=0,
        end_ms=0,
        artifact_path=f"visual/{media.media_id}/frame.jpg",
        extractor="fake",
        extractor_version="1",
        configuration_hash="c" * 64,
    )
    db.add(trace)
    db.flush()
    normalized = normalize_vector(vector)
    db.add(
        Embedding(
            trace_id=trace.trace_id,
            model_id=model_id,
            embedding_dimension=4,
            dtype="float32",
            normalized=True,
            vector_blob=vector_to_float32_blob(normalized),
        )
    )
    db.commit()
    return trace


def test_load_evaluation_spec_validates_queries_and_dimensions(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "description": "test",
                "dimensions": [4, 2],
                "topK": 3,
                "queries": [
                    {"id": "present", "kind": "positive", "text": "guitar"},
                    {"id": "absent", "kind": "control", "text": "sports car"},
                ],
            }
        ),
        encoding="utf-8",
    )

    spec = load_evaluation_spec(path)
    assert spec.dimensions == (4, 2)
    assert spec.top_k == 3
    assert [query.kind for query in spec.queries] == ["positive", "control"]

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["queries"][1]["id"] = "present"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_evaluation_spec(path)


def test_evaluation_batches_queries_filters_generation_and_omits_filename_metadata(tmp_path):
    db = make_session(tmp_path)
    try:
        first = add_candidate(db, tmp_path, label="first", vector=[1.0, 1.0, 0.0, 0.0])
        second = add_candidate(db, tmp_path, label="second", vector=[0.7, 0.7, 0.7, 0.7])
        add_candidate(
            db,
            tmp_path,
            label="other-generation",
            vector=[1.0, 1.0, 0.0, 0.0],
            model_id="fake@generation-b",
        )
        backend = FakeEvaluationBackend()
        spec = EvaluationSpec(
            dimensions=(4, 2),
            top_k=2,
            queries=(
                EvaluationQuery(query_id="present", kind="positive", text="guitar"),
                EvaluationQuery(query_id="control", kind="control", text="sports car"),
            ),
        )

        result = evaluate_visual_search(db, backend=backend, spec=spec)

        assert backend.text_calls == [["guitar", "sports car"]]
        assert result["vectorCount"] == 2
        assert result["dimensions"] == [4, 2]
        assert result["positiveQueryCount"] == 1
        assert result["controlQueryCount"] == 1
        assert result["scoringIsolation"] == {
            "usesVisualEmbeddingsOnly": True,
            "filenameUsed": False,
            "titleUsed": False,
            "sourcePathUsed": False,
            "presentationMetadataIncluded": False,
        }
        assert result["queryEmbeddingMs"] >= 0
        assert result["candidateLoadMs"] >= 0
        assert result["scoringMs"] >= 0

        four_dim = result["results"][0]
        assert four_dim["dimension"] == 4
        guitar = next(item for item in four_dim["queries"] if item["queryId"] == "present")
        assert guitar["matches"][0]["traceId"] == first.trace_id
        assert {match["traceId"] for match in guitar["matches"]} == {first.trace_id, second.trace_id}

        rendered = json.dumps(result)
        assert "SECRET TITLE" not in rendered
        assert "SECRET FILENAME" not in rendered
        assert "source_path" not in rendered
    finally:
        db.close()


def test_evaluation_rejects_dimension_above_native_backend(tmp_path):
    db = make_session(tmp_path)
    try:
        add_candidate(db, tmp_path, label="first", vector=[1.0, 0.0, 0.0, 0.0])
        backend = FakeEvaluationBackend()
        spec = EvaluationSpec(
            dimensions=(8,),
            top_k=1,
            queries=(EvaluationQuery(query_id="present", kind="positive", text="guitar"),),
        )
        with pytest.raises(ValueError, match="exceed backend native dimension"):
            evaluate_visual_search(db, backend=backend, spec=spec)
    finally:
        db.close()
