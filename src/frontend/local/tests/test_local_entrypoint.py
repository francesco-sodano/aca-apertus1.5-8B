from types import SimpleNamespace

import httpx
import pytest

import app_local as frontend


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_health_is_json_before_spa_and_readiness_is_honest(monkeypatch, caplog, fails):
    async def ready():
        if fails:
            raise RuntimeError("SECRET_SENTINEL from a stopped backend")

    monkeypatch.setattr(frontend, "get_runtime", lambda: SimpleNamespace(ready=ready))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=frontend.app), base_url="http://localhost:8080",
    ) as client:
        health = await client.get("/healthz")
        readiness = await client.get("/healthz/ready")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert readiness.status_code == (503 if fails else 200)
    assert readiness.json() == {"status": "not-ready" if fails else "ready"}
    assert readiness.headers["content-type"] == "application/json"
    assert "default-src 'self'" in readiness.headers["content-security-policy"]
    assert "SECRET_SENTINEL" not in caplog.text
    assert "SECRET_SENTINEL" not in readiness.text


@pytest.mark.asyncio
async def test_interrupted_generation_is_visible_and_not_remembered(monkeypatch, caplog):
    displayed = []

    class Message:
        def __init__(self, content=""):
            self.content = content
            displayed.append(self)

        async def send(self):
            return self

        async def update(self):
            pass

        async def stream_token(self, token):
            self.content += token

    async def complete(**kwargs):
        await kwargs["on_token"]("partial")
        raise httpx.ConnectError("SECRET_SENTINEL in connection")

    def remember(*args):
        pytest.fail("An interrupted answer must not enter successful history.")

    monkeypatch.setattr(frontend.cl, "Message", Message)
    monkeypatch.setattr(frontend.cl, "user_session", SimpleNamespace(get=lambda key: None, set=remember))
    monkeypatch.setattr(frontend, "get_runtime", lambda: SimpleNamespace(complete=complete))
    await frontend.on_message(SimpleNamespace(content="hello", elements=[]))
    assert len(displayed) == 1
    assert displayed[0].content.startswith("partial")
    assert "Request failed:" in displayed[0].content
    assert "SECRET_SENTINEL" not in displayed[0].content
    assert "SECRET_SENTINEL" not in caplog.text


@pytest.mark.asyncio
async def test_profiles_are_local_and_accept_chainlit_user_argument():
    profiles = await frontend.chat_profiles(None)
    assert [profile.name for profile in profiles] == ["Chat", "Tools", "Thinking"]
