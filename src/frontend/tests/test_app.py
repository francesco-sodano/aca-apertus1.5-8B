from fastapi.testclient import TestClient

from app import _grounding_error_message, app
from apertus_frontend.pipeline import GroundingUnavailableError


def test_health_endpoint_precedes_chainlit_spa_fallback() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_grounding_error_names_service_reason_and_reference() -> None:
    error = GroundingUnavailableError(
        "The answer did not receive explicit approval.",
        source="Azure AI Content Safety Groundedness Detection",
    )

    message = _grounding_error_message(error, "grounding-test")

    assert "Answer withheld by:** Azure AI Content Safety" in message
    assert "Reason:** The answer did not receive explicit approval." in message
    assert "`grounding-test`" in message