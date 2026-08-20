import copy
import json

import pytest
from sqlalchemy import select

from app.models import AnalysisRun, CandidateWindow, DerivedAsset
from app.services.processing import analyze_one_stream
from app.services.reanalysis import ReanalysisBlockedError, build_reanalysis_plan, reanalyze_stream
from app.services.repository import upsert_stream


def _stream_payload():
    return {
        "platform": "youtube",
        "channel_id": "channel_1",
        "source_video_id": "fixture_stream_001",
        "title": "Fixture Stream",
        "description": "",
        "url": "https://www.youtube.com/watch?v=fixture_stream_001",
        "published_at": "2026-07-31T12:00:00Z",
        "duration": 1200,
        "thumbnail": "",
        "processing_status": "queued",
        "schema_version": "1.0",
    }


class PayloadAnalyzer:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def analyze_stream(self, stream):
        self.calls += 1
        return json.dumps(self.payload)

    def repair_response(self, previous_response, validation_errors):
        raise AssertionError("repair should not be called for a valid payload")


class FailingAnalyzer:
    def analyze_stream(self, stream):
        raise RuntimeError("replacement analyzer failed")

    def repair_response(self, previous_response, validation_errors):
        raise AssertionError("repair should not be called")


def _payload_for_stream(valid_candidate_data, stream, *, title):
    payload = copy.deepcopy(valid_candidate_data)
    payload["stream_id"] = stream.stream_id
    payload["source_video_id"] = stream.source_video_id
    payload["candidate"]["title"] = title
    return payload


def _disable_transcript_side_effects(monkeypatch):
    monkeypatch.setattr("app.services.processing.ensure_stream_transcript", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.processing.sync_stream_media", lambda *args, **kwargs: (None, False, 0, 0, None))


def _seed_candidate(db_session, valid_candidate_data, monkeypatch):
    _disable_transcript_side_effects(monkeypatch)
    stream, _ = upsert_stream(db_session, _stream_payload())
    db_session.commit()
    payload = _payload_for_stream(valid_candidate_data, stream, title="Original candidate")
    candidates = analyze_one_stream(db_session, stream.stream_id, analyzer=PayloadAnalyzer(payload))
    return stream, candidates[0]


def test_reanalysis_plan_requires_untouched_candidate_generation(db_session, valid_candidate_data, monkeypatch):
    stream, candidate = _seed_candidate(db_session, valid_candidate_data, monkeypatch)

    plan = build_reanalysis_plan(
        db_session,
        stream.stream_id,
        expected_candidate_ids=[candidate.candidate_window_id],
    )

    assert plan.safe is True
    assert plan.active_candidate_ids == (candidate.candidate_window_id,)

    candidate.is_favorite = True
    db_session.commit()
    blocked = build_reanalysis_plan(
        db_session,
        stream.stream_id,
        expected_candidate_ids=[candidate.candidate_window_id],
    )
    assert blocked.safe is False
    assert any("favorite candidate" in blocker for blocker in blocked.blockers)


def test_reanalysis_plan_blocks_downstream_assets(db_session, valid_candidate_data, monkeypatch):
    stream, candidate = _seed_candidate(db_session, valid_candidate_data, monkeypatch)
    db_session.add(
        DerivedAsset(
            candidate_window_id=candidate.candidate_window_id,
            asset_type="video_clip",
            external_reference="fixture.mp4",
            creation_status="complete",
        )
    )
    db_session.commit()

    plan = build_reanalysis_plan(
        db_session,
        stream.stream_id,
        expected_candidate_ids=[candidate.candidate_window_id],
    )

    assert plan.safe is False
    assert any("derived asset" in blocker for blocker in plan.blockers)


def test_reanalysis_requires_exact_active_candidate_set(db_session, valid_candidate_data, monkeypatch):
    stream, candidate = _seed_candidate(db_session, valid_candidate_data, monkeypatch)

    plan = build_reanalysis_plan(
        db_session,
        stream.stream_id,
        expected_candidate_ids=["candidate_not_the_current_row"],
    )

    assert plan.safe is False
    assert any("active candidate set changed" in blocker for blocker in plan.blockers)
    with pytest.raises(ReanalysisBlockedError):
        reanalyze_stream(
            db_session,
            stream.stream_id,
            expected_candidate_ids=["candidate_not_the_current_row"],
            reason="fixture replacement",
            analyzer=FailingAnalyzer(),
        )


def test_failed_reanalysis_preserves_old_candidate_and_stream_status(db_session, valid_candidate_data, monkeypatch):
    stream, candidate = _seed_candidate(db_session, valid_candidate_data, monkeypatch)
    old_run_id = candidate.analysis_run_id
    old_stream_status = stream.processing_status

    with pytest.raises(RuntimeError, match="replacement analyzer failed"):
        reanalyze_stream(
            db_session,
            stream.stream_id,
            expected_candidate_ids=[candidate.candidate_window_id],
            reason="fixture replacement",
            analyzer=FailingAnalyzer(),
        )

    db_session.expire_all()
    old_candidate = db_session.get(CandidateWindow, candidate.candidate_window_id)
    refreshed_stream = old_candidate.stream
    assert old_candidate.review_status == "pending_review"
    assert refreshed_stream.processing_status == old_stream_status
    assert db_session.get(AnalysisRun, old_run_id).status == "complete"

    runs = list(
        db_session.scalars(
            select(AnalysisRun).where(AnalysisRun.stream_id == stream.stream_id).order_by(AnalysisRun.created_at)
        ).all()
    )
    assert len(runs) == 2
    assert runs[-1].status == "failed"
    assert runs[-1].usage["reanalysis"]["status"] == "failed"


def test_successful_reanalysis_archives_only_old_generation(db_session, valid_candidate_data, monkeypatch):
    stream, candidate = _seed_candidate(db_session, valid_candidate_data, monkeypatch)
    old_run_id = candidate.analysis_run_id
    replacement_payload = _payload_for_stream(valid_candidate_data, stream, title="Replacement candidate")

    result = reanalyze_stream(
        db_session,
        stream.stream_id,
        expected_candidate_ids=[candidate.candidate_window_id],
        reason="temporal evidence cleanup",
        analyzer=PayloadAnalyzer(replacement_payload),
    )

    db_session.expire_all()
    old_candidate = db_session.get(CandidateWindow, candidate.candidate_window_id)
    new_candidate = db_session.get(CandidateWindow, result.replacement_candidate_ids[0])
    new_run = db_session.get(AnalysisRun, result.analysis_run_id)

    assert old_candidate.review_status == "archived"
    assert old_candidate.analysis_run_id == old_run_id
    assert old_candidate.analysis_run.status == "complete"
    assert old_candidate.emergent_observations["_supersession_history"][-1]["replacementAnalysisRunId"] == result.analysis_run_id

    assert new_candidate.review_status == "pending_review"
    assert new_candidate.title == "Replacement candidate"
    assert new_candidate.analysis_run_id == result.analysis_run_id
    assert new_run.status == "complete"
    assert new_run.usage["reanalysis"]["status"] == "complete"
    assert new_run.usage["reanalysis"]["supersededCandidateIds"] == [candidate.candidate_window_id]
    assert new_run.usage["reanalysis"]["replacementCandidateIds"] == list(result.replacement_candidate_ids)

    visible = list(
        db_session.scalars(
            select(CandidateWindow).where(
                CandidateWindow.stream_id == stream.stream_id,
                CandidateWindow.review_status != "archived",
            )
        ).all()
    )
    assert [row.candidate_window_id for row in visible] == list(result.replacement_candidate_ids)
