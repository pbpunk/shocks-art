from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[2]
LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", CODE_ROOT)).resolve()
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def choose_private_video_id(search_items: list[dict[str, Any]], detail_items: list[dict[str, Any]]) -> str:
    ordered_ids = [str(item.get("id", {}).get("videoId") or "") for item in search_items]
    details = {str(item.get("id") or ""): item for item in detail_items}
    for video_id in ordered_ids:
        if not video_id:
            continue
        status = details.get(video_id, {}).get("status", {})
        if str(status.get("privacyStatus") or "").lower() != "private":
            continue
        upload_status = str(status.get("uploadStatus") or "").lower()
        if upload_status and upload_status != "processed":
            continue
        return video_id
    return ""


def discover_private_owner_url() -> str:
    from googleapiclient.discovery import build

    previous_cwd = Path.cwd()
    try:
        os.chdir(LIVE_ROOT)
        from app.core.database import SessionLocal
        from app.services.youtube_analytics import connected_credential, credentials_from_record

        with SessionLocal() as db:
            record = connected_credential(db)
            if record is None:
                return ""
            credentials = credentials_from_record(record)
    finally:
        os.chdir(previous_cwd)

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    search_response = (
        youtube.search()
        .list(part="id", forMine=True, type="video", order="date", maxResults=25)
        .execute()
    )
    search_items = list(search_response.get("items", []))
    video_ids = [str(item.get("id", {}).get("videoId") or "") for item in search_items]
    video_ids = [video_id for video_id in video_ids if video_id]
    if not video_ids:
        return ""
    details_response = (
        youtube.videos()
        .list(part="status", id=",".join(video_ids[:25]), maxResults=25)
        .execute()
    )
    video_id = choose_private_video_id(search_items, list(details_response.get("items", [])))
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def resolve_probe_url() -> tuple[str, str]:
    configured = os.getenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", "").strip()
    if configured:
        return configured, "configured-host-url"
    try:
        discovered = discover_private_owner_url()
    except Exception:
        discovered = ""
    return (discovered, "owner-oauth-private-upload") if discovered else ("", "unavailable")


def main() -> int:
    url, source_mode = resolve_probe_url()
    if not url:
        return emit(
            {
                "summary": "Private YouTube probe has no configured or discoverable private owner upload",
                "configured": False,
                "source_mode": source_mode,
            },
            2,
        )
    executable = shutil.which("yt-dlp")
    if not executable:
        return emit({"summary": "yt-dlp is unavailable on the host", "source_mode": source_mode}, 2)
    browser = os.getenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", "chrome").strip() or "chrome"
    common = [executable, "--no-playlist", "--cookies-from-browser", browser]

    metadata_started = time.monotonic()
    metadata = run([*common, "--skip-download", "--dump-single-json", url], timeout=120)
    metadata_seconds = time.monotonic() - metadata_started
    if metadata.returncode != 0:
        return emit({"summary": "Private YouTube authentication/metadata probe failed", "metadata_seconds": round(metadata_seconds, 3), "source_mode": source_mode, "error_tail": (metadata.stderr or metadata.stdout)[-2000:]}, 1)
    info = json.loads(metadata.stdout)

    scratch_root = Path(os.getenv("SHOCKS_HOST_SCRATCH_ROOT", tempfile.gettempdir()))
    with tempfile.TemporaryDirectory(prefix="shocks-youtube-probe-", dir=scratch_root) as temp_dir:
        temp = Path(temp_dir)
        partial_output = temp / "partial.%(ext)s"
        partial_started = time.monotonic()
        partial = run([*common, "--download-sections", "*0-30", "--force-keyframes-at-cuts", "-f", "best[ext=mp4]/best", "-o", str(partial_output), url], timeout=600)
        partial_seconds = time.monotonic() - partial_started
        partial_files = [p for p in temp.iterdir() if p.is_file() and p.name.startswith("partial") and not p.name.endswith((".part", ".ytdl"))]
        partial_bytes = sum(p.stat().st_size for p in partial_files)
        if partial.returncode != 0 or partial_bytes <= 0:
            return emit({"summary": "Private YouTube metadata succeeded but partial retrieval failed", "video_id": str(info.get("id") or ""), "metadata_seconds": round(metadata_seconds, 3), "partial_seconds": round(partial_seconds, 3), "source_mode": source_mode, "error_tail": (partial.stderr or partial.stdout)[-2000:]}, 1)

        full_output = temp / "full.%(ext)s"
        full_started = time.monotonic()
        full = run([*common, "-f", "best[ext=mp4]/best", "-o", str(full_output), url], timeout=3600)
        full_seconds = time.monotonic() - full_started
        full_files = [p for p in temp.iterdir() if p.is_file() and p.name.startswith("full") and not p.name.endswith((".part", ".ytdl"))]
        full_bytes = sum(p.stat().st_size for p in full_files)
        if full.returncode != 0 or full_bytes <= 0:
            return emit({"summary": "Private YouTube partial retrieval succeeded but full retrieval failed", "video_id": str(info.get("id") or ""), "metadata_seconds": round(metadata_seconds, 3), "partial_seconds": round(partial_seconds, 3), "partial_bytes": partial_bytes, "full_seconds": round(full_seconds, 3), "source_mode": source_mode, "error_tail": (full.stderr or full.stdout)[-2000:]}, 1)

    partial_mbps = (partial_bytes * 8 / 1_000_000) / partial_seconds if partial_seconds else None
    full_mbps = (full_bytes * 8 / 1_000_000) / full_seconds if full_seconds else None
    return emit({
        "summary": "Private YouTube authentication, partial retrieval, and full retrieval succeeded",
        "configured": True, "source_mode": source_mode, "video_id": str(info.get("id") or ""), "duration_seconds": info.get("duration"),
        "metadata_seconds": round(metadata_seconds, 3),
        "partial": {"seconds": round(partial_seconds, 3), "bytes": partial_bytes, "throughput_mbps": round(partial_mbps, 3) if partial_mbps is not None else None},
        "full": {"seconds": round(full_seconds, 3), "bytes": full_bytes, "throughput_mbps": round(full_mbps, 3) if full_mbps is not None else None},
        "fallback": "If bounded section/range retrieval is unreliable for a private source, materialize the full source into the existing temporary Library scratch lease and delete it after use.",
        "credentials_emitted": False, "signed_urls_emitted": False,
    })


if __name__ == "__main__":
    raise SystemExit(main())
