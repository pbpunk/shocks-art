from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Iterator, Protocol

from app.core.config import ROOT_DIR, get_settings
from app.library_models import Media


class MediaRetrievalError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterializedMedia:
    path: Path
    temporary: bool
    source_type: str
    size_bytes: int


class MediaRetriever(Protocol):
    def materialize(self, media: Media) -> ContextManager[MaterializedMedia]: ...


class LocalMediaRetriever:
    @contextmanager
    def materialize(self, media: Media) -> Iterator[MaterializedMedia]:
        path = Path(media.source_path)
        if not path.is_file():
            raise MediaRetrievalError(f"local Media source is unavailable: {path.name or media.media_id}")
        yield MaterializedMedia(
            path=path,
            temporary=False,
            source_type=media.source_type,
            size_bytes=path.stat().st_size,
        )


class YtDlpMediaRetriever:
    """Download one remote Media item into a disposable per-job scratch lease."""

    def __init__(self, scratch_root: Path | None = None):
        settings = get_settings()
        self.scratch_root = Path(scratch_root or settings.library_scratch_path)

    def _command(self, media: Media, output_template: Path) -> list[str]:
        executable = shutil.which("yt-dlp")
        if not executable:
            raise MediaRetrievalError("yt-dlp executable is not available on PATH")
        if not media.source_url:
            raise MediaRetrievalError(f"remote Media has no source URL: {media.media_id}")
        return [
            executable,
            "--no-playlist",
            "-f",
            "bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "-o",
            str(output_template),
            media.source_url,
        ]

    @contextmanager
    def materialize(self, media: Media) -> Iterator[MaterializedMedia]:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{media.media_id}-", dir=self.scratch_root))
        try:
            output_template = temp_dir / "source.%(ext)s"
            command = self._command(media, output_template)
            completed = subprocess.run(
                command,
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise MediaRetrievalError(f"yt-dlp materialization failed: {detail[-1200:]}")

            candidates = sorted(
                path
                for path in temp_dir.iterdir()
                if path.is_file() and not path.name.endswith((".part", ".ytdl"))
            )
            if not candidates:
                raise MediaRetrievalError("yt-dlp did not produce a source media file")
            path = max(candidates, key=lambda item: item.stat().st_size)
            yield MaterializedMedia(
                path=path,
                temporary=True,
                source_type=media.source_type,
                size_bytes=path.stat().st_size,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class DefaultMediaRetriever:
    """Resolve durable Media metadata to bytes only for the lifetime of one job."""

    def __init__(self, scratch_root: Path | None = None):
        self.local = LocalMediaRetriever()
        self.youtube = YtDlpMediaRetriever(scratch_root=scratch_root)

    @contextmanager
    def materialize(self, media: Media) -> Iterator[MaterializedMedia]:
        if media.source_type == "local":
            with self.local.materialize(media) as materialized:
                yield materialized
            return
        if media.source_type == "youtube":
            with self.youtube.materialize(media) as materialized:
                yield materialized
            return
        raise MediaRetrievalError(f"unsupported Media source_type: {media.source_type!r}")
