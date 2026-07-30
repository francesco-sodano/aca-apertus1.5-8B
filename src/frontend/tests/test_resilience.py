from __future__ import annotations

import httpx
import pytest

from apertus_frontend.resilience import (
    AsyncCircuitBreaker,
    CircuitOpenError,
    RateLimitExceededError,
    RequestAdmissionController,
    RetryPolicy,
    is_retryable_service_error,
    retry_async,
)


@pytest.mark.asyncio
async def test_retry_recovers_from_transient_status(monkeypatch):
    attempts = 0

    async def no_sleep(delay):
        return None

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            request = httpx.Request("POST", "https://example.com")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("unavailable", request=request, response=response)
        return "ok"

    monkeypatch.setattr("apertus_frontend.resilience.asyncio.sleep", no_sleep)
    result = await retry_async(
        operation,
        is_retryable=is_retryable_service_error,
        policy=RetryPolicy(attempts=3, base_delay_seconds=0),
    )

    assert result == "ok"
    assert attempts == 3


@pytest.mark.asyncio
async def test_circuit_opens_after_repeated_transient_failures():
    breaker = AsyncCircuitBreaker(failure_threshold=2, recovery_seconds=60)

    async def fail():
        raise httpx.ConnectError("offline")

    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            await breaker.call(fail, is_failure=is_retryable_service_error)

    with pytest.raises(CircuitOpenError):
        await breaker.call(fail, is_failure=is_retryable_service_error)


@pytest.mark.asyncio
async def test_admission_controller_enforces_per_requester_rate_limit():
    controller = RequestAdmissionController(
        max_concurrent=1,
        requests_per_minute=1,
        queue_timeout_seconds=0.1,
    )

    async with controller.admit("user-1"):
        pass

    with pytest.raises(RateLimitExceededError):
        async with controller.admit("user-1"):
            pass