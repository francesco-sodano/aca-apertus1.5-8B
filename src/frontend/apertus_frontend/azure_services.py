"""Managed-identity clients for Content Safety and Foundry Web Search."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any, Protocol
from uuid import uuid4

import httpx
from opentelemetry.trace import SpanKind

from .pipeline import (
    Attachment,
    Citation,
    GroundingPacket,
    SafetyAssessment,
    SafetyBlockedError,
)
from .resilience import (
    AsyncCircuitBreaker,
    RetryPolicy,
    is_retryable_service_error,
    retry_async,
)
from .tracing import record_messages, record_response_metadata, set_attributes, trace_scope, traced

logger = logging.getLogger("apertus.frontend.azure")

CONTENT_SAFETY_SCOPE = "https://cognitiveservices.azure.com/.default"
FOUNDRY_SCOPE = "https://ai.azure.com/.default"
CONTENT_SAFETY_API_VERSION = "2024-09-01"
GROUNDEDNESS_API_VERSION = "2024-09-15-preview"
MAX_GROUNDING_SUMMARY_CHARACTERS = 8_000
MAX_GROUNDING_CITATIONS = 5


class SafetyResponseError(RuntimeError):
    """A moderation service response did not contain an explicit valid decision."""


class AsyncTokenCredential(Protocol):
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...


class AzureContentSafetyGateway:
    """Apply prompt, content, image, evidence, and groundedness policy checks."""
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

    @traced("content_safety.text")
    async def screen_text(
        self, text: str, *, purpose: str
    ) -> SafetyAssessment | None:
        set_attributes(**{
            "apertus.safety.stage": purpose,
            "apertus.safety.threshold": self._threshold,
        })
        if not text.strip():
            set_attributes(**{"apertus.outcome": "skipped"})
            return None
        if purpose == "user-input":
            shield = await self._post(
                "/contentsafety/text:shieldPrompt",
                {"userPrompt": text},
                CONTENT_SAFETY_API_VERSION,
            )
            attack_detected = _attack_detected(shield.get("userPromptAnalysis"))
            set_attributes(**{
                "apertus.safety.attack_detected": attack_detected,
            })
            if attack_detected:
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
        _validate_categories(result, image=False)
        assessment = _highest_category_details(result)
        if assessment:
            set_attributes(**{
                "apertus.safety.category": assessment[0],
                "apertus.safety.severity": assessment[1],
            })
        if assessment and assessment[1] >= self._threshold:
            category, severity = assessment
            raise SafetyBlockedError(
                f"{purpose}: {category} severity {severity}",
                stage=purpose,
                rule=category,
                severity=severity,
                threshold=self._threshold,
            )
            set_attributes(**{"apertus.outcome": "allowed"})
        if assessment and assessment[1] > 0:
            return SafetyAssessment(
                category=assessment[0],
                severity=assessment[1],
                threshold=self._threshold,
            )
        return None

    @traced("content_safety.image")
    async def screen_image(self, attachment: Attachment) -> None:
        set_attributes(**{
            "apertus.safety.stage": "image",
            "apertus.safety.threshold": self._threshold,
            "apertus.image.mime_type": attachment.mime_type,
        })
        result = await self._post(
            "/contentsafety/image:analyze",
            {
                "image": {"content": attachment.data_base64},
                "outputType": "FourSeverityLevels",
            },
            CONTENT_SAFETY_API_VERSION,
        )
        _validate_categories(result, image=True)
        assessment = _highest_category_details(result)
        if assessment:
            set_attributes(**{
                "apertus.safety.category": assessment[0],
                "apertus.safety.severity": assessment[1],
            })
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
        set_attributes(**{"apertus.outcome": "allowed"})

    @traced("content_safety.grounding")
    async def screen_grounding(self, prompt: str, packet: GroundingPacket) -> None:
        set_attributes(**{"apertus.safety.stage": "grounding"})
        result = await self._post(
            "/contentsafety/text:shieldPrompt",
            {"userPrompt": prompt, "documents": [packet.summary]},
            CONTENT_SAFETY_API_VERSION,
        )
        analyses = result.get("documentsAnalysis")
        if not isinstance(analyses, list) or len(analyses) != 1:
            raise SafetyResponseError("Prompt Shield did not return one evidence decision.")
        attack_detected = _attack_detected(analyses[0])
        set_attributes(**{"apertus.safety.attack_detected": attack_detected})
        if attack_detected:
            raise SafetyBlockedError(
                "grounding: indirect prompt attack",
                stage="grounding",
                rule="Indirect prompt attack detection",
            )
        set_attributes(**{"apertus.outcome": "allowed"})

    @traced("groundedness.detect")
    async def is_grounded(
        self, *, query: str, answer: str, sources: tuple[str, ...]
    ) -> bool | None:
        set_attributes(**{"apertus.grounding_source_count": len(sources)})
        if not answer.strip() or not sources:
            set_attributes(**{"apertus.groundedness_result": "ungrounded"})
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
        if not isinstance(result.get("ungroundedDetected"), bool):
            set_attributes(**{"apertus.groundedness_result": "indeterminate"})
            return None
        grounded = not bool(result["ungroundedDetected"])
        set_attributes(**{"apertus.groundedness_result": "grounded" if grounded else "ungrounded"})
        return grounded

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
            set_attributes(**{"http.response.status_code": getattr(response, "status_code", None)})
            response.raise_for_status()
            return response.json()

        operation = path.rsplit(":", 1)[-1]
        with trace_scope(
            f"content_safety.{operation}",
            kind=SpanKind.CLIENT,
            attributes={"apertus.api_version": api_version},
        ):
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


def _attack_detected(analysis: Any) -> bool:
    if not isinstance(analysis, dict) or not isinstance(analysis.get("attackDetected"), bool):
        raise SafetyResponseError("Prompt Shield did not return an explicit attack decision.")
    return analysis["attackDetected"]


def _validate_categories(payload: dict[str, Any], *, image: bool) -> None:
    categories = payload.get("categoriesAnalysis")
    required = {"Hate", "SelfHarm", "Sexual", "Violence"}
    if not isinstance(categories, list) or len(categories) != len(required):
        raise SafetyResponseError("Content Safety did not return all category decisions.")
    severities = {0, 2, 4, 6} if image else set(range(8))
    seen = set()
    for category in categories:
        if not isinstance(category, dict):
            raise SafetyResponseError("Content Safety returned an invalid category decision.")
        name, severity = category.get("category"), category.get("severity")
        if (
            not isinstance(name, str)
            or name not in required
            or name in seen
            or type(severity) is not int
            or severity not in severities
        ):
            raise SafetyResponseError("Content Safety returned an invalid category decision.")
        seen.add(name)


class FoundryWebSearchGateway:
    """Retrieve and synthesize one cited public-web evidence packet."""
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

    @traced(
        "foundry.web_search",
        kind=SpanKind.CLIENT,
        attributes={"gen_ai.operation.name": "chat", "gen_ai.provider.name": "azure.ai.openai"},
    )
    async def search(self, query: str) -> GroundingPacket:
        started = time.perf_counter()
        client_request_id = str(uuid4())
        set_attributes(**{
            "gen_ai.request.model": self._model,
            "gen_ai.request.max_tokens": 600,
            "apertus.foundry.client_request_id": client_request_id,
        })
        record_messages("gen_ai.input.messages", [{"role": "user", "content": query}])

        async def request() -> tuple[dict[str, Any], dict[str, str]]:
            token = await self._credential.get_token(FOUNDRY_SCOPE)
            request_body: dict[str, Any] = {
                "model": self._model,
                "instructions": (
                    "Search the web and produce a concise, self-contained answer "
                    "grounded only in the retrieved sources. Follow the user's "
                    "requested language and output format. This response may be "
                    "shown directly if downstream validation is inconclusive. "
                    "Apply a strict safe-search policy: exclude adult or sexually "
                    "explicit results and do not quote unsafe material. Do not "
                    "mention internal processing. Keep it under 400 words."
                ),
                "input": query,
                "max_output_tokens": 600,
                "include": ["web_search_call.action.sources"],
                "tool_choice": "required",
                "tools": [
                    {
                        "type": "web_search",
                        "search_context_size": "low",
                    }
                ],
            }
            if self._model.startswith("gpt-5"):
                request_body.update(
                    {"reasoning": {"effort": "low"}, "text": {"verbosity": "low"}}
                )
            response = await self._client.post(
                f"{self._project_endpoint}/openai/v1/responses",
                headers={
                    "Authorization": f"Bearer {token.token}",
                    "x-ms-client-request-id": client_request_id,
                },
                json=request_body,
            )
            set_attributes(**{"http.response.status_code": getattr(response, "status_code", None)})
            response.raise_for_status()
            return response.json(), {
                "apim_request_id": response.headers.get("apim-request-id", ""),
                "x_ms_request_id": response.headers.get("x-ms-request-id", ""),
                "request_id": response.headers.get("x-request-id", ""),
            }

        payload, support_ids = await self._circuit_breaker.call(
            lambda: retry_async(
                request,
                is_retryable=is_retryable_service_error,
                policy=self._retry_policy,
            ),
            is_failure=is_retryable_service_error,
        )
        summary, citations = _extract_foundry_result(payload)
        summary = summary[:MAX_GROUNDING_SUMMARY_CHARACTERS].strip()
        web_search_calls = [
            item
            for item in payload.get("output", [])
            if isinstance(item, dict) and item.get("type") == "web_search_call"
        ]
        search_action_count = sum(
            1
            for item in web_search_calls
            if (item.get("action") or {}).get("type") == "search"
        )
        record_response_metadata(payload)
        record_messages("gen_ai.output.messages", [{"role": "assistant", "content": summary}])
        set_attributes(**{
            "apertus.web_search_call_count": len(web_search_calls),
            "apertus.search_action_count": search_action_count,
            "apertus.citation_count": len(citations),
            "apertus.evidence_characters": len(summary),
            **{f"apertus.foundry.{name}": value for name, value in support_ids.items() if value},
        })
        logger.info(
            "web_search_completed",
            extra={
                "custom_dimensions": {
                    "client_request_id": client_request_id,
                    "response_id": str(payload.get("id") or ""),
                    **support_ids,
                    "web_search_call_count": len(web_search_calls),
                    "search_action_count": search_action_count,
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
    """Return the first Content Safety category at or above the block threshold."""
    assessment = _highest_category_details(payload)
    if assessment is None or assessment[1] < threshold:
        return None
    return assessment


def _highest_category_details(
    payload: dict[str, Any],
) -> tuple[str, int] | None:
    assessments = [
        (str(item.get("category") or "Unknown"), int(item.get("severity") or 0))
        for item in payload.get("categoriesAnalysis") or []
    ]
    return max(assessments, key=lambda item: item[1], default=None)


def _extract_foundry_result(
    payload: dict[str, Any],
) -> tuple[str, tuple[Citation, ...]]:
    """Extract response text and deduplicated URLs from nested Responses output."""
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
    return (
        "\n\n".join(texts),
        tuple(unique.values())[:MAX_GROUNDING_CITATIONS],
    )


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)