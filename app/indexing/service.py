from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library_models import IndexRun, Media, Trace
from app.models import now_utc


class VisualExtractionError(RuntimeError):
    pass


class FrameExtractionBackend(Protocol):
    name: str

    @property
    def version(self) -> str: ...

    def extract_frame(
        self,
        source_path: Path,
        timestamp_ms: int,
        output_path: Path,
        *,
        still_image: bool,
    ) -> None: ...


@lru_cache(maxsize=1)
def _ffmpeg_version() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        return "unavailable"
    try:
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    first_line = (completed.stdout or "").splitlines()
    return first_line[0].strip() if first_line else "unknown"


class FfmpegFrameBackend:
    name = "ffmpeg"

    @property
    def version(self) -> str:
        return _ffmpeg_version()

    def extract_frame(
        self,
        source_path: Path,
        timestamp_ms: int,
        output_path: Path,
        *,
        still_image: bool,
    ) -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise VisualExtractionError("ffmpeg executable is not available on PATH")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [executable, "-hide_banner", "-loglevel", "error", "-y"]
        if not still_image:
            command.extend(["-ss", f"{timestamp_ms / 1000:.3f}"])
        command.extend(
            [
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(output_path),
            ]
        )
        try:
            subprocess.run(command, capture_output=True, text=True, check=True, timeout=90)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise VisualExtractionError(detail[:1000]) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise VisualExtractionError(str(exc)) from exc

        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise VisualExtractionError(f"ffmpeg did not create a usable artifact: {output_path}")


@dataclass(frozen=True)
class VisualExtractionConfig:
    sample_interval_seconds: float = 5.0
    artifact_format: str = "jpg"

    def as_payload(self) -> dict:
        return asdict(self)

    @property
    def configuration_hash(self) -> str:
        encoded = json.dumps(self.as_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class VisualIndexResult:
    media_id: str
    index_run_id: str
    expected: int
    created: int
    reused: int
    repaired: int
    status: str

    def as_dict(self) -> dict:
        return asdict(self)


def visual_sample_timestamps_ms(media: Media, config: VisualExtractionConfig) -> list[int]:
    if media.media_kind == "image":
        return [0]
    if media.media_kind != "video":
        return []

    interval_ms = max(1, int(round(config.sample_interval_seconds * 1000)))
    duration_ms = max(0, int(round((media.duration_seconds or 0) * 1000)))
    if duration_ms <= 0:
        return [0]
    return list(range(0, duration_ms, interval_ms))


def _backend_artifact_generation(backend: FrameExtractionBackend, config: VisualExtractionConfig) -> str:
    raw = f"{backend.name}\n{backend.version}\n{config.configuration_hash}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _artifact_relative_path(
    media: Media,
    timestamp_ms: int,
    backend: FrameExtractionBackend,
    config: VisualExtractionConfig,
) -> Path:
    generation = _backend_artifact_generation(backend, config)
    return Path("visual") / media.media_id / generation / f"{timestamp_ms:012d}.{config.artifact_format}"


def _find_existing_trace(
    db: Session,
    *,
    media: Media,
    timestamp_ms: int,
    backend: FrameExtractionBackend,
    config: VisualExtractionConfig,
) -> Trace | None:
    return db.scalar(
        select(Trace).where(
            Trace.media_id == media.media_id,
            Trace.trace_type == "visual",
            Trace.start_ms == timestamp_ms,
            Trace.end_ms == timestamp_ms,
            Trace.extractor == backend.name,
            Trace.extractor_version == backend.version,
            Trace.configuration_hash == config.configuration_hash,
        )
    )


