"""Safety-gated chat orchestration with native Apertus tool selection."""

from __future__ import annotations

import base64
import binascii
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from .tools import (
    ToolCall,
    ToolCitation,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    calculator_spec,
    current_time_spec,
    search_web_spec,
)

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
    r"write|draft|rewrite|translate|summarize|explain|brainstorm|"
    r"proofread|format|compose|create)\b|"
    r"^(?:what is|what's)\s+the\s+capital\s+of\b",
    re.IGNORECASE,
)
_YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")
_MODEL_REFUSAL_PATTERN = re.compile(
    r"^\s*(?:i\s+(?:cannot|can't|won't|will not|am unable to)|"
    r"(?:i(?:'m| am)\s+)?sorry[,;:]?\s+(?:but\s+)?i\s+"
    r"(?:cannot|can't|won't|will not|am unable to))\b",
    re.IGNORECASE,
)
_ACTIONABLE_HARM_PATTERN = re.compile(
    r"\b(?:molotov|bomb|explosive|incendiary|weapon|firearm|poison|"
    r"kill|murder|attack|self[- ]?harm|suicide|malware|ransomware|"
    r"phishing|steal credentials)\b",
    re.IGNORECASE,
)


class ChatProfile(StrEnum):
    TOOLS = "Tools"
    THINKING = "Thinking"


class ProgressStage(StrEnum):
    CHECKING_INPUT = "checking-input"
    SELECTING_TOOLS = "selecting-tools"
    USING_TOOL = "using-tool"
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
        return (
            "Your message was blocked by Azure AI Content Safety. "
            f"Rule: {self.rule}."
        )


@dataclass(frozen=True)
class SafetyAssessment:
    category: str
    severity: int
    threshold: int


class GroundingUnavailableError(RuntimeError):
    def __init__(self, message: str, *, source: str, rule: str) -> None:
        super().__init__(message)
        self.source = source
        self.rule = rule


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
    selected_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompletionResult:
    answer: str
    citations: tuple[Citation, ...]
    correlation_id: str
    selected_tools: tuple[str, ...] = ()


class SafetyGateway(Protocol):
    async def screen_text(
        self, text: str, *, purpose: str
    ) -> SafetyAssessment | None: ...

    async def screen_image(self, attachment: Attachment) -> None: ...

    async def screen_grounding(self, prompt: str, packet: GroundingPacket) -> None: ...

    async def is_grounded(
        self, *, query: str, answer: str, sources: tuple[str, ...]
    ) -> bool | None: ...


class GroundingGateway(Protocol):
    async def search(self, query: str) -> GroundingPacket: ...


ProgressCallback = Callable[[ProgressStage, str | None], Awaitable[None]]


class ModelGateway(Protocol):
    async def complete(
        self,
        *,
        request: ChatRequest,
        profile: ChatProfile,
        grounding: GroundingPacket,
        tools: tuple[ToolSpec, ...],
        execute_tool: ToolExecutor,
        require_tool: bool,
    ) -> ModelCompletion: ...


