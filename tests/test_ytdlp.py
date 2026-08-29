from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.ytdlp import (
    SOURCE_EXTRACTOR_ARGS,
    SOURCE_FORMATS,
    YtDlpError,
    browser_cookie_args,
    download_youtube_section,
    download_youtube_source,
    fetch_youtube_auto_captions,
    fetch_youtube_metadata,
)


def test_download_source_uses_proven_primary_policy_and_reports_progress(tmp_path, monkeypatch):
    commands = []
    progress = []

    monkeypatch.setenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", "")
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
    assert "--cookies-from-browser" not in command


def test_download_source_falls_back_to_second_proven_format(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", "")
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


def test_download_source_uses_workstation_browser_auth_for_private_capable_path(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", "chrome:Default")
    monkeypatch.setattr("app.services.ytdlp.shutil.which", lambda name: "yt-dlp.exe")

    class FakeProcess:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.command = command
            self.stdout = []

        def wait(self):
            template = Path(self.command[self.command.index("-o") + 1])
            Path(str(template).replace("%(ext)s", "mp4")).write_bytes(b"private-source")
            return 0

    monkeypatch.setattr("app.services.ytdlp.subprocess.Popen", FakeProcess)

    output = download_youtube_source(
        url="https://www.youtube.com/watch?v=private123",
        output_template=tmp_path / "source.%(ext)s",
        expected_path=tmp_path / "source.mp4",
    )

    assert output.read_bytes() == b"private-source"
    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("--cookies-from-browser") + 1] == "chrome:Default"
    assert "--extractor-args" not in command


def test_download_source_falls_back_to_anonymous_public_policy_when_browser_auth_fails(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", "chrome")
    monkeypatch.setattr("app.services.ytdlp.shutil.which", lambda name: "yt-dlp.exe")

    class FakeProcess:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.command = command
            self.stdout = ["browser cookie read failed\n"] if len(commands) <= 2 else []

        def wait(self):
            if len(commands) <= 2:
                return 1
            template = Path(self.command[self.command.index("-o") + 1])
            Path(str(template).replace("%(ext)s", "mp4")).write_bytes(b"public-fallback")
            return 0

    monkeypatch.setattr("app.services.ytdlp.subprocess.Popen", FakeProcess)

    output = download_youtube_source(
        url="https://www.youtube.com/watch?v=public123",
        output_template=tmp_path / "source.%(ext)s",
        expected_path=tmp_path / "source.mp4",
    )

    assert output.read_bytes() == b"public-fallback"
    assert len(commands) == 3
    assert all("--cookies-from-browser" in command for command in commands[:2])
    assert "--cookies-from-browser" not in commands[2]
    assert commands[2][commands[2].index("--extractor-args") + 1] == SOURCE_EXTRACTOR_ARGS


def test_browser_cookie_args_defaults_to_chrome_and_explicit_blank_disables(monkeypatch):
    monkeypatch.delenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", raising=False)
    assert browser_cookie_args() == ("--cookies-from-browser", "chrome")

    monkeypatch.setenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", "")
    assert browser_cookie_args() == ()


def test_download_source_fails_clearly_when_ytdlp_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ytdlp.shutil.which", lambda name: None)

    with pytest.raises(YtDlpError, match="not available on PATH"):
        download_youtube_source(
            url="https://www.youtube.com/watch?v=abc123",
            output_template=tmp_path / "source.%(ext)s",
            expected_path=tmp_path / "source.mp4",
        )


def test_metadata_and_section_use_shared_browser_auth(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", "chrome")
    monkeypatch.setattr("app.services.ytdlp.shutil.which", lambda name: "yt-dlp.exe")

    def fake_run(command, **kwargs):
        commands.append(command)
        if "--dump-single-json" in command:
            return SimpleNamespace(returncode=0, stdout='{"id":"private123","duration":42}', stderr="")
        template = Path(command[command.index("-o") + 1])
        Path(str(template).replace("%(ext)s", "mp4")).write_bytes(b"section")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.services.ytdlp.subprocess.run", fake_run)

    metadata = fetch_youtube_metadata(url="https://www.youtube.com/watch?v=private123")
    section = download_youtube_section(
        url="https://www.youtube.com/watch?v=private123",
        output_template=tmp_path / "partial.%(ext)s",
        expected_path=tmp_path / "partial.mp4",
    )

    assert metadata["id"] == "private123"
    assert section.read_bytes() == b"section"
    assert len(commands) == 2
    assert all(command[command.index("--cookies-from-browser") + 1] == "chrome" for command in commands)
    assert "--download-sections" in commands[1]
    assert commands[1][commands[1].index("--download-sections") + 1] == "*0-30"


def test_caption_fetch_uses_shared_ytdlp_adapter_without_source_download(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setenv("SHOCKS_YTDLP_COOKIES_FROM_BROWSER", "")
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
        "--cookies-from-browser",
        "--download-sections",
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
