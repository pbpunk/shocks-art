from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")

_PERMANENT_MARKERS = (
    "gemini_api_key",
    "api key is missing",
    "invalid api key",
    "permission denied",
    "unauthenticated",
    "quota exceeded",
    "resource_exhausted",
    "no longer available to new users",
    "not_found",
)

_TRANSIENT_MARKERS = (
    "high demand",
    "temporarily unavailable",
    "temporary unavailable",
    "service unavailable",
    "unavailable",
    "internal server error",
    "internal error",
    "status 500",
    "status 502",
    "status 503",
    "status 504",
    "code 500",
    "code 502",
    "code 503",
    "code 504",
)


def is_transient_gemini_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    if any(marker in message for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def call_with_transient_gemini_retry(
    operation: Callable[[], T],
    *,
    retries: int,
    sleep_fn: Callable[[float], None] | None = None,
) -> T:
    """Retry only temporary Gemini transport/service failures.

    Validation/schema repair retries are handled elsewhere. This helper is deliberately
    limited to transport/service errors such as temporary 5xx/high-demand responses;
    auth, quota, and unavailable-model errors fail immediately.
    """

    sleeper = sleep_fn or time.sleep
    max_retries = max(0, int(retries))
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= max_retries or not is_transient_gemini_error(exc):
                raise
            sleeper(min(8.0, float(2**attempt)))

    raise RuntimeError("unreachable")
