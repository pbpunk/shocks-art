import sys
from pathlib import Path

from sqlalchemy import select

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.models import AnalysisRun, Stream  # noqa: E402
from app.services.stream_archive import ensure_stream_transcript, save_structured_pass_artifacts  # noqa: E402


def main() -> int:
    init_db()
    transcript_count = 0
    artifact_count = 0
    with SessionLocal() as db:
        streams = list(db.scalars(select(Stream)).all())
        for stream in streams:
            transcript = ensure_stream_transcript(db, stream, fetch_missing=False)
            if transcript:
                transcript_count += 1

        runs = list(db.scalars(select(AnalysisRun)).all())
        for run in runs:
            structured_dir = run.usage.get("structured_pass_dir") if isinstance(run.usage, dict) else None
            if not structured_dir:
                continue
            run_dir = Path(structured_dir)
            if not run_dir.exists():
                continue
            artifact_count += len(save_structured_pass_artifacts(db, run.stream, run, run_dir))

        db.commit()

    print(f"transcripts_available={transcript_count}")
    print(f"artifacts_saved_or_updated={artifact_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
