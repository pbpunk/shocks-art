from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.ytdlp import (
    SOURCE_EXTRACTOR_ARGS,
    SOURCE_FORMATS,
    YtDlpError,
    download_youtube_source,
    fetch_youtube_auto_captions,
)


def test_download_source_uses_proven_primary_policy_and_reports_progress(tmp_path, monkeypatch):
    commands = []
    progress = []

    monkeypatch.setattr("app.services.ytdlp.shutil.which", lambda name: "yt-dlp.exe")

    class FakeProcess:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.command = command
            self.stdout = ["[download]  50.0% of 10.00MiB at 1.00MiB/s\n"]

        def wait(self):
            template = Path(self.command[self.command.index("-o") + 1])
            output = Path(str(template).replace("%(ext)s", "mp4"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"source-video")
            return 0

    monkeypatch.setattr("app.services.ytdlp.subprocess.Popen", FakeProcess)

    output = download_youtube_source(
        url="https://www.youtube.com/watch?v=abc123",
        output_template=tmp_path / "source.%(ext)s",
        expected_path=tmp_path / "source.mp4",
        progress_callback=progress.append,
        label="fixture",
    )

    assert output == tmp_path / "source.mp4"
    assert output.read_bytes() == b"source-video"
    assert progress == [50.0]
    assert len(commands) == 1
    command = commands[0]
    assert command[0] == "yt-dlp.exe"
    assert "--no-playlist" in command
    assert command[command.index("--extractor-args") + 1] == SOURCE_EXTRACTOR_ARGS
    assert command[command.index("-f") + 1] == SOURCE_FORMATS[0]
    assert command[command.index("--merge-output-format") + 1] == "mp4"


def test_download_source_falls_back_to_second_proven_format(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr("app.services.ytdlp.shutil.which", lambda name: "yt-dlp.exe")

    class FakeProcess:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.command = command
            self.stdout = ["first failed\n"] if len(commands) == 1 else []

        def wait(self):
            if len(commands) == 1:
                return 1
            template = Path(self.command[self.command.index("-o") + 1])
            Path(str(template).replace("%(ext)s", "mp4")).write_bytes(b"fallback-video")
            return 0

    monkeypatch.setattr("app.services.ytdlp.subprocess.Popen", FakeProcess)

    output = download_youtube_source(
        url="https://www.youtube.com/watch?v=abc123",
        output_template=tmp_path / "source.%(ext)s",
        expected_path=tmp_path / "source.mp4",
    )

    assert output.read_bytes() == b"fallback-video"
    assert [command[command.index("-f") + 1] for command in commands] == list(SOURCE_FORMATS)


def test_download_source_fails_clearly_when_ytdlp_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ytdlp.shutil.which", lambda name: None)

    with pytest.raises(YtDlpError, match="not available on PATH"):
        download_youtube_source(
            url="https://www.youtube.com/watch?v=abc123",
            output_template=tmp_path / "source.%(ext)s",
            expected_path=tmp_path / "source.mp4",
        )


def test_caption_fetch_uses_shared_ytdlp_adapter_without_source_download(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr("app.services.ytdlp.shutil.which", lambda name: "yt-dlp.exe")

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.services.ytdlp.subprocess.run", fake_run)

    ok = fetch_youtube_auto_captions(
        url="https://www.youtube.com/watch?v=abc123",
        output_template=tmp_path / "%(id)s.%(ext)s",
    )

    assert ok is True
    assert len(commands) == 1
    command = commands[0]
    assert command[0] == "yt-dlp.exe"
    assert "--skip-download" in command
    assert "--write-auto-subs" in command
    assert "--merge-output-format" not in command


def test_ytdlp_command_ownership_is_centralized():
    command_markers = (
        "--extractor-args",
        "--merge-output-format",
        "--write-auto-subs",
    )
    owners = []
    for path in Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in command_markers):
            owners.append(path.as_posix())

    assert owners == ["app/services/ytdlp.py"]

    clip_text = Path("app/services/clip_download.py").read_text(encoding="utf-8")
    library_text = Path("app/indexing/media_retrieval.py").read_text(encoding="utf-8")
    archive_text = Path("app/services/stream_archive.py").read_text(encoding="utf-8")

    assert "run_ytdlp_command" not in clip_text
    assert "YTDLP_FORMATS" not in clip_text
    assert "bv*+ba/b" not in library_text
    assert "subprocess" not in library_text
    assert '"yt-dlp"' not in archive_text
    assert "subprocess" not in archive_text
