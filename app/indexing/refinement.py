from __future__ import annotations

import math
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, Sequence

from sqlalchemy.orm import Session

from app.indexing.embedding_service import normalize_vector
from app.indexing.embeddings import EmbeddingBackendError, Vector
from app.indexing.service import FfmpegFrameBackend, FrameExtractionBackend, VisualExtractionError
from app.library_models import Media, Trace


class MixedEmbeddingBackend(Protocol):
    model_id: str
    dimension: int

    def embed_text_and_images(
        self,
        texts: Sequence[str],
        image_paths: Sequence[Path],
    ) -> tuple[list[Vector], list[Vector]]: ...


@dataclass(frozen=True)
class RefinementConfig:
    radius_seconds: float | None = None
    step_seconds: float | None = None
    max_samples: int = 31
    top_k: int = 10
    min_radius_seconds: float = 2.5
    max_radius_seconds: float = 30.0
    min_step_seconds: float = 0.5
    max_step_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.radius_seconds is not None and self.radius_seconds <= 0:
            raise ValueError("radius_seconds must be greater than zero when provided")
        if self.step_seconds is not None and self.step_seconds <= 0:
            raise ValueError("step_seconds must be greater than zero when provided")
        if self.max_samples < 3:
            raise ValueError("max_samples must be at least 3")
        if self.top_k <= 0:
            raise ValueError("top_k must be greater than zero")
        if not (0 < self.min_radius_seconds <= self.max_radius_seconds):
            raise ValueError("refinement radius bounds are invalid")
        if not (0 < self.min_step_seconds <= self.max_step_seconds):
            raise ValueError("refinement step bounds are invalid")


@dataclass(frozen=True)
class RefinementPlan:
    coarse_timestamp_ms: int
    coarse_interval_seconds: float
    window_start_ms: int
    window_end_ms: int
    radius_seconds: float
    step_seconds: float
    sample_count: int
    timestamps_ms: tuple[int, ...]

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["timestamps_ms"] = list(self.timestamps_ms)
        return payload


@dataclass(frozen=True)
class RefinementMatch:
    rank: int
    timestamp_ms: int
    offset_ms: int
    score: float
    artifact_file: str | None

    def as_dict(self) -> dict:
        return {
            "rank": self.rank,
            "timestampMs": self.timestamp_ms,
            "offsetMs": self.offset_ms,
            "score": round(self.score, 8),
            "artifactFile": self.artifact_file,
        }


@dataclass(frozen=True)
class RefinementResult:
    query: str
    trace_id: str
    media_id: str
    model_id: str
    dimension: int
    plan: RefinementPlan
    extraction_ms: float
    embedding_ms: float
    scoring_ms: float
    elapsed_ms: float
    matches: tuple[RefinementMatch, ...]
    output_directory: str | None

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "traceId": self.trace_id,
            "mediaId": self.media_id,
            "modelId": self.model_id,
            "dimension": self.dimension,
            "plan": {
                "coarseTimestampMs": self.plan.coarse_timestamp_ms,
                "coarseIntervalSeconds": self.plan.coarse_interval_seconds,
                "windowStartMs": self.plan.window_start_ms,
                "windowEndMs": self.plan.window_end_ms,
                "radiusSeconds": self.plan.radius_seconds,
                "stepSeconds": self.plan.step_seconds,
                "sampleCount": self.plan.sample_count,
                "timestampsMs": list(self.plan.timestamps_ms),
            },
            "extractionMs": round(self.extraction_ms, 4),
            "embeddingMs": round(self.embedding_ms, 4),
            "scoringMs": round(self.scoring_ms, 4),
            "elapsedMs": round(self.elapsed_ms, 4),
            "returned": len(self.matches),
            "outputDirectory": self.output_directory,
            "matches": [match.as_dict() for match in self.matches],
            "mutatesIndex": False,
        }


def _coarse_interval_seconds(trace: Trace) -> float:
    metadata = trace.metadata_json if isinstance(trace.metadata_json, dict) else {}
    value = metadata.get("sampleIntervalSeconds")
    try:
        interval = float(value)
    except (TypeError, ValueError):
        interval = 5.0
    return interval if interval > 0 else 5.0


