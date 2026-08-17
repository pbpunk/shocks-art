from pathlib import Path

from app.models import DerivedAsset
from app.services.clip_download import generate_clip_file, read_progress
from tests.test_ranking_exports_api import create_candidate


def test_clip_download_status_missing(client, db_session, valid_candidate_data):
    candidate = create_candidate(db_session, valid_candidate_data)

    response = client.get(f"/api/clips/{candidate.candidate_window_id}/download-status")

    assert response.status_code == 200
    assert response.json() == {"status": "missing", "progress": 0, "phase": "missing"}


def test_generate_download_queues_processing_asset(client, db_session, valid_candidate_data, tmp_path, monkeypatch):
    candidate = create_candidate(db_session, valid_candidate_data)
    calls = []
    monkeypatch.setattr("app.services.clip_download.DERIVED_CLIP_DIR", tmp_path)

    def fake_background(candidate_id):
        calls.append(candidate_id)

    monkeypatch.setattr("app.main.generate_clip_background", fake_background)

    response = client.post(f"/api/clips/{candidate.candidate_window_id}/generate-download")

    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    assert response.json()["progress"] == 1
    assert calls == [candidate.candidate_window_id]
    asset = db_session.query(DerivedAsset).filter_by(candidate_window_id=candidate.candidate_window_id).one()
    assert asset.asset_type == "source_clip_mp4"
    assert asset.creation_status == "processing"
    assert read_progress(candidate.candidate_window_id)["progress"] == 1


def test_ready_download_returns_mp4(client, db_session, valid_candidate_data, tmp_path, monkeypatch):
    candidate = create_candidate(db_session, valid_candidate_data)
    monkeypatch.setattr("app.services.clip_download.DERIVED_CLIP_DIR", tmp_path)
    path = tmp_path / f"{candidate.candidate_window_id}.mp4"
    path.write_bytes(b"fake mp4")

    response = client.get(f"/api/clips/{candidate.candidate_window_id}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == b"fake mp4"


def test_archived_candidate_download_returns_404(client, db_session, valid_candidate_data):
    candidate = create_candidate(db_session, valid_candidate_data)
    candidate.review_status = "archived"
    db_session.commit()

    response = client.get(f"/api/clips/{candidate.candidate_window_id}/download-status")

    assert response.status_code == 404


def test_generate_clip_file_uses_ytdlp_and_ffmpeg(db_session, valid_candidate_data, tmp_path, monkeypatch):
    candidate = create_candidate(db_session, valid_candidate_data)
    source_dir = tmp_path / "source"
    clip_dir = tmp_path / "clips"
    commands = []

    monkeypatch.setattr("app.services.clip_download.SOURCE_VIDEO_DIR", source_dir)
    monkeypatch.setattr("app.services.clip_download.DERIVED_CLIP_DIR", clip_dir)

    def fake_ytdlp(db, candidate_arg, command):
        commands.append(command)
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / f"{candidate.stream.source_video_id}.mp4").write_bytes(b"source")

    def fake_ffmpeg(db, candidate_arg, command, duration):
        commands.append(command)
        Path(command[-1]).write_bytes(b"clip")

    monkeypatch.setattr("app.services.clip_download.run_ytdlp_command", fake_ytdlp)
    monkeypatch.setattr("app.services.clip_download.run_ffmpeg_command", fake_ffmpeg)

    output = generate_clip_file(db_session, candidate)

    assert output == clip_dir / f"{candidate.candidate_window_id}.mp4"
    assert output.read_bytes() == b"clip"
    assert commands[0][0] == "yt-dlp"
    assert commands[1][0] == "ffmpeg"
    assert str(candidate.start_seconds) in commands[1]


def test_clips_page_renders_generate_download_button(client, db_session, valid_candidate_data):
    create_candidate(db_session, valid_candidate_data)

    response = client.get("/")

    assert response.status_code == 200
    assert "<span>Generate Download</span>" in response.text
    assert "generate-download" in response.text
