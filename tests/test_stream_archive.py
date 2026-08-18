import json

from app.models import AnalysisRun, Stream, StreamAnalysisArtifact
from app.services.stream_archive import ensure_stream_transcript, save_structured_pass_artifacts


def make_stream(db_session):
    stream = Stream(
        platform="youtube",
        channel_id="fixture_channel",
        source_video_id="fixture_video_123",
        title="Fixture Live",
        description="Stored stream description.",
        url="https://www.youtube.com/watch?v=fixture_video_123",
        published_at="2026-07-31T12:00:00Z",
        duration=1200,
        thumbnail="",
        processing_status="queued",
        schema_version="1.0",
    )
    db_session.add(stream)
    db_session.flush()
    return stream


def test_ensure_stream_transcript_persists_caption_text(db_session, tmp_path, monkeypatch):
    stream = make_stream(db_session)
    captions_dir = tmp_path / "captions"
    captions_dir.mkdir()
    caption_path = captions_dir / "fixture_video_123.en-orig.json3"
    caption_path.write_text(
        json.dumps(
            {
                "events": [
                    {"tStartMs": 1000, "segs": [{"utf8": "hello"}, {"utf8": " world"}]},
                    {"tStartMs": 65000, "segs": [{"utf8": "second line"}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.stream_archive.CAPTIONS_DIR", captions_dir)

    transcript = ensure_stream_transcript(db_session, stream)
    duplicate = ensure_stream_transcript(db_session, stream)

    assert transcript is not None
    assert duplicate.stream_transcript_id == transcript.stream_transcript_id
    assert transcript.raw_location == str(caption_path)
    assert "00:01 hello world" in transcript.text
    assert "01:05 second line" in transcript.text


def test_fetch_missing_transcript_uses_shared_ytdlp_adapter(db_session, tmp_path, monkeypatch):
    stream = make_stream(db_session)
    captions_dir = tmp_path / "captions"
    calls = []

    monkeypatch.setattr("app.services.stream_archive.CAPTIONS_DIR", captions_dir)

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        captions_dir.mkdir(parents=True, exist_ok=True)
        (captions_dir / "fixture_video_123.en-orig.json3").write_text(
            json.dumps({"events": [{"tStartMs": 1000, "segs": [{"utf8": "fetched caption"}]}]}),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr("app.services.stream_archive.fetch_youtube_auto_captions", fake_fetch)

    transcript = ensure_stream_transcript(db_session, stream, fetch_missing=True)

    assert transcript is not None
    assert transcript.text == "00:01 fetched caption"
    assert len(calls) == 1
    assert calls[0]["url"] == stream.url
    assert calls[0]["output_template"] == captions_dir / "%(id)s.%(ext)s"


def test_save_structured_pass_artifacts_persists_prompt_and_conversation_files(db_session, tmp_path):
    stream = make_stream(db_session)
    run = AnalysisRun(
        stream_id=stream.stream_id,
        model="native-youtube-structured-v1",
        prompt_version="test",
        schema_version="1.0",
        status="complete",
    )
    db_session.add(run)
    db_session.flush()
    run_dir = tmp_path / "fixture_video_123_20260731_120000"
    run_dir.mkdir()
    (run_dir / "prompts.json").write_text(json.dumps({"outline": "Prompt text"}), encoding="utf-8")
    (run_dir / "outline.txt").write_text("Outline response", encoding="utf-8")
    (run_dir / "final.txt").write_text("Final winners", encoding="utf-8")

    artifacts = save_structured_pass_artifacts(db_session, stream, run, run_dir)

    assert len(artifacts) == 3
    stored = db_session.query(StreamAnalysisArtifact).order_by(StreamAnalysisArtifact.artifact_type).all()
    assert [artifact.artifact_type for artifact in stored] == ["final", "outline", "prompts"]
    assert stored[0].stream_id == stream.stream_id
    assert any("Prompt text" in artifact.text for artifact in stored)
    assert all(artifact.artifact_metadata["structured_pass_dir"] == str(run_dir) for artifact in stored)
