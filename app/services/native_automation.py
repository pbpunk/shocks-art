import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import ROOT_DIR
from app.core.database import SessionLocal
from app.models import Stream
from app.services.native_youtube import (
    build_native_youtube_fallback_prompt,
    build_native_youtube_prompt,
    save_native_youtube_response,
)


JOB_DIR = ROOT_DIR / "data" / "native_youtube_jobs"
PROFILE_SETUP_STATUS_PATH = JOB_DIR / "profile_setup.json"


def native_job_path(stream_id: str) -> Path:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    return JOB_DIR / f"{stream_id}.json"


def read_native_job_status(stream_id: str) -> dict:
    path = native_job_path(stream_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "message": "Could not read native automation job file."}


def read_profile_setup_status() -> dict:
    if not PROFILE_SETUP_STATUS_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_SETUP_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "message": "Could not read profile setup status."}


def write_native_job_status(job_stream_id: str, **values) -> dict:
    current = read_native_job_status(job_stream_id)
    current.update(values)
    current["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = native_job_path(job_stream_id)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def open_native_profile_setup() -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_SETUP_STATUS_PATH.write_text(
        json.dumps(
            {
                "status": "queued",
                "message": "Opening YouTube profile setup browser.",
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    subprocess.Popen(
        [
            sys.executable,
            str(ROOT_DIR / "tools" / "native_youtube_profile_setup.py"),
            "--url",
            "https://www.youtube.com",
        ],
        cwd=str(ROOT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def run_native_ask_background(stream_id: str, source: str = "native-youtube-gemini-sidebar-automated") -> None:
    write_native_job_status(
        stream_id,
        stream_id=stream_id,
        status="starting",
        message="Preparing native YouTube Ask automation.",
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    with SessionLocal() as db:
        stream = db.get(Stream, stream_id)
        if not stream:
            write_native_job_status(stream_id, status="failed", message=f"Stream not found: {stream_id}")
            return

        output_path = run_prompt_attempts(stream_id, stream.url, build_native_youtube_prompt(stream), build_native_youtube_fallback_prompt(stream))
        if not output_path:
            return

        try:
            result = save_native_youtube_response(
                db,
                stream,
                output_path.read_text(encoding="utf-8"),
                source=source,
            )
            db.commit()
            write_native_job_status(
                stream_id,
                status="complete",
                message=f"Imported {len(result.candidates)} candidate(s); skipped {result.skipped_duplicates} duplicate(s).",
                analysis_run_id=result.run.analysis_run_id,
                candidate_window_ids=[candidate.candidate_window_id for candidate in result.candidates],
            )
        except Exception as exc:
            db.commit()
            write_native_job_status(stream_id, status="failed", message=f"Import failed: {exc}")


def run_prompt_attempts(stream_id: str, url: str, primary_prompt: str, fallback_prompt: str) -> Path | None:
    attempts = [("primary", primary_prompt), ("fallback", fallback_prompt)]
    for attempt_name, prompt in attempts:
        prompt_path = JOB_DIR / f"{stream_id}.{attempt_name}.prompt.txt"
        output_path = JOB_DIR / f"{stream_id}.{attempt_name}.response.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        command = [
            sys.executable,
            str(ROOT_DIR / "tools" / "native_youtube_ask_runner.py"),
            "--url",
            url,
            "--prompt-file",
            str(prompt_path),
            "--out",
            str(output_path),
            "--timeout",
            "240",
        ]
        write_native_job_status(
            stream_id,
            status="running",
            message=f"Browser is asking YouTube with the {attempt_name} prompt.",
            attempt=attempt_name,
            command=" ".join(command),
            video_url=url,
            prompt_path=str(prompt_path),
            output_path=str(output_path),
        )
        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                timeout=360,
            )
        except subprocess.TimeoutExpired as exc:
            write_native_job_status(
                stream_id,
                status="failed",
                message=f"Native YouTube Ask {attempt_name} prompt timed out.",
                stdout=(exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
            )
            return None

        response_text = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        write_native_job_status(
            stream_id,
            returncode=completed.returncode,
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
            response_preview=response_text[-1000:],
        )
        if completed.returncode != 0:
            write_native_job_status(
                stream_id,
                status="failed",
                message="Native YouTube Ask automation failed. Check stderr and automation failure artifacts.",
            )
            return None
        if not response_text.strip():
            write_native_job_status(stream_id, status="failed", message="Runner completed without a response file.")
            return None
        if ask_refused(response_text):
            if attempt_name == "primary":
                write_native_job_status(
                    stream_id,
                    status="running",
                    message="YouTube Ask refused the primary prompt; retrying with fallback prompt.",
                )
                continue
            write_native_job_status(
                stream_id,
                status="failed",
                message="YouTube Ask refused both prompts. Try a shorter/manual prompt or inspect the response preview.",
            )
            return None
        return output_path
    return None


def ask_refused(response_text: str) -> bool:
    lowered = response_text.lower()
    refusal_markers = [
        "i can't help with that",
        "i can’t help with that",
        "try asking something else",
        "i can't assist",
        "i can’t assist",
    ]
    return any(marker in lowered for marker in refusal_markers)
