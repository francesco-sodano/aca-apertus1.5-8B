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
        rule="Groundedness approval required",
    )

    message = _grounding_error_message(error)

    assert message == (
        "Your message was blocked by Azure AI Content Safety Groundedness "
        "Detection. Rule: Groundedness approval required."
    )