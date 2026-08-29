from tools.host_profiles.indexer_soak import MAX_SOAK_SECONDS, resolve_soak_duration
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
