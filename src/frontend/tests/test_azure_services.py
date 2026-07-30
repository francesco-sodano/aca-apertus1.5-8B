from types import SimpleNamespace

import pytest

from apertus_frontend.azure_services import (
    FoundryWebSearchGateway,
    _blocked_category,
    _extract_foundry_result,
)
from apertus_frontend.pipeline import Citation


class FakeCredential:
    async def get_token(self, *scopes, **kwargs):
        return SimpleNamespace(token="token")


class RecordingClient:
    def __init__(self):
        self.body = None

    async def post(self, url, *, headers, json, params=None):
        self.body = json
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"output_text": "Evidence", "output": []},
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
    assert client.body["tools"][0]["search_context_size"] == "low"