class GroundedCompletionService:
    """Fail-closed boundary around every Apertus invocation."""

    def __init__(
        self,
        *,
        safety: SafetyGateway,
        grounding: GroundingGateway,
        model: ModelGateway,
        additional_tools: tuple[ToolSpec, ...] = (),
    ) -> None:
        self._safety = safety
        self._grounding = grounding
        self._model = model
        self._additional_tools = additional_tools

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
        input_safety_assessment = None
        if request.text.strip():
            input_safety_assessment = await self._safety.screen_text(
                request.text, purpose="user-input"
            )

        for attachment in request.attachments:
            if attachment.is_image:
                await self._safety.screen_image(attachment)

        await _report_progress(on_progress, ProgressStage.SELECTING_TOOLS)
        query = grounding_query(request)
        route_hint = grounding_route_hint(request)

        async def search_web(arguments: dict[str, object]) -> ToolResult:
            objective = str(arguments["query"]).strip()
            search_query = (
                f"Search objective selected by Apertus: {objective}\n"
                f"LATEST USER REQUEST: {request.text.strip()[:1000]}\n"
                f"Conversation context for reference resolution only: {query[:2000]}"
            )
            return await self._search_web({"query": search_query})

        tool_registry = ToolRegistry(
            (
                search_web_spec(search_web),
                calculator_spec(),
                current_time_spec(),
                *self._additional_tools,
            )
        )

        async def execute_tool(call: ToolCall) -> ToolResult:
            try:
                spec = tool_registry.get(call.name)
                await _report_progress(
                    on_progress, ProgressStage.USING_TOOL, spec.display_name
                )
                logger.info(
                    "tool_selected",
                    extra={
                        "custom_dimensions": {
                            "correlation_id": request.correlation_id,
                            "tool": call.name,
                        }
                    },
                )
                result = await tool_registry.execute(call)
                await self._safety.screen_text(result.content, purpose="tool-output")
            except Exception:
                logger.exception(
                    "tool_rejected",
                    extra={
                        "custom_dimensions": {
                            "correlation_id": request.correlation_id,
                            "tool": call.name,
                        }
                    },
                )
                raise
            logger.info(
                "tool_completed",
                extra={
                    "custom_dimensions": {
                        "correlation_id": request.correlation_id,
                        "tool": call.name,
                        "citation_count": len(result.citations),
                    }
                },
            )
            return result

        grounding = GroundingPacket(summary="")
        await _report_progress(on_progress, ProgressStage.GENERATING)
        completion = await self._model.complete(
            request=request,
            profile=profile,
            grounding=grounding,
            tools=(
                tool_registry.specs
                if profile is ChatProfile.TOOLS and route_hint is not False
                else ()
            ),
            execute_tool=execute_tool,
            require_tool=route_hint is True,
        )

        await _report_progress(on_progress, ProgressStage.CHECKING_OUTPUT)
        citations = _unique_citations(completion.citations)
        grounding = GroundingPacket(
            summary="\n\n".join(completion.grounding_sources),
            citations=citations,
        )
        selected_tools = completion.selected_tools
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
                tools=(),
                execute_tool=execute_tool,
                require_tool=False,
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
                    "The generated answer did not receive an explicit groundedness approval.",
                    source="Azure AI Content Safety Groundedness Detection",
                    rule="Groundedness approval required",
                )

        model_refusal_explained = False
        if (
            not groundedness_fallback_used
            and _looks_like_model_refusal(completion.answer)
        ):
            completion = ModelCompletion(
                answer=_explain_model_refusal(
                    request=request,
                    answer=completion.answer,
                    assessment=input_safety_assessment,
                )
            )
            model_refusal_explained = True

        logger.info(
            "completion_allowed",
            extra={
                "custom_dimensions": {
                    "correlation_id": request.correlation_id,
                    "profile": profile.value,
                    "attachment_count": len(request.attachments),
                    "citation_count": len(citations),
                    "selected_tools": ",".join(selected_tools),
                    "groundedness_checked": grounded is not None,
                    "groundedness_fallback_used": groundedness_fallback_used,
                    "model_refusal_explained": model_refusal_explained,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                }
            },
        )
        return CompletionResult(
            answer=completion.answer,
            citations=citations,
            correlation_id=request.correlation_id,
            selected_tools=selected_tools,
        )

    async def _search_web(self, arguments: dict[str, object]) -> ToolResult:
        query = str(arguments["query"]).strip()
        packet = await self._get_safe_grounding(query)
        return ToolResult(
            content=packet.summary,
            citations=tuple(
                ToolCitation(citation.title, citation.url)
                for citation in packet.valid_citations
            ),
            grounding_sources=(packet.summary,),
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
            dict.fromkeys(
                source
                for source in (grounding.summary, *completion.grounding_sources)
                if source.strip()
            )
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
                "Web Search returned no evidence; Apertus was not called.",
                source="Microsoft Foundry Web Search",
                rule="Grounding evidence unavailable",
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
    """Identify requests that require some tool while Apertus chooses its name."""
    current = request.text.strip()
    if (
        _WEB_GROUNDING_PATTERN.search(current)
        or _TEMPORAL_WEB_PATTERN.search(current)
    ):
        return True
    current_year = datetime.now(UTC).year
    if any(int(year) >= current_year for year in _YEAR_PATTERN.findall(current)):
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


def _looks_like_model_refusal(answer: str) -> bool:
    return bool(_MODEL_REFUSAL_PATTERN.search(answer[:500]))


def _explain_model_refusal(
    *,
    request: ChatRequest,
    answer: str,
    assessment: SafetyAssessment | None,
) -> str:
    if assessment is not None:
        rule = assessment.category
    elif _ACTIONABLE_HARM_PATTERN.search(request.text):
        rule = "Violence"
    else:
        rule = "Model safety policy"
    return f"Your message was blocked by Apertus. Rule: {rule}."


async def _report_progress(
    callback: ProgressCallback | None,
    stage: ProgressStage,
    detail: str | None = None,
) -> None:
    if callback is None:
        return
    try:
        await callback(stage, detail)
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