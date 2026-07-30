"""Safety-gated chat orchestration with deterministic and semantic web routing."""

from __future__ import annotations

import base64
import binascii
import logging
import re
import time
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
MAX_HISTORY_TURNS = 6

_WEB_GROUNDING_PATTERN = re.compile(
    r"\b(?:"
    r"current|currently|latest|today|tonight|now|recent|recently|live|"
    r"real[- ]?time|up[- ]?to[- ]?date|as of|"
    r"search|look up|lookup|browse|web|internet|online|source|citation|verify|"
    r"double[- ]?check|cross[- ]?check|fact[- ]?check"
    r")\b",
    re.IGNORECASE,
)
_TEMPORAL_WEB_PATTERN = re.compile(
    r"\b(?:when|what(?:'s| is)?\s+(?:the\s+)?(?:date|time))\b"
    r".{0,160}\b(?:next|upcoming|planned|scheduled)\b|"
    r"\b(?:next|upcoming|planned|scheduled)\b.{0,160}\b(?:when|date|time)\b",
    re.IGNORECASE,
)
_FOLLOW_UP_PATTERN = re.compile(
    r"^(?:and|also|what about|how about|then|their|theirs|it|that|those)\b",
    re.IGNORECASE,
)
_LOCAL_TASK_PATTERN = re.compile(
    r"^(?:hi|hello|hey|who are you|what are you|"
    r"write|draft|rewrite|translate|summarize|explain|brainstorm|calculate|"
    r"solve|proofread|format|compose|create)\b",
    re.IGNORECASE,
)


class ChatProfile(StrEnum):
    TOOLS = "Tools"
    THINKING = "Thinking"


class ProgressStage(StrEnum):
    CHECKING_INPUT = "checking-input"
    SELECTING_TOOLS = "selecting-tools"
    SEARCHING_WEB = "searching-web"
    GENERATING = "generating"
    REFINING = "refining"
    USING_SEARCH_SUMMARY = "using-search-summary"
    CHECKING_OUTPUT = "checking-output"


class SafetyBlockedError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str = "content",
        rule: str = "Safety policy",
        severity: int | None = None,
        threshold: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.rule = rule
        self.severity = severity
        self.threshold = threshold

    @property
    def user_message(self) -> str:
        subjects = {
            "user-input": "Your message",
            "image": "The uploaded image",
            "grounding": "Retrieved web content",
            "model-output": "The generated answer",
        }
        subject = subjects.get(self.stage, "The content")
        if self.severity is not None and self.threshold is not None:
            details = (
                f"Rule: {self.rule}; severity {self.severity} met the configured "
                f"block threshold {self.threshold}."
            )
        else:
            details = f"Rule: {self.rule}."
        return f"{subject} was blocked by Azure AI Content Safety. {details}"


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
class GroundingDecision:
    use_web: bool
    search_query: str = ""


@dataclass(frozen=True)
class ChatTurn:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("Chat history roles must be user or assistant.")


@dataclass(frozen=True)
class ChatRequest:
    text: str
    attachments: tuple[Attachment, ...] = ()
    history: tuple[ChatTurn, ...] = ()
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
    async def route(self, query: str) -> GroundingDecision: ...

    async def search(self, query: str) -> GroundingPacket: ...


