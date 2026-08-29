from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections import deque
from pathlib import Path
from typing import Any, Callable, Sequence

from app.core.config import ROOT_DIR


SOURCE_EXTRACTOR_ARGS = "youtube:player_client=mweb"
SOURCE_FORMATS = (
    "best[ext=mp4]/best",
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
)
DEFAULT_COOKIE_BROWSER = "chrome"
ProgressCallback = Callable[[float], None]

logger = logging.getLogger(__name__)


class YtDlpError(RuntimeError):
    pass


def _executable() -> str:
    executable = shutil.which("yt-dlp")
    if not executable:
        raise YtDlpError("yt-dlp executable is not available on PATH")
    return executable


def browser_cookie_args() -> tuple[str, ...]:
    """Return workstation-owned browser authentication args.

    An explicitly blank SHOCKS_YTDLP_COOKIES_FROM_BROWSER disables browser-cookie
    auth. Otherwise the historical workstation default is Chrome. The value is
    trusted host configuration and is never supplied through the Google Sheet.
    """

    browser = os.getenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", DEFAULT_COOKIE_BROWSER).strip()
    return ("--cookies-from-browser", browser) if browser else ()


def authentication_variants() -> tuple[tuple[str, ...], ...]:
    """Try workstation authentication first, then public/anonymous fallback.

    Public YouTube retrieval therefore keeps working when a local browser cookie
    store is unavailable, while private media still requires the authenticated
    variant to succeed.
    """

    authenticated = browser_cookie_args()
    if not authenticated:
        return ((),)
    return (authenticated, ())


def parse_download_percent(line: str) -> float | None:
    match = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
    if not match:
        return None
    return min(100.0, max(0.0, float(match.group(1))))


def _base_command(executable: str, auth_args: Sequence[str]) -> list[str]:
    command = [executable, "--no-playlist", *auth_args]
    # The historical mweb extractor policy is retained for anonymous/public
    # retrieval. Cookie-authenticated private retrieval must use an auth-capable
    # player client, so let yt-dlp select its normal authenticated client.
    if not auth_args:
        command.extend(["--extractor-args", SOURCE_EXTRACTOR_ARGS])
    return command


def _source_command(
    executable: str,
    *,
    url: str,
    output_template: Path,
    format_selector: str,
    auth_args: Sequence[str] = (),
) -> list[str]:
    return [
        *_base_command(executable, auth_args),
        "-f",
        format_selector,
        "--merge-output-format",
        "mp4",
        "--newline",
        "-o",
        str(output_template),
        url,
    ]


