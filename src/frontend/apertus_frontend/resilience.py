from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar

import httpx

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    pass


class RateLimitExceededError(RuntimeError):
    pass


class CapacityExceededError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 5.0


class AsyncCircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    async def call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        is_failure: Callable[[Exception], bool],
    ) -> T:
        async with self._lock:
            if self._opened_at is not None:
                if time.monotonic() - self._opened_at < self._recovery_seconds:
                    raise CircuitOpenError("Dependency circuit is temporarily open.")
                self._opened_at = None
                self._failures = 0

        try:
            result = await operation()
        except Exception as exc:
            if is_failure(exc):
                async with self._lock:
                    self._failures += 1
                    if self._failures >= self._failure_threshold:
                        self._opened_at = time.monotonic()
            raise

        async with self._lock:
            self._failures = 0
            self._opened_at = None
        return result


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[Exception], bool],
    policy: RetryPolicy,
) -> T:
    for attempt in range(1, policy.attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt == policy.attempts or not is_retryable(exc):
                raise
            retry_after = _retry_after_seconds(exc)
            exponential = min(
                policy.max_delay_seconds,
                policy.base_delay_seconds * (2 ** (attempt - 1)),
            )
            delay = retry_after if retry_after is not None else exponential
            await asyncio.sleep(delay + random.uniform(0, max(delay * 0.2, 0.01)))
    raise RuntimeError("Retry loop exited unexpectedly.")


def is_retryable_service_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code is None and isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
    return status_code in {408, 409, 429} or (
        isinstance(status_code, int) and 500 <= status_code <= 599
    )


class RequestAdmissionController:
    def __init__(
        self,
        *,
        max_concurrent: int,
        requests_per_minute: int,
        queue_timeout_seconds: float,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._requests_per_minute = requests_per_minute
        self._queue_timeout_seconds = queue_timeout_seconds
        self._request_times: dict[str, deque[float]] = defaultdict(deque)
        self._rate_lock = asyncio.Lock()

    @asynccontextmanager
    async def admit(self, requester: str) -> AsyncIterator[None]:
        now = time.monotonic()
        async with self._rate_lock:
            request_times = self._request_times[requester]
            while request_times and now - request_times[0] >= 60:
                request_times.popleft()
            if len(request_times) >= self._requests_per_minute:
                raise RateLimitExceededError(
                    "Request limit reached. Wait a minute before trying again."
                )
            request_times.append(now)

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self._queue_timeout_seconds
            )
        except TimeoutError as exc:
            raise CapacityExceededError(
                "The model is at capacity. Try again shortly."
            ) from exc

        try:
            yield
        finally:
            self._semaphore.release()


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None