def _replace_nearest_with_coarse(timestamps: list[int], coarse_ms: int) -> list[int]:
    if not timestamps:
        return [coarse_ms]
    if coarse_ms in timestamps:
        return timestamps
    nearest_index = min(range(len(timestamps)), key=lambda index: abs(timestamps[index] - coarse_ms))
    timestamps[nearest_index] = coarse_ms
    return sorted(set(timestamps))


def build_refinement_plan(
    media: Media,
    trace: Trace,
    config: RefinementConfig | None = None,
) -> RefinementPlan:
    config = config or RefinementConfig()
    if media.media_kind != "video":
        raise ValueError("localized refinement requires video Media")

    duration_ms = max(0, int(round(float(media.duration_seconds or 0.0) * 1000.0)))
    if duration_ms <= 0:
        raise ValueError("video duration is required for localized refinement")

    coarse_ms = min(max(0, int(trace.start_ms)), max(0, duration_ms - 1))
    coarse_interval = _coarse_interval_seconds(trace)
    radius = config.radius_seconds
    if radius is None:
        radius = min(
            config.max_radius_seconds,
            max(config.min_radius_seconds, coarse_interval / 2.0),
        )
    step = config.step_seconds
    if step is None:
        step = min(
            config.max_step_seconds,
            max(config.min_step_seconds, coarse_interval / 10.0),
        )

    radius_ms = max(1, int(round(radius * 1000.0)))
    window_start_ms = max(0, coarse_ms - radius_ms)
    window_end_ms = min(duration_ms - 1, coarse_ms + radius_ms)
    span_ms = max(0, window_end_ms - window_start_ms)

    step_ms = max(1, int(round(step * 1000.0)))
    if span_ms > 0:
        minimum_step_for_cap = max(1, math.ceil(span_ms / max(1, config.max_samples - 1)))
        step_ms = max(step_ms, minimum_step_for_cap)

    timestamps = list(range(window_start_ms, window_end_ms + 1, step_ms))
    if not timestamps:
        timestamps = [coarse_ms]
    timestamps = _replace_nearest_with_coarse(timestamps, coarse_ms)
    if len(timestamps) > config.max_samples:
        # This can happen only from integer rounding around the inclusive end.
        timestamps = timestamps[: config.max_samples]
        timestamps = _replace_nearest_with_coarse(timestamps, coarse_ms)

    return RefinementPlan(
        coarse_timestamp_ms=coarse_ms,
        coarse_interval_seconds=coarse_interval,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        radius_seconds=radius,
        step_seconds=step_ms / 1000.0,
        sample_count=len(timestamps),
        timestamps_ms=tuple(timestamps),
    )


def _score(query: Sequence[float], candidate: Sequence[float]) -> float:
    if len(query) != len(candidate):
        raise EmbeddingBackendError(
            f"refinement dimension mismatch: query={len(query)}, candidate={len(candidate)}"
        )
    value = sum(float(left) * float(right) for left, right in zip(query, candidate, strict=True))
    return max(-1.0, min(1.0, value))


