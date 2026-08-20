import pytest

from app.services.transient_retry import call_with_transient_gemini_retry, is_transient_gemini_error


def test_high_demand_error_is_transient():
    error = RuntimeError(
        "gemini-3.1-flash-lite is currently experiencing high demand, spikes in demand are usually temporary."
    )
    assert is_transient_gemini_error(error) is True


def test_quota_error_is_not_transient():
    error = RuntimeError("429 RESOURCE_EXHAUSTED. Quota exceeded for metric")
    assert is_transient_gemini_error(error) is False


def test_transient_call_retries_with_bounded_backoff():
    calls = []
    sleeps = []

    def operation():
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise RuntimeError("503 service unavailable: high demand")
        return "ok"

    result = call_with_transient_gemini_retry(operation, retries=2, sleep_fn=sleeps.append)

    assert result == "ok"
    assert calls == [1, 2, 3]
    assert sleeps == [1.0, 2.0]


def test_transient_call_raises_after_retry_budget():
    calls = 0
    sleeps = []

    def operation():
        nonlocal calls
        calls += 1
        raise RuntimeError("500 internal server error")

    with pytest.raises(RuntimeError, match="500 internal server error"):
        call_with_transient_gemini_retry(operation, retries=2, sleep_fn=sleeps.append)

    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_permanent_error_fails_without_sleeping():
    calls = 0
    sleeps = []

    def operation():
        nonlocal calls
        calls += 1
        raise RuntimeError("GEMINI_API_KEY is required for video analysis")

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        call_with_transient_gemini_retry(operation, retries=5, sleep_fn=sleeps.append)

    assert calls == 1
    assert sleeps == []
