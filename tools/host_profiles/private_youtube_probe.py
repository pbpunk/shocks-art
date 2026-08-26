from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)


def main() -> int:
    url = os.getenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", "").strip()
    if not url:
        return emit({"summary": "Private YouTube probe URL is not configured on the host", "configured": False}, 2)
    executable = shutil.which("yt-dlp")
    if not executable:
        return emit({"summary": "yt-dlp is unavailable on the host"}, 2)

    browser = os.getenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", "chrome").strip() or "chrome"
    common = [executable, "--no-playlist", "--cookies-from-browser", browser]
    metadata_started = time.monotonic()
    metadata = run([*common, "--skip-download", "--dump-single-json", url], timeout=120)
    metadata_seconds = time.monotonic() - metadata_started
    if metadata.returncode != 0:
        return emit({
            "summary": "Private YouTube authentication/metadata probe failed",
            "metadata_seconds": round(metadata_seconds, 3),
            "error_tail": (metadata.stderr or metadata.stdout)[-3000:],
        }, 1)

    info = json.loads(metadata.stdout)
    scratch_root = Path(os.getenv("SHOCKS_HOST_SCRATCH_ROOT", tempfile.gettempdir()))
    with tempfile.TemporaryDirectory(prefix="shocks-youtube-probe-", dir=scratch_root) as temp_dir:
        temp = Path(temp_dir)
        output = temp / "probe.%(ext)s"
        sample_started = time.monotonic()
        sample = run(
            [
                *common,
                "--download-sections",
                "*0-30",
                "--force-keyframes-at-cuts",
                "-f",
                "best[ext=mp4]/best",
                "-o",
                str(output),
                url,
            ],
            timeout=600,
        )
        sample_seconds = time.monotonic() - sample_started
        files = [p for p in temp.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        sample_bytes = sum(p.stat().st_size for p in files)
        if sample.returncode != 0 or sample_bytes <= 0:
            return emit({
                "summary": "Private YouTube metadata succeeded but partial retrieval failed",
                "video_id": str(info.get("id") or ""),
                "metadata_seconds": round(metadata_seconds, 3),
                "sample_seconds": round(sample_seconds, 3),
                "error_tail": (sample.stderr or sample.stdout)[-3000:],
            }, 1)

    throughput_mbps = (sample_bytes * 8 / 1_000_000) / sample_seconds if sample_seconds else None
    return emit({
        "summary": "Private YouTube authentication and bounded partial retrieval succeeded",
        "configured": True,
        "video_id": str(info.get("id") or ""),
        "duration_seconds": info.get("duration"),
        "metadata_seconds": round(metadata_seconds, 3),
        "sample_seconds": round(sample_seconds, 3),
        "sample_bytes": sample_bytes,
        "sample_throughput_mbps": round(throughput_mbps, 3) if throughput_mbps is not None else None,
        "cookies_source": browser,
    })


if __name__ == "__main__":
    raise SystemExit(main())
