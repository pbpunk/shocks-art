from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import AnalysisRun, CandidateWindow, Stream
from app.services.gemini import GeminiAnalyzer
from app.services.processing import analyze_existing_run
from app.services.repository import create_analysis_run


class ReanalysisBlockedError(RuntimeError):
    def __init__(self, blockers: list[str]) -> None:
        self.blockers = blockers
        super().__init__("Reanalysis blocked: " + "; ".join(blockers))


@dataclass(frozen=True)
class ReanalysisPlan:
    stream_id: str
    stream_title: str
    active_candidate_ids: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["active_candidate_ids"] = list(self.active_candidate_ids)
        payload["blockers"] = list(self.blockers)
        payload["safe"] = self.safe
        return payload


@dataclass(frozen=True)
class ReanalysisResult:
    stream_id: str
    analysis_run_id: str
    superseded_candidate_ids: tuple[str, ...]
    replacement_candidate_ids: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["superseded_candidate_ids"] = list(self.superseded_candidate_ids)
        payload["replacement_candidate_ids"] = list(self.replacement_candidate_ids)
        return payload


def _active_candidates(db: Session, stream_id: str) -> list[CandidateWindow]:
    return list(
        db.scalars(
            select(CandidateWindow)
            .where(
                CandidateWindow.stream_id == stream_id,
                CandidateWindow.review_status != "archived",
            )
            .order_by(CandidateWindow.created_at, CandidateWindow.candidate_rank)
        ).all()
    )


def _candidate_blockers(candidate: CandidateWindow) -> list[str]:
    blockers: list[str] = []
    prefix = candidate.candidate_window_id
    if candidate.is_favorite:
        blockers.append(f"{prefix}: favorite candidate")
    if candidate.review_status != "pending_review":
        blockers.append(f"{prefix}: review_status={candidate.review_status}")
    if candidate.processing_status != "complete":
        blockers.append(f"{prefix}: processing_status={candidate.processing_status}")
    if str(candidate.reviewer_notes or "").strip():
        blockers.append(f"{prefix}: reviewer_notes are present")
    if candidate.derived_assets:
        blockers.append(f"{prefix}: has {len(candidate.derived_assets)} derived asset(s)")
    return blockers


def build_reanalysis_plan(
    db: Session,
    stream_id: str,
    *,
    expected_candidate_ids: list[str] | tuple[str, ...] | None = None,
) -> ReanalysisPlan:
    stream = db.get(Stream, stream_id)
    if stream is None:
        raise ValueError(f"Stream not found: {stream_id}")

    candidates = _active_candidates(db, stream_id)
    blockers: list[str] = []
    if not candidates:
        blockers.append("stream has no active candidates to supersede")

    processing_run = db.scalar(
        select(AnalysisRun).where(
            AnalysisRun.stream_id == stream_id,
            AnalysisRun.status == "processing",
        )
    )
    if processing_run is not None:
        blockers.append(f"analysis run is already processing: {processing_run.analysis_run_id}")

    active_ids = tuple(candidate.candidate_window_id for candidate in candidates)
    if expected_candidate_ids is not None:
        expected = set(expected_candidate_ids)
        actual = set(active_ids)
        if expected != actual:
            blockers.append(
                "active candidate set changed: "
                f"expected={sorted(expected)} actual={sorted(actual)}"
            )

    for candidate in candidates:
        blockers.extend(_candidate_blockers(candidate))

    return ReanalysisPlan(
        stream_id=stream.stream_id,
        stream_title=stream.title,
        active_candidate_ids=active_ids,
        blockers=tuple(blockers),
    )


def _supersession_metadata(
    candidate: CandidateWindow,
    *,
    replacement_run_id: str,
    reason: str,
) -> dict:
    metadata = dict(candidate.emergent_observations or {})
    history = list(metadata.get("_supersession_history") or [])
    history.append(
        {
            "replacementAnalysisRunId": replacement_run_id,
            "reason": reason,
        }
    )
    metadata["_supersession_history"] = history
    return metadata


def _set_reanalysis_usage(run: AnalysisRun, **updates) -> None:
    usage = dict(run.usage or {})
    reanalysis = dict(usage.get("reanalysis") or {})
    reanalysis.update(updates)
    usage["reanalysis"] = reanalysis
    run.usage = usage


