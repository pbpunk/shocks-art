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
                "scoringMs": 33.12345,
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
