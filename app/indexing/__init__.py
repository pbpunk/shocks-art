"""Offline/local indexing services for Library Media.

This package intentionally has no FastAPI dependency. Heavy ML backends will be
added lazily in later backlog items so importing the web application never
requires GPU/model packages.
"""

from app.indexing.service import (
    FfmpegFrameBackend,
    FrameExtractionBackend,
    VisualExtractionConfig,
    VisualIndexResult,
    index_all_visual_media,
    index_visual_media,
)

__all__ = [
    "FfmpegFrameBackend",
    "FrameExtractionBackend",
    "VisualExtractionConfig",
    "VisualIndexResult",
    "index_all_visual_media",
    "index_visual_media",
]
