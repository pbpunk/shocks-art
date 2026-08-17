import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT_DIR / "data" / "browser_profile"
STATUS_PATH = ROOT_DIR / "data" / "native_youtube_jobs" / "profile_setup.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Open the persistent YouTube automation profile for one-time sign-in.")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--url", default="https://www.youtube.com")
    parser.add_argument("--minutes", type=int, default=20)
    args = parser.parse_args()

    chrome_path = find_chrome()
    if not chrome_path:
        write_status("failed", "Could not find Google Chrome on this machine.")
        return 1

    args.profile.mkdir(parents=True, exist_ok=True)
    write_status(
        "running",
        f"Opened normal Chrome for YouTube profile setup. Sign in, then close that Chrome window.",
    )
    process = subprocess.Popen(
        [
            str(chrome_path),
            f"--user-data-dir={args.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            args.url,
        ]
    )
    try:
        process.wait(timeout=args.minutes * 60)
        write_status("complete", "YouTube profile setup Chrome window closed.")
    except subprocess.TimeoutExpired:
        write_status("running", "YouTube profile setup Chrome window is still open.")
    return 0


def find_chrome() -> Path | None:
    candidates = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def write_status(status: str, message: str) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(
            {
                "status": status,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
