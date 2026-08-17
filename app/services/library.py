import hashlib
import json
import mimetypes
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library_models import Media


SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
SUPPORTED_SUFFIXES = SUPPORTED_VIDEO_SUFFIXES | SUPPORTED_IMAGE_SUFFIXES


@dataclass(frozen=True)
class IngestResult:
    discovered: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0


def media_kind_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_VIDEO_SUFFIXES:
        return "video"
    if suffix in SUPPORTED_IMAGE_SUFFIXES:
        return "image"
    return "unknown"


def file_checksum(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def probe_media(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        payload = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}

    video_stream = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), {})
    duration_value = payload.get("format", {}).get("duration") or video_stream.get("duration")
    try:
        duration_seconds = float(duration_value) if duration_value is not None else None
    except (TypeError, ValueError):
        duration_seconds = None

    return {
        "duration_seconds": duration_seconds,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
    }


def scan_media_files(root: Path) -> list[Path]:
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        return []
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def ingest_local_media(db: Session, root: Path) -> IngestResult:
    files = scan_media_files(root)
    created = 0
    updated = 0
    skipped = 0
    errors = 0

    for path in files:
        try:
            resolved = path.resolve()
            stat = resolved.stat()
            source_id = str(resolved)
            existing = db.scalar(
                select(Media).where(Media.source_type == "local", Media.source_id == source_id)
            )

            if existing and existing.size_bytes == stat.st_size and existing.source_modified_ns == stat.st_mtime_ns:
                skipped += 1
                continue

            checksum = file_checksum(resolved)
            duplicate = db.scalar(select(Media).where(Media.checksum_sha256 == checksum))
            probe = probe_media(resolved)
            mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"

            if duplicate and duplicate.media_id != getattr(existing, "media_id", None):
                skipped += 1
                continue

            media = existing or Media(source_type="local", source_id=source_id)
            media.title = resolved.stem
            media.filename = resolved.name
            media.source_path = str(resolved)
            media.mime_type = mime_type
            media.media_kind = media_kind_for_path(resolved)
            media.size_bytes = stat.st_size
            media.source_modified_ns = stat.st_mtime_ns
            media.checksum_sha256 = checksum
            media.duration_seconds = probe.get("duration_seconds")
            media.width = probe.get("width", 0)
            media.height = probe.get("height", 0)
            media.processing_status = "discovered"
            media.metadata_json = {
                "relative_path": str(resolved.relative_to(root.resolve())) if resolved.is_relative_to(root.resolve()) else resolved.name,
                "ffprobe_available": bool(shutil.which("ffprobe")),
            }
            if existing:
                updated += 1
            else:
                db.add(media)
                created += 1
        except (OSError, ValueError):
            errors += 1

    db.commit()
    return IngestResult(
        discovered=len(files),
        created=created,
        updated=updated,
        skipped=skipped,
        errors=errors,
    )
