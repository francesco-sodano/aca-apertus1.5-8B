from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any, Protocol

import httpx

from .pipeline import (
    Attachment,
    Citation,
    GroundingPacket,
    SafetyBlockedError,
)
from .resilience import (
    AsyncCircuitBreaker,
    RetryPolicy,
    is_retryable_service_error,
    retry_async,
)

logger = logging.getLogger("apertus.frontend.azure")

CONTENT_SAFETY_SCOPE = "https://cognitiveservices.azure.com/.default"
FOUNDRY_SCOPE = "https://ai.azure.com/.default"
CONTENT_SAFETY_API_VERSION = "2024-09-01"
GROUNDEDNESS_API_VERSION = "2024-09-15-preview"


class AsyncTokenCredential(Protocol):
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...


class AzureContentSafetyGateway:
    def __init__(
        self,
        *,
        endpoint: str,
        credential: AsyncTokenCredential,
        threshold: int = 4,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._credential = credential
        self._threshold = threshold
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._retry_policy = retry_policy
        self._circuit_breaker = AsyncCircuitBreaker()

    async def screen_text(self, text: str, *, purpose: str) -> None:
        if not text.strip():
            return
        if purpose == "user-input":
            shield = await self._post(
                "/contentsafety/text:shieldPrompt",
                {"userPrompt": text},
                CONTENT_SAFETY_API_VERSION,
            )
            if shield.get("userPromptAnalysis", {}).get("attackDetected") is True:
                raise SafetyBlockedError(f"{purpose}: prompt attack")

        result = await self._post(
            "/contentsafety/text:analyze",
            {"text": text, "outputType": "EightSeverityLevels"},
            CONTENT_SAFETY_API_VERSION,
        )
        blocked = _blocked_category(result, self._threshold)
        if blocked:
            raise SafetyBlockedError(f"{purpose}: {blocked}")

    async def screen_image(self, attachment: Attachment) -> None:
        result = await self._post(
            "/contentsafety/image:analyze",
            {
                "image": {"content": attachment.data_base64},
                "outputType": "FourSeverityLevels",
            },
            CONTENT_SAFETY_API_VERSION,
        )
        blocked = _blocked_category(result, self._threshold)
        if blocked:
            raise SafetyBlockedError(f"image: {blocked}")

    async def screen_grounding(self, prompt: str, packet: GroundingPacket) -> None:
        result = await self._post(
            "/contentsafety/text:shieldPrompt",
            {"userPrompt": prompt, "documents": [packet.summary]},
            CONTENT_SAFETY_API_VERSION,
        )
        analyses = result.get("documentsAnalysis") or []
        if any(item.get("attackDetected") is True for item in analyses):
            raise SafetyBlockedError("grounding: indirect prompt attack")

    async def is_grounded(
        self, *, query: str, answer: str, sources: tuple[str, ...]
    ) -> bool | None:
        if not answer.strip() or not sources:
            return False
        result = await self._post(
            "/contentsafety/text:detectGroundedness",
            {
                "domain": "Generic",
                "task": "QnA",
                "qna": {"query": query},
                "text": answer,
                "groundingSources": list(sources),
                "reasoning": False,
            },
            GROUNDEDNESS_API_VERSION,
        )
        if "ungroundedDetected" not in result:
            return None
        return not bool(result["ungroundedDetected"])

    async def _post(
        self, path: str, payload: dict[str, Any], api_version: str
    ) -> dict[str, Any]:
        async def request() -> dict[str, Any]:
            token = await self._credential.get_token(CONTENT_SAFETY_SCOPE)
            response = await self._client.post(
                f"{self._endpoint}{path}",
                params={"api-version": api_version},
                headers={"Authorization": f"Bearer {token.token}"},
                json=payload,
            )
            response.raise_for_status()
            return response.json()

        return await self._circuit_breaker.call(
            lambda: retry_async(
                request,
                is_retryable=is_retryable_service_error,
                policy=self._retry_policy,
            ),
            is_failure=is_retryable_service_error,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class FoundryWebSearchGateway:
    def __init__(
        self,
        *,
        project_endpoint: str,
        model: str,
        credential: AsyncTokenCredential,
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> None:
        self._project_endpoint = project_endpoint.rstrip("/")
        self._model = model
        self._credential = credential
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None
        self._retry_policy = retry_policy
        self._circuit_breaker = AsyncCircuitBreaker()

    async def search(self, query: str) -> GroundingPacket:
        started = time.perf_counter()

        async def request() -> dict[str, Any]:
            token = await self._credential.get_token(FOUNDRY_SCOPE)
            response = await self._client.post(
                f"{self._project_endpoint}/openai/v1/responses",
                headers={"Authorization": f"Bearer {token.token}"},
                json={
                    "model": self._model,
                    "instructions": (
                        "Search the web and return only concise factual evidence "
                        "useful for another model. Keep the response under 400 words."
                    ),
                    "input": query,
                    "reasoning": {"effort": "low"},
                    "text": {"verbosity": "low"},
                    "max_output_tokens": 600,
                    "include": ["web_search_call.action.sources"],
                    "tool_choice": "required",
                    "tools": [
                        {
                            "type": "web_search",
                            "search_context_size": "low",
                            "user_location": {
                                "type": "approximate",
                                "country": "SE",
                                "city": "Stockholm",
                                "region": "Stockholm County",
                            },
                        }
                    ],
                },
            )
            response.raise_for_status()
            return response.json()

        payload = await self._circuit_breaker.call(
            lambda: retry_async(
                request,
                is_retryable=is_retryable_service_error,
                policy=self._retry_policy,
            ),
            is_failure=is_retryable_service_error,
        )
        summary, citations = _extract_foundry_result(payload)
        logger.info(
            "web_search_completed",
            extra={
                "custom_dimensions": {
                    "citation_count": len(citations),
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "summary_characters": len(summary),
                }
            },
        )
        return GroundingPacket(summary=summary, citations=citations)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _blocked_category(payload: dict[str, Any], threshold: int) -> str | None:
    for item in payload.get("categoriesAnalysis") or []:
        severity = int(item.get("severity") or 0)
        if severity >= threshold:
            return f"{item.get('category', 'Unknown')} severity {severity}"
    return None


def _extract_foundry_result(
    payload: dict[str, Any],
) -> tuple[str, tuple[Citation, ...]]:
    texts: list[str] = []
    inline_citations: list[Citation] = []
    included_sources: list[Citation] = []

    direct_text = payload.get("output_text")
    if isinstance(direct_text, str) and direct_text.strip():
        texts.append(direct_text.strip())

    for item in _walk_dicts(payload.get("output", [])):
        if item.get("type") == "output_text" and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text and text not in texts:
                texts.append(text)
        if item.get("type") == "url_citation" and isinstance(item.get("url"), str):
            inline_citations.append(
                Citation(
                    title=str(item.get("title") or item["url"]),
                    url=item["url"],
                )
            )
        if item.get("type") == "url" and isinstance(item.get("url"), str):
            included_sources.append(
                Citation(
                    title=str(item.get("title") or item["url"]),
                    url=item["url"],
                )
            )

    unique: dict[str, Citation] = {}
    for citation in (*inline_citations, *included_sources):
        unique.setdefault(citation.url, citation)
    return "\n\n".join(texts), tuple(unique.values())[:5]


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)