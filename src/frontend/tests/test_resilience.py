from __future__ import annotations

import httpx
import httpx2
import pytest
from openai import APIConnectionError, APITimeoutError

from apertus_frontend.resilience import (
    AsyncCircuitBreaker,
    CircuitOpenError,
    RateLimitExceededError,
    RequestAdmissionController,
    RetryPolicy,
    is_retryable_service_error,
    retry_async,
)
from apertus_frontend.tracing import trace_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("error_class", [APIConnectionError, APITimeoutError])
async def test_retry_handles_sdk_wrapped_transport_errors(error_class, monkeypatch):
    attempts = 0

    async def no_sleep(delay):
        pass

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error_class(request=httpx2.Request("POST", "https://model.example/v1/chat/completions"))
        return "recovered"

    monkeypatch.setattr("apertus_frontend.resilience.asyncio.sleep", no_sleep)
    result = await retry_async(
        operation,
        is_retryable=is_retryable_service_error,
        policy=RetryPolicy(attempts=2, base_delay_seconds=0),
    )

    assert result == "recovered"
    assert attempts == 2


@pytest.mark.asyncio
async def test_retry_recovers_from_transient_status(monkeypatch, traced_spans):
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
    with trace_scope("dependency"):
        result = await retry_async(
            operation,
            is_retryable=is_retryable_service_error,
            policy=RetryPolicy(attempts=3, base_delay_seconds=0),
        )

    assert result == "ok"
    assert attempts == 3
    span = traced_spans.get_finished_spans()[0]
    assert span.attributes["apertus.attempt_count"] == 3
    assert span.attributes["apertus.retry_count"] == 2
    assert len(span.events) == 2
    assert all(event.name == "retry" for event in span.events)
    assert "unavailable" not in span.to_json()


@pytest.mark.asyncio
async def test_circuit_opens_after_repeated_transient_failures(traced_spans):
    breaker = AsyncCircuitBreaker(failure_threshold=2, recovery_seconds=60)

    async def fail():
        raise httpx.ConnectError("offline")

    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            await breaker.call(fail, is_failure=is_retryable_service_error)

    with pytest.raises(CircuitOpenError):
        with trace_scope("dependency"):
            await breaker.call(fail, is_failure=is_retryable_service_error)

    span = traced_spans.get_finished_spans()[0]
    assert span.attributes["apertus.circuit.state"] == "open"
    assert span.attributes["error.type"] == "CircuitOpenError"


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