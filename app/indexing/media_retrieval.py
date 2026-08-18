from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Iterator, Protocol

from app.core.config import get_settings
from app.library_models import Media
from app.services.ytdlp import YtDlpError, download_youtube_source


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
    """Lease one remote Media item through the shared YouTube source fetcher."""

    def __init__(self, scratch_root: Path | None = None):
        settings = get_settings()
        self.scratch_root = Path(scratch_root or settings.library_scratch_path)

    @contextmanager
    def materialize(self, media: Media) -> Iterator[MaterializedMedia]:
        if not media.source_url:
            raise MediaRetrievalError(f"remote Media has no source URL: {media.media_id}")

        self.scratch_root.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{media.media_id}-", dir=self.scratch_root))
        try:
            try:
                path = download_youtube_source(
                    url=media.source_url,
                    output_template=temp_dir / "source.%(ext)s",
                    expected_path=temp_dir / "source.mp4",
                    label=f"Media {media.media_id}",
                )
            except YtDlpError as exc:
                raise MediaRetrievalError(f"yt-dlp materialization failed: {exc}") from exc

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
