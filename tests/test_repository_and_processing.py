import json

import pytest

from app.models import CandidateWindow, Stream
from app.services.processing import analyze_one_stream, discover_and_store_streams, start_next_analysis_run
from app.services.repository import upsert_stream


def stream_payload():
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


def test_stream_upsert_is_idempotent(db_session):
    first, first_created = upsert_stream(db_session, stream_payload())
    second, second_created = upsert_stream(db_session, {**stream_payload(), "title": "Updated"})
    db_session.commit()
    assert first.stream_id == second.stream_id
    assert first_created is True
    assert second_created is False
    assert db_session.get(Stream, first.stream_id).title == "Updated"


def test_stream_upsert_preserves_existing_processing_status(db_session):
    stream, _ = upsert_stream(db_session, {**stream_payload(), "processing_status": "complete"})
    db_session.commit()

    updated, was_created = upsert_stream(db_session, {**stream_payload(), "title": "Updated"})
    db_session.commit()

    assert was_created is False
    assert updated.stream_id == stream.stream_id
    assert updated.title == "Updated"
    assert updated.processing_status == "complete"


class FakeYouTubeClient:
    def discover_streams(self, channel_handle):
        return [
            stream_payload(),
            {**stream_payload(), "source_video_id": "fixture_stream_002", "title": "Second Fixture Stream"},
        ]


def test_discovery_fetches_captions_for_discovered_streams(db_session, monkeypatch):
    calls = []

    def fake_transcript(db, stream, fetch_missing=False):
        calls.append((stream.source_video_id, fetch_missing))
        return object() if stream.source_video_id == "fixture_stream_001" else None

    monkeypatch.setattr("app.services.processing.try_ensure_stream_transcript", fake_transcript)

    result = discover_and_store_streams(db_session, youtube_client=FakeYouTubeClient())

    assert result["discovered"] == 2
    assert result["created"] == 2
    assert result["transcripts_available"] == 1
    assert result["transcripts_missing"] == 1
    assert calls == [("fixture_stream_001", True), ("fixture_stream_002", True)]


class RepairingAnalyzer:
    model = "fake"

    def __init__(self, valid_payload):
        self.valid_payload = valid_payload
        self.calls = 0

    def analyze_stream(self, stream):
        self.calls += 1
        return '{"schema_version":"1.0"}'

    def repair_response(self, previous_response, validation_errors):
        self.calls += 1
        payload = {**self.valid_payload, "stream_id": "stream_fixture"}
        payload["stream_id"] = previous_response and payload["stream_id"]
        return json.dumps(payload)


def test_invalid_gemini_response_repair_and_candidate_persistence(db_session, valid_candidate_data, monkeypatch):
    monkeypatch.setenv("MAX_RETRIES", "1")
    stream, _ = upsert_stream(db_session, stream_payload())
    valid_candidate_data["stream_id"] = stream.stream_id
    db_session.commit()
    analyzer = RepairingAnalyzer(valid_candidate_data)
    candidate = analyze_one_stream(db_session, stream.stream_id, analyzer=analyzer)
    assert analyzer.calls == 2
    assert candidate[0].primary_pillar == "mistakes_problem_solving"
    assert db_session.query(CandidateWindow).count() == 1
    assert db_session.get(Stream, stream.stream_id).processing_status == "complete"


def test_resume_skips_completed_analysis(db_session, valid_candidate_data):
    stream, _ = upsert_stream(db_session, stream_payload())
    valid_candidate_data["stream_id"] = stream.stream_id
    db_session.commit()
    analyzer = RepairingAnalyzer(valid_candidate_data)
    first = analyze_one_stream(db_session, stream.stream_id, analyzer=analyzer)
    second = analyze_one_stream(db_session, stream.stream_id, analyzer=analyzer)
    assert first[0].candidate_window_id == second[0].candidate_window_id


def test_multiple_candidates_are_persisted(db_session, valid_candidate_data):
    stream, _ = upsert_stream(db_session, stream_payload())
    first_payload = valid_candidate_data["candidate"]
    second_payload = {
        **first_payload,
        "title": "Second ranked process candidate",
        "start_seconds": 1200,
        "end_seconds": 1620,
        "start_timestamp": "00:20:00",
        "end_timestamp": "00:27:00",
        "duration_seconds": 420,
    }
    payload = {
        "schema_version": "1.0",
        "stream_id": stream.stream_id,
        "source_video_id": valid_candidate_data["source_video_id"],
        "candidates": [first_payload, second_payload],
    }
    db_session.commit()
    analyzer = RepairingAnalyzer(payload)
    candidates = analyze_one_stream(db_session, stream.stream_id, analyzer=analyzer)
    assert len(candidates) == 2
    assert [candidate.candidate_rank for candidate in candidates] == [1, 2]


class FailingAnalyzer:
    def analyze_stream(self, stream):
        raise RuntimeError("429 RESOURCE_EXHAUSTED. Quota exceeded for metric")

    def repair_response(self, previous_response, validation_errors):
        raise AssertionError("repair should not be called")


def test_failed_analysis_records_visible_error(db_session):
    stream, _ = upsert_stream(db_session, stream_payload())
    db_session.commit()
    with pytest.raises(RuntimeError):
        analyze_one_stream(db_session, stream.stream_id, analyzer=FailingAnalyzer())
    db_session.refresh(stream)
    assert stream.processing_status == "failed"
    assert stream.analysis_runs[-1].exception_message.startswith("Gemini quota exceeded")


def test_start_next_analysis_marks_stream_processing(db_session):
    stream, _ = upsert_stream(db_session, stream_payload())
    db_session.commit()
    run = start_next_analysis_run(db_session)
    db_session.refresh(stream)
    assert run is not None
    assert run.status == "processing"
    assert stream.processing_status == "processing"
