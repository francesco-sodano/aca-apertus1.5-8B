from types import SimpleNamespace

import pytest

from apertus_frontend.azure_services import (
    AzureContentSafetyGateway,
    _blocked_category,
)
from apertus_frontend.pipeline import (
    SafetyBlockedError,
)


class FakeCredential:
    async def get_token(self, *scopes, **kwargs):
        return SimpleNamespace(token="token")


class RecordingClient:
    def __init__(self, payload=None):
        self.body = None
        self.payload = payload or {"output_text": "Evidence", "output": []}

    async def post(self, url, *, headers, json, params=None):
        self.body = json
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: self.payload,
        )


def test_content_safety_threshold_is_inclusive():
    payload = {
        "categoriesAnalysis": [
            {"category": "Hate", "severity": 2},
            {"category": "Violence", "severity": 4},
        ]
    }

    assert _blocked_category(payload, 4) == "Violence severity 4"
    assert _blocked_category(payload, 6) is None


@pytest.mark.asyncio
async def test_content_safety_block_preserves_rule_and_severity_for_ui():
    client = RecordingClient(
        payload={
            "userPromptAnalysis": {"attackDetected": False},
            "categoriesAnalysis": [{"category": "Violence", "severity": 4}],
        }
    )
    gateway = AzureContentSafetyGateway(
        endpoint="https://safety.example",
        credential=FakeCredential(),
        threshold=4,
        client=client,
    )

    with pytest.raises(SafetyBlockedError) as blocked:
        await gateway.screen_text("unsafe content", purpose="user-input")

    assert blocked.value.rule == "Violence"
    assert blocked.value.severity == 4
    assert blocked.value.threshold == 4
    assert blocked.value.user_message == (
        "Your message was blocked by Azure AI Content Safety. "
        "Rule: Violence; severity 4 met the configured block threshold 4."
    )
