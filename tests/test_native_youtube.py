from app.models import Stream
from app.services.native_youtube import (
    build_native_youtube_prompt,
    parse_native_youtube_response,
    save_native_youtube_response,
)


NATIVE_RESPONSE = """
As an assistant editor, I have identified three distinct, high-quality segments from this stream suitable for repurposing into short-form content.

1. Turning Pain into Purpose: A Journey of Recovery (40:35 - 47:05)
Rank: 1
Duration: 6m 30s
Primary Pillar: Personal journey and recovery
Summary: Nate opens up about his past struggles with addiction, the turning point when he was locked up, and how art saved his life.
Selection Reason: This segment contains the most compelling emotional arc of the stream.
Tags: #Recovery, #AddictionAwareness, #SecondChances, #ArtistStory
Transcript Evidence: "I had lost 85 lbs in 4 and a half months..." (41:03-41:46)
Visual Evidence: Nate is talking directly to the camera; his emotional intensity and sincerity are visible in his expressions. (40:35-47:05)
Estimated Short Count: 2-3
Possible Hooks: "I lost everything, and then I found art." / "The day I decided to finally get clean."
Editing Potential: 95
Component Scores: Pillar: 100, Hook: 95, Clarity: 98, Visuals: 90, Audio: 95, Impact: 100, Education: 80, Entertainment: 85, Potential: 95, Brand: 95, Confidence: 100

2. The Art of the 'Challe' and Creative Hustle (31:20 - 36:10)
Rank: 2
Duration: 4m 50s
Primary Pillar: Artistic process
Summary: Nate showcases his workshop items and explains his workflow.
Selection Reason: This segment highlights the professional side of his business and craft techniques.
Tags: #Woodworking, #EpoxyArt, #SmallBusiness, #ProcessVideo
Transcript Evidence: "This is purple heart. This is maple. Spalted maple." (31:52-32:19)
Visual Evidence: Close-up shots of his hands working on wood and epoxy pieces. (31:20-36:10)
Estimated Short Count: 2
Possible Hooks: "How I make my art glow in the dark." / "Behind the scenes of my favorite project."
Editing Potential: 90
Component Scores: Pillar: 95, Hook: 85, Clarity: 90, Visuals: 95, Audio: 90, Impact: 70, Education: 95, Entertainment: 80, Potential: 90, Brand: 100, Confidence: 95
"""


def test_build_native_prompt_includes_review_contract(db_session):
    stream = make_stream(db_session)

    prompt = build_native_youtube_prompt(stream)

    assert "timestamped review guide" in prompt
    assert "complete thought" in prompt
    assert "setup, development, and resolution" in prompt
    assert "dangling allusion" in prompt
    assert "Exact Caption Quote" in prompt
    assert "Chatter Risk" in prompt
    assert "90-100: obvious keeper" in prompt
    assert stream.url in prompt
    assert "Transcript Evidence" in prompt
    assert "Completeness Check" in prompt


def test_parse_native_youtube_response(db_session):
    stream = make_stream(db_session)

    response = parse_native_youtube_response(NATIVE_RESPONSE, stream)

    assert len(response.candidates) == 2
    first = response.candidates[0]
    assert first.title == "Turning Pain into Purpose: A Journey of Recovery"
    assert first.start_seconds == 2435
    assert first.end_seconds == 2825
    assert first.primary_pillar == "personal_journey_recovery"
    assert first.scores.confidence == 84
    assert "recovery" in first.tags
    assert first.transcript_evidence[0].seconds == 2463


def test_parse_native_youtube_response_ignores_echoed_prompt_example(db_session):
    stream = make_stream(db_session)
    echoed_response = """
For each section, return this exact format:
1. Section Title (MM:SS - MM:SS)
Rank:
Duration:
Here is the timestamped review guide:
1. Rough Shaping the Staff (00:00 - 05:00)
Rank: 1
Duration: 5:00
Primary Pillar: Craft Process
Summary: Nate introduces the Bloodwood material and starts shaping the staff.
Why It Is Useful: Clearly establishes the project and material.
Tags: Bloodwood, Bandsaw, Crafting
Transcript Evidence: "meet Bloodwood." (00:06)
Visual Evidence: Nate showing the bloodwood to the camera (00:25-00:35)
Estimated Short Count: 2
Possible Opening Lines: "Meet bloodwood."
Usefulness Score: 90
Component Scores: Pillar: 95, Hook: 90, Clarity: 85, Visuals: 80, Audio: 85, Impact: 80, Education: 85, Entertainment: 75, Potential: 90, Brand: 85, Confidence: 95
"""

    response = parse_native_youtube_response(echoed_response, stream)

    assert len(response.candidates) == 1
    assert response.candidates[0].title == "Rough Shaping the Staff"


