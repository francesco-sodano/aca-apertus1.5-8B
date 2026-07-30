from fastapi.testclient import TestClient

from app import app


def test_health_endpoint_precedes_chainlit_spa_fallback() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}