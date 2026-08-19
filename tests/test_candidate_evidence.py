import json
from types import SimpleNamespace

import pytest

from app.schemas.candidate import CandidateResponse, seconds_to_timestamp
from app.services.candidate_evidence import (
    CandidateEvidenceValidationError,
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


def test_known_good_suicidal_depression_candidate_passes_temporal_evidence_gate(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            _event(2876, "honestly, I was suicidal"),
            _event(2885, "knowing that people gave me money and they needed their art"),
            _event(2898, "Obviously, I'm still supposed to be here and do this thing"),
        ],
    )
    candidate = _candidate(
        title="Handling Suicidal Depression",
        start=2870,
        end=2900,
        excerpt=(
            "honestly, I was suicidal. knowing that people gave me money and they needed their art. "
            "Obviously, I'm still supposed to be here and do this thing."
        ),
        evidence=[
            {"timestamp": seconds_to_timestamp(2876), "seconds": 2876, "text": "honestly, I was suicidal"},
            {
                "timestamp": seconds_to_timestamp(2885),
                "seconds": 2885,
                "text": "knowing that people gave me money and they needed their art",
            },
            {
                "timestamp": seconds_to_timestamp(2898),
                "seconds": 2898,
                "text": "Obviously, I'm still supposed to be here",
            },
        ],
    )

    response = _response(candidate)
    assert validate_candidate_transcript_evidence(response, transcript) is response


def test_known_bad_fractal_burning_candidate_is_rejected_when_excerpt_has_no_timed_evidence(tmp_path):
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


def test_declared_transcript_evidence_must_match_captions_near_its_in_window_timestamp(tmp_path):
    transcript = _transcript(
        tmp_path,
        [_event(135, "So I soak them with baking soda and borax to carry the current through the wood")],
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
