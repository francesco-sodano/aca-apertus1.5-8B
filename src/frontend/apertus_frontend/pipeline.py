from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlparse
from uuid import uuid4

logger = logging.getLogger("apertus.frontend.pipeline")

MAX_TEXT_CHARACTERS = 32_000
MAX_IMAGES = 4
MAX_AUDIO_FILES = 1
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024


class ChatProfile(StrEnum):
    TOOLS = "Tools"
    THINKING = "Thinking"


class SafetyBlockedError(RuntimeError):
    pass


class GroundingUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class Attachment:
    name: str
    mime_type: str
    data_base64: str

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")

    @property
    def is_audio(self) -> bool:
        return self.mime_type.startswith("audio/")


@dataclass(frozen=True)
class Citation:
    title: str
    url: str


@dataclass(frozen=True)
class GroundingPacket:
    summary: str
    citations: tuple[Citation, ...] = ()

    @property
    def valid_citations(self) -> tuple[Citation, ...]:
        return tuple(citation for citation in self.citations if _is_web_url(citation.url))


@dataclass(frozen=True)
class ChatRequest:
    text: str
    attachments: tuple[Attachment, ...] = ()
    correlation_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class ModelCompletion:
    answer: str
    citations: tuple[Citation, ...] = ()
    grounding_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompletionResult:
    answer: str
    citations: tuple[Citation, ...]
    correlation_id: str


class SafetyGateway(Protocol):
    async def screen_text(self, text: str, *, purpose: str) -> None: ...

    async def screen_image(self, attachment: Attachment) -> None: ...

    async def screen_grounding(self, prompt: str, packet: GroundingPacket) -> None: ...

    async def is_grounded(
        self, *, query: str, answer: str, sources: tuple[str, ...]
    ) -> bool | None: ...


class GroundingGateway(Protocol):
    async def search(self, query: str) -> GroundingPacket: ...


ToolSearch = Callable[[str], Awaitable[GroundingPacket]]


class ModelGateway(Protocol):
    async def complete(
        self,
        *,
        request: ChatRequest,
        profile: ChatProfile,
        grounding: GroundingPacket,
        search_web: ToolSearch,
    ) -> ModelCompletion: ...


class GroundedCompletionService:
    """Fail-closed boundary around every Apertus invocation."""

    def __init__(
        self,
        *,
        safety: SafetyGateway,
        grounding: GroundingGateway,
        model: ModelGateway,
    ) -> None:
        self._safety = safety
        self._grounding = grounding
        self._model = model

    async def complete(
        self, request: ChatRequest, profile: ChatProfile
    ) -> CompletionResult:
        self._validate_request(request)

        if request.text.strip():
            await self._safety.screen_text(request.text, purpose="user-input")

        for attachment in request.attachments:
            if attachment.is_image:
                await self._safety.screen_image(attachment)

        query = request.text.strip() or _media_grounding_query(request.attachments)
        grounding = await self._get_safe_grounding(query)

        if not grounding.valid_citations and not request.attachments:
            raise GroundingUnavailableError(
                "Web Search returned no usable citations; Apertus was not called."
            )

        completion = await self._model.complete(
            request=request,
            profile=profile,
            grounding=grounding,
            search_web=self._get_safe_grounding,
        )

        await self._safety.screen_text(completion.answer, purpose="model-output")
        grounded = await self._safety.is_grounded(
            query=query,
            answer=completion.answer,
            sources=(grounding.summary, *completion.grounding_sources),
        )
        if grounded is not True:
            raise GroundingUnavailableError(
                "The generated answer did not receive an explicit groundedness approval."
            )

        citations = _unique_citations(
            (*grounding.valid_citations, *completion.citations)
        )
        logger.info(
            "completion_allowed",
            extra={
                "custom_dimensions": {
                    "correlation_id": request.correlation_id,
                    "profile": profile.value,
                    "attachment_count": len(request.attachments),
                    "citation_count": len(citations),
                    "groundedness_checked": grounded is not None,
                }
            },
        )
        return CompletionResult(
            answer=completion.answer,
            citations=citations,
            correlation_id=request.correlation_id,
        )

    async def _get_safe_grounding(self, query: str) -> GroundingPacket:
        packet = await self._grounding.search(query)
        if not packet.summary.strip():
            raise GroundingUnavailableError(
                "Web Search returned no evidence; Apertus was not called."
            )
        await self._safety.screen_grounding(query, packet)
        return packet

    @staticmethod
    def _validate_request(request: ChatRequest) -> None:
        if not request.text.strip() and not request.attachments:
            raise ValueError("A message or attachment is required.")
        if len(request.text) > MAX_TEXT_CHARACTERS:
            raise ValueError(
                f"Message exceeds the {MAX_TEXT_CHARACTERS}-character limit."
            )
        if sum(item.is_image for item in request.attachments) > MAX_IMAGES:
            raise ValueError(f"A request can include at most {MAX_IMAGES} images.")
        if sum(item.is_audio for item in request.attachments) > MAX_AUDIO_FILES:
            raise ValueError(
                f"A request can include at most {MAX_AUDIO_FILES} audio file."
            )
        for attachment in request.attachments:
            if not attachment.is_image and not attachment.is_audio:
                raise ValueError(f"Unsupported attachment type: {attachment.mime_type}")
            if not attachment.data_base64:
                raise ValueError(f"Attachment is empty: {attachment.name}")
            try:
                decoded_size = len(
                    base64.b64decode(attachment.data_base64, validate=True)
                )
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    f"Attachment is not valid base64: {attachment.name}"
                ) from exc
            byte_limit = (
                MAX_IMAGE_BYTES if attachment.is_image else MAX_AUDIO_BYTES
            )
            if decoded_size > byte_limit:
                raise ValueError(
                    f"Attachment exceeds the {byte_limit}-byte limit: "
                    f"{attachment.name}"
                )


def _media_grounding_query(attachments: tuple[Attachment, ...]) -> str:
    kinds = sorted({"image" if item.is_image else "audio" for item in attachments})
    return (
        "Find authoritative public context that may help answer a request about "
        f"the user-provided {', '.join(kinds)} attachment."
    )


def _is_web_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _unique_citations(citations: tuple[Citation, ...]) -> tuple[Citation, ...]:
    unique: list[Citation] = []
    seen: set[str] = set()
    for citation in citations:
        if not _is_web_url(citation.url) or citation.url in seen:
            continue
        seen.add(citation.url)
        unique.append(citation)
    return tuple(unique)