def _run_streaming(command: list[str], progress_callback: ProgressCallback | None = None) -> None:
    process = subprocess.Popen(
        command,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    recent_output: deque[str] = deque(maxlen=200)
    if process.stdout:
        for line in process.stdout:
            recent_output.append(line)
            percent = parse_download_percent(line)
            if percent is not None and progress_callback is not None:
                progress_callback(percent)
    return_code = process.wait()
    if return_code != 0:
        detail = "".join(recent_output)[-1200:].strip()
        raise YtDlpError(detail or f"yt-dlp exited with code {return_code}")


def _run_capture(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT_DIR,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _capture_error(result: subprocess.CompletedProcess[str]) -> YtDlpError:
    detail = (result.stderr or result.stdout or "").strip()[-1200:]
    return YtDlpError(detail or f"yt-dlp exited with code {result.returncode}")


def _resolve_completed_source(expected_path: Path) -> Path:
    if expected_path.is_file():
        return expected_path

    candidates = []
    for path in expected_path.parent.glob(f"{expected_path.stem}.*"):
        if not path.is_file():
            continue
        if path.name.endswith((".part", ".ytdl")):
            continue
        if re.search(r"\.f\d+\.", path.name):
            continue
        candidates.append(path)

    if not candidates:
        raise YtDlpError("yt-dlp completed but did not produce a source media file")

    source = max(candidates, key=lambda item: item.stat().st_size)
    source.replace(expected_path)
    return expected_path


def fetch_youtube_metadata(*, url: str, timeout: int = 120) -> dict[str, Any]:
    """Read one YouTube video's metadata through the shared auth policy."""

    if not url:
        raise YtDlpError("YouTube source URL is required")
    executable = _executable()
    last_error: YtDlpError | None = None
    for auth_args in authentication_variants():
        result = _run_capture(
            [*_base_command(executable, auth_args), "--skip-download", "--dump-single-json", url],
            timeout=timeout,
        )
        if result.returncode != 0:
            last_error = _capture_error(result)
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            last_error = YtDlpError("yt-dlp metadata output was not valid JSON")
            continue
        if isinstance(payload, dict):
            return payload
        last_error = YtDlpError("yt-dlp metadata output was not an object")
    if last_error is not None:
        raise last_error
    raise YtDlpError("yt-dlp metadata fetch failed without an error")


def download_youtube_section(
    *,
    url: str,
    output_template: Path,
    expected_path: Path,
    start_seconds: int = 0,
    end_seconds: int = 30,
    timeout: int = 600,
) -> Path:
    """Download one bounded source section through the shared auth policy."""

    if not url:
        raise YtDlpError("YouTube source URL is required")
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError("section timestamps must satisfy 0 <= start < end")

    executable = _executable()
    output_template.parent.mkdir(parents=True, exist_ok=True)
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    section = f"*{start_seconds}-{end_seconds}"
    last_error: YtDlpError | None = None
    for auth_args in authentication_variants():
        result = _run_capture(
            [
                *_base_command(executable, auth_args),
                "--download-sections",
                section,
                "--force-keyframes-at-cuts",
                "-f",
                SOURCE_FORMATS[0],
                "--merge-output-format",
                "mp4",
                "-o",
                str(output_template),
                url,
            ],
            timeout=timeout,
        )
        if result.returncode != 0:
            last_error = _capture_error(result)
            continue
        return _resolve_completed_source(expected_path)
    if last_error is not None:
        raise last_error
    raise YtDlpError("yt-dlp section fetch failed without an error")


def download_youtube_source(
    *,
    url: str,
    output_template: Path,
    expected_path: Path,
    progress_callback: ProgressCallback | None = None,
    label: str = "",
) -> Path:
    """Download one YouTube source using the shared production auth/policy path."""

    if not url:
        raise YtDlpError("YouTube source URL is required")

    executable = _executable()
    output_template.parent.mkdir(parents=True, exist_ok=True)
    expected_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: YtDlpError | None = None
    for auth_args in authentication_variants():
        for format_selector in SOURCE_FORMATS:
            command = _source_command(
                executable,
                url=url,
                output_template=output_template,
                format_selector=format_selector,
                auth_args=auth_args,
            )
            try:
                _run_streaming(command, progress_callback=progress_callback)
                return _resolve_completed_source(expected_path)
            except YtDlpError as exc:
                last_error = exc
                logger.warning(
                    "yt-dlp source fetch failed%s with format selector %s and auth=%s: %s",
                    f" for {label}" if label else "",
                    format_selector,
                    "browser" if auth_args else "anonymous",
                    exc,
                )

    if last_error is not None:
        raise last_error
    raise YtDlpError("yt-dlp source fetch failed without an error")


def fetch_youtube_auto_captions(*, url: str, output_template: Path) -> bool:
    """Fetch YouTube auto-caption JSON3 without downloading source media bytes."""

    if not url:
        return False
    executable = _executable()
    output_template.parent.mkdir(parents=True, exist_ok=True)
    for auth_args in authentication_variants():
        command = [
            *_base_command(executable, auth_args),
            "--skip-download",
            "--write-auto-subs",
            "--sub-langs",
            "en-orig",
            "--sub-format",
            "json3",
            "-o",
            str(output_template),
            url,
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT_DIR,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            return True
    return False
