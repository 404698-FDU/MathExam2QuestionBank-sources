from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable


DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class RetryResult:
    value: Any = None
    attempts: int = 0
    elapsed_seconds: float = 0.0
    error: Exception | None = None

    @property
    def success(self) -> bool:
        return self.error is None


def call_with_retries(
    operation: Callable[[], Any],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    should_retry: Callable[[Exception], bool] | None = None,
) -> RetryResult:
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    should_retry = should_retry or _default_should_retry
    started = time.monotonic()
    attempts = 0

    while attempts < max_attempts:
        attempts += 1
        try:
            value = operation()
            return RetryResult(
                value=value,
                attempts=attempts,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
        except Exception as exc:
            if attempts >= max_attempts or not should_retry(exc):
                return RetryResult(
                    attempts=attempts,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    error=exc,
                )
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds * attempts)

    raise RuntimeError("call_with_retries exhausted without returning a result")


def _default_should_retry(exc: Exception) -> bool:
    return not isinstance(exc, FileNotFoundError)
