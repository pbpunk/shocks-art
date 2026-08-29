from __future__ import annotations

import os
import statistics
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * fraction))
    return ordered[max(0, min(len(ordered) - 1, index))]


def summarize_media_pairs(
    *,
    media_id: str,
    duration_seconds: float,
    timestamps_ms: list[int],
    cosines: list[float],
) -> dict[str, Any]:
    intervals = [
        max(0.0, (right - left) / 1000.0)
        for left, right in zip(timestamps_ms, timestamps_ms[1:], strict=False)
    ]
    return {
        "media_id": media_id,
        "duration_seconds": round(duration_seconds, 3),
        "trace_count": len(timestamps_ms),
        "adjacent_pair_count": len(cosines),
        "median_interval_seconds": round(float(statistics.median(intervals)), 3) if intervals else None,
        "mean_cosine": round(sum(cosines) / len(cosines), 6) if cosines else None,
        "p50_cosine": round(float(percentile(cosines, 0.50)), 6) if cosines else None,
        "p95_cosine": round(float(percentile(cosines, 0.95)), 6) if cosines else None,
        "fraction_ge_0_98": round(sum(1 for value in cosines if value >= 0.98) / len(cosines), 6) if cosines else None,
        "fraction_ge_0_995": round(sum(1 for value in cosines if value >= 0.995) / len(cosines), 6) if cosines else None,
    }


def decode_normalized_float32(blob: bytes, dimension: int) -> tuple[float, ...]:
    expected = dimension * 4
    if len(blob) != expected:
        raise ValueError(f"vector byte length {len(blob)} does not match dimension {dimension}")
    return struct.unpack(f"<{dimension}f", blob)


def normalized_cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("vector dimensions differ")
    value = sum(a * b for a, b in zip(left, right, strict=True))
    return max(-1.0, min(1.0, float(value)))


def collect_long_form_redundancy(
    *,
    live_root: Path,
    model_id: str,
    dimension: int,
    max_media: int = 5,
) -> dict[str, Any]:
    """Measure adjacent visual-embedding redundancy on existing >1h media.

    This is read-only. Media filename/title/source metadata are never loaded or
    used. The diagnostic only uses Media duration, Trace timestamps/identity, and
    persisted normalized visual embeddings from the exact active generation.
    """

    previous_cwd = Path.cwd()
    live_root = live_root.resolve()
    if str(live_root) not in sys.path:
        sys.path.insert(0, str(live_root))
    try:
        os.chdir(live_root)
        from sqlalchemy import select

        from app.core.database import SessionLocal
        from app.library_models import Embedding, Media, Trace

        with SessionLocal() as db:
            rows = list(
                db.execute(
                    select(
                        Trace.media_id,
                        Trace.start_ms,
                        Media.duration_seconds,
                        Embedding.vector_blob,
                    )
                    .join(Embedding, Embedding.trace_id == Trace.trace_id)
                    .join(Media, Media.media_id == Trace.media_id)
                    .where(
                        Trace.trace_type == "visual",
                        Media.media_kind == "video",
                        Media.duration_seconds > 3600,
                        Embedding.model_id == model_id,
                        Embedding.embedding_dimension == dimension,
                        Embedding.normalized.is_(True),
                    )
                    .order_by(Trace.media_id.asc(), Trace.start_ms.asc())
                ).all()
            )
    finally:
        os.chdir(previous_cwd)

    grouped: dict[str, list[tuple[int, float, bytes]]] = defaultdict(list)
    for media_id, start_ms, duration_seconds, vector_blob in rows:
        grouped[str(media_id)].append((int(start_ms), float(duration_seconds or 0.0), bytes(vector_blob)))

    media_summaries: list[dict[str, Any]] = []
    aggregate_cosines: list[float] = []
    for media_id, items in grouped.items():
        if len(items) < 2:
            continue
        timestamps = [item[0] for item in items]
        vectors = [decode_normalized_float32(item[2], dimension) for item in items]
        cosines = [
            normalized_cosine(left, right)
            for left, right in zip(vectors, vectors[1:], strict=False)
        ]
        aggregate_cosines.extend(cosines)
        media_summaries.append(
            summarize_media_pairs(
                media_id=media_id,
                duration_seconds=items[0][1],
                timestamps_ms=timestamps,
                cosines=cosines,
            )
        )

    media_summaries.sort(key=lambda item: (-float(item["duration_seconds"]), str(item["media_id"])))
    return {
        "available": bool(aggregate_cosines),
        "model_id": model_id,
        "dimension": dimension,
        "long_form_media_count": len(media_summaries),
        "adjacent_pair_count": len(aggregate_cosines),
        "aggregate": {
            "mean_cosine": round(sum(aggregate_cosines) / len(aggregate_cosines), 6) if aggregate_cosines else None,
            "p50_cosine": round(float(percentile(aggregate_cosines, 0.50)), 6) if aggregate_cosines else None,
            "p95_cosine": round(float(percentile(aggregate_cosines, 0.95)), 6) if aggregate_cosines else None,
            "fraction_ge_0_98": round(sum(1 for value in aggregate_cosines if value >= 0.98) / len(aggregate_cosines), 6) if aggregate_cosines else None,
            "fraction_ge_0_995": round(sum(1 for value in aggregate_cosines if value >= 0.995) / len(aggregate_cosines), 6) if aggregate_cosines else None,
        },
        "representative_media": media_summaries[:max_media],
        "interpretation": "Descriptive only: higher adjacent cosine indicates more visual redundancy. No pruning threshold is inferred by this soak.",
        "metadata_used_for_scoring": False,
        "source": "live-production-sqlite-existing-embeddings",
    }
