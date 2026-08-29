from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select

LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", Path(__file__).resolve().parents[2])).resolve()
os.chdir(LIVE_ROOT)
if str(LIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_ROOT))

from app.core.database import SessionLocal
from app.indexing.language_traces import LanguageTraceImportError, parse_youtube_json3_segments
from app.models import AnalysisRun, CandidateWindow, StreamTranscript
from app.services.ytdlp import SOURCE_EXTRACTOR_ARGS

BENCHMARK_DIR = LIVE_ROOT / "data" / "whisper_benchmark"
CLIPS_DIR = BENCHMARK_DIR / "clips"
DRAFT_MANIFEST = BENCHMARK_DIR / "manifest.draft.json"
REVIEWED_MANIFEST = BENCHMARK_DIR / "manifest.json"
SOURCE_VIDEO_DIR = LIVE_ROOT / "data" / "source_videos"
DERIVED_CLIP_DIR = LIVE_ROOT / "data" / "derived_clips"
TARGET_CASES = 8
MIN_CASES = 6
MAX_CASES_PER_STREAM = 2
MAX_REMOTE_ATTEMPTS = 12
CASE_SECONDS = 45
PRE_ROLL_SECONDS = 3
GROUNDING_SOURCE = "stored_json3_captions"
GROUNDING_VERSION_PREFIX = "native-ask-caption-grounding-"


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def bounded_window(candidate_start: int, candidate_end: int, evidence_seconds: int) -> tuple[int, int]:
    candidate_start = max(0, int(candidate_start))
    candidate_end = max(candidate_start, int(candidate_end))
    evidence_seconds = max(candidate_start, min(int(evidence_seconds), candidate_end))
    start = max(candidate_start, evidence_seconds - PRE_ROLL_SECONDS)
    end = min(candidate_end, start + CASE_SECONDS)
    if end - start < 10 and candidate_end - candidate_start >= 10:
        start = max(candidate_start, end - min(CASE_SECONDS, candidate_end - candidate_start))
    return start, end


def _evidence_seconds(candidate: CandidateWindow) -> int | None:
    for item in list(candidate.transcript_evidence or []):
        try:
            if isinstance(item, dict):
                return int(item.get("seconds", 0))
            return int(item.seconds)
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def _is_repaired_native_ask(candidate: CandidateWindow) -> bool:
    observations = dict(candidate.emergent_observations or {})
    grounding = observations.get("_transcript_evidence_grounding")
    if not isinstance(grounding, dict):
        return False
    return (
        str(grounding.get("source") or "") == GROUNDING_SOURCE
        and str(grounding.get("version") or "").startswith(GROUNDING_VERSION_PREFIX)
        and bool(candidate.transcript_evidence)
    )


def _transcript_path(transcript: StreamTranscript) -> Path:
    path = Path(str(transcript.raw_location or ""))
    if not path.is_absolute():
        path = LIVE_ROOT / path
    return path.resolve()


def _caption_text(transcript: StreamTranscript, start_seconds: int, end_seconds: int) -> str:
    path = _transcript_path(transcript)
    if not path.is_file():
        return ""
    try:
        segments = parse_youtube_json3_segments(path)
    except LanguageTraceImportError:
        return ""
    start_ms = int(start_seconds) * 1000
    end_ms = int(end_seconds) * 1000
    parts = [
        segment.text
        for segment in segments
        if segment.start_ms <= end_ms and max(segment.start_ms, segment.end_ms) >= start_ms
    ]
    return " ".join(" ".join(parts).split()).strip()


def _clean_term(value: object) -> str:
    text = " ".join(str(value or "").split()).strip(" -_:;,.\t\r\n")
    if not text or len(text) > 60 or not re.search(r"[A-Za-z]", text):
        return ""
    if len(re.findall(r"[A-Za-z0-9']+", text)) > 6:
        return ""
    return text


def _suggested_terms(candidate: CandidateWindow) -> list[str]:
    values: list[object] = [candidate.title]
    values.extend(list(candidate.tags or []))
    if candidate.primary_pillar:
        values.append(candidate.primary_pillar)
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = _clean_term(value)
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= 4:
            break
    return terms


