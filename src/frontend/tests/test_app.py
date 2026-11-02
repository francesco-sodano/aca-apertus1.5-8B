from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
import app as frontend

from app import _grounding_error_message, app
from apertus_frontend.pipeline import CompletionResult, GroundingUnavailableError
from apertus_frontend.tracing import trace_scope
from chainlit.config import config


def test_health_endpoint_precedes_chainlit_spa_fallback() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("fails", [False, True])
def test_readiness_reports_configuration_failures_without_secrets(
    monkeypatch, caplog, fails
) -> None:
    def runtime():
        if fails:
            raise RuntimeError("SECRET_SENTINEL in configuration")
        return SimpleNamespace()

    monkeypatch.setattr(frontend, "get_runtime", runtime)

    response = TestClient(app).get("/healthz/ready")

    assert response.status_code == (503 if fails else 200)
    assert response.json() == {"status": "not-ready" if fails else "ready"}
    assert ("readiness_failed" in caplog.text) is fails
    assert "SECRET_SENTINEL" not in caplog.text
    assert "SECRET_SENTINEL" not in response.text


@pytest.fixture
def model_health_runtime(monkeypatch):
    runtime = SimpleNamespace(settings=SimpleNamespace(
        health_token="test-health-token",
        model_endpoint="https://model.example.com/v1",
        vllm_api_key="test-model-key",
    ))
    monkeypatch.setattr(frontend, "get_runtime", lambda: runtime)


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_model_health_requires_operator_token(
    model_health_runtime, authorization
) -> None:
    headers = {"Authorization": authorization} if authorization else {}

    response = TestClient(app).get("/healthz/model", headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize("failure", [None, 503, httpx.ConnectError, httpx.ReadTimeout])
def test_model_health_reports_unavailable_dependency(
    monkeypatch, caplog, model_health_runtime, failure
) -> None:
    def handler(request):
        assert str(request.url) == "https://model.example.com/health"
        assert request.headers["Authorization"] == "Bearer test-model-key"
        if isinstance(failure, type):
            raise failure("SECRET_SENTINEL in dependency error", request=request)
        return httpx.Response(failure or 200, json={"status": "ok"})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        frontend.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )

    response = TestClient(app).get(
        "/healthz/model", headers={"Authorization": "Bearer test-health-token"}
    )

    assert response.status_code == (503 if failure else 200)
    if not failure:
        assert response.json() == {"status": "ready"}
    assert ("model_health_failed" in caplog.text) is bool(failure)
    assert "SECRET_SENTINEL" not in caplog.text
    assert "SECRET_SENTINEL" not in response.text


@pytest.mark.asyncio
async def test_chainlit_shutdown_closes_runtime_and_flushes_traces(monkeypatch):
    actions = []

    async def close():
        actions.append("closed")

    def flush(*, timeout_millis):
        actions.append(("flushed", timeout_millis))

    monkeypatch.setattr(frontend, "_runtime", SimpleNamespace(aclose=close))
    monkeypatch.setattr(frontend.trace, "get_tracer_provider", lambda: SimpleNamespace(force_flush=flush))

    assert config.code.on_app_shutdown is not None
    await config.code.on_app_shutdown()

    assert frontend._runtime is None
    assert actions == ["closed", ("flushed", 5000)]


def test_grounding_error_names_service_reason_and_reference() -> None:
    error = GroundingUnavailableError(
        "The answer did not receive explicit approval.",
        source="Azure AI Content Safety Groundedness Detection",
        rule="Groundedness approval required",
    )

    message = _grounding_error_message(error)

    assert message == (
        "An answer was not returned because Azure AI Content Safety Groundedness "
        "Detection could not verify it. Reason: Groundedness approval required."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_message_delivery_and_handled_errors_stay_in_same_trace(
    traced_spans, monkeypatch, caplog, fails
):
    monkeypatch.setenv("TRACE_CONTENT", "true")

    class TestMessage:
        def __init__(self, content=""):
            self.content = content

        async def send(self):
            return self

        async def remove(self):
            pass

        async def update(self):
            pass

        async def stream_token(self, token):
            self.content += token

    @asynccontextmanager
    async def admit(requester):
        yield

    async def complete(request, profile, on_progress):
        with trace_scope("apertus.request"):
            if fails:
                raise RuntimeError("SECRET_SENTINEL in dependency response")
            return CompletionResult(answer="A harmless answer", citations=(), correlation_id=request.correlation_id)

    monkeypatch.setattr(frontend.cl, "Message", TestMessage)
    monkeypatch.setattr(frontend.cl, "ErrorMessage", TestMessage)
    monkeypatch.setattr(frontend.cl, "user_session", SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr(frontend, "_conversation_history", lambda: ())
    monkeypatch.setattr(frontend, "_remember_conversation", lambda request, result: None)
    monkeypatch.setattr(frontend, "_requester_key", lambda: "private-user-id")
    monkeypatch.setattr(frontend, "get_runtime", lambda: SimpleNamespace(
        admission=SimpleNamespace(admit=admit), service=SimpleNamespace(complete=complete)
    ))

    await frontend.on_message(SimpleNamespace(content="A harmless request", elements=[]))

    spans = traced_spans.get_finished_spans()
    root = next(span for span in spans if span.name == "apertus.chat")
    response = next(span for span in spans if span.name == "apertus.respond")
    assert response.parent.span_id == root.context.span_id
    assert len({span.context.trace_id for span in spans}) == 1
    assert root.attributes["apertus.outcome"] == ("failed" if fails else "delivered")
    assert response.attributes["apertus.delivery_status"] == "sent"
    assert "SECRET_SENTINEL" not in caplog.text
    assert all("SECRET_SENTINEL" not in span.to_json() for span in spans)
    assert all("private-user-id" not in span.to_json() for span in spans)
    if fails:
        assert root.attributes["error.type"] == "RuntimeError"