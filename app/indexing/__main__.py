from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.indexing.embedding_service import index_visual_trace_embeddings
from app.indexing.qwen_backend import QwenSubprocessEmbeddingBackend
from app.indexing.qwen_runtime import inspect_qwen_runtime
from app.indexing.service import VisualExtractionConfig, index_all_visual_media, index_visual_media
from app.library_models import Embedding, IndexRun, Media, Trace  # noqa: F401 - registers indexing tables


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shock's Art offline Library indexer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    media_parser = subparsers.add_parser("index-media", help="Extract visual Traces for one Media record")
    media_parser.add_argument("media_id")
    media_parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Optional fixed video sample interval in seconds; omit to use adaptive-v1",
    )
    media_parser.add_argument("--index-root", default=None)

    pending_parser = subparsers.add_parser(
        "index-pending",
        help="Idempotently extract/reuse visual Traces for Library Media",
    )
    pending_parser.add_argument("--limit", type=int, default=None)
    pending_parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Optional fixed video sample interval in seconds; omit to use adaptive-v1",
    )
    pending_parser.add_argument("--index-root", default=None)

    embed_parser = subparsers.add_parser(
        "embed-visual",
        help="Generate/reuse normalized Qwen embeddings for visual Trace artifacts",
    )
    embed_parser.add_argument("--limit", type=int, default=None)
    embed_parser.add_argument("--index-root", default=None)

    subparsers.add_parser("qwen-status", help="Report the pinned isolated Qwen runtime without loading it")
    subparsers.add_parser("status", help="Print machine-readable indexing table counts")
    return parser


def _root_from_args(value: str | None) -> Path:
    return Path(value or get_settings().library_index_path)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Only create missing indexing tables; do not bootstrap/import the FastAPI app.
    Base.metadata.create_all(bind=engine)

    if args.command == "qwen-status":
        try:
            status = inspect_qwen_runtime()
            print(json.dumps(status.as_dict(), indent=2, sort_keys=True))
            return 0 if status.available else 1
        except Exception as exc:
            print(json.dumps({"available": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
            return 1

    with SessionLocal() as db:
        if args.command == "status":
            payload = {
                "media": db.scalar(select(func.count()).select_from(Media)) or 0,
                "traces": db.scalar(select(func.count()).select_from(Trace)) or 0,
                "visualTraces": db.scalar(
                    select(func.count()).select_from(Trace).where(Trace.trace_type == "visual")
                )
                or 0,
                "embeddings": db.scalar(select(func.count()).select_from(Embedding)) or 0,
                "indexRuns": db.scalar(select(func.count()).select_from(IndexRun)) or 0,
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        if args.command == "embed-visual":
            try:
                backend = QwenSubprocessEmbeddingBackend()
                result = index_visual_trace_embeddings(
                    db,
                    index_root=_root_from_args(args.index_root),
                    backend=backend,
                    limit=args.limit,
                )
            except Exception as exc:
                print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
                return 1
            print(json.dumps({"ok": True, "result": result.as_dict()}, indent=2, sort_keys=True))
            return 0

        config = VisualExtractionConfig(sample_interval_seconds=args.interval)
        index_root = _root_from_args(args.index_root)

        if args.command == "index-media":
            media = db.get(Media, args.media_id)
            if media is None:
                print(json.dumps({"ok": False, "error": f"Media not found: {args.media_id}"}, indent=2))
                return 2
            try:
                result = index_visual_media(db, media, index_root=index_root, config=config)
            except Exception as exc:
                print(
                    json.dumps(
                        {"ok": False, "mediaId": media.media_id, "error": f"{type(exc).__name__}: {exc}"},
                        indent=2,
                    )
                )
                return 1
            print(json.dumps({"ok": True, "result": result.as_dict()}, indent=2, sort_keys=True))
            return 0

        try:
            results = index_all_visual_media(
                db,
                index_root=index_root,
                config=config,
                limit=args.limit,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "count": len(results),
                    "samplingPolicy": config.sampling_policy,
                    "results": [result.as_dict() for result in results],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
