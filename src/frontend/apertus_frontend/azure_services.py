from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from typing import Any, Protocol

import httpx

from .pipeline import (
    Attachment,
    Citation,
    GroundingDecision,
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
GROUNDING_ROUTER_INSTRUCTIONS = """Decide whether a reliable answer requires live public web information.

Use web for information that can change after model training, including current,
latest, upcoming, planned, scheduled, status, price, office holder, release, or
availability facts. Also use web when the user explicitly asks to search,
browse, cite, verify, double-check, or use the internet.

Do not use web for identity, greetings, writing, translation, summarization of
provided content, timeless explanations, math, or stable facts.

Conversation history is context for resolving references, not evidence. Never
skip web because an earlier assistant answer appears to contain the requested
changing fact. A follow-up about a real-world next, upcoming, planned, or
scheduled occurrence requires fresh web information.

When web is required, produce a standalone search query that resolves references
using the supplied conversation. Otherwise return an empty search query."""


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
                raise SafetyBlockedError(
                    f"{purpose}: prompt attack",
                    stage=purpose,
                    rule="Prompt attack detection",
                )

        result = await self._post(
            "/contentsafety/text:analyze",
            {"text": text, "outputType": "EightSeverityLevels"},
            CONTENT_SAFETY_API_VERSION,
        )
        blocked = _blocked_category_details(result, self._threshold)
        if blocked:
            category, severity = blocked
            raise SafetyBlockedError(
                f"{purpose}: {category} severity {severity}",
                stage=purpose,
                rule=category,
                severity=severity,
                threshold=self._threshold,
            )

    async def screen_image(self, attachment: Attachment) -> None:
        result = await self._post(
            "/contentsafety/image:analyze",
            {
                "image": {"content": attachment.data_base64},
                "outputType": "FourSeverityLevels",
            },
            CONTENT_SAFETY_API_VERSION,
        )
        blocked = _blocked_category_details(result, self._threshold)
        if blocked:
            category, severity = blocked
            raise SafetyBlockedError(
                f"image: {category} severity {severity}",
                stage="image",
                rule=category,
                severity=severity,
                threshold=self._threshold,
            )

    async def screen_grounding(self, prompt: str, packet: GroundingPacket) -> None:
        result = await self._post(
            "/contentsafety/text:shieldPrompt",
            {"userPrompt": prompt, "documents": [packet.summary]},
            CONTENT_SAFETY_API_VERSION,
        )
        analyses = result.get("documentsAnalysis") or []
        if any(item.get("attackDetected") is True for item in analyses):
            raise SafetyBlockedError(
                "grounding: indirect prompt attack",
                stage="grounding",
                rule="Indirect prompt attack detection",
            )

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
        self._router_circuit_breaker = AsyncCircuitBreaker()

    async def route(self, query: str) -> GroundingDecision:
        started = time.perf_counter()

        async def request() -> dict[str, Any]:
            token = await self._credential.get_token(FOUNDRY_SCOPE)
            response = await self._client.post(
                f"{self._project_endpoint}/openai/v1/responses",
                headers={"Authorization": f"Bearer {token.token}"},
                json={
                    "model": self._model,
                    "instructions": GROUNDING_ROUTER_INSTRUCTIONS,
                    "input": query,
                    "max_output_tokens": 150,
                    "temperature": 0,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "grounding_route",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "use_web": {"type": "boolean"},
                                    "search_query": {"type": "string"},
                                },
                                "required": ["use_web", "search_query"],
                                "additionalProperties": False,
                            },
                        }
                    },
                },
            )
            response.raise_for_status()
            return response.json()

        payload = await self._router_circuit_breaker.call(
            lambda: retry_async(
                request,
                is_retryable=is_retryable_service_error,
                policy=self._retry_policy,
            ),
            is_failure=is_retryable_service_error,
        )
        raw_decision, _ = _extract_foundry_result(payload)
        decision_data = json.loads(raw_decision)
        use_web = decision_data.get("use_web")
        search_query = decision_data.get("search_query")
        if not isinstance(use_web, bool) or not isinstance(search_query, str):
            raise ValueError("Grounding router returned an invalid decision.")
        if not use_web:
            search_query = ""
        logger.info(
            "grounding_route_completed",
            extra={
                "custom_dimensions": {
                    "use_web": use_web,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                }
            },
        )
        return GroundingDecision(use_web=use_web, search_query=search_query.strip())

    async def search(self, query: str) -> GroundingPacket:
        started = time.perf_counter()

        async def request() -> dict[str, Any]:
            token = await self._credential.get_token(FOUNDRY_SCOPE)
            request_body: dict[str, Any] = {
                "model": self._model,
                "instructions": (
                    "Search the web and return only concise factual evidence "
                    "useful for another model. Keep the response under 400 words."
                ),
                "input": query,
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
            }
            if self._model.startswith("gpt-5"):
                request_body.update(
                    {"reasoning": {"effort": "low"}, "text": {"verbosity": "low"}}
                )
            response = await self._client.post(
                f"{self._project_endpoint}/openai/v1/responses",
                headers={"Authorization": f"Bearer {token.token}"},
                json=request_body,
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
    blocked = _blocked_category_details(payload, threshold)
    if blocked is None:
        return None
    category, severity = blocked
    return f"{category} severity {severity}"


def _blocked_category_details(
    payload: dict[str, Any], threshold: int
) -> tuple[str, int] | None:
    for item in payload.get("categoriesAnalysis") or []:
        severity = int(item.get("severity") or 0)
        if severity >= threshold:
            return str(item.get("category") or "Unknown"), severity
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