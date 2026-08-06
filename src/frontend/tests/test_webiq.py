import httpx
import pytest

from apertus_frontend.pipeline import Citation
from apertus_frontend.webiq import (
    WebIqSearchGateway,
    _copy_retry_after_hint,
    _extract_webiq_packet,
)


class RecordingClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.url = None
        self.headers = None
        self.body = None

    async def post(self, url, *, headers, json):
        self.url = url
        self.headers = headers
        self.body = json
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json=self.payload)


def test_extracts_bounded_unique_non_adult_passages() -> None:
    payload = {
        "webResults": [
            {
                "title": "Primary",
                "url": "https://example.com/primary",
                "content": "Relevant primary passage.",
                "crawledAt": "2026-08-01T10:00:00Z",
                "lastUpdatedAt": "2026-08-01T09:00:00Z",
                "isAdult": False,
            },
            {
                "title": "Duplicate",
                "url": "https://example.com/primary",
                "content": "Duplicate passage.",
                "isAdult": False,
            },
            {
                "title": "Unsafe",
                "url": "https://example.com/unsafe",
                "content": "Filtered passage.",
                "isAdult": True,
            },
            {
                "title": "Secondary",
                "url": "https://example.com/secondary",
                "content": "Relevant secondary passage.",
                "isAdult": False,
            },
        ]
    }

    packet = _extract_webiq_packet(payload, max_results=2)

    assert packet.citations == (
        Citation(title="Primary", url="https://example.com/primary"),
        Citation(title="Secondary", url="https://example.com/secondary"),
    )
    assert '<WEB_SOURCE id="1">' in packet.summary
    assert "Relevant primary passage." in packet.summary
    assert "Filtered passage." not in packet.summary
    assert "Duplicate passage." not in packet.summary
    assert "[Primary](https://example.com/primary)" in packet.fallback_answer


@pytest.mark.asyncio
async def test_search_uses_strict_bounded_passage_request() -> None:
    client = RecordingClient(
        {
            "traceId": "trace-123",
            "webResults": [
                {
                    "title": "Source",
                    "url": "https://example.com/source",
                    "content": "Evidence",
                    "isAdult": False,
                }
            ],
        }
    )
    gateway = WebIqSearchGateway(
        api_key="secret-key",
        client=client,
        max_results=5,
        max_length=1500,
    )

    packet = await gateway.search("  current   information  ")

    assert client.url == "https://api.microsoft.ai/v3/search/web"
    assert client.headers == {"x-apikey": "secret-key"}
    assert client.body == {
        "query": "current information",
        "maxResults": 5,
        "contentFormat": "passage",
        "maxLength": 1500,
        "safeSearch": "strict",
    }
    assert packet.citations[0].url == "https://example.com/source"


def test_rejects_invalid_configuration_and_query() -> None:
    with pytest.raises(ValueError, match="API key"):
        WebIqSearchGateway(api_key="")
    with pytest.raises(ValueError, match="HTTPS"):
        WebIqSearchGateway(api_key="key", endpoint="http://example.com")


def test_copies_body_retry_after_hint_to_response_header() -> None:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://api.microsoft.ai/v3/search/web"),
        json={"retryAfter": "12s"},
    )

    _copy_retry_after_hint(response)

    assert response.headers["Retry-After"] == "12"