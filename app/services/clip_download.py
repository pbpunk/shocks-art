import json
import logging
import re
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR
from app.core.database import SessionLocal
from app.models import CandidateWindow, DerivedAsset


SOURCE_VIDEO_DIR = ROOT_DIR / "data" / "source_videos"
DERIVED_CLIP_DIR = ROOT_DIR / "data" / "derived_clips"
ASSET_TYPE = "source_clip_mp4"
TOOL_USED = "yt-dlp+ffmpeg"
MIN_SOURCE_VIDEO_BYTES = 1_000_000
YTDLP_EXTRACTOR_ARGS = "youtube:player_client=mweb"
YTDLP_FORMATS = [
    "best[ext=mp4]/best",
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
]


logger = logging.getLogger(__name__)


class ClipDownloadError(RuntimeError):
    pass


def clip_path(candidate_id: str) -> Path:
    return DERIVED_CLIP_DIR / f"{candidate_id}.mp4"


def status_path(candidate_id: str) -> Path:
    return DERIVED_CLIP_DIR / f"{candidate_id}.status.json"


def source_video_path(source_video_id: str) -> Path:
    return SOURCE_VIDEO_DIR / f"{source_video_id}.mp4"


def get_clip_asset(db: Session, candidate_id: str) -> DerivedAsset | None:
    return db.scalar(
        select(DerivedAsset).where(
            DerivedAsset.candidate_window_id == candidate_id,
            DerivedAsset.asset_type == ASSET_TYPE,
        )
    )


def ensure_clip_asset(db: Session, candidate: CandidateWindow, status: str) -> DerivedAsset:
    asset = get_clip_asset(db, candidate.candidate_window_id)
    path = clip_path(candidate.candidate_window_id)
    if asset:
        asset.external_reference = str(path)
        asset.editor = "server"
        asset.tool_used = TOOL_USED
        asset.creation_status = status
        db.flush()
        return asset
    asset = DerivedAsset(
        candidate_window_id=candidate.candidate_window_id,
        asset_type=ASSET_TYPE,
        external_reference=str(path),
        editor="server",
        tool_used=TOOL_USED,
        creation_status=status,
    )
    db.add(asset)
    db.flush()
    return asset


def read_progress(candidate_id: str) -> dict:
    path = status_path(candidate_id)
    if not path.exists():
        return {"progress": 0, "phase": "missing"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "progress": max(0, min(100, int(data.get("progress", 0)))),
            "phase": str(data.get("phase", "processing")),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"progress": 0, "phase": "processing"}


def write_progress(candidate_id: str, progress: int, phase: str) -> None:
    DERIVED_CLIP_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_progress(candidate_id)
    if phase not in {"queued", "failed", "missing"}:
        progress = max(int(existing.get("progress", 0)), progress)
    status_path(candidate_id).write_text(
        json.dumps({"progress": max(0, min(100, int(progress))), "phase": phase}),
        encoding="utf-8",
    )


def update_clip_asset_progress(
    db: Session,
    candidate: CandidateWindow,
    status: str,
    progress: int,
    phase: str,
) -> DerivedAsset:
    asset = ensure_clip_asset(db, candidate, status)
    write_progress(candidate.candidate_window_id, progress, phase)
    db.commit()
    return asset


def clip_download_status(db: Session, candidate: CandidateWindow) -> dict:
    path = clip_path(candidate.candidate_window_id)
    asset = get_clip_asset(db, candidate.candidate_window_id)
    if path.exists():
        if not asset or asset.creation_status != "complete":
            ensure_clip_asset(db, candidate, "complete")
        return {
            "status": "ready",
            "progress": 100,
            "phase": "ready",
            "download_url": f"/api/clips/{candidate.candidate_window_id}/download",
        }
    if asset and asset.creation_status == "processing":
        progress = read_progress(candidate.candidate_window_id)
        if progress["progress"] < 1:
            progress["progress"] = 1
        return {
            "status": "processing",
            **progress,
        }
    if asset and asset.creation_status == "failed":
        return {
            "status": "failed",
            **read_progress(candidate.candidate_window_id),
        }
    return {"status": "missing", "progress": 0, "phase": "missing"}


def queue_clip_generation(db: Session, candidate: CandidateWindow) -> dict:
    status = clip_download_status(db, candidate)
    if status["status"] == "ready":
        return status
    if status["status"] == "processing":
        return {**status, "queued": False}
    update_clip_asset_progress(db, candidate, "processing", 1, "queued")
    return {"status": "processing", "progress": 1, "phase": "queued", "queued": True}


def generate_clip_background(candidate_id: str) -> None:
    with SessionLocal() as db:
        candidate = db.get(CandidateWindow, candidate_id)
        if not candidate or candidate.review_status == "archived":
            return
        try:
            generate_clip_file(db, candidate)
            update_clip_asset_progress(db, candidate, "complete", 100, "ready")
        except Exception:
            logger.exception("Clip generation failed for candidate %s", candidate_id)
            update_clip_asset_progress(db, candidate, "failed", 0, "failed")
        db.commit()


