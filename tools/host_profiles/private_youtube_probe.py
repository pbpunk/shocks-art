from __future__ import annotations

import json
import os
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

from app.services.ytdlp import (
    YtDlpError,
    download_youtube_section,
    download_youtube_source,
    fetch_youtube_metadata,
)

OWNER_DISCOVERY_HELPER = CODE_ROOT / "tools" / "host_profiles" / "private_youtube_owner_discovery.py"


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def discover_private_owner_video_id() -> tuple[str, str]:
    """Discover one private owner upload in a fresh live-root interpreter.

    OAuth/database state belongs to the deployed checkout. Candidate retrieval
    code stays in this interpreter, while a fixed helper subprocess prevents
    Python's module cache from mixing candidate app modules with live config.
    """

    env = os.environ.copy()
    env["SHOCKS_HOST_LIVE_ROOT"] = str(LIVE_ROOT)
    try:
        result = subprocess.run(
            [sys.executable, str(OWNER_DISCOVERY_HELPER)],
            cwd=CODE_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "", "owner_discovery_timeout"

    payload: dict[str, Any] = {}
    if result.stdout:
        try:
            parsed = json.loads(result.stdout.strip().splitlines()[-1])
            if isinstance(parsed, dict):
                payload = parsed
        except (json.JSONDecodeError, IndexError):
            payload = {}
    if result.returncode == 0 and payload.get("ok") is True:
        video_id = str(payload.get("video_id") or "").strip()
        if video_id:
            return video_id, ""
        return "", "owner_discovery_empty_video_id"
    status = str(payload.get("status") or "")
    if status:
        return "", status[:200]
    return "", f"owner_discovery_exit_{result.returncode}"


def resolve_probe_url() -> tuple[str, str, str]:
    configured = os.getenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", "").strip()
    if configured:
        return configured, "configured-host-url", ""
    video_id, status = discover_private_owner_video_id()
    if not video_id:
        return "", "unavailable", status or "no_private_owner_upload"
    return f"https://www.youtube.com/watch?v={video_id}", "owner-oauth-private-upload", ""


def safe_ytdlp_failure(stage: str, *, source_mode: str, video_id: str = "", elapsed_seconds: float | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": f"Private YouTube {stage} probe failed",
        "failure_stage": stage,
        "source_mode": source_mode,
        "error_type": "YtDlpError",
        "credentials_emitted": False,
        "signed_urls_emitted": False,
    }
    if video_id:
        payload["video_id"] = video_id
    if elapsed_seconds is not None:
        payload[f"{stage}_seconds"] = round(elapsed_seconds, 3)
    return payload


def main() -> int:
    url, source_mode, discovery_status = resolve_probe_url()
    if not url:
        return emit(
            {
                "summary": "Private YouTube probe has no configured or discoverable private owner upload",
                "configured": False,
                "source_mode": source_mode,
                "discovery_status": discovery_status,
                "credentials_emitted": False,
                "signed_urls_emitted": False,
            },
            2,
        )

    metadata_started = time.monotonic()
    try:
        info = fetch_youtube_metadata(url=url, timeout=120)
    except YtDlpError:
        return emit(
            safe_ytdlp_failure(
                "metadata",
                source_mode=source_mode,
                elapsed_seconds=time.monotonic() - metadata_started,
            ),
            1,
        )
    metadata_seconds = time.monotonic() - metadata_started
    video_id = str(info.get("id") or "")

    scratch_root = Path(os.getenv("SHOCKS_HOST_SCRATCH_ROOT", tempfile.gettempdir()))
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shocks-youtube-probe-", dir=scratch_root) as temp_dir:
        temp = Path(temp_dir)

        partial_started = time.monotonic()
        try:
            partial_path = download_youtube_section(
                url=url,
                output_template=temp / "partial.%(ext)s",
                expected_path=temp / "partial.mp4",
                start_seconds=0,
                end_seconds=30,
                timeout=600,
            )
        except YtDlpError:
            return emit(
                safe_ytdlp_failure(
                    "partial",
                    source_mode=source_mode,
                    video_id=video_id,
                    elapsed_seconds=time.monotonic() - partial_started,
                ),
                1,
            )
        partial_seconds = time.monotonic() - partial_started
        partial_bytes = partial_path.stat().st_size
        if partial_bytes <= 0:
            return emit(
                {
                    **safe_ytdlp_failure("partial", source_mode=source_mode, video_id=video_id, elapsed_seconds=partial_seconds),
                    "failure_reason": "empty_output",
                },
                1,
            )

        full_started = time.monotonic()
        try:
            full_path = download_youtube_source(
                url=url,
                output_template=temp / "full.%(ext)s",
                expected_path=temp / "full.mp4",
                label="private-youtube-probe",
            )
        except YtDlpError:
            return emit(
                safe_ytdlp_failure(
                    "full",
                    source_mode=source_mode,
                    video_id=video_id,
                    elapsed_seconds=time.monotonic() - full_started,
                ),
                1,
            )
        full_seconds = time.monotonic() - full_started
        full_bytes = full_path.stat().st_size
        if full_bytes <= 0:
            return emit(
                {
                    **safe_ytdlp_failure("full", source_mode=source_mode, video_id=video_id, elapsed_seconds=full_seconds),
                    "failure_reason": "empty_output",
                },
                1,
            )

    partial_mbps = (partial_bytes * 8 / 1_000_000) / partial_seconds if partial_seconds else None
    full_mbps = (full_bytes * 8 / 1_000_000) / full_seconds if full_seconds else None
    return emit(
        {
            "summary": "Private YouTube authentication, partial retrieval, and production-path full retrieval succeeded",
            "configured": source_mode == "configured-host-url",
            "source_mode": source_mode,
            "video_id": video_id,
            "duration_seconds": info.get("duration"),
            "metadata_seconds": round(metadata_seconds, 3),
            "partial": {
                "seconds": round(partial_seconds, 3),
                "bytes": partial_bytes,
                "throughput_mbps": round(partial_mbps, 3) if partial_mbps is not None else None,
            },
            "full": {
                "seconds": round(full_seconds, 3),
                "bytes": full_bytes,
                "throughput_mbps": round(full_mbps, 3) if full_mbps is not None else None,
            },
            "production_materialization_path_proven": True,
            "authentication_policy": "shared-production-ytdlp",
            "owner_discovery_isolated": source_mode == "owner-oauth-private-upload",
            "fallback": "If bounded section retrieval is unreliable for a private source, materialize the full source through the production MediaRetriever scratch lease and delete it after use.",
            "credentials_emitted": False,
            "signed_urls_emitted": False,
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
