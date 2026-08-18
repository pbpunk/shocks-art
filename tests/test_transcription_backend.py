from pathlib import Path

import pytest

from app.indexing.transcription import (
    LazyTranscriptionBackend,
    TranscriptionBackendError,
    TranscriptionResult,
    TranscriptionSegment,
    unavailable_transcription_loader,
)


class FakeBackend:
    def __init__(self, model_id: str = "fake-whisper"):
        self.model_id = model_id
        self.calls = []

    def transcribe(self, media_path: Path, *, language: str | None = None) -> TranscriptionResult:
        self.calls.append((media_path, language))
        return TranscriptionResult(
            model_id=self.model_id,
            language=language or "en",
            segments=(
                TranscriptionSegment(
                    start_ms=100,
                    end_ms=900,
                    text="  Dragon staff  ",
                    confidence=0.9,
                    metadata={"source": "fake"},
                ),
                TranscriptionSegment(start_ms=1000, end_ms=1600, text="second line"),
            ),
            metadata={"durationSeconds": 1.6},
        )


def test_lazy_backend_loads_once_on_first_inference_and_normalizes_output(tmp_path):
    backend_impl = FakeBackend()
    loads = []

    def loader():
        loads.append(True)
        return backend_impl

    backend = LazyTranscriptionBackend(model_id="fake-whisper", loader=loader)
    media = tmp_path / "clip.mp4"

    assert backend.is_loaded is False
    first = backend.transcribe(media, language="en")
    second = backend.transcribe(media, language="en")

    assert backend.is_loaded is True
    assert len(loads) == 1
    assert len(backend_impl.calls) == 2
    assert first.model_id == "fake-whisper"
    assert first.language == "en"
    assert first.segments[0].text == "Dragon staff"
    assert first.segments[0].start_ms == 100
    assert first.segments[0].end_ms == 900
    assert first.segments[0].confidence == 0.9
    assert first.metadata == {"durationSeconds": 1.6}
    assert second == first


def test_lazy_backend_rejects_loaded_model_identity_mismatch(tmp_path):
    backend = LazyTranscriptionBackend(model_id="expected", loader=lambda: FakeBackend("other"))

    with pytest.raises(TranscriptionBackendError, match="does not match declared"):
        backend.transcribe(tmp_path / "clip.mp4")


@pytest.mark.parametrize(
    "segment,error",
    [
        (TranscriptionSegment(start_ms=-1, end_ms=10, text="bad"), "start_ms"),
        (TranscriptionSegment(start_ms=20, end_ms=10, text="bad"), "end_ms"),
        (TranscriptionSegment(start_ms=0, end_ms=10, text="   "), "text"),
        (TranscriptionSegment(start_ms=0, end_ms=10, text="bad", confidence=-0.1), "confidence"),
        (TranscriptionSegment(start_ms=0, end_ms=10, text="bad", confidence=1.1), "confidence"),
    ],
)
def test_lazy_backend_rejects_invalid_segments(tmp_path, segment, error):
    class InvalidBackend(FakeBackend):
        def transcribe(self, media_path: Path, *, language: str | None = None) -> TranscriptionResult:
            return TranscriptionResult(model_id=self.model_id, language="en", segments=(segment,))

    backend = LazyTranscriptionBackend(model_id="fake-whisper", loader=InvalidBackend)

    with pytest.raises(TranscriptionBackendError, match=error):
        backend.transcribe(tmp_path / "clip.mp4")


def test_lazy_backend_rejects_out_of_order_segments(tmp_path):
    class InvalidBackend(FakeBackend):
        def transcribe(self, media_path: Path, *, language: str | None = None) -> TranscriptionResult:
            return TranscriptionResult(
                model_id=self.model_id,
                language="en",
                segments=(
                    TranscriptionSegment(start_ms=1000, end_ms=1200, text="later"),
                    TranscriptionSegment(start_ms=500, end_ms=700, text="earlier"),
                ),
            )

    backend = LazyTranscriptionBackend(model_id="fake-whisper", loader=InvalidBackend)

    with pytest.raises(TranscriptionBackendError, match="ordered"):
        backend.transcribe(tmp_path / "clip.mp4")


def test_lazy_backend_rejects_empty_language(tmp_path):
    class InvalidBackend(FakeBackend):
        def transcribe(self, media_path: Path, *, language: str | None = None) -> TranscriptionResult:
            return TranscriptionResult(model_id=self.model_id, language=" ", segments=())

    backend = LazyTranscriptionBackend(model_id="fake-whisper", loader=InvalidBackend)

    with pytest.raises(TranscriptionBackendError, match="language"):
        backend.transcribe(tmp_path / "clip.mp4")


def test_unavailable_loader_defers_failure_until_inference(tmp_path):
    backend = LazyTranscriptionBackend(
        model_id="candidate-whisper",
        loader=unavailable_transcription_loader("faster-whisper is not installed"),
    )

    assert backend.is_loaded is False
    with pytest.raises(TranscriptionBackendError, match="not installed"):
        backend.transcribe(tmp_path / "clip.mp4")
    assert backend.is_loaded is False


def test_importing_transcription_contract_does_not_require_faster_whisper():
    import app.indexing.transcription as transcription

    assert "faster_whisper" not in transcription.__dict__
    assert "ctranslate2" not in transcription.__dict__