def generate_clip_file(db: Session, candidate: CandidateWindow) -> Path:
    output_path = clip_path(candidate.candidate_window_id)
    if output_path.exists():
        update_clip_asset_progress(db, candidate, "complete", 100, "ready")
        return output_path

    update_clip_asset_progress(db, candidate, "processing", 2, "preparing")
    source_path = ensure_source_video(db, candidate)
    DERIVED_CLIP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".tmp.mp4")
    if tmp_path.exists():
        tmp_path.unlink()

    duration = max(1, int(candidate.end_seconds) - int(candidate.start_seconds))
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        str(max(0, int(candidate.start_seconds))),
        "-i",
        str(source_path),
        "-t",
        str(duration),
        "-progress",
        "pipe:1",
        "-nostats",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(tmp_path),
    ]
    run_ffmpeg_command(db, candidate, command, duration)
    if not tmp_path.exists():
        raise ClipDownloadError("ffmpeg did not produce a clip file")
    tmp_path.replace(output_path)
    return output_path


def ensure_source_video(db: Session, candidate: CandidateWindow) -> Path:
    source_video_id = candidate.stream.source_video_id
    path = source_video_path(source_video_id)
    if path.exists():
        if path.stat().st_size >= MIN_SOURCE_VIDEO_BYTES:
            update_clip_asset_progress(db, candidate, "processing", 70, "source cached")
            return path
        path.unlink()
    SOURCE_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    output_template = SOURCE_VIDEO_DIR / "%(id)s.%(ext)s"
    last_error: ClipDownloadError | None = None
    for format_selector in YTDLP_FORMATS:
        command = [
            "yt-dlp",
            "--extractor-args",
            YTDLP_EXTRACTOR_ARGS,
            "-f",
            format_selector,
            "--merge-output-format",
            "mp4",
            "--newline",
            "-o",
            str(output_template),
            candidate.stream.url,
        ]
        try:
            run_ytdlp_command(db, candidate, command)
            break
        except ClipDownloadError as exc:
            last_error = exc
            logger.warning(
                "yt-dlp failed for candidate %s with format selector %s",
                candidate.candidate_window_id,
                format_selector,
            )
    else:
        if last_error:
            raise last_error
    if path.exists():
        return path
    matches = list(SOURCE_VIDEO_DIR.glob(f"{source_video_id}.*"))
    if matches:
        matches[0].replace(path)
        return path
    raise ClipDownloadError("yt-dlp did not produce a source video file")


def run_command(command: list[str], failure_message: str) -> None:
    result = subprocess.run(command, cwd=ROOT_DIR, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise ClipDownloadError(f"{failure_message}: {details[:1000]}")


def run_ytdlp_command(db: Session, candidate: CandidateWindow, command: list[str]) -> None:
    process = subprocess.Popen(
        command,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output: list[str] = []
    if process.stdout:
        for line in process.stdout:
            output.append(line)
            percent = parse_ytdlp_percent(line)
            if percent is not None:
                update_clip_asset_progress(db, candidate, "processing", 2 + int(percent * 0.68), "downloading")
    return_code = process.wait()
    if return_code != 0:
        raise ClipDownloadError(f"yt-dlp source download failed: {''.join(output)[-1000:].strip()}")
    update_clip_asset_progress(db, candidate, "processing", 70, "downloaded")


def run_ffmpeg_command(db: Session, candidate: CandidateWindow, command: list[str], duration: int) -> None:
    process = subprocess.Popen(
        command,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output: list[str] = []
    if process.stdout:
        for line in process.stdout:
            output.append(line)
            if line.startswith("out_time_ms="):
                raw_value = line.split("=", 1)[1].strip()
                if raw_value.isdigit():
                    seconds = int(raw_value) / 1_000_000
                    percent = min(99, 70 + int((seconds / max(1, duration)) * 29))
                    update_clip_asset_progress(db, candidate, "processing", percent, "cutting")
    return_code = process.wait()
    if return_code != 0:
        raise ClipDownloadError(f"ffmpeg clip cut failed: {''.join(output)[-1000:].strip()}")
    update_clip_asset_progress(db, candidate, "processing", 99, "finalizing")


def parse_ytdlp_percent(line: str) -> float | None:
    match = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
    if not match:
        return None
    return min(100.0, max(0.0, float(match.group(1))))


def download_filename(candidate: CandidateWindow) -> str:
    title = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate.title).strip("-")[:80] or "clip"
    return f"{candidate.stream.source_video_id}_{candidate.start_seconds}_{candidate.end_seconds}_{title}.mp4"