def _archive_replacement_attempt(
    db: Session,
    replacements: list[CandidateWindow],
    run: AnalysisRun,
    *,
    reason: str,
    restore_stream_status: str,
) -> None:
    for candidate in replacements:
        candidate.review_status = "archived"
        candidate.emergent_observations = _supersession_metadata(
            candidate,
            replacement_run_id=run.analysis_run_id,
            reason=f"replacement attempt aborted: {reason}",
        )
    _set_reanalysis_usage(run, status="aborted", reason=reason)
    run.stream.processing_status = restore_stream_status
    db.commit()


def reanalyze_stream(
    db: Session,
    stream_id: str,
    *,
    expected_candidate_ids: list[str] | tuple[str, ...],
    reason: str,
    analyzer: GeminiAnalyzer | None = None,
) -> ReanalysisResult:
    """Generate a replacement analysis before hiding the previous candidate generation.

    Existing candidates are eligible only when they are untouched pending-review rows
    with no favorites, reviewer notes, or derived assets. The exact active candidate
    set must also match the caller's expectation.

    A failed/quarantined replacement leaves the old candidates active and restores the
    Stream's prior processing status. Only after a new run succeeds do we archive the
    captured old rows. Historical AnalysisRun rows and raw responses are never deleted
    or rewritten.
    """

    if not str(reason or "").strip():
        raise ValueError("reason is required for reanalysis")
    if not expected_candidate_ids:
        raise ValueError("expected_candidate_ids is required for reanalysis")

    plan = build_reanalysis_plan(
        db,
        stream_id,
        expected_candidate_ids=expected_candidate_ids,
    )
    if not plan.safe:
        raise ReanalysisBlockedError(list(plan.blockers))

    stream = db.get(Stream, stream_id)
    assert stream is not None
    previous_stream_status = stream.processing_status
    captured_ids = tuple(plan.active_candidate_ids)
    settings = get_settings()
    run = create_analysis_run(
        db,
        stream,
        settings.gemini_model,
        settings.prompt_version,
        settings.schema_version,
    )
    _set_reanalysis_usage(
        run,
        status="processing",
        reason=reason,
        supersedesCandidateIds=list(captured_ids),
    )
    db.commit()

    # analyze_existing_run owns failure/quarantine bookkeeping. If it raises, restore
    # only the Stream's prior status; the failed/quarantined replacement AnalysisRun is
    # intentionally preserved for diagnosis and the old candidates remain untouched.
    try:
        replacements = analyze_existing_run(
            db,
            run.analysis_run_id,
            analyzer=analyzer,
        )
    except Exception:
        db.rollback()
        persisted_run = db.get(AnalysisRun, run.analysis_run_id)
        persisted_stream = db.get(Stream, stream_id)
        if persisted_run is not None:
            _set_reanalysis_usage(persisted_run, status="failed")
        if persisted_stream is not None:
            persisted_stream.processing_status = previous_stream_status
        db.commit()
        raise

    replacement_ids = {candidate.candidate_window_id for candidate in replacements}

    # Re-check the captured rows after inference in case a human touched one while the
    # model was running. If anything changed, hide only this new disposable generation
    # and preserve the old state.
    current_active = _active_candidates(db, stream_id)
    old_rows = [candidate for candidate in current_active if candidate.candidate_window_id in captured_ids]
    allowed_ids = set(captured_ids) | replacement_ids
    unexpected_ids = {
        candidate.candidate_window_id
        for candidate in current_active
        if candidate.candidate_window_id not in allowed_ids
    }
    blockers: list[str] = []
    if {candidate.candidate_window_id for candidate in old_rows} != set(captured_ids):
        blockers.append("captured candidate set changed during replacement analysis")
    if unexpected_ids:
        blockers.append(f"unexpected active candidates appeared: {sorted(unexpected_ids)}")
    for candidate in old_rows:
        blockers.extend(_candidate_blockers(candidate))

    if blockers:
        _archive_replacement_attempt(
            db,
            replacements,
            run,
            reason="; ".join(blockers),
            restore_stream_status=previous_stream_status,
        )
        raise ReanalysisBlockedError(blockers)

    for candidate in old_rows:
        candidate.review_status = "archived"
        candidate.emergent_observations = _supersession_metadata(
            candidate,
            replacement_run_id=run.analysis_run_id,
            reason=reason,
        )

    _set_reanalysis_usage(
        run,
        status="complete",
        reason=reason,
        supersededCandidateIds=list(captured_ids),
        replacementCandidateIds=[candidate.candidate_window_id for candidate in replacements],
    )
    db.commit()

    return ReanalysisResult(
        stream_id=stream_id,
        analysis_run_id=run.analysis_run_id,
        superseded_candidate_ids=captured_ids,
        replacement_candidate_ids=tuple(candidate.candidate_window_id for candidate in replacements),
        reason=reason,
    )
