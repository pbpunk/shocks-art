import json

from app.models import StreamTranscript
from app.schemas.candidate import CandidateResponse, seconds_to_timestamp
from app.services.repository import create_analysis_run, save_candidates, upsert_stream


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


def test_save_candidates_persists_auto_grounded_caption_evidence(db_session, tmp_path):
    stream, _ = upsert_stream(
        db_session,
        {
            "platform": "youtube",
            "channel_id": "channel",
            "source_video_id": "fixture_video",
            "title": "Fixture stream",
            "description": "",
            "url": "https://www.youtube.com/watch?v=fixture_video",
            "published_at": "2026-08-17T00:00:00Z",
            "duration": 1200,
            "thumbnail": "",
            "processing_status": "queued",
            "schema_version": "1.0",
        },
    )
    raw = tmp_path / "fixture.en-orig.json3"
    raw.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "tStartMs": 120000,
                        "dDurationMs": 3000,
                        "segs": [{"utf8": "I soak the wood with baking soda and borax"}],
                    },
                    {
                        "tStartMs": 124000,
                        "dDurationMs": 3000,
                        "segs": [{"utf8": "that helps carry the current through the wood"}],
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
                    "title": "Grounded process explanation",
                    "start_seconds": 100,
                    "end_seconds": 180,
                    "start_timestamp": seconds_to_timestamp(100),
                    "end_timestamp": seconds_to_timestamp(180),
                    "duration_seconds": 80,
                    "concise_summary": "Process explanation.",
                    "selection_reason": "Fixture",
                    "primary_pillar": "explanation_education",
                    "secondary_pillars": [],
                    "tags": [],
                    "transcript_excerpt": "I soak the wood with baking soda and borax. That helps carry the current through the wood.",
                    "visual_description": "Visible process footage.",
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

    saved = save_candidates(db_session, run, response)
    db_session.commit()

    candidate = saved[0]
    assert candidate.transcript_evidence
    assert candidate.transcript_evidence[0]["seconds"] == 120
    assert "baking soda and borax" in candidate.transcript_evidence[0]["text"]
    assert candidate.emergent_observations["_transcript_evidence_grounding"]["version"] == "caption-grounding-v1"
    assert run.status == "complete"
    assert stream.processing_status == "complete"
