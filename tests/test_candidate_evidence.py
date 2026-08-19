import json
from types import SimpleNamespace

import pytest

from app.schemas.candidate import CandidateResponse, seconds_to_timestamp
from app.services.candidate_evidence import (
    CandidateEvidenceValidationError,
    audit_candidate_window,
    validate_candidate_transcript_evidence,
)


SCORES = {
    "pillar_relevance": 90,
    "hook_strength": 90,
    "standalone_clarity": 90,
    "visual_quality": 90,
    "audio_clarity": 90,
    "emotional_impact": 90,
    "educational_value": 90,
    "entertainment_value": 90,
    "editing_potential": 90,
    "brand_fit": 90,
    "confidence": 90,
}


def _candidate(*, title, start, end, excerpt, evidence):
    return {
        "title": title,
        "start_seconds": start,
        "end_seconds": end,
        "start_timestamp": seconds_to_timestamp(start),
        "end_timestamp": seconds_to_timestamp(end),
        "duration_seconds": end - start,
        "concise_summary": "Fixture candidate",
        "selection_reason": "Fixture selection",
        "primary_pillar": "explanation_education",
        "secondary_pillars": [],
        "tags": [],
        "transcript_excerpt": excerpt,
        "visual_description": "Visible process footage.",
        "transcript_evidence": evidence,
        "visual_evidence": [],
        "contextual_notes": "",
        "estimated_short_count": 1,
        "possible_hooks": [],
        "editing_notes": [],
        "risks": [],
        "scores": SCORES,
        "emergent_observations": {},
    }


def _response(candidate):
    return CandidateResponse.model_validate(
        {
            "schema_version": "1.0",
            "stream_id": "stream_fixture",
            "source_video_id": "video_fixture",
            "candidates": [candidate],
        }
    )


def _transcript(tmp_path, events):
    raw = tmp_path / "fixture.en-orig.json3"
    raw.write_text(json.dumps({"events": events}), encoding="utf-8")
    return SimpleNamespace(raw_location=str(raw))


def _event(seconds, text, duration_ms=3000):
    return {"tStartMs": seconds * 1000, "dDurationMs": duration_ms, "segs": [{"utf8": text}]}


def _audit_candidate(candidate_payload, *, model="native-youtube-structured-v1"):
    return SimpleNamespace(
        candidate_window_id="candidate_fixture",
        stream_id="stream_fixture",
        analysis_run_id="run_fixture",
        analysis_run=SimpleNamespace(model=model, prompt_version="1.0"),
        **candidate_payload,
    )


def test_known_good_long_quote_passes_when_evidence_timestamp_marks_quote_start(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            _event(2870, "this right here you guys it honest to God there was a point where I was honestly I was suicidal"),
            _event(2883, "what kept me going is knowing that people gave me money and they needed their art"),
            _event(2893, "every time I'd get close to finishing a piece somebody else would hit me up and order another"),
            _event(2898, "Obviously I'm still supposed to be here and do this thing"),
        ],
    )
    quote = (
        "this right here, you guys, it it honest to God, there was a point where I was uh honestly, I was suicidal. "
        "And what kept me going is knowing that people gave me money and they needed their art. "
        "And what's crazy is every time I'd get close to finishing a piece, somebody else would hit me up and order another. "
        "Obviously, I'm still supposed to be here and do this thing."
    )
    candidate = _candidate(
        title="Handling Suicidal Depression",
        start=2870,
        end=2900,
        excerpt=quote,
        evidence=[{"timestamp": seconds_to_timestamp(2870), "seconds": 2870, "text": quote}],
    )

    response = _response(candidate)
    assert validate_candidate_transcript_evidence(response, transcript) is response