def index_visual_media(
    db: Session,
    media: Media,
    *,
    index_root: Path,
    backend: FrameExtractionBackend | None = None,
    config: VisualExtractionConfig | None = None,
) -> VisualIndexResult:
    """Extract restart-safe visual Traces for one Media item.

    Completed Trace rows whose artifacts still exist are reused. If a previous
    attempt stopped midway, a rerun continues only the missing timestamps. If a
    Trace exists but its generated artifact is missing, that artifact is repaired
    in place rather than creating a duplicate Trace.
    """

    backend = backend or FfmpegFrameBackend()
    config = config or VisualExtractionConfig()
    source_path = Path(media.source_path)
    if media.source_type != "local":
        raise VisualExtractionError(
            f"visual extraction currently supports local Media only; got source_type={media.source_type!r}"
        )
    if not source_path.is_file():
        raise VisualExtractionError(f"source Media is not available locally: {source_path}")

    timestamps = visual_sample_timestamps_ms(media, config)
    run = IndexRun(
        media_id=media.media_id,
        stage="visual_extract",
        configuration_hash=config.configuration_hash,
        status="running",
        started_at=now_utc(),
        statistics_json={"expected": len(timestamps), "created": 0, "reused": 0, "repaired": 0},
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    created = 0
    reused = 0
    repaired = 0

    try:
        for timestamp_ms in timestamps:
            relative_artifact = _artifact_relative_path(media, timestamp_ms, backend, config)
            absolute_artifact = index_root / relative_artifact
            existing = _find_existing_trace(
                db,
                media=media,
                timestamp_ms=timestamp_ms,
                backend=backend,
                config=config,
            )

            if existing is not None and absolute_artifact.is_file() and absolute_artifact.stat().st_size > 0:
                reused += 1
                continue

            backend.extract_frame(
                source_path,
                timestamp_ms,
                absolute_artifact,
                still_image=media.media_kind == "image",
            )

            provenance = {
                "sourceType": media.source_type,
                "sourceSha256": media.checksum_sha256,
                "timestampMs": timestamp_ms,
            }
            metadata = {
                "artifactFormat": config.artifact_format,
                "sampleIntervalSeconds": config.sample_interval_seconds,
            }

            if existing is not None:
                existing.artifact_path = relative_artifact.as_posix()
                existing.provenance_json = provenance
                existing.metadata_json = metadata
                repaired += 1
            else:
                db.add(
                    Trace(
                        media_id=media.media_id,
                        trace_type="visual",
                        start_ms=timestamp_ms,
                        end_ms=timestamp_ms,
                        artifact_path=relative_artifact.as_posix(),
                        extractor=backend.name,
                        extractor_version=backend.version,
                        configuration_hash=config.configuration_hash,
                        provenance_json=provenance,
                        metadata_json=metadata,
                    )
                )
                created += 1
            # Persist progress incrementally so a failed run can resume safely.
            db.commit()

        run.status = "complete"
        run.completed_at = now_utc()
        run.error_message = ""
        run.statistics_json = {
            "expected": len(timestamps),
            "created": created,
            "reused": reused,
            "repaired": repaired,
        }
        db.commit()
    except Exception as exc:
        run.status = "failed"
        run.completed_at = now_utc()
        run.error_message = f"{type(exc).__name__}: {exc}"[:4000]
        run.statistics_json = {
            "expected": len(timestamps),
            "created": created,
            "reused": reused,
            "repaired": repaired,
        }
        db.commit()
        raise

    return VisualIndexResult(
        media_id=media.media_id,
        index_run_id=run.index_run_id,
        expected=len(timestamps),
        created=created,
        reused=reused,
        repaired=repaired,
        status=run.status,
    )


def index_all_visual_media(
    db: Session,
    *,
    index_root: Path,
    backend: FrameExtractionBackend | None = None,
    config: VisualExtractionConfig | None = None,
    limit: int | None = None,
) -> list[VisualIndexResult]:
    query = select(Media).where(Media.media_kind.in_(["image", "video"])).order_by(Media.created_at.asc())
    if limit is not None:
        query = query.limit(limit)
    media_items = list(db.scalars(query).all())
    return [
        index_visual_media(db, media, index_root=index_root, backend=backend, config=config)
        for media in media_items
    ]
