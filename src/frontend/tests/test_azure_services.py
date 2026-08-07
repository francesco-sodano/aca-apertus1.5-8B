from types import SimpleNamespace
import logging

import pytest

from apertus_frontend.azure_services import (
    AzureContentSafetyGateway,
    FoundryWebSearchGateway,
    MAX_GROUNDING_SUMMARY_CHARACTERS,
    _blocked_category,
    _extract_foundry_result,
)
from apertus_frontend.pipeline import (
    Citation,
    SafetyBlockedError,
)


class FakeCredential:
    async def get_token(self, *scopes, **kwargs):
        return SimpleNamespace(token="token")


class RecordingClient:
    def __init__(self, payload=None, headers=None):
        self.body = None
        self.headers = headers or {}
        self.payload = payload or {"output_text": "Evidence", "output": []}

    async def post(self, url, *, headers, json, params=None):
        self.body = json
        self.request_headers = headers
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: self.payload,
            headers=self.headers,
        )


def test_extracts_foundry_text_and_unique_citations():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Grounded summary",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Primary source",
                                "url": "https://example.com/source",
                            },
                            {
                                "type": "url_citation",
                                "title": "Duplicate",
                                "url": "https://example.com/source",
                            },
                        ],
                    }
                ],
            }
        ]
    }

    summary, citations = _extract_foundry_result(payload)

    assert summary == "Grounded summary"
    assert len(citations) == 1
    assert citations[0].url == "https://example.com/source"


def test_extracts_included_web_search_sources_without_inline_annotations():
    payload = {
        "output_text": "Evidence without inline annotations",
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "type": "search",
                    "sources": [
                        {
                            "type": "url",
                            "title": "Primary source",
                            "url": "https://example.com/primary",
                        }
                    ],
                },
            }
        ],
    }

    summary, citations = _extract_foundry_result(payload)

    assert summary == "Evidence without inline annotations"
    assert citations == (
        Citation(title="Primary source", url="https://example.com/primary"),
    )


def test_prefers_inline_citations_and_limits_display_sources():
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "sources": [
                        {
                            "type": "url",
                            "title": f"Source {index}",
                            "url": f"https://example.com/{index}",
                        }
                        for index in range(10)
                    ]
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Evidence",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Cited source",
                                "url": "https://cited.example.com",
                            }
                        ],
                    }
                ],
            },
        ]
    }

    _, citations = _extract_foundry_result(payload)

    assert len(citations) == 5
    assert citations[0].url == "https://cited.example.com"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expects_reasoning"),
    [
        ("gpt-4.1-nano-grounding", False),
        ("gpt-5-nano-grounding", True),
    ],
)
async def test_web_search_uses_model_compatible_latency_controls(
    model, expects_reasoning
):
    client = RecordingClient()
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model=model,
        credential=FakeCredential(),
        client=client,
    )

    await gateway.search("Current information")

    assert ("reasoning" in client.body) is expects_reasoning
    assert ("text" in client.body) is expects_reasoning
    assert "shown directly" in client.body["instructions"]
    assert "requested language" in client.body["instructions"]
    assert "strict safe-search policy" in client.body["instructions"]
    assert client.body["tools"][0]["search_context_size"] == "low"
    assert "user_location" not in client.body["tools"][0]
    assert client.request_headers["x-ms-client-request-id"]


@pytest.mark.asyncio
async def test_web_search_caps_grounding_summary_size():
    client = RecordingClient(
        payload={"output_text": "x" * (MAX_GROUNDING_SUMMARY_CHARACTERS + 1)}
    )
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="gpt-4.1-nano-grounding",
        credential=FakeCredential(),
        client=client,
    )

    packet = await gateway.search("Current information")

    assert len(packet.summary) == MAX_GROUNDING_SUMMARY_CHARACTERS


@pytest.mark.asyncio
async def test_web_search_logs_only_support_metadata(caplog):
    client = RecordingClient(
        payload={"id": "resp_support_123", "output_text": "Evidence", "output": []},
        headers={
            "apim-request-id": "apim_support_123",
            "x-ms-request-id": "ms_support_123",
            "x-request-id": "request_support_123",
        },
    )
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="gpt-4.1-nano-grounding",
        credential=FakeCredential(),
        client=client,
    )

    with caplog.at_level(logging.INFO, logger="apertus.frontend.azure"):
        await gateway.search("private user query")

    record = next(item for item in caplog.records if item.msg == "web_search_completed")
    assert record.custom_dimensions["client_request_id"]
    assert record.custom_dimensions["response_id"] == "resp_support_123"
    assert record.custom_dimensions["apim_request_id"] == "apim_support_123"
    assert record.custom_dimensions["x_ms_request_id"] == "ms_support_123"
    assert record.custom_dimensions["request_id"] == "request_support_123"
    assert "private user query" not in caplog.text
    assert "Evidence" not in caplog.text
