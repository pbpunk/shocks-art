import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR
from app.models import AnalysisRun, Stream, StreamAnalysisArtifact, StreamTranscript
from app.services.ytdlp import fetch_youtube_auto_captions


CAPTIONS_DIR = ROOT_DIR / "data" / "captions"
STRUCTURED_ARTIFACT_FILES = [
    "prompts.json",
    "outline.txt",
    "opportunities.txt",
    "drilldown.txt",
    "final.txt",
    "local_review.json",
    "local_review.txt",
]


def caption_json3_path(stream: Stream) -> Path:
    return CAPTIONS_DIR / f"{stream.source_video_id}.en-orig.json3"


def ensure_stream_transcript(db: Session, stream: Stream, fetch_missing: bool = False) -> StreamTranscript | None:
    existing = db.scalar(
        select(StreamTranscript).where(
            StreamTranscript.stream_id == stream.stream_id,
            StreamTranscript.source == "youtube_auto_captions",
        )
    )
    if existing:
        return existing

    caption_path = caption_json3_path(stream)
    if not caption_path.exists() and fetch_missing:
        fetch_caption_file(stream, caption_path)
    if not caption_path.exists():
        return None

    text = transcript_text_from_json3(caption_path)
    if not text:
        return None

    transcript = StreamTranscript(
        stream_id=stream.stream_id,
        language="en-orig",
        source="youtube_auto_captions",
        format="plain_text",
        text=text,
        raw_location=str(caption_path),
        fetched_at=datetime.now(timezone.utc),
    )
    db.add(transcript)
    db.flush()
    return transcript


def try_ensure_stream_transcript(db: Session, stream: Stream, fetch_missing: bool = False) -> StreamTranscript | None:
    try:
        return ensure_stream_transcript(db, stream, fetch_missing=fetch_missing)
    except Exception:
        return None


def fetch_caption_file(stream: Stream, caption_path: Path) -> None:
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    fetch_youtube_auto_captions(
        url=stream.url,
        output_template=CAPTIONS_DIR / "%(id)s.%(ext)s",
    )


def transcript_text_from_json3(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    lines = []
    for event in data.get("events", []):
        parts = []
        for segment in event.get("segs", []):
            text = segment.get("utf8", "")
            if text.strip():
                parts.append(text.replace("\n", " ").strip())
        line = " ".join(parts).strip()
        if line:
            seconds = int(event.get("tStartMs", 0) / 1000)
            lines.append(f"{seconds_to_timestamp(seconds)} {line}")
    return "\n".join(lines)


def seconds_to_timestamp(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def save_structured_pass_artifacts(db: Session, stream: Stream, run: AnalysisRun, run_dir: Path) -> list[StreamAnalysisArtifact]:
    artifacts = []
    for filename in STRUCTURED_ARTIFACT_FILES:
        path = run_dir / filename
        if not path.exists():
            continue
        artifact_type = artifact_type_from_filename(filename)
        existing = db.scalar(
            select(StreamAnalysisArtifact).where(
                StreamAnalysisArtifact.stream_id == stream.stream_id,
                StreamAnalysisArtifact.analysis_run_id == run.analysis_run_id,
                StreamAnalysisArtifact.artifact_type == artifact_type,
                StreamAnalysisArtifact.location == str(path),
            )
        )
        text = path.read_text(encoding="utf-8")
        if existing:
            existing.text = text
            existing.artifact_metadata = {"structured_pass_dir": str(run_dir), "filename": filename}
            artifacts.append(existing)
            continue
        artifact = StreamAnalysisArtifact(
            stream_id=stream.stream_id,
            analysis_run_id=run.analysis_run_id,
            artifact_type=artifact_type,
            source="native-youtube-structured-v1",
            text=text,
            location=str(path),
            artifact_metadata={"structured_pass_dir": str(run_dir), "filename": filename},
        )
        db.add(artifact)
        artifacts.append(artifact)
    db.flush()
    return artifacts


def artifact_type_from_filename(filename: str) -> str:
    return Path(filename).stem
