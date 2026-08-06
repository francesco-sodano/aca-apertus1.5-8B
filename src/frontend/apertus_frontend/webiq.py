"""Direct Microsoft Web IQ passage retrieval for Apertus grounding."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .pipeline import Citation, GroundingPacket
from .resilience import (
    AsyncCircuitBreaker,
    RetryPolicy,
    is_retryable_service_error,
    retry_async,
)

logger = logging.getLogger("apertus.frontend.webiq")

DEFAULT_WEBIQ_ENDPOINT = "https://api.microsoft.ai/v3/search/web"


class WebIqSearchGateway:
    """Retrieve bounded passage evidence and citations from Microsoft Web IQ."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = DEFAULT_WEBIQ_ENDPOINT,
        max_results: int = 5,
        max_length: int = 1500,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> None:
        if not api_key.strip():
            raise ValueError("Web IQ API key cannot be empty.")
        if urlparse(endpoint).scheme != "https":
            raise ValueError("Web IQ endpoint must use HTTPS.")
        if not 1 <= max_results <= 50:
            raise ValueError("Web IQ max_results must be between 1 and 50.")
        if not 1 <= max_length <= 500_000:
            raise ValueError("Web IQ max_length must be between 1 and 500000.")

        self._api_key = api_key
        self._endpoint = endpoint.rstrip("/")
        self._max_results = max_results
        self._max_length = max_length
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None
        self._retry_policy = retry_policy
        self._circuit_breaker = AsyncCircuitBreaker()

    async def search(self, query: str) -> GroundingPacket:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise ValueError("Web IQ query cannot be empty.")
        if len(normalized_query) > 1000:
            raise ValueError("Web IQ query cannot exceed 1000 characters.")

        started = time.perf_counter()

        async def request() -> dict[str, Any]:
            response = await self._client.post(
                self._endpoint,
                headers={"x-apikey": self._api_key},
                json={
                    "query": normalized_query,
                    "maxResults": self._max_results,
                    "contentFormat": "passage",
                    "maxLength": self._max_length,
                    "safeSearch": "strict",
                },
            )
            _copy_retry_after_hint(response)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Web IQ returned a non-object response.")
            return payload

        payload = await self._circuit_breaker.call(
            lambda: retry_async(
                request,
                is_retryable=is_retryable_service_error,
                policy=self._retry_policy,
            ),
            is_failure=is_retryable_service_error,
        )
        packet = _extract_webiq_packet(payload, max_results=self._max_results)
        logger.info(
            "webiq_search_completed",
            extra={
                "custom_dimensions": {
                    "trace_id": str(payload.get("traceId") or ""),
                    "citation_count": len(packet.citations),
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "evidence_characters": len(packet.summary),
                }
            },
        )
        return packet

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _extract_webiq_packet(
    payload: dict[str, Any], *, max_results: int
) -> GroundingPacket:
    evidence: list[str] = []
    citations: list[Citation] = []
    fallback_items: list[str] = []
    seen_urls: set[str] = set()

    for item in payload.get("webResults") or []:
        if len(citations) >= max_results or not isinstance(item, dict):
            break
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()
        if (
            item.get("isAdult") is True
            or not title
            or not content
            or not _is_http_url(url)
            or url in seen_urls
        ):
            continue

        seen_urls.add(url)
        source_number = len(citations) + 1
        citations.append(Citation(title=title, url=url))
        crawled_at = str(item.get("crawledAt") or "unknown")
        updated_at = str(item.get("lastUpdatedAt") or "unknown")
        evidence.append(
            "\n".join(
                (
                    f'<WEB_SOURCE id="{source_number}">',
                    f"TITLE: {title}",
                    f"URL: {url}",
                    f"CRAWLED_AT: {crawled_at}",
                    f"LAST_UPDATED_AT: {updated_at}",
                    "PASSAGE:",
                    content,
                    "</WEB_SOURCE>",
                )
            )
        )
        excerpt = " ".join(content.split())[:400]
        fallback_items.append(f"- [{title}]({url}): {excerpt}")

    fallback_answer = ""
    if fallback_items:
        fallback_answer = (
            "I could not produce a fully verified synthesis. These cited source "
            "excerpts are available:\n\n" + "\n".join(fallback_items)
        )
    return GroundingPacket(
        summary="\n\n".join(evidence),
        citations=tuple(citations),
        fallback_answer=fallback_answer,
    )


def _copy_retry_after_hint(response: httpx.Response) -> None:
    if response.status_code != 429 or "Retry-After" in response.headers:
        return
    try:
        value = str(response.json().get("retryAfter") or "").strip()
    except (ValueError, AttributeError):
        return
    if value.lower().endswith("s"):
        value = value[:-1]
    try:
        float(value)
    except ValueError:
        return
    response.headers["Retry-After"] = value


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)