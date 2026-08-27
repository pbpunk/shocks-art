from app.models import AnalysisRun, CandidateWindow, Stream
from app.services.clips_native_ask import (
    CLIPS_NATIVE_ASK_SOURCE,
    pending_native_ask_streams,
    production_clip_candidates,
    save_clips_native_ask_response,
)
from app.services.native_youtube import _candidate_from_payload, parse_native_youtube_response


ASK_RESPONSE = """
1. Nate Explains the Full Recovery Story (10:43 - 16:00)
Rank: 1
Duration: 5:17
Primary Pillar: Personal journey and recovery
Summary: Nate gives a complete account of a recovery turning point and what changed afterward.
Why It Is Useful: The story has context, development, and a clear payoff inside the window.
Tags: Recovery, ArtistStory
Transcript Evidence: "I quit doing all the hard drugs" (12:10).
Visual Evidence: Nate speaks while continuing the visible shop work (10:43-16:00).
Completeness Check: Begins with the old situation, develops what changed, and ends with the present-day lesson.
Window Type: source_window
Chatter Risk: low
Exact Caption Quote: I quit doing all the hard drugs
Estimated Short Count: 2
Possible Opening Lines: "Here is what finally changed."
Usefulness Score: 90
Component Scores: Pillar: 95, Hook: 88, Clarity: 92, Visuals: 82, Audio: 90, Impact: 95, Education: 80, Entertainment: 80, Potential: 90, Brand: 92, Confidence: 92
"""


def make_stream(db_session, *, source_video_id="pDC14ymQqWY"):
    stream = Stream(
        platform="youtube",
        channel_id="fixture_channel",
        source_video_id=source_video_id,
        title="Fractal Burning Irish Shillelaghs",
        description="Fixture stream.",
        url=f"https://www.youtube.com/watch?v={source_video_id}",
        published_at="2026-08-27T12:00:00Z",
        duration=3600,
        thumbnail="",
        processing_status="complete",
        schema_version="1.0",
    )
    db_session.add(stream)
    db_session.flush()
    return stream


def add_run(db_session, stream, model, status="complete"):
    run = AnalysisRun(
        stream_id=stream.stream_id,
        model=model,
        prompt_version="fixture",
        schema_version="1.0",
        status=status,
    )
    db_session.add(run)
    db_session.flush()
    return run


def test_direct_gemini_complete_stream_is_still_pending_for_youtube_ask(db_session):
    stream = make_stream(db_session)
    add_run(db_session, stream, "gemini-3.1-flash-lite")
    db_session.commit()

    pending_ids = {row.stream_id for row in pending_native_ask_streams(db_session)}

    assert stream.stream_id in pending_ids


def test_completed_native_ask_run_is_not_pending(db_session):
    stream = make_stream(db_session)
    add_run(db_session, stream, CLIPS_NATIVE_ASK_SOURCE)
    db_session.commit()

    pending_ids = {row.stream_id for row in pending_native_ask_streams(db_session)}

    assert stream.stream_id not in pending_ids


def test_native_ask_import_is_not_suppressed_by_matching_direct_gemini_candidate(db_session):
    stream = make_stream(db_session)
    direct_run = add_run(db_session, stream, "gemini-3.1-flash-lite")
    parsed = parse_native_youtube_response(ASK_RESPONSE, stream)
    direct_candidate = _candidate_from_payload(direct_run, parsed.candidates[0], 1)
    db_session.add(direct_candidate)
    db_session.commit()

    native_run, native_candidates, skipped = save_clips_native_ask_response(db_session, stream, ASK_RESPONSE)
    db_session.commit()

    assert skipped == 0
    assert len(native_candidates) == 1
    assert native_run.model == CLIPS_NATIVE_ASK_SOURCE
    assert native_candidates[0].candidate_window_id != direct_candidate.candidate_window_id


def test_production_clip_candidates_exclude_direct_gemini_lineage(db_session):
    stream = make_stream(db_session)
    parsed = parse_native_youtube_response(ASK_RESPONSE, stream)

    direct_run = add_run(db_session, stream, "gemini-3.1-flash-lite")
    direct_candidate = _candidate_from_payload(direct_run, parsed.candidates[0], 1)
    direct_candidate.title = "Legacy direct Gemini candidate"
    db_session.add(direct_candidate)

    native_run = add_run(db_session, stream, CLIPS_NATIVE_ASK_SOURCE)
    native_candidate = _candidate_from_payload(native_run, parsed.candidates[0], 1)
    native_candidate.title = "Native YouTube Ask candidate"
    db_session.add(native_candidate)
    db_session.commit()

    candidates, total = production_clip_candidates(db_session, limit=200, include_check=True)

    assert total == 1
    assert [candidate.title for candidate in candidates] == ["Native YouTube Ask candidate"]


def test_legacy_direct_gemini_http_routes_are_gone(client):
    for path in (
        "/api/process",
        "/actions/process-one",
        "/api/streams/fixture/analyze",
        "/shocks_art/api/process",
    ):
        response = client.post(path, follow_redirects=False)
        assert response.status_code == 410
        assert "Direct Gemini video analysis is disabled" in response.json()["detail"]
