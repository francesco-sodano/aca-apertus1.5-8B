"""Managed-identity client for Azure AI Content Safety."""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import httpx

from .pipeline import (
    Attachment,
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
CONTENT_SAFETY_API_VERSION = "2024-09-01"
GROUNDEDNESS_API_VERSION = "2024-09-15-preview"
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
    for item in payload.get("categoriesAnalysis") or []:
        severity = int(item.get("severity") or 0)
        if severity >= threshold:
            return str(item.get("category") or "Unknown"), severity
    return None
