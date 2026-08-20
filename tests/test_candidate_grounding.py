import json
from types import SimpleNamespace

import pytest

from app.schemas.candidate import CandidateResponse, seconds_to_timestamp
from app.services.candidate_evidence import CandidateEvidenceValidationError, validate_candidate_transcript_evidence
from app.services.candidate_grounding import ground_missing_transcript_evidence


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


def _candidate(*, start=100, end=180, excerpt, evidence=None):
    return {
        "title": "Grounding fixture",
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
        "transcript_evidence": evidence or [],
        "visual_evidence": [],
        "contextual_notes": "",
        "estimated_short_count": 1,
        "possible_hooks": [],
        "editing_notes": [],
        "risks": [],
        "scores": SCORES,
        "emergent_observations": {"modelNote": "preserve me"},
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


def test_missing_evidence_is_grounded_from_exact_in_window_captions(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            _event(120, "I soak the wood with baking soda and borax"),
            _event(124, "that helps carry the current through the wood"),
        ],
    )
    response = _response(
        _candidate(excerpt="I soak the wood with baking soda and borax. That helps carry the current through the wood.")
    )

    grounded = ground_missing_transcript_evidence(response, transcript)

    assert grounded is response
    evidence = response.candidates[0].transcript_evidence
    assert evidence
    assert all(100 <= item.seconds <= 180 for item in evidence)
    assert all(item.timestamp == seconds_to_timestamp(item.seconds) for item in evidence)
    assert any("baking soda and borax" in item.text for item in evidence)
    assert response.candidates[0].emergent_observations["modelNote"] == "preserve me"
    assert response.candidates[0].emergent_observations["_transcript_evidence_grounding"]["version"] == "caption-grounding-v1"
    assert validate_candidate_transcript_evidence(response, transcript) is response


def test_missing_evidence_rejects_claim_found_only_outside_candidate_window(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            _event(120, "I soak the wood with baking soda and borax"),
            _event(205, "this is ten thousand volts of electricity"),
        ],
    )
    response = _response(
        _candidate(
            start=100,
            end=180,
            excerpt="This is ten thousand volts of electricity.",
        )
    )

    with pytest.raises(CandidateEvidenceValidationError, match="could not be grounded inside the candidate window"):
        ground_missing_transcript_evidence(response, transcript)

    assert response.candidates[0].transcript_evidence == []


def test_grounding_requires_every_excerpt_chunk_to_be_supported(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            _event(120, "I soak the wood with baking soda and borax"),
            _event(205, "don't touch it or you will have a bad time"),
        ],
    )
    response = _response(
        _candidate(
            start=100,
            end=180,
            excerpt="I soak the wood with baking soda and borax. Don't touch it or you will have a bad time.",
        )
    )

    with pytest.raises(CandidateEvidenceValidationError):
        ground_missing_transcript_evidence(response, transcript)

    assert response.candidates[0].transcript_evidence == []


def test_existing_declared_evidence_is_not_replaced(tmp_path):
    transcript = _transcript(tmp_path, [_event(120, "the real caption text")])
    declared = [
        {
            "timestamp": seconds_to_timestamp(130),
            "seconds": 130,
            "text": "model supplied evidence",
        }
    ]
    response = _response(_candidate(excerpt="model supplied evidence", evidence=declared))

    ground_missing_transcript_evidence(response, transcript)

    evidence = response.candidates[0].transcript_evidence
    assert len(evidence) == 1
    assert evidence[0].seconds == 130
    assert evidence[0].text == "model supplied evidence"
    assert "_transcript_evidence_grounding" not in response.candidates[0].emergent_observations


def test_no_timestamped_transcript_leaves_response_unchanged():
    response = _response(_candidate(excerpt="spoken line from an uncaptioned source"))

    assert ground_missing_transcript_evidence(response, None) is response
    assert response.candidates[0].transcript_evidence == []