def test_new_candidate_with_spoken_excerpt_but_no_timed_evidence_is_rejected(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            _event(135, "So I soak them with baking soda and borax in it to carry the current through the wood"),
            _event(396, "this is extremely dangerous"),
            _event(403, "this is 10,000 V of electricity, 500 mA"),
            _event(417, "Don't touch it. If you touch it, you're going to have a bad time and never wake up again"),
        ],
    )
    candidate = _candidate(
        title="Fractal Burning: The Process and Safety Warning",
        start=18,
        end=360,
        excerpt=(
            "So I soak them with baking soda, with water that has baking soda and borax in it to carry the current "
            "through the wood. This is 10,000 volts of electricity, 500 milliamps. Don't touch it. If you touch it, "
            "you're going to have a bad time and never wake up again."
        ),
        evidence=[],
    )

    with pytest.raises(CandidateEvidenceValidationError, match="transcript_evidence is empty"):
        validate_candidate_transcript_evidence(_response(candidate), transcript)


def test_declared_transcript_evidence_must_be_supported_somewhere_inside_candidate_window(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            _event(135, "So I soak them with baking soda and borax to carry the current through the wood"),
            _event(403, "This is 10,000 V of electricity, 500 mA"),
        ],
    )
    candidate = _candidate(
        title="Fractal Burning: The Process and Safety Warning",
        start=18,
        end=360,
        excerpt="This is 10,000 volts of electricity, 500 milliamps.",
        evidence=[
            {
                "timestamp": seconds_to_timestamp(300),
                "seconds": 300,
                "text": "This is 10,000 volts of electricity, 500 milliamps",
            }
        ],
    )

    with pytest.raises(CandidateEvidenceValidationError, match="not supported by stored captions"):
        validate_candidate_transcript_evidence(_response(candidate), transcript)


def test_legacy_candidate_without_timed_evidence_passes_if_entire_excerpt_is_in_window(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            _event(2876, "honestly I was suicidal"),
            _event(2885, "knowing that people gave me money and they needed their art"),
        ],
    )
    payload = _candidate(
        title="Legacy supported quote",
        start=2870,
        end=2900,
        excerpt="honestly I was suicidal. knowing that people gave me money and they needed their art.",
        evidence=[],
    )

    audit = audit_candidate_window(_audit_candidate(payload), transcript)
    assert audit.status == "pass"
    assert audit.issues == ()


def test_legacy_candidate_without_timed_evidence_fails_when_excerpt_mixes_windows(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            _event(135, "So I soak them with baking soda and borax to carry the current through the wood"),
            _event(403, "This is 10,000 V of electricity, 500 mA"),
            _event(417, "Don't touch it or you're going to have a bad time and never wake up again"),
        ],
    )
    payload = _candidate(
        title="Legacy mixed-window quote",
        start=18,
        end=360,
        excerpt=(
            "So I soak them with baking soda and borax to carry the current through the wood. "
            "This is 10,000 volts of electricity, 500 milliamps. "
            "Don't touch it or you're going to have a bad time and never wake up again."
        ),
        evidence=[],
    )

    audit = audit_candidate_window(_audit_candidate(payload, model="gemini-3.1-flash-lite"), transcript)
    assert audit.status == "fail"
    assert any("mixes in-window and unsupported speech" in issue for issue in audit.issues)


def test_legacy_candidate_without_timed_evidence_is_unverifiable_if_excerpt_is_only_unmatched_summary(tmp_path):
    transcript = _transcript(tmp_path, [_event(60, "the literal captions say something else here")])
    payload = _candidate(
        title="Legacy paraphrase",
        start=30,
        end=90,
        excerpt="Nate generally explains the history of the project and why it matters to him.",
        evidence=[],
    )

    audit = audit_candidate_window(_audit_candidate(payload, model="gemini-3.1-flash-lite"), transcript)
    assert audit.status == "unverifiable"


def test_visual_only_candidate_does_not_require_transcript_evidence(tmp_path):
    transcript = _transcript(tmp_path, [_event(60, "some unrelated speech")])
    candidate = _candidate(
        title="Silent close-up process",
        start=30,
        end=90,
        excerpt="No verified in-window transcript evidence",
        evidence=[],
    )

    response = _response(candidate)
    assert validate_candidate_transcript_evidence(response, transcript) is response


def test_missing_timestamped_transcript_does_not_block_candidate():
    candidate = _candidate(
        title="Uncaptioned source",
        start=30,
        end=90,
        excerpt="spoken line heard in the video",
        evidence=[],
    )
    response = _response(candidate)

    assert validate_candidate_transcript_evidence(response, None) is response