ToolSearch = Callable[[str], Awaitable[GroundingPacket]]
ProgressCallback = Callable[[ProgressStage], Awaitable[None]]


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
        self,
        request: ChatRequest,
        profile: ChatProfile,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> CompletionResult:
        started = time.perf_counter()
        self._validate_request(request)

        await _report_progress(on_progress, ProgressStage.CHECKING_INPUT)
        if request.text.strip():
            await self._safety.screen_text(request.text, purpose="user-input")

        for attachment in request.attachments:
            if attachment.is_image:
                await self._safety.screen_image(attachment)

        await _report_progress(on_progress, ProgressStage.SELECTING_TOOLS)
        query = grounding_query(request)
        route_hint = grounding_route_hint(request)
        route_source = "rule"
        if route_hint is None:
            route_source = "semantic"
            try:
                decision = await self._grounding.route(query)
            except Exception:
                logger.exception(
                    "grounding_route_failed",
                    extra={
                        "custom_dimensions": {
                            "correlation_id": request.correlation_id
                        }
                    },
                )
                decision = GroundingDecision(use_web=True, search_query=query)
                route_source = "safe-fallback"
        else:
            decision = GroundingDecision(
                use_web=route_hint,
                search_query=query if route_hint else "",
            )

        grounding_required = decision.use_web
        grounding = GroundingPacket(summary="")
        if grounding_required:
            await _report_progress(on_progress, ProgressStage.SEARCHING_WEB)
            search_query = decision.search_query.strip()
            if search_query and search_query != query:
                search_query = (
                    f"Search objective: {search_query}\n"
                    f"User request and response requirements: {query}"
                )
            else:
                search_query = query
            grounding = await self._get_safe_grounding(search_query)

        await _report_progress(on_progress, ProgressStage.GENERATING)
        completion = await self._model.complete(
            request=request,
            profile=profile,
            grounding=grounding,
            search_web=self._get_safe_grounding,
        )

        await _report_progress(on_progress, ProgressStage.CHECKING_OUTPUT)
        citations = _unique_citations(
            (*grounding.valid_citations, *completion.citations)
        )
        grounded, grounding_sources = await self._validate_completion(
            query=query,
            completion=completion,
            grounding=grounding,
        )
        groundedness_fallback_used = False
        if grounding_sources and grounded is not True:
            # Groundedness Detection is a preview API and can reject an otherwise
            # supported answer, especially across languages. Retry generation
            # once, then use only the cited and policy-screened search summary.
            await _report_progress(on_progress, ProgressStage.REFINING)
            retry_completion = await self._model.complete(
                request=request,
                profile=profile,
                grounding=grounding,
                search_web=self._get_safe_grounding,
            )
            await _report_progress(on_progress, ProgressStage.CHECKING_OUTPUT)
            retry_grounded, _ = await self._validate_completion(
                query=query,
                completion=retry_completion,
                grounding=grounding,
            )
            if retry_grounded is True:
                completion = retry_completion
                grounded = True
            elif grounding.summary.strip() and citations:
                await _report_progress(
                    on_progress, ProgressStage.USING_SEARCH_SUMMARY
                )
                await self._safety.screen_text(
                    grounding.summary, purpose="model-output"
                )
                completion = ModelCompletion(answer=grounding.summary)
                grounded = retry_grounded
                groundedness_fallback_used = True
                logger.warning(
                    "groundedness_fallback_used",
                    extra={
                        "custom_dimensions": {
                            "correlation_id": request.correlation_id,
                            "citation_count": len(citations),
                        }
                    },
                )
            else:
                raise GroundingUnavailableError(
                    "The generated answer did not receive an explicit groundedness approval."
                )

        logger.info(
            "completion_allowed",
            extra={
                "custom_dimensions": {
                    "correlation_id": request.correlation_id,
                    "profile": profile.value,
                    "attachment_count": len(request.attachments),
                    "citation_count": len(citations),
                    "web_grounding_required": grounding_required,
                    "grounding_route_source": route_source,
                    "groundedness_checked": grounded is not None,
                    "groundedness_fallback_used": groundedness_fallback_used,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                }
            },
        )
        return CompletionResult(
            answer=completion.answer,
            citations=citations,
            correlation_id=request.correlation_id,
        )

    async def _validate_completion(
        self,
        *,
        query: str,
        completion: ModelCompletion,
        grounding: GroundingPacket,
    ) -> tuple[bool | None, tuple[str, ...]]:
        await self._safety.screen_text(completion.answer, purpose="model-output")
        grounding_sources = tuple(
            source
            for source in (grounding.summary, *completion.grounding_sources)
            if source.strip()
        )
        if not grounding_sources:
            return None, ()
        grounded = await self._safety.is_grounded(
            query=query,
            answer=completion.answer,
            sources=grounding_sources,
        )
        return grounded, grounding_sources

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


def grounding_route_hint(request: ChatRequest) -> bool | None:
    """Return an obvious route or defer ambiguous factual intent to Foundry."""
    current = request.text.strip()
    if (
        _WEB_GROUNDING_PATTERN.search(current)
        or _TEMPORAL_WEB_PATTERN.search(current)
    ):
        return True
    words = current.split()
    contextual_follow_up = bool(_FOLLOW_UP_PATTERN.search(current)) or (
        len(words) <= 2 and current.endswith("?")
    )
    if contextual_follow_up:
        context = "\n".join(
            turn.content for turn in request.history[-MAX_HISTORY_TURNS:]
        )
        if _WEB_GROUNDING_PATTERN.search(context):
            return True
    if _LOCAL_TASK_PATTERN.search(current) or (not current and request.attachments):
        return False
    return None


def grounding_query(request: ChatRequest) -> str:
    """Build a standalone query while preserving recent reference context."""
    current = request.text.strip() or _media_grounding_query(request.attachments)
    if not request.history:
        return current
    history = "\n".join(
        f"{turn.role.title()}: {turn.content[:2000]}"
        for turn in request.history[-MAX_HISTORY_TURNS:]
    )
    return (
        "Use the recent conversation only to resolve references in the current "
        f"request.\n{history}\nCurrent user request: {current}"
    )


async def _report_progress(
    callback: ProgressCallback | None, stage: ProgressStage
) -> None:
    if callback is None:
        return
    try:
        await callback(stage)
    except Exception:
        logger.warning(
            "progress_callback_failed",
            extra={"custom_dimensions": {"stage": stage.value}},
            exc_info=True,
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