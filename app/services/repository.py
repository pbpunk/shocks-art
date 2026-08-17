from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnalysisRun, CandidateWindow, Stream
from app.schemas.candidate import CandidateResponse
from app.services.ranking import weighted_score
from app.services.tags import normalize_tags


def upsert_stream(db: Session, stream_data: dict) -> tuple[Stream, bool]:
    existing = db.scalar(
        select(Stream).where(
            Stream.platform == stream_data.get("platform", "youtube"),
            Stream.source_video_id == stream_data["source_video_id"],
        )
    )
    if existing:
        for key, value in stream_data.items():
            if key == "processing_status":
                continue
            setattr(existing, key, value)
        db.flush()
        return existing, False
    stream = Stream(**stream_data)
    db.add(stream)
    db.flush()
    return stream, True


def create_analysis_run(db: Session, stream: Stream, model: str, prompt_version: str, schema_version: str) -> AnalysisRun:
    run = AnalysisRun(
        stream_id=stream.stream_id,
        model=model,
        prompt_version=prompt_version,
        schema_version=schema_version,
        status="processing",
    )
    stream.processing_status = "processing"
    db.add(run)
    db.flush()
    return run


def build_candidate(run: AnalysisRun, payload, rank: int) -> CandidateWindow:
    scores = payload.scores.model_dump()
    return CandidateWindow(
        stream_id=run.stream_id,
        analysis_run_id=run.analysis_run_id,
        candidate_rank=rank,
        start_seconds=payload.start_seconds,
        end_seconds=payload.end_seconds,
        start_timestamp=payload.start_timestamp,
        end_timestamp=payload.end_timestamp,
        duration_seconds=payload.duration_seconds,
        title=payload.title,
        concise_summary=payload.concise_summary,
        selection_reason=payload.selection_reason,
        primary_pillar=payload.primary_pillar,
        secondary_pillars=list(payload.secondary_pillars),
        tags=normalize_tags(payload.tags),
        transcript_excerpt=payload.transcript_excerpt,
        visual_description=payload.visual_description,
        transcript_evidence=[evidence.model_dump() for evidence in payload.transcript_evidence],
        visual_evidence=[evidence.model_dump() for evidence in payload.visual_evidence],
        contextual_notes=payload.contextual_notes,
        estimated_short_count=payload.estimated_short_count,
        possible_hooks=payload.possible_hooks,
        editing_notes=payload.editing_notes,
        risks=payload.risks,
        scores=scores,
        confidence=scores["confidence"],
        emergent_observations=payload.emergent_observations,
        weighted_score=weighted_score(scores),
    )


def save_candidates(db: Session, run: AnalysisRun, response: CandidateResponse) -> list[CandidateWindow]:
    candidates = [
        build_candidate(run, payload, rank=index + 1)
        for index, payload in enumerate(response.candidates)
    ]
    run.status = "complete"
    if run.validation_errors:
        run.exception_message = f"Candidate saved after repair on retry {run.retry_count}."
    else:
        run.exception_message = ""
    run.stream.processing_status = "complete"
    db.add_all(candidates)
    db.flush()
    return candidates


def save_candidate(db: Session, run: AnalysisRun, response: CandidateResponse) -> CandidateWindow:
    return save_candidates(db, run, response)[0]


def candidate_query(
    db: Session,
    pillar: str | None = None,
    tag: str | None = None,
    method: str | None = None,
    review_status: str | None = None,
) -> list[CandidateWindow]:
    query = select(CandidateWindow)
    if method:
        query = query.join(AnalysisRun).where(AnalysisRun.model == method)
    if pillar:
        query = query.where(CandidateWindow.primary_pillar == pillar)
    if review_status:
        query = query.where(CandidateWindow.review_status == review_status)
    else:
        query = query.where(CandidateWindow.review_status != "archived")
    candidates = list(db.scalars(query).all())
    if tag:
        normalized = normalize_tags([tag])[0]
        candidates = [candidate for candidate in candidates if normalized in candidate.tags]
    return sorted(candidates, key=lambda candidate: (candidate.stream.published_at, -candidate.weighted_score), reverse=True)
