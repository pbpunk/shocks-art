from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable


class TranscriptionBackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptionSegment:
    """One timestamped speech segment returned by a local transcription backend."""

    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptionResult:
    """Normalized local transcription result independent of any Whisper library."""

    model_id: str
    language: str
    segments: tuple[TranscriptionSegment, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)


@runtime_checkable
class TranscriptionBackend(Protocol):
    """Minimal speech-to-text contract used by the offline indexer.

    Implementations may depend on heavyweight ML libraries, but those imports
    belong behind a lazy loader. Importing this module or the FastAPI application
    must never require faster-whisper, CTranslate2, CUDA, or a model download.
    """

    model_id: str

    def transcribe(
        self,
        media_path: Path,
        *,
        language: str | None = None,
    ) -> TranscriptionResult: ...


BackendLoader = Callable[[], TranscriptionBackend]


class LazyTranscriptionBackend:
    """Load a heavyweight transcription implementation only on first inference."""

    def __init__(self, *, model_id: str, loader: BackendLoader):
        model_id = model_id.strip()
        if not model_id:
            raise ValueError("model_id must not be empty")
        self.model_id = model_id
        self._loader = loader
        self._backend: TranscriptionBackend | None = None
        self._load_lock = Lock()

    @property
    def is_loaded(self) -> bool:
        return self._backend is not None

    def _get_backend(self) -> TranscriptionBackend:
        if self._backend is None:
            with self._load_lock:
                if self._backend is None:
                    backend = self._loader()
                    if backend.model_id != self.model_id:
                        raise TranscriptionBackendError(
                            f"loaded backend model_id {backend.model_id!r} does not match declared {self.model_id!r}"
                        )
                    self._backend = backend
        return self._backend

    def transcribe(
        self,
        media_path: Path,
        *,
        language: str | None = None,
    ) -> TranscriptionResult:
        path = Path(media_path)
        result = self._get_backend().transcribe(path, language=language)
        return self._validate_result(result)

    def _validate_result(self, result: TranscriptionResult) -> TranscriptionResult:
        if result.model_id != self.model_id:
            raise TranscriptionBackendError(
                f"backend result model_id {result.model_id!r} does not match declared {self.model_id!r}"
            )
        language = result.language.strip()
        if not language:
            raise TranscriptionBackendError("backend result language must not be empty")

        validated: list[TranscriptionSegment] = []
        previous_start = -1
        for index, segment in enumerate(result.segments):
            if segment.start_ms < 0:
                raise TranscriptionBackendError(f"segment {index} start_ms must be nonnegative")
            if segment.end_ms < segment.start_ms:
                raise TranscriptionBackendError(f"segment {index} end_ms precedes start_ms")
            text = segment.text.strip()
            if not text:
                raise TranscriptionBackendError(f"segment {index} text must not be empty")
            if segment.start_ms < previous_start:
                raise TranscriptionBackendError("segments must be ordered by start_ms")
            if segment.confidence is not None:
                confidence = float(segment.confidence)
                if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
                    raise TranscriptionBackendError(
                        f"segment {index} confidence must be finite and between 0 and 1"
                    )
            previous_start = segment.start_ms
            validated.append(
                TranscriptionSegment(
                    start_ms=int(segment.start_ms),
                    end_ms=int(segment.end_ms),
                    text=text,
                    confidence=segment.confidence,
                    metadata=dict(segment.metadata),
                )
            )

        return TranscriptionResult(
            model_id=result.model_id,
            language=language,
            segments=tuple(validated),
            metadata=dict(result.metadata),
        )


def unavailable_transcription_loader(reason: str) -> BackendLoader:
    """Return a loader that fails only when transcription is actually requested."""

    detail = reason.strip() or "local transcription runtime is unavailable"

    def _load() -> TranscriptionBackend:
        raise TranscriptionBackendError(detail)

    return _load
