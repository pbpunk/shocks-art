from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.indexing.job_queue import IndexJob, IndexJobQueue


DEFAULT_LEASE_SECONDS = 90
DEFAULT_HEARTBEAT_SECONDS = 20
DEFAULT_POLL_SECONDS = 2.0


class LeaseHeartbeat(threading.Thread):
    def __init__(
        self,
        queue: IndexJobQueue,
        owner_id: str,
        *,
        lease_seconds: int,
        interval_seconds: int,
    ) -> None:
        super().__init__(name="indexer-lease-heartbeat", daemon=True)
        self.queue = queue
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stopping = threading.Event()
        self._job_lock = threading.Lock()
        self._job_id = ""
        self.lost_lease = False

    def set_job(self, job_id: str) -> None:
        with self._job_lock:
            self._job_id = str(job_id or "")

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        while not self._stopping.wait(self.interval_seconds):
            with self._job_lock:
                job_id = self._job_id
            try:
                ok = self.queue.heartbeat(
                    self.owner_id,
                    job_id=job_id,
                    ttl_seconds=self.lease_seconds,
                )
            except Exception:
                ok = False
            if not ok:
                self.lost_lease = True
                return


def _owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


def _dispatch(job: IndexJob) -> dict[str, Any]:
    """Execute one fixed indexing job type outside FastAPI.

    Heavy Qwen imports are deliberately inside the relevant branch so the
    worker can service non-embedding jobs without loading the ML runtime.
    """

    Base.metadata.create_all(bind=engine)
    settings = get_settings()

    with SessionLocal() as db:
        if job.job_type == "visual-media":
            from app.indexing.service import VisualExtractionConfig, index_visual_media
            from app.library_models import Media

            media = db.get(Media, job.media_id)
            if media is None:
                raise RuntimeError(f"Media not found: {job.media_id}")
            config = VisualExtractionConfig(
                sample_interval_seconds=job.payload.get("sample_interval_seconds")
            )
            result = index_visual_media(
                db,
                media,
                index_root=Path(settings.library_index_path),
                config=config,
            )
            return result.as_dict()

        if job.job_type == "visual-pending":
            from app.indexing.service import VisualExtractionConfig, index_all_visual_media

            config = VisualExtractionConfig(
                sample_interval_seconds=job.payload.get("sample_interval_seconds")
            )
            results = index_all_visual_media(
                db,
                index_root=Path(settings.library_index_path),
                config=config,
                limit=job.payload.get("limit"),
                include_remote=bool(job.payload.get("include_remote", False)),
            )
            return {
                "count": len(results),
                "results": [result.as_dict() for result in results],
            }

        if job.job_type == "visual-embeddings":
            from app.indexing.embedding_service import index_visual_trace_embeddings
            from app.indexing.qwen_backend import QwenSubprocessEmbeddingBackend

            backend = QwenSubprocessEmbeddingBackend()
            result = index_visual_trace_embeddings(
                db,
                index_root=Path(settings.library_index_path),
                backend=backend,
                limit=job.payload.get("limit"),
            )
            return result.as_dict()

        if job.job_type == "sync-stream-media":
            from app.indexing.stream_media import sync_all_stream_media

            result = sync_all_stream_media(
                db,
                import_language=bool(job.payload.get("import_language", True)),
                limit=job.payload.get("limit"),
            )
            return result.as_dict()

    raise RuntimeError(f"unsupported indexing job type: {job.job_type}")


def run_worker(
    *,
    queue_path: str | Path | None = None,
    once: bool = False,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
) -> int:
    queue = IndexJobQueue(queue_path)
    owner_id = _owner_id()
    if not queue.acquire_worker(owner_id, ttl_seconds=lease_seconds):
        print(json.dumps({"ok": False, "outcome": "already_owned", "ownerId": owner_id}))
        return 3

    heartbeat = LeaseHeartbeat(
        queue,
        owner_id,
        lease_seconds=lease_seconds,
        interval_seconds=heartbeat_seconds,
    )
    heartbeat.start()
    processed = 0
    try:
        queue.recover_stale_jobs()
        while True:
            if heartbeat.lost_lease:
                print(json.dumps({"ok": False, "outcome": "lease_lost", "processed": processed}))
                return 4

            job = queue.claim_next(owner_id, ttl_seconds=lease_seconds)
            if job is None:
                if once:
                    print(json.dumps({"ok": True, "outcome": "idle", "processed": processed}))
                    return 0
                time.sleep(max(0.1, float(poll_seconds)))
                continue

            heartbeat.set_job(job.job_id)
            queue.update_progress(job.job_id, owner_id, {"stage": "starting"})
            try:
                result = _dispatch(job)
            except KeyboardInterrupt:
                queue.fail(job.job_id, owner_id, "worker interrupted", retryable=True)
                raise
            except Exception as exc:
                status = queue.fail(
                    job.job_id,
                    owner_id,
                    f"{type(exc).__name__}: {exc}",
                    retryable=True,
                )
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "jobId": job.job_id,
                            "jobType": job.job_type,
                            "status": status,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                        sort_keys=True,
                    )
                )
            else:
                if heartbeat.lost_lease:
                    print(json.dumps({"ok": False, "outcome": "lease_lost_after_job", "jobId": job.job_id}))
                    return 4
                if not queue.complete(job.job_id, owner_id, result):
                    print(json.dumps({"ok": False, "outcome": "completion_lease_mismatch", "jobId": job.job_id}))
                    return 4
                print(
                    json.dumps(
                        {"ok": True, "jobId": job.job_id, "jobType": job.job_type, "status": "completed"},
                        sort_keys=True,
                    )
                )
            finally:
                heartbeat.set_job("")
            processed += 1
            if once:
                return 0
    except KeyboardInterrupt:
        print(json.dumps({"ok": True, "outcome": "stopped", "processed": processed}))
        return 0
    finally:
        heartbeat.stop()
        heartbeat.join(timeout=max(1.0, float(heartbeat_seconds) + 1.0))
        queue.release_worker(owner_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shock's Art singleton offline indexing worker")
    parser.add_argument("--queue-db", default=None, help="Override persistent queue SQLite path")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job then exit")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--heartbeat-seconds", type=int, default=DEFAULT_HEARTBEAT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.heartbeat_seconds <= 0 or args.heartbeat_seconds >= args.lease_seconds:
        print(json.dumps({"ok": False, "error": "heartbeat must be positive and shorter than the lease"}))
        return 2
    return run_worker(
        queue_path=args.queue_db,
        once=args.once,
        poll_seconds=args.poll_seconds,
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
