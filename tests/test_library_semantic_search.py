from types import SimpleNamespace

from app.indexing.embedding_service import vector_to_float32_blob
from app.library_models import Embedding, Media, Trace


class FakeQueryBackend:
    model_id = "fake-generation"
    dimension = 4

    def __init__(self):
        self.queries = []

    def embed_text(self, texts):
        self.queries.append(list(texts))
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_images(self, image_paths):
        raise AssertionError("Library search must not regenerate image embeddings")


def add_media_trace_embedding(
    db,
    *,
    source_id: str,
    checksum: str,
    title: str,
    filename: str,
    artifact_path: str,
    vector: list[float],
    start_ms: int = 0,
):
    media = Media(
        source_type="local",
        source_id=source_id,
        source_path=source_id,
        title=title,
        filename=filename,
        mime_type="image/jpeg",
        media_kind="image",
        size_bytes=10,
        source_modified_ns=1,
        checksum_sha256=checksum,
        processing_status="discovered",
    )
    db.add(media)
    db.flush()
    trace = Trace(
        media_id=media.media_id,
        trace_type="visual",
        start_ms=start_ms,
        end_ms=start_ms,
        artifact_path=artifact_path,
        extractor="ffmpeg-frame",
        extractor_version="test-1",
        configuration_hash="c" * 64,
    )
    db.add(trace)
    db.flush()
    db.add(
        Embedding(
            trace_id=trace.trace_id,
            model_id="fake-generation",
            embedding_dimension=4,
            dtype="float32",
            vector_blob=vector_to_float32_blob(vector),
            normalized=True,
        )
    )
    db.commit()
    return media, trace


def test_semantic_search_uses_persistent_query_runtime_and_attaches_media_only_after_vector_ranking(client, db_session, monkeypatch):
    first_media, first_trace = add_media_trace_embedding(
        db_session,
        source_id="C:/private/first.jpg",
        checksum="1" * 64,
        title="Completely unrelated display title",
        filename="nothing-useful-here.jpg",
        artifact_path="visual/first.jpg",
        vector=[1.0, 0.0, 0.0, 0.0],
    )
    second_media, second_trace = add_media_trace_embedding(
        db_session,
        source_id="C:/private/second.jpg",
        checksum="2" * 64,
        title="Man Playing Guitar Filename Bait",
        filename="man-playing-guitar.jpg",
        artifact_path="visual/second.jpg",
        vector=[0.0, 1.0, 0.0, 0.0],
    )
    backend = FakeQueryBackend()
    monkeypatch.setattr("app.library_routes.QwenPersistentQueryEmbeddingBackend", lambda: backend)

    first = client.post(
        "/shocks_art/api/library/search/visual",
        json={"query": "man playing guitar", "top_k": 2},
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["mutatesState"] is False
    assert payload["scoringIsolation"].startswith("filename/title are presentation metadata only")
    assert payload["result"]["vectorCount"] == 2
    assert payload["result"]["modelId"] == "fake-generation"
    assert [item["traceId"] for item in payload["result"]["matches"]] == [
        first_trace.trace_id,
        second_trace.trace_id,
    ]
    assert payload["result"]["matches"][0]["score"] == 1.0
    assert payload["result"]["matches"][1]["score"] == 0.0
    assert payload["result"]["matches"][0]["title"] == "Completely unrelated display title"
    assert payload["result"]["matches"][1]["title"] == "Man Playing Guitar Filename Bait"
    assert payload["result"]["matches"][0]["thumbnailUrl"].startswith(
        "/shocks_art/api/library/traces/"
    )
    assert "C:/private" not in first.text
    assert backend.queries == [["man playing guitar"]]

    first_media.title = "Man Playing Guitar"
    first_media.filename = "guitar-guitar-guitar.jpg"
    second_media.title = "No semantic words"
    second_media.filename = "plain.jpg"
    db_session.commit()

    second = client.post(
        "/api/library/search/visual",
        json={"query": "man playing guitar", "top_k": 2},
    )
    assert second.status_code == 200
    assert [item["traceId"] for item in second.json()["result"]["matches"]] == [
        first_trace.trace_id,
        second_trace.trace_id,
    ]
    assert [item["score"] for item in second.json()["result"]["matches"]] == [1.0, 0.0]
    assert backend.queries == [["man playing guitar"], ["man playing guitar"]]


def test_visual_trace_artifact_is_prefix_safe_and_confined(client, db_session, tmp_path, monkeypatch):
    index_root = tmp_path / "library_index"
    artifact = index_root / "visual" / "safe.jpg"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"safe-image")
    monkeypatch.setattr(
        "app.library_routes.get_settings",
        lambda: SimpleNamespace(library_index_path=str(index_root)),
    )

    _media, trace = add_media_trace_embedding(
        db_session,
        source_id="C:/private/artifact.jpg",
        checksum="3" * 64,
        title="Artifact",
        filename="artifact.jpg",
        artifact_path="visual/safe.jpg",
        vector=[1.0, 0.0, 0.0, 0.0],
    )

    local = client.get(f"/api/library/traces/{trace.trace_id}/artifact")
    prefixed = client.get(f"/shocks_art/api/library/traces/{trace.trace_id}/artifact")
    assert local.status_code == 200
    assert prefixed.status_code == 200
    assert prefixed.content == b"safe-image"

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"private")
    trace.artifact_path = "../outside.jpg"
    db_session.commit()
    escaped = client.get(f"/api/library/traces/{trace.trace_id}/artifact")
    assert escaped.status_code == 404
    assert escaped.content != b"private"


def test_library_page_exposes_visual_semantic_search(client):
    page = client.get("/shocks_art/library")
    assert page.status_code == 200
    assert 'id="semantic-search-form"' in page.text
    assert 'placeholder="Search what is in the footage…"' in page.text
    assert 'id="semantic-search-button"' in page.text
    assert ">Search</button>" in page.text
    assert "Semantic indexing comes next." not in page.text
