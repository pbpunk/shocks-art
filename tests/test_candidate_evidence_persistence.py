import json

import pytest

from app.models import CandidateWindow, StreamTranscript
from app.schemas.candidate import CandidateResponse, seconds_to_timestamp
from app.services.candidate_evidence import CandidateEvidenceValidationError
from app.services.repository import create_analysis_run, save_candidates, upsert_stream


SCORES = {
    "pillar_relevance": 88,
    "hook_strength": 88,
    "standalone_clarity": 88,
    "visual_quality": 88,
    "audio_clarity": 88,
    "emotional_impact": 88,
    "educational_value": 88,
    "entertainment_value": 88,
    "editing_potential": 88,
    "brand_fit": 88,
    "confidence": 88,
}


def test_temporal_evidence_failure_quarantines_run_before_candidate_persistence(db_session, tmp_path):
    stream, _ = upsert_stream(
        db_session,
        {
            "platform": "youtube",
            "channel_id": "channel",
            "source_video_id": "pDC14ymQqWY",
            "title": "Fractal Burning Irish Shillelaghs",
            "description": "",
            "url": "https://www.youtube.com/watch?v=pDC14ymQqWY",
            "published_at": "2026-08-17T00:00:00Z",
            "duration": 2733,
            "thumbnail": "",
            "processing_status": "queued",
            "schema_version": "1.0",
        },
    )
    raw = tmp_path / "pDC14ymQqWY.en-orig.json3"
    raw.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "tStartMs": 135000,
                        "dDurationMs": 4000,
                        "segs": [{"utf8": "baking soda and borax to carry the current through the wood"}],
                    },
                    {
                        "tStartMs": 403000,
                        "dDurationMs": 4000,
                        "segs": [{"utf8": "this is 10,000 V of electricity, 500 mA"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    db_session.add(
        StreamTranscript(
            stream_id=stream.stream_id,
            language="en-orig",
            source="youtube_auto_captions",
            format="plain_text",
            text="fixture",
            raw_location=str(raw),
        )
    )
    run = create_analysis_run(db_session, stream, "gemini-3.1-flash-lite", "1.0", "1.0")
    db_session.flush()

    response = CandidateResponse.model_validate(
        {
            "schema_version": "1.0",
            "stream_id": stream.stream_id,
            "source_video_id": stream.source_video_id,
            "candidates": [
                {
                    "title": "Fractal Burning: The Process and Safety Warning",
                    "start_seconds": 18,
                    "end_seconds": 360,
                    "start_timestamp": seconds_to_timestamp(18),
                    "end_timestamp": seconds_to_timestamp(360),
                    "duration_seconds": 342,
                    "concise_summary": "Combines process explanation and a safety warning.",
                    "selection_reason": "Fixture",
                    "primary_pillar": "explanation_education",
                    "secondary_pillars": [],
                    "tags": [],
                    "transcript_excerpt": "This is 10,000 volts of electricity, 500 milliamps.",
                    "visual_description": "Fractal burning process.",
                    "transcript_evidence": [],
                    "visual_evidence": [],
                    "contextual_notes": "",
                    "estimated_short_count": 1,
                    "possible_hooks": [],
                    "editing_notes": [],
                    "risks": [],
                    "scores": SCORES,
                    "emergent_observations": {},
                }
            ],
        }
    )

    with pytest.raises(CandidateEvidenceValidationError):
        save_candidates(db_session, run, response)

    assert run.status == "quarantined"
    assert stream.processing_status == "quarantined"
    assert run.validation_errors
    assert "transcript_evidence is empty" in run.validation_errors[0]
    assert db_session.query(CandidateWindow).count() == 0
