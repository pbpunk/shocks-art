import pytest

from app.schemas.candidate import seconds_to_timestamp
from app.services.validation import CandidateValidationError, validate_candidate_response


def test_schema_validation_accepts_valid_response(valid_candidate_data):
    response = validate_candidate_response(valid_candidate_data)
    assert response.candidate.primary_pillar == "mistakes_problem_solving"


def test_timestamp_validation_rejects_inconsistent_duration(valid_candidate_data):
    valid_candidate_data["candidate"]["duration_seconds"] = 999
    with pytest.raises(CandidateValidationError) as exc:
        validate_candidate_response(valid_candidate_data)
    assert "duration_seconds" in exc.value.errors[0]


def test_evidence_must_be_inside_candidate_window(valid_candidate_data):
    valid_candidate_data["candidate"]["transcript_evidence"] = [
        {"timestamp": "00:01:00", "seconds": 60, "text": "Outside the selected window."}
    ]
    with pytest.raises(CandidateValidationError) as exc:
        validate_candidate_response(valid_candidate_data)
    assert "evidence seconds must fall inside candidate window" in exc.value.errors[0]


def test_seconds_to_timestamp():
    assert seconds_to_timestamp(3671) == "01:01:11"
