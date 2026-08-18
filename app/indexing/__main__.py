from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.indexing.embedding_service import index_visual_trace_embeddings
from app.indexing.evaluation import evaluate_visual_search, load_evaluation_spec
from app.indexing.language_traces import import_existing_stream_transcript
from app.indexing.qwen_backend import QwenSubprocessEmbeddingBackend
from app.indexing.qwen_runtime import inspect_qwen_runtime
from app.indexing.refinement import RefinementConfig, refine_visual_trace
from app.indexing.service import VisualExtractionConfig, index_all_visual_media, index_visual_media
from app.indexing.visual_search import search_visual_embeddings
from app.library_models import Embedding, IndexRun, Media, Trace  # noqa: F401 - registers indexing tables


DEFAULT_VISUAL_EVALUATION_SPEC = Path("config/visual-search-evaluation.json")


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

    search_parser = subparsers.add_parser(
        "search-visual",
        help="Embed a text query with Qwen and brute-force cosine search persisted visual vectors",
    )
    search_parser.add_argument("query")
    search_parser.add_argument("--top-k", type=int, default=10)

    refinement_parser = subparsers.add_parser(
        "refine-visual",
        help="Densely rescan a bounded local window around one coarse video Trace",
    )
    refinement_parser.add_argument("trace_id", help="Coarse visual Trace ID to refine")
    refinement_parser.add_argument("query", help="Semantic text query used to rerank dense local frames")
    refinement_parser.add_argument("--radius", type=float, default=None, help="Optional explicit radius in seconds")
    refinement_parser.add_argument("--step", type=float, default=None, help="Optional explicit dense step in seconds")
    refinement_parser.add_argument("--max-samples", type=int, default=31)
    refinement_parser.add_argument("--top-k", type=int, default=10)
    refinement_parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional ignored directory that preserves the returned top review frames",
    )

    language_parser = subparsers.add_parser(
        "import-language",
        help="Convert an existing StreamTranscript raw JSON3 caption artifact into Language Traces",
    )
    language_parser.add_argument("media_id", help="Media record the transcript evidence belongs to")
    language_parser.add_argument("stream_id", help="Existing Stream whose StreamTranscript should be reused")

    evaluation_parser = subparsers.add_parser(
        "evaluate-visual",
        help="Run the blind multi-query/multi-dimension semantic visual evaluation bundle",
    )
    evaluation_parser.add_argument(
        "--spec",
        default=str(DEFAULT_VISUAL_EVALUATION_SPEC),
        help="Evaluation query/dimension JSON specification",
    )
    evaluation_parser.add_argument(
        "--output",
        default=None,
        help="Optional path to also write the complete JSON result",
    )

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
                "languageTraces": db.scalar(
                    select(func.count()).select_from(Trace).where(Trace.trace_type == "language")
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

        if args.command == "search-visual":
            try:
                backend = QwenSubprocessEmbeddingBackend()
                embedding_started = time.perf_counter()
                query_vector = backend.embed_text([args.query])[0]
                query_embedding_ms = (time.perf_counter() - embedding_started) * 1000.0
                result = search_visual_embeddings(
                    db,
                    query_vector=query_vector,
                    model_id=backend.model_id,
                    dimension=backend.dimension,
                    top_k=args.top_k,
                )
            except Exception as exc:
                print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
                return 1
            print(
                json.dumps(
                    {
                        "ok": True,
                        "query": args.query,
                        "queryEmbeddingMs": round(query_embedding_ms, 4),
                        "result": result.as_dict(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "refine-visual":
            try:
                backend = QwenSubprocessEmbeddingBackend()
                config = RefinementConfig(
                    radius_seconds=args.radius,
                    step_seconds=args.step,
                    max_samples=args.max_samples,
                    top_k=args.top_k,
                )
                result = refine_visual_trace(
                    db,
                    trace_id=args.trace_id,
                    query=args.query,
                    embedding_backend=backend,
                    config=config,
                    output_directory=Path(args.output_dir) if args.output_dir else None,
                )
            except Exception as exc:
                print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
                return 1
            print(json.dumps({"ok": True, "refinement": result.as_dict()}, indent=2, sort_keys=True))
            return 0

        if args.command == "import-language":
            try:
                result = import_existing_stream_transcript(
                    db,
                    media_id=args.media_id,
                    stream_id=args.stream_id,
                )
            except Exception as exc:
                print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
                return 1
            print(json.dumps({"ok": True, "result": result.as_dict()}, indent=2, sort_keys=True))
            return 0

        if args.command == "evaluate-visual":
            try:
                backend = QwenSubprocessEmbeddingBackend()
                spec = load_evaluation_spec(Path(args.spec))
                result = evaluate_visual_search(db, backend=backend, spec=spec)
            except Exception as exc:
                print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
                return 1

            payload = {"ok": True, "evaluation": result}
            rendered = json.dumps(payload, indent=2, sort_keys=True)
            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(rendered + "\n", encoding="utf-8")
            print(rendered)
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