def _case_id(candidate: CandidateWindow) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "-", candidate.candidate_window_id).strip("-")[-32:]
    return f"native-ask-{suffix or 'case'}"


def _candidate_payload(candidate: CandidateWindow, transcript: StreamTranscript) -> dict[str, Any] | None:
    evidence_seconds = _evidence_seconds(candidate)
    if evidence_seconds is None:
        return None
    start_seconds, end_seconds = bounded_window(candidate.start_seconds, candidate.end_seconds, evidence_seconds)
    if end_seconds - start_seconds < 10:
        return None
    caption_text = _caption_text(transcript, start_seconds, end_seconds)
    if len(re.findall(r"[A-Za-z0-9']+", caption_text)) < 8:
        return None
    case_id = _case_id(candidate)
    return {
        "id": case_id,
        "media_path": f"clips/{case_id}.wav",
        "language": "en",
        "caption_text": caption_text,
        "suggested_project_terms": _suggested_terms(candidate),
        "source": {
            "candidate_window_id": candidate.candidate_window_id,
            "stream_id": candidate.stream_id,
            "source_video_id": candidate.stream.source_video_id,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "grounding": GROUNDING_SOURCE,
        },
    }


def _run_ffmpeg(input_path: Path, *, start_seconds: int, duration_seconds: int, output_path: Path) -> None:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg executable is not available on PATH")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".tmp.wav")
    tmp_path.unlink(missing_ok=True)
    completed = subprocess.run(
        [
            executable,
            "-y",
            "-ss",
            str(max(0, int(start_seconds))),
            "-i",
            str(input_path),
            "-t",
            str(max(1, int(duration_seconds))),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(tmp_path),
        ],
        cwd=LIVE_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0 or not tmp_path.is_file():
        detail = (completed.stderr or completed.stdout or "")[-800:].strip()
        raise RuntimeError(f"ffmpeg audio cut failed: {detail or completed.returncode}")
    tmp_path.replace(output_path)


def _download_section(url: str, *, start_seconds: int, end_seconds: int, output_path: Path) -> None:
    executable = shutil.which("yt-dlp")
    if not executable:
        raise RuntimeError("yt-dlp executable is not available on PATH")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg executable is not available on PATH")
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="whisper-section-", dir=BENCHMARK_DIR) as temp_name:
        temp_dir = Path(temp_name)
        template = temp_dir / "section.%(ext)s"
        completed = subprocess.run(
            [
                executable,
                "--no-playlist",
                "--extractor-args",
                SOURCE_EXTRACTOR_ARGS,
                "--download-sections",
                f"*{int(start_seconds)}-{int(end_seconds)}",
                "-f",
                "bestaudio/best",
                "-x",
                "--audio-format",
                "wav",
                "-o",
                str(template),
                url,
            ],
            cwd=LIVE_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        wavs = sorted(temp_dir.glob("section*.wav"))
        if completed.returncode != 0 or not wavs:
            detail = (completed.stderr or completed.stdout or "")[-1000:].strip()
            raise RuntimeError(f"bounded yt-dlp audio fetch failed: {detail or completed.returncode}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(wavs[0]), output_path)


def _materialize_case_audio(candidate: CandidateWindow, payload: dict[str, Any]) -> str:
    output_path = BENCHMARK_DIR / payload["media_path"]
    if output_path.is_file() and output_path.stat().st_size > 10_000:
        return "reused"

    source = payload["source"]
    start_seconds = int(source["start_seconds"])
    end_seconds = int(source["end_seconds"])
    duration_seconds = end_seconds - start_seconds

    derived = DERIVED_CLIP_DIR / f"{candidate.candidate_window_id}.mp4"
    if derived.is_file():
        _run_ffmpeg(
            derived,
            start_seconds=max(0, start_seconds - int(candidate.start_seconds)),
            duration_seconds=duration_seconds,
            output_path=output_path,
        )
        return "derived-clip-cache"

    cached_source = SOURCE_VIDEO_DIR / f"{candidate.stream.source_video_id}.mp4"
    if cached_source.is_file():
        _run_ffmpeg(
            cached_source,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
            output_path=output_path,
        )
        return "source-video-cache"

    _download_section(
        candidate.stream.url,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        output_path=output_path,
    )
    return "bounded-youtube-section"


def _write_draft(cases: list[dict[str, Any]]) -> None:
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "review_required": True,
        "ground_truth_policy": "caption_text is a source-backed review seed only; a human must create exact reference_text and project_terms before benchmark execution",
        "cases": cases,
    }
    tmp_path = DRAFT_MANIFEST.with_suffix(".tmp.json")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(DRAFT_MANIFEST)


def main() -> int:
    if REVIEWED_MANIFEST.is_file():
        return emit(
            {
                "summary": "Reviewed Whisper benchmark manifest already exists; preparation is not needed",
                "reviewed_manifest": "data/whisper_benchmark/manifest.json",
                "review_required": False,
            }
        )

    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    prepared: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    source_counts: Counter[str] = Counter()
    per_stream: defaultdict[str, int] = defaultdict(int)
    remote_attempts = 0

    with SessionLocal() as db:
        candidates = db.scalars(
            select(CandidateWindow)
            .join(AnalysisRun, CandidateWindow.analysis_run_id == AnalysisRun.analysis_run_id)
            .where(AnalysisRun.status == "complete")
            .order_by(CandidateWindow.created_at.desc(), CandidateWindow.candidate_rank.asc())
        ).all()

        transcript_cache: dict[str, StreamTranscript | None] = {}
        for candidate in candidates:
            if len(prepared) >= TARGET_CASES:
                break
            if not _is_repaired_native_ask(candidate):
                continue
            if per_stream[candidate.stream_id] >= MAX_CASES_PER_STREAM:
                continue

            if candidate.stream_id not in transcript_cache:
                transcript_cache[candidate.stream_id] = db.scalar(
                    select(StreamTranscript).where(
                        StreamTranscript.stream_id == candidate.stream_id,
                        StreamTranscript.source == "youtube_auto_captions",
                    )
                )
            transcript = transcript_cache[candidate.stream_id]
            if transcript is None:
                continue
            payload = _candidate_payload(candidate, transcript)
            if payload is None:
                continue

            output_path = BENCHMARK_DIR / payload["media_path"]
            needs_remote = not output_path.is_file() and not (DERIVED_CLIP_DIR / f"{candidate.candidate_window_id}.mp4").is_file() and not (SOURCE_VIDEO_DIR / f"{candidate.stream.source_video_id}.mp4").is_file()
            if needs_remote:
                if remote_attempts >= MAX_REMOTE_ATTEMPTS:
                    continue
                remote_attempts += 1

            try:
                source_kind = _materialize_case_audio(candidate, payload)
            except Exception as exc:
                failures.append(
                    {
                        "candidate_window_id": candidate.candidate_window_id,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
                continue

            source_counts[source_kind] += 1
            per_stream[candidate.stream_id] += 1
            payload["audio_source"] = source_kind
            prepared.append(payload)

    _write_draft(prepared)
    receipt_cases = [
        {
            "id": case["id"],
            "candidate_window_id": case["source"]["candidate_window_id"],
            "source_video_id": case["source"]["source_video_id"],
            "start_seconds": case["source"]["start_seconds"],
            "end_seconds": case["source"]["end_seconds"],
            "caption_text": case["caption_text"][:180],
            "suggested_project_terms": case["suggested_project_terms"],
            "audio_source": case["audio_source"],
        }
        for case in prepared
    ]
    payload = {
        "summary": "Whisper benchmark review draft prepared from repaired native-Ask caption lineage" if len(prepared) >= MIN_CASES else "Whisper benchmark review draft is incomplete; too few bounded cases could be materialized",
        "draft_manifest": "data/whisper_benchmark/manifest.draft.json",
        "reviewed_manifest": "data/whisper_benchmark/manifest.json",
        "review_required": True,
        "case_count": len(prepared),
        "minimum_case_count": MIN_CASES,
        "target_case_count": TARGET_CASES,
        "audio_sources": dict(source_counts),
        "remote_attempts": remote_attempts,
        "cases": receipt_cases,
        "failures": failures[:8],
    }
    return emit(payload, 0 if len(prepared) >= MIN_CASES else 1)


if __name__ == "__main__":
    raise SystemExit(main())