def _run_refinement(
    *,
    media: Media,
    trace: Trace,
    query: str,
    plan: RefinementPlan,
    working_directory: Path,
    output_directory: Path | None,
    frame_backend: FrameExtractionBackend,
    embedding_backend: MixedEmbeddingBackend,
    top_k: int,
) -> tuple[float, float, float, tuple[RefinementMatch, ...]]:
    source_path = Path(media.source_path)
    frame_paths: list[Path] = []

    extraction_started = time.perf_counter()
    for timestamp_ms in plan.timestamps_ms:
        frame_path = working_directory / f"{timestamp_ms:012d}.jpg"
        frame_backend.extract_frame(
            source_path,
            timestamp_ms,
            frame_path,
            still_image=False,
        )
        frame_paths.append(frame_path)
    extraction_ms = (time.perf_counter() - extraction_started) * 1000.0

    embedding_started = time.perf_counter()
    text_vectors, image_vectors = embedding_backend.embed_text_and_images([query], frame_paths)
    embedding_ms = (time.perf_counter() - embedding_started) * 1000.0
    if len(text_vectors) != 1 or len(image_vectors) != len(frame_paths):
        raise EmbeddingBackendError(
            "localized refinement backend returned an unexpected number of vectors"
        )

    query_vector = normalize_vector(text_vectors[0])
    scoring_started = time.perf_counter()
    scored: list[tuple[float, int, Path]] = []
    for timestamp_ms, frame_path, vector in zip(plan.timestamps_ms, frame_paths, image_vectors, strict=True):
        candidate = normalize_vector(vector)
        scored.append((_score(query_vector, candidate), timestamp_ms, frame_path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    scoring_ms = (time.perf_counter() - scoring_started) * 1000.0

    matches: list[RefinementMatch] = []
    for rank, (score, timestamp_ms, frame_path) in enumerate(scored[:top_k], start=1):
        artifact_file = None
        if output_directory is not None:
            destination = output_directory / frame_path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(frame_path.read_bytes())
            artifact_file = destination.name
        matches.append(
            RefinementMatch(
                rank=rank,
                timestamp_ms=timestamp_ms,
                offset_ms=timestamp_ms - plan.coarse_timestamp_ms,
                score=score,
                artifact_file=artifact_file,
            )
        )
    return extraction_ms, embedding_ms, scoring_ms, tuple(matches)


def refine_visual_trace(
    db: Session,
    *,
    trace_id: str,
    query: str,
    embedding_backend: MixedEmbeddingBackend,
    frame_backend: FrameExtractionBackend | None = None,
    config: RefinementConfig | None = None,
    output_directory: Path | None = None,
) -> RefinementResult:
    """Densely rescan only the local time window around one coarse visual Trace.

    The operation is intentionally non-mutating with respect to the persistent
    Media/Trace/Embedding index. Dense frames are temporary unless an explicit
    ignored review output directory is supplied.
    """

    semantic_query = query.strip()
    if not semantic_query:
        raise ValueError("query must not be blank")
    trace = db.get(Trace, trace_id)
    if trace is None or trace.trace_type != "visual":
        raise ValueError(f"visual Trace not found: {trace_id}")
    media = db.get(Media, trace.media_id)
    if media is None:
        raise ValueError(f"Media not found for Trace: {trace_id}")
    if media.media_kind != "video":
        raise ValueError("localized refinement requires a video Trace")
    if media.source_type != "local":
        raise VisualExtractionError(
            f"localized refinement currently supports local Media only; got source_type={media.source_type!r}"
        )
    source_path = Path(media.source_path)
    if not source_path.is_file():
        raise VisualExtractionError(f"source Media is not available locally: {source_path}")

    config = config or RefinementConfig()
    frame_backend = frame_backend or FfmpegFrameBackend()
    plan = build_refinement_plan(media, trace, config)
    output_path = Path(output_directory).resolve() if output_directory is not None else None
    if output_path is not None:
        output_path.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="visual-refinement-") as temp_dir:
        extraction_ms, embedding_ms, scoring_ms, matches = _run_refinement(
            media=media,
            trace=trace,
            query=semantic_query,
            plan=plan,
            working_directory=Path(temp_dir),
            output_directory=output_path,
            frame_backend=frame_backend,
            embedding_backend=embedding_backend,
            top_k=min(config.top_k, plan.sample_count),
        )
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    return RefinementResult(
        query=semantic_query,
        trace_id=trace.trace_id,
        media_id=media.media_id,
        model_id=embedding_backend.model_id,
        dimension=embedding_backend.dimension,
        plan=plan,
        extraction_ms=extraction_ms,
        embedding_ms=embedding_ms,
        scoring_ms=scoring_ms,
        elapsed_ms=elapsed_ms,
        matches=matches,
        output_directory=str(output_directory) if output_directory is not None else None,
    )
