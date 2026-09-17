"""OpenTelemetry spans and opt-in text content for the Apertus workflow."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, StatusCode
from opentelemetry.util.types import AttributeValue

Parameters = ParamSpec("Parameters")
Result = TypeVar("Result")
_tracer = trace.get_tracer("apertus.frontend")


def configure_tracing() -> None:
    """Export complete request trees instead of applying the distro's span rate limit."""
    if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(logger_name="apertus", sampling_ratio=1.0)


def content_enabled() -> bool:
    return os.getenv("TRACE_CONTENT", "false").strip().lower() == "true"


@contextmanager
def trace_scope(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, AttributeValue] | None = None,
    new_trace: bool = False,
) -> Iterator[Span]:
    """Scope a span without copying dependency exception messages or stack traces."""
    with _tracer.start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        context=Context() if new_trace else None,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            record_error(error)
            raise


def record_error(error: BaseException) -> None:
    span = trace.get_current_span()
    error_type = type(error).__name__
    span.set_attribute("error.type", error_type)
    span.set_status(StatusCode.ERROR)
    span.add_event("exception", {"exception.type": error_type})
    outcome = {
        "SafetyBlockedError": "blocked",
        "GroundingUnavailableError": "grounding_rejected",
        "RateLimitExceededError": "rate_limited",
        "CapacityExceededError": "capacity_exceeded",
        "ValueError": "invalid_input",
        "CancelledError": "cancelled",
    }.get(error_type, "failed")
    span.set_attribute("apertus.outcome", outcome)
    if error_type == "SafetyBlockedError":
        for field in ("stage", "rule", "severity", "threshold"):
            value = getattr(error, field, None)
            if isinstance(value, (str, int)):
                span.set_attribute(f"apertus.safety.{field}", value)


def traced(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, AttributeValue] | None = None,
    new_trace: bool = False,
) -> Callable[[Callable[Parameters, Awaitable[Result]]], Callable[Parameters, Awaitable[Result]]]:
    def decorate(
        operation: Callable[Parameters, Awaitable[Result]],
    ) -> Callable[Parameters, Awaitable[Result]]:
        @wraps(operation)
        async def wrapped(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
            with trace_scope(name, kind=kind, attributes=attributes, new_trace=new_trace):
                return await operation(*args, **kwargs)

        return wrapped

    return decorate


def set_attributes(**attributes: AttributeValue | None) -> None:
    span = trace.get_current_span()
    for name, value in attributes.items():
        if value is not None:
            span.set_attribute(name, value)


def record_content(name: str, value: Any) -> None:
    """Record opted-in content as supplied; no application redaction or truncation."""
    span = trace.get_current_span()
    if content_enabled() and span.is_recording():
        span.set_attribute(
            name, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )


def record_messages(name: str, messages: Sequence[Mapping[str, Any]]) -> None:
    """Select visible text/tool fields, excluding privileged roles and media parts."""
    if not content_enabled() or not trace.get_current_span().is_recording():
        return
    captured = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        parts: list[dict[str, Any]] = []
        content = message.get("content")
        if isinstance(content, str) and content:
            if role == "tool":
                parts.append({
                    "type": "tool_call_response",
                    "id": message.get("tool_call_id", ""),
                    "response": content,
                })
            else:
                parts.append({"type": "text", "content": content})
        elif isinstance(content, list):
            parts.extend(
                {"type": "text", "content": part["text"]}
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            parts.append({
                "type": "tool_call",
                "id": call.get("id", ""),
                "name": function.get("name", ""),
                "arguments": function.get("arguments", ""),
            })
        if parts:
            captured.append({"role": role, "parts": parts})
    if captured:
        record_content(name, captured)


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def record_response_metadata(response: Any) -> None:
    """Handle SDK responses and usage-only chunks without inventing missing counts."""
    attributes: dict[str, AttributeValue] = {}
    for field in ("id", "model"):
        value = _field(response, field)
        if isinstance(value, str) and value:
            attributes[f"gen_ai.response.{field}"] = value
    response_status = _field(response, "status")
    if isinstance(response_status, str):
        attributes["apertus.response_status"] = response_status
    reasons = [
        reason
        for choice in _field(response, "choices") or []
        if isinstance(reason := _field(choice, "finish_reason"), str)
    ]
    if reasons:
        attributes["gen_ai.response.finish_reasons"] = reasons
    usage = _field(response, "usage")
    for target, fields in (
        ("input_tokens", ("prompt_tokens", "input_tokens")),
        ("output_tokens", ("completion_tokens", "output_tokens")),
    ):
        for field in fields:
            count = _field(usage, field)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                attributes[f"gen_ai.usage.{target}"] = count
                break
    set_attributes(**attributes)