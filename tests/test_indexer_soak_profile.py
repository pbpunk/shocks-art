from tools.host_profiles import indexer_soak
from tools.host_profiles.indexer_soak import (
    MAX_SOAK_SECONDS,
    resolve_soak_duration,
    scratch_is_clean,
    semantic_search_measurement,
)
from tools.run_host_profile import PROFILE_TIMEOUT_SECONDS


def test_indexer_soak_budget_stays_below_host_runner_timeout() -> None:
    assert MAX_SOAK_SECONDS == 900
    assert MAX_SOAK_SECONDS < PROFILE_TIMEOUT_SECONDS["indexer-soak"]


def test_indexer_soak_duration_clamps_long_host_setting() -> None:
    requested, effective = resolve_soak_duration("1800")
    assert requested == 1800
    assert effective == 900


def test_indexer_soak_duration_preserves_shorter_bounded_setting() -> None:
    requested, effective = resolve_soak_duration("300")
    assert requested == 300
    assert effective == 300


def test_indexer_soak_duration_enforces_minimum_observation_window() -> None:
    requested, effective = resolve_soak_duration("30")
    assert requested == 120
    assert effective == 120


def test_semantic_search_measurement_keeps_model_and_vector_latency_separate() -> None:
    measurement = semantic_search_measurement(
        {
            "queryEmbeddingMs": 612.34567,
            "result": {
                "elapsedMs": 43.21987,
                "databaseMs": 9.87654,
                "scoringMs": 33.12346,
                "vectorCount": 321,
            },
        },
        0.70129,
    )

    assert measurement == {
        "request_seconds": 0.7013,
        "query_embedding_ms": 612.3457,
        "vector_retrieval_ms": 43.2199,
        "database_ms": 9.8765,
        "scoring_ms": 33.1235,
        "vector_count": 321,
    }


def test_semantic_search_measurement_fails_closed_on_missing_split_metrics() -> None:
    assert semantic_search_measurement({"result": {"vectorCount": 3}}, 0.2) is None
    assert semantic_search_measurement({"queryEmbeddingMs": 10, "result": {"elapsedMs": 1, "databaseMs": 1, "scoringMs": 0, "vectorCount": 0}}, 0.2) is None


def test_scratch_cleanup_requires_final_usage_not_to_exceed_initial_usage() -> None:
    assert scratch_is_clean(0, 0) is True
    assert scratch_is_clean(100, 50) is True
    assert scratch_is_clean(100, 100) is True
    assert scratch_is_clean(100, 101) is False


def test_wait_for_job_proves_health_overlap_while_status_is_running(monkeypatch) -> None:
    snapshots = iter(
        [
            (
                True,
                {"jobs": [{"jobId": "job-1", "status": "running", "result": {}}]},
            ),
            (
                True,
                {"jobs": [{"jobId": "job-1", "status": "completed", "result": {"ok": True}}]},
            ),
        ]
    )
    monkeypatch.setattr(indexer_soak, "queue_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(indexer_soak, "request_json", lambda *args, **kwargs: (200, {"app": "shocks-art"}, 0.001))
    monkeypatch.setattr(indexer_soak.time, "sleep", lambda _: None)

    status, job, overlap = indexer_soak.wait_for_job_with_health("job-1", timeout=5)

    assert status == "completed"
    assert job["result"] == {"ok": True}
    assert overlap == {
        "saw_running": True,
        "running_health_checks": 1,
        "running_health_failures": 0,
    }


def test_wait_for_job_records_failed_health_during_running_status(monkeypatch) -> None:
    snapshots = iter(
        [
            (True, {"jobs": [{"jobId": "job-1", "status": "running", "result": {}}]}),
            (True, {"jobs": [{"jobId": "job-1", "status": "completed", "result": {}}]}),
        ]
    )
    monkeypatch.setattr(indexer_soak, "queue_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(indexer_soak, "request_json", lambda *args, **kwargs: (0, {}, 0.001))
    monkeypatch.setattr(indexer_soak.time, "sleep", lambda _: None)

    _, _, overlap = indexer_soak.wait_for_job_with_health("job-1", timeout=5)

    assert overlap["saw_running"] is True
    assert overlap["running_health_checks"] == 1
    assert overlap["running_health_failures"] == 1
