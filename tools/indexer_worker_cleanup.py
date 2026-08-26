from __future__ import annotations

import argparse
from pathlib import Path

from app.indexing.job_queue import DEFAULT_WORKER_KEY, IndexJobQueue, _iso


def clear_dead_owner(pid: int, *, queue_path: str | Path | None = None) -> dict[str, object]:
    if int(pid) <= 0:
        raise ValueError("pid must be positive")
    queue = IndexJobQueue(queue_path)
    marker = f":{int(pid)}:"
    timestamp = _iso()
    with queue._transaction() as connection:
        lease = connection.execute(
            "SELECT owner_id FROM index_worker_lease WHERE worker_key = ?",
            (DEFAULT_WORKER_KEY,),
        ).fetchone()
        if lease is None:
            return {"released": False, "jobsReleased": 0, "reason": "no-worker-lease"}
        owner_id = str(lease["owner_id"] or "")
        if marker not in owner_id:
            return {"released": False, "jobsReleased": 0, "reason": "pid-owner-mismatch"}
        jobs = connection.execute(
            """
            UPDATE index_jobs
            SET lease_expires_at = '', updated_at = ?
            WHERE status = 'running' AND lease_owner = ?
            """,
            (timestamp, owner_id),
        ).rowcount
        connection.execute(
            "DELETE FROM index_worker_lease WHERE worker_key = ? AND owner_id = ?",
            (DEFAULT_WORKER_KEY, owner_id),
        )
    return {"released": True, "jobsReleased": int(jobs), "reason": "dead-owner-cleared"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Release an exact dead Library indexer PID from the local durable queue")
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    print(clear_dead_owner(args.pid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())