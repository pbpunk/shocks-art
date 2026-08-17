import argparse
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from sqlalchemy import select

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.models import Stream  # noqa: E402
from app.services.stream_archive import caption_json3_path, fetch_caption_file  # noqa: E402
from app.services.structured_import import import_structured_pass  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run structured native YouTube Ask extraction over stored streams.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum streams to process. 0 means all.")
    parser.add_argument("--offset", type=int, default=0, help="Number of ordered streams to skip before processing.")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--skip-captions", action="store_true")
    args = parser.parse_args()

    init_db()
    streams = load_streams(args.limit or None, args.offset)
    summary_path = ROOT_DIR / "data" / "structured_passes" / "batch_latest.txt"
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"Structured batch streams: {len(streams)}"]
    successes = 0
    failures = 0
    for index, stream in enumerate(streams, start=1):
        label = f"[{index}/{len(streams)}] {stream.source_video_id} {stream.title}"
        print(label, flush=True)
        if not args.skip_captions:
            fetch_captions(stream)
        run_dir = run_structured_pass(stream, args.timeout)
        if not run_dir:
            failures += 1
            lines.append(f"FAILED run {stream.source_video_id} {stream.title}")
            write_summary(summary_path, lines)
            continue
        try:
            imported, skipped = import_run_dir(run_dir)
        except Exception as exc:
            failures += 1
            lines.append(f"FAILED import {stream.source_video_id} {run_dir}: {exc}")
            write_summary(summary_path, lines)
            continue
        successes += 1
        lines.append(f"OK {stream.source_video_id} imported={imported} skipped={skipped} dir={run_dir}")
        write_summary(summary_path, lines)

    lines.append(f"Complete successes={successes} failures={failures}")
    write_summary(summary_path, lines)
    print(summary_path)
    return 1 if failures else 0


def load_streams(limit: int | None, offset: int = 0) -> list[Stream]:
    db = SessionLocal()
    try:
        query = select(Stream).order_by(Stream.published_at.desc())
        if offset:
            query = query.offset(offset)
        if limit:
            query = query.limit(limit)
        return list(db.scalars(query).all())
    finally:
        db.close()


def fetch_captions(stream: Stream) -> None:
    path = caption_json3_path(stream)
    if path.exists():
        return
    fetch_caption_file(stream, path)


def run_structured_pass(stream: Stream, timeout: int) -> Path | None:
    command = [
        sys.executable,
        str(ROOT_DIR / "tools" / "native_youtube_structured_pass.py"),
        "--url",
        stream.url,
        f"--video-id={stream.source_video_id}",
        f"--title={stream.title}",
        "--timeout",
        str(timeout),
    ]
    completed = subprocess.run(command, cwd=ROOT_DIR, capture_output=True, text=True)
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        return None
    for line in reversed(completed.stdout.splitlines()):
        path = Path(line.strip())
        if path.exists():
            return path
    return None


def import_run_dir(run_dir: Path) -> tuple[int, int]:
    db = SessionLocal()
    try:
        result = import_structured_pass(db, run_dir)
        db.commit()
        return len(result.candidates), result.skipped_duplicates
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def write_summary(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
