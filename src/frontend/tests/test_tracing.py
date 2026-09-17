import asyncio
import json
from types import SimpleNamespace

import pytest
from opentelemetry.trace import StatusCode

from apertus_frontend.tracing import (
    configure_tracing,
    record_content,
    record_messages,
    record_response_metadata,
    trace_scope,
    traced,
)


@pytest.mark.parametrize("configured", [False, True])
def test_exporter_uses_full_sampling_only_when_configured(monkeypatch, configured):
    import azure.monitor.opentelemetry as azure_monitor

    calls = []
    monkeypatch.setattr(azure_monitor, "configure_azure_monitor", lambda **kwargs: calls.append(kwargs))
    if configured:
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "test-connection")
    else:
        monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)

    configure_tracing()

    assert calls == ([{"logger_name": "apertus", "sampling_ratio": 1.0}] if configured else [])


def test_content_capture_is_disabled_by_default(traced_spans):
    with trace_scope("test"):
        record_messages("gen_ai.input.messages", [{"role": "user", "content": "private"}])
        record_content("gen_ai.tool.call.arguments", "private arguments")

    assert not traced_spans.get_finished_spans()[0].attributes


def test_opt_in_preserves_text_but_excludes_privileged_fields(traced_spans, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "true")
    text = "Contact person@example.com. Keep this text unchanged."
    messages = [
        {"role": "system", "content": "SYSTEM_SENTINEL"},
        {"role": "developer", "content": "DEVELOPER_SENTINEL"},
        {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "IMAGE_SENTINEL"}},
            {"type": "input_audio", "input_audio": {"data": "AUDIO_SENTINEL"}},
        ]},
        {"role": "assistant", "content": "Answer", "reasoning_content": "REASONING_SENTINEL"},
    ]
    with trace_scope("test"):
        record_messages("gen_ai.input.messages", messages)

    recorded = traced_spans.get_finished_spans()[0].attributes["gen_ai.input.messages"]
    assert json.loads(recorded) == [
        {"role": "user", "parts": [{"type": "text", "content": text}]},
        {"role": "assistant", "parts": [{"type": "text", "content": "Answer"}]},
    ]
    assert "SENTINEL" not in recorded


def test_error_does_not_record_exception_body(traced_spans, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "true")
    with pytest.raises(RuntimeError):
        with trace_scope("test"):
            raise RuntimeError("SECRET_SENTINEL in a dependency error body")

    span = traced_spans.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "RuntimeError"
    assert "SECRET_SENTINEL" not in str(span.to_json())


@pytest.mark.parametrize("usage", [
    SimpleNamespace(prompt_tokens=15, completion_tokens=8),
    {"input_tokens": 15, "output_tokens": 8},
])
def test_records_actual_response_usage(traced_spans, usage):
    with trace_scope("test"):
        record_response_metadata(SimpleNamespace(id="response-id", model="apertus", usage=usage))

    attributes = traced_spans.get_finished_spans()[0].attributes
    assert attributes["gen_ai.response.id"] == "response-id"
    assert attributes["gen_ai.usage.input_tokens"] == 15
    assert attributes["gen_ai.usage.output_tokens"] == 8


def test_missing_usage_is_not_reported_as_zero(traced_spans):
    with trace_scope("test"):
        record_response_metadata(SimpleNamespace())

    assert not traced_spans.get_finished_spans()[0].attributes


@pytest.mark.asyncio
async def test_concurrent_messages_do_not_inherit_a_websocket_trace(traced_spans):
    @traced("message", new_trace=True)
    async def handle_message():
        with trace_scope("model"):
            return None

    with trace_scope("websocket"):
        await asyncio.gather(handle_message(), handle_message())

    spans = traced_spans.get_finished_spans()
    messages = [span for span in spans if span.name == "message"]
    models = [span for span in spans if span.name == "model"]
    assert len({span.context.trace_id for span in messages}) == 2
    assert all(span.parent is None for span in messages)
    assert {span.parent.span_id for span in models} == {span.context.span_id for span in messages}


@pytest.mark.asyncio
async def test_cancelled_trace_records_only_cancellation_type(traced_spans):
    with pytest.raises(asyncio.CancelledError):
        with trace_scope("request"):
            raise asyncio.CancelledError("PRIVATE_CANCELLATION_DETAIL")

    span = traced_spans.get_finished_spans()[0]
    assert span.attributes["apertus.outcome"] == "cancelled"
    assert "PRIVATE_CANCELLATION_DETAIL" not in span.to_json()