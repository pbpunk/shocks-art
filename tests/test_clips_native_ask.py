import json

import pytest

from app.models import AnalysisRun, Stream, StreamTranscript
from app.services.candidate_evidence import CandidateEvidenceValidationError
from app.services.clips_native_ask import (
    CLIPS_NATIVE_ASK_SOURCE,
    pending_native_ask_streams,
    production_clip_candidates,
    save_clips_native_ask_response,
)
from app.services.gemini import GeminiAnalyzer
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


GLUING_RESPONSE = """
1. Gluing the Celebrity Sign (00:46 - 06:17)
Rank: 1
Duration: 5:31
Primary Pillar: Artistic process
Summary: Nate demonstrates positioning and gluing laser-cut letters onto a backing board.
Why It Is Useful: The section shows a complete visible process.
Tags: artistic process, sign making
Transcript Evidence: "Now, we have a bunch of letters and what I'm going to do is I'm going to kind of lay them out before I glue them because that is the smart thing to do." (00:46)
Visual Evidence: Nate lays out and glues letters on the sign (00:46-06:17).
Completeness Check: Begins with layout, develops the gluing process, and ends after the letters are positioned.
Window Type: source_window
Chatter Risk: low
Exact Caption Quote: Now, we have a bunch of letters and what I'm going to do is I'm going to kind of lay them out before I glue them because that is the smart thing to do.
Estimated Short Count: 1
Possible Opening Lines: "Today we are gluing up this custom sign."
Usefulness Score: 89
Component Scores: Pillar: 90, Hook: 84, Clarity: 91, Visuals: 92, Audio: 88, Impact: 80, Education: 88, Entertainment: 82, Potential: 89, Brand: 91, Confidence: 93
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


def _event(seconds, text, duration_ms=3000):
    return {"tStartMs": seconds * 1000, "dDurationMs": duration_ms, "segs": [{"utf8": text}]}


def _add_transcript(db_session, stream, tmp_path, events):
    raw = tmp_path / f"{stream.source_video_id}.en-orig.json3"
    raw.write_text(json.dumps({"events": events}), encoding="utf-8")
    transcript = StreamTranscript(
        stream_id=stream.stream_id,
        language="en",
        source="youtube_auto_captions",
        format="json3",
        text=" ".join(
            segment.get("utf8", "")
            for event in events
            for segment in event.get("segs", [])
        ),
        raw_location=str(raw),
    )
    db_session.add(transcript)
    db_session.flush()
    return transcript


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
    assert native_candidates[0].review_status == "needs_verification"
    assert native_candidates[0].transcript_excerpt == "No verified in-window transcript evidence"


def test_native_ask_replaces_model_timestamp_with_real_caption_timestamp(db_session, tmp_path):
    stream = make_stream(db_session, source_video_id="E9F-vEbmZpg")
    quote = (
        "Now, we have a bunch of letters and what I'm going to do is I'm going to kind of lay them out "
        "before I glue them because that is the smart thing to do."
    )
    _add_transcript(
        db_session,
        stream,
        tmp_path,
        [
            _event(90, "unrelated shop chatter inside the candidate window"),
            _event(180, quote),
        ],
    )

    _, candidates, skipped = save_clips_native_ask_response(db_session, stream, GLUING_RESPONSE)
    db_session.commit()

    assert skipped == 0
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.start_seconds == 46
    assert candidate.end_seconds == 377
    assert candidate.transcript_evidence[0]["seconds"] == 180
    assert candidate.transcript_evidence[0]["timestamp"] == "00:03:00"
    assert quote in candidate.transcript_evidence[0]["text"]
    assert candidate.transcript_excerpt.endswith("(00:03:00)")
    assert candidate.emergent_observations["_transcript_evidence_grounding"]["source"] == "stored_json3_captions"


def test_native_ask_rejects_quote_found_only_outside_proposed_window(db_session, tmp_path):
    stream = make_stream(db_session, source_video_id="E9F-vEbmZpg")
    quote = (
        "Now, we have a bunch of letters and what I'm going to do is I'm going to kind of lay them out "
        "before I glue them because that is the smart thing to do."
    )
    _add_transcript(
        db_session,
        stream,
        tmp_path,
        [
            _event(90, "unrelated shop chatter inside the candidate window"),
            _event(720, quote),
        ],
    )

    with pytest.raises(CandidateEvidenceValidationError, match="could not be grounded inside the proposed candidate window"):
        save_clips_native_ask_response(db_session, stream, GLUING_RESPONSE)

    assert stream.processing_status == "failed"
    assert not stream.candidates


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


def test_direct_gemini_analyzer_cannot_contact_network(db_session):
    stream = make_stream(db_session, source_video_id="network_guard")
    analyzer = GeminiAnalyzer("pretend-api-key", "gemini-3.1-flash-lite")

    with pytest.raises(RuntimeError, match="Direct Gemini API video analysis is disabled"):
        analyzer.analyze_stream(stream)
    with pytest.raises(RuntimeError, match="Direct Gemini API video analysis is disabled"):
        analyzer.repair_response("{}", ["fixture error"])