def test_parse_native_youtube_response_accepts_unnumbered_headings(db_session):
    stream = make_stream(db_session)
    unnumbered_response = """
Here is the timestamped review guide:
Epoxy Project Overview (07:30 - 13:00)
Rank: 1
Duration: 5:30
Primary Pillar: Craft/Demonstration
Summary: The creator explains a custom epoxy frame project.
Why It Is Useful: Clearly showcases process and client-specific choices.
Tags: Epoxy, Custom Art, Crafting
Transcript Evidence: "This is going to be a frame" (07:34-07:47).
Visual Evidence: Close-up of the wooden frame (07:30-08:00).
Estimated Short Count: 2
Possible Opening Lines: "I'll show you what I'm working on."
Usefulness Score: 92
Component Scores: Pillar: 95, Hook: 85, Clarity: 90, Visuals: 90, Audio: 85, Impact: 80, Education: 85, Entertainment: 80, Potential: 85, Brand: 90, Confidence: 95
Shillelagh Finishing Process (13:10 - 20:10)
Rank: 2
Duration: 7:00
Primary Pillar: Technique/Educational
Summary: A focused demonstration of applying finish.
Why It Is Useful: Provides a practical look at finishing.
Tags: Shillelagh, Woodworking
Transcript Evidence: "Ready? Right here." (19:34-19:36).
Visual Evidence: Hands applying oil to the wood grain (14:30-19:50).
Estimated Short Count: 3
Possible Opening Lines: "Ready? Right here."
Usefulness Score: 88
Component Scores: Pillar: 90, Hook: 80, Clarity: 85, Visuals: 95, Audio: 90, Impact: 75, Education: 90, Entertainment: 85, Potential: 80, Brand: 95, Confidence: 90
"""

    response = parse_native_youtube_response(unnumbered_response, stream)

    assert len(response.candidates) == 2
    assert response.candidates[0].title == "Epoxy Project Overview"
    assert response.candidates[1].start_seconds == 790


def test_parse_native_youtube_response_preserves_completeness_check(db_session):
    stream = make_stream(db_session)
    response_text = """
Personal Recovery Lesson (40:35 - 47:05)
Rank: 1
Duration: 6:30
Primary Pillar: Personal journey and recovery
Summary: Nate explains a complete turning point from addiction into recovery.
Why It Is Useful: The section contains the full story and lesson.
Tags: Recovery, ArtistStory
Transcript Evidence: "that is the day that I decided" (41:46).
Visual Evidence: Nate speaking directly to camera (40:35-47:05).
Completeness Check: Begins with the context, develops the struggle, and ends with the decision to get clean.
Window Type: source_window
Chatter Risk: low
Exact Caption Quote: that is the day that I decided
Estimated Short Count: 2
Possible Opening Lines: "The day I decided to get clean."
Usefulness Score: 95
Component Scores: Pillar: 100, Hook: 95, Clarity: 95, Visuals: 90, Audio: 90, Impact: 100, Education: 80, Entertainment: 80, Potential: 95, Brand: 95, Confidence: 95
"""

    parsed = parse_native_youtube_response(response_text, stream)
    saved = save_native_youtube_response(db_session, stream, response_text)

    assert "ends with the decision" in parsed.candidates[0].contextual_notes
    assert "Exact caption quote" in parsed.candidates[0].contextual_notes
    assert "Completeness check" in saved.candidates[0].contextual_notes
    assert saved.candidates[0].emergent_observations["window_type"] == "source_window"
    assert saved.candidates[0].risks == []


def test_parse_native_youtube_response_calibrates_risky_scores(db_session):
    stream = make_stream(db_session)
    response_text = """
Loose Personal Aside (30:20 - 31:10)
Rank: 1
Duration: 0:50
Primary Pillar: Personal journey and recovery
Summary: Nate briefly mentions past hardship.
Why It Is Useful: It may connect to a larger personal story.
Tags: Recovery, ArtistStory
Transcript Evidence: "I used to be homeless..." (30:28-30:49).
Visual Evidence: Nate working near the dust collector (30:20-31:10).
Completeness Check: The section begins with a reference to hardship but does not fully resolve the story.
Window Type: needs_trim
Chatter Risk: high
Exact Caption Quote:
Estimated Short Count: 1
Possible Opening Lines: "I used to be homeless."
Usefulness Score: 96
Component Scores: Pillar: 96, Hook: 96, Clarity: 96, Visuals: 96, Audio: 96, Impact: 96, Education: 96, Entertainment: 96, Potential: 96, Brand: 96, Confidence: 96
"""

    saved = save_native_youtube_response(db_session, stream, response_text)
    candidate = saved.candidates[0]

    assert candidate.confidence <= 76
    assert candidate.scores["editing_potential"] <= 72
    assert "needs_trim" in candidate.risks
    assert "high_chatter_risk" in candidate.risks
    assert "missing_exact_caption_quote" in candidate.risks


def test_save_native_youtube_response_deduplicates(db_session):
    stream = make_stream(db_session)

    first = save_native_youtube_response(db_session, stream, NATIVE_RESPONSE)
    second = save_native_youtube_response(db_session, stream, NATIVE_RESPONSE)
    db_session.commit()

    assert len(first.candidates) == 2
    assert len(second.candidates) == 0
    assert second.skipped_duplicates == 2
    assert first.run.model == "native-youtube-gemini-sidebar"
    assert first.run.raw_response_location.endswith(".txt")


def test_native_prompt_and_import_api(client, db_session):
    stream = make_stream(db_session)
    db_session.commit()

    prompt_response = client.get(f"/api/streams/{stream.stream_id}/native-prompt")
    assert prompt_response.status_code == 200
    assert prompt_response.json()["url"] == stream.url

    import_response = client.post(
        "/api/native/import",
        data={
            "stream_id": stream.stream_id,
            "source": "native-youtube-gemini-sidebar-test",
            "response_text": NATIVE_RESPONSE,
        },
    )

    assert import_response.status_code == 200
    body = import_response.json()
    assert len(body["candidate_window_ids"]) == 2
    assert body["skipped_duplicates"] == 0


def make_stream(db_session):
    stream = Stream(
        platform="youtube",
        channel_id="fixture_channel",
        source_video_id="E9F-vEbmZpg",
        title="gluing a celebrity's sign and answering questions",
        description="Fixture native YouTube Ask stream.",
        url="https://www.youtube.com/watch?v=E9F-vEbmZpg",
        published_at="2026-07-31T12:00:00Z",
        duration=3600,
        thumbnail="",
        processing_status="queued",
        schema_version="1.0",
    )
    db_session.add(stream)
    db_session.flush()
    return stream
