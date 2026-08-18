from pathlib import Path

import pytest

from app.indexing.embeddings import EmbeddingBackendError, LazyEmbeddingBackend


class FakeEmbeddingBackend:
    model_id = "fake/model"
    dimension = 3

    def __init__(self):
        self.text_calls = 0
        self.image_calls = 0

    def embed_text(self, texts):
        self.text_calls += 1
        return [[float(index), 1.0, 2.0] for index, _ in enumerate(texts)]

    def embed_images(self, image_paths):
        self.image_calls += 1
        return [[float(index), 3.0, 4.0] for index, _ in enumerate(image_paths)]


def test_lazy_backend_does_not_load_until_first_inference(tmp_path):
    loads = []
    backend = FakeEmbeddingBackend()

    lazy = LazyEmbeddingBackend(
        model_id="fake/model",
        dimension=3,
        loader=lambda: loads.append("loaded") or backend,
    )

    assert lazy.is_loaded is False
    assert loads == []
    assert lazy.embed_text([]) == []
    assert lazy.embed_images([]) == []
    assert lazy.is_loaded is False

    text_vectors = lazy.embed_text(["dragon staff", "fractal burning"])
    assert lazy.is_loaded is True
    assert loads == ["loaded"]
    assert text_vectors == [[0.0, 1.0, 2.0], [1.0, 1.0, 2.0]]

    image_vectors = lazy.embed_images([tmp_path / "a.jpg", tmp_path / "b.jpg"])
    assert image_vectors == [[0.0, 3.0, 4.0], [1.0, 3.0, 4.0]]
    assert loads == ["loaded"]
    assert backend.text_calls == 1
    assert backend.image_calls == 1


def test_lazy_backend_rejects_loader_identity_mismatch():
    backend = FakeEmbeddingBackend()
    backend.model_id = "wrong/model"
    lazy = LazyEmbeddingBackend(model_id="fake/model", dimension=3, loader=lambda: backend)

    with pytest.raises(EmbeddingBackendError, match="model_id"):
        lazy.embed_text(["test"])


def test_lazy_backend_validates_vector_shape_and_values():
    class BadDimensionBackend(FakeEmbeddingBackend):
        def embed_text(self, texts):
            return [[1.0, 2.0] for _ in texts]

    lazy = LazyEmbeddingBackend(model_id="fake/model", dimension=3, loader=BadDimensionBackend)
    with pytest.raises(EmbeddingBackendError, match="dimension"):
        lazy.embed_text(["test"])

    class BadValueBackend(FakeEmbeddingBackend):
        def embed_images(self, image_paths):
            return [[1.0, float("nan"), 3.0] for _ in image_paths]

    lazy = LazyEmbeddingBackend(model_id="fake/model", dimension=3, loader=BadValueBackend)
    with pytest.raises(EmbeddingBackendError, match="non-finite"):
        lazy.embed_images([Path("frame.jpg")])
