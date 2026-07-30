"""Chainlit and health endpoints for the authenticated Apertus frontend."""

from __future__ import annotations

import base64
import hmac
import logging
import os
import re
from pathlib import Path
from typing import List, Optional

import chainlit as cl
import httpx
from azure.identity.aio import DefaultAzureCredential
from chainlit.server import app
from chainlit.context import context
from fastapi import Header, HTTPException, Response, status

from apertus_frontend.azure_services import (
    AzureContentSafetyGateway,
    FoundryWebSearchGateway,
)
from apertus_frontend.pipeline import (
    Attachment,
    ChatProfile,
    ChatRequest,
    ChatTurn,
    CompletionResult,
    GroundedCompletionService,
    GroundingUnavailableError,
    MAX_HISTORY_TURNS,
    ProgressStage,
    SafetyBlockedError,
)
from apertus_frontend.settings import Settings
from apertus_frontend.vllm_gateway import VllmGateway
from apertus_frontend.resilience import (
    CapacityExceededError,
    RateLimitExceededError,
    RequestAdmissionController,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("apertus.frontend")

if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(logger_name="apertus")


class Runtime:
    def __init__(
        self,
        *,
        settings: Settings,
        service: GroundedCompletionService,
        credential: DefaultAzureCredential,
        safety: AzureContentSafetyGateway,
        grounding: FoundryWebSearchGateway,
        model: VllmGateway,
        admission: RequestAdmissionController,
    ) -> None:
        self.settings = settings
        self.service = service
        self.credential = credential
        self.safety = safety
        self.grounding = grounding
        self.model = model
        self.admission = admission

    async def aclose(self) -> None:
        await self.safety.aclose()
        await self.grounding.aclose()
        await self.model.aclose()
        await self.credential.close()


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is not None:
        return _runtime

    settings = Settings.from_env()
    credential = DefaultAzureCredential()
    safety = AzureContentSafetyGateway(
        endpoint=settings.content_safety_endpoint,
        credential=credential,
        threshold=settings.content_safety_threshold,
    )
    grounding = FoundryWebSearchGateway(
        project_endpoint=settings.foundry_project_endpoint,
        model=settings.foundry_grounding_model,
        credential=credential,
        timeout_seconds=settings.grounding_timeout_seconds,
    )
    model = VllmGateway(
        base_url=settings.model_endpoint,
        api_key=settings.vllm_api_key,
        model=settings.model_id,
        max_tokens=settings.max_output_tokens,
        temperature=settings.temperature,
        timeout_seconds=settings.model_timeout_seconds,
    )
    _runtime = Runtime(
        settings=settings,
        service=GroundedCompletionService(
            safety=safety,
            grounding=grounding,
            model=model,
        ),
        credential=credential,
        safety=safety,
        grounding=grounding,
        model=model,
        admission=RequestAdmissionController(
            max_concurrent=settings.max_concurrent_requests,
            requests_per_minute=settings.requests_per_minute,
            queue_timeout_seconds=settings.admission_queue_timeout_seconds,
        ),
    )
    return _runtime


@app.on_event("shutdown")
async def shutdown_runtime() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.aclose()
        _runtime = None


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz/ready", include_in_schema=False)
async def readiness(response: Response) -> dict[str, str]:
    try:
        get_runtime()
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not-ready"}
    return {"status": "ready"}


@app.get("/healthz/model", include_in_schema=False)
async def model_health(
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    runtime = get_runtime()
    expected = f"Bearer {runtime.settings.health_token}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    model_root = runtime.settings.model_endpoint.removesuffix("/v1")
    async with httpx.AsyncClient(timeout=30.0) as client:
        result = await client.get(
            f"{model_root}/health",
            headers={"Authorization": f"Bearer {runtime.settings.vllm_api_key}"},
        )
    if result.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return {"status": "ready"}


@cl.set_chat_profiles
async def chat_profiles(user=None):
    return [
        cl.ChatProfile(
            name=ChatProfile.TOOLS.value,
            markdown_description="Fast answers with automatic live web grounding when needed.",
            default=True,
        ),
        cl.ChatProfile(
            name=ChatProfile.THINKING.value,
            markdown_description="Grounded answers with Apertus deliberation enabled.",
        ),
    ]


@cl.set_starters
async def set_starters(
    user: Optional["cl.User"] = None,
    project: Optional[str] = None,
) -> List[cl.Starter]:
    return [
        cl.Starter(
            label="Morning routine ideation",
            message=(
                "Can you help me create a personalized morning routine that would "
                "help increase my productivity throughout the day? Start by asking "
                "me about my current habits and what activities energize me in the "
                "morning."
            ),
            icon="/public/idea.svg",
        ),
        cl.Starter(
            label="Explain Gravity at high school",
            message=(
                "Give a simple explanation of what gravity is for a high school "
                "level physics course with a few typical formulas. Use lots of "
                "emojis and do it in French, Swiss German, Italian and Romansh."
            ),
            icon="/public/learn.svg",
        ),
        cl.Starter(
            label="Swiss Bundesrat Members",
            message="Who are the current members of the Swiss Bundesrat?",
            icon="/public/swiss-flag.svg",
        ),
        cl.Starter(
            label="Text inviting friend to wedding",
            message=(
                "Write a text asking a friend to be my plus-one at a wedding next "
                "month. I want to keep it super short and casual, and offer an out."
            ),
            icon="/public/write.svg",
        ),
    ]


@cl.on_message
async def on_message(message: cl.Message) -> None:
    request = ChatRequest(
        text=message.content or "",
        attachments=tuple(_read_attachment(element) for element in message.elements or []),
        history=_conversation_history(),
    )
    profile_name = cl.user_session.get("chat_profile") or ChatProfile.TOOLS.value
    profile = (
        ChatProfile.THINKING
        if profile_name == ChatProfile.THINKING.value
        else ChatProfile.TOOLS
    )

    if any(item.is_audio for item in request.attachments):
        await cl.Message(
            content=(
                "Audio is sent directly to Apertus and is not inspected by "
                "Azure AI Content Safety."
            )
        ).send()

    activity = cl.Message(content="Getting ready...")
    await activity.send()

    async def report_progress(stage: ProgressStage) -> None:
        activity.content = _progress_text(stage, profile)
        await activity.update()

    try:
        runtime = get_runtime()
        async with runtime.admission.admit(_requester_key()):
            result = await runtime.service.complete(
                request,
                profile,
                on_progress=report_progress,
            )
    except (RateLimitExceededError, CapacityExceededError) as exc:
        await activity.remove()
        await cl.Message(content=str(exc)).send()
        return
    except ValueError as exc:
        await activity.remove()
        await cl.Message(content=str(exc)).send()
        return
    except SafetyBlockedError as exc:
        logger.info(
            "request_blocked",
            extra={
                "custom_dimensions": {
                    "correlation_id": request.correlation_id,
                    "stage": exc.stage,
                    "rule": exc.rule,
                    "severity": exc.severity,
                    "threshold": exc.threshold,
                }
            },
        )
        await activity.remove()
        await cl.ErrorMessage(content=exc.user_message).send()
        return
    except GroundingUnavailableError as exc:
        logger.info(
            "grounding_rejected",
            extra={
                "custom_dimensions": {
                    "correlation_id": request.correlation_id,
                    "reason": str(exc),
                }
            },
        )
        await activity.remove()
        await cl.Message(
            content="I could not produce a sufficiently grounded answer for this request."
        ).send()
        return
    except Exception:
        logger.exception(
            "request_failed",
            extra={"custom_dimensions": {"correlation_id": request.correlation_id}},
        )
        await activity.remove()
        await cl.Message(
            content=f"The request failed. Reference: `{request.correlation_id}`"
        ).send()
        return

    await activity.remove()
    _remember_conversation(request, result)
    await _send_result(result)


def _progress_text(stage: ProgressStage, profile: ChatProfile) -> str:
    if stage is ProgressStage.CHECKING_INPUT:
        return "Checking safety..."
    if stage is ProgressStage.SELECTING_TOOLS:
        return "Deciding whether live sources are needed..."
    if stage is ProgressStage.SEARCHING_WEB:
        return "Using Web Search for fresh sources..."
    if stage is ProgressStage.REFINING:
        return "Tightening the answer to the sources..."
    if stage is ProgressStage.USING_SEARCH_SUMMARY:
        return "Using the cited search summary..."
    if stage is ProgressStage.CHECKING_OUTPUT:
        return "Checking the answer for safety and grounding..."
    if profile is ChatProfile.THINKING:
        return "Apertus is thinking..."
    return "Apertus is preparing the answer..."


def _conversation_history() -> tuple[ChatTurn, ...]:
    stored = cl.user_session.get("conversation_history") or []
    return tuple(
        ChatTurn(role=str(item["role"]), content=str(item["content"]))
        for item in stored[-MAX_HISTORY_TURNS:]
        if isinstance(item, dict) and "role" in item and "content" in item
    )


def _remember_conversation(
    request: ChatRequest, result: CompletionResult
) -> None:
    user_content = request.text.strip()
    if not user_content and request.attachments:
        kinds = ", ".join(item.mime_type for item in request.attachments)
        user_content = f"[User supplied attachments: {kinds}]"
    turns = (
        *request.history,
        ChatTurn(role="user", content=user_content),
        ChatTurn(role="assistant", content=result.answer),
    )[-MAX_HISTORY_TURNS:]
    cl.user_session.set(
        "conversation_history",
        [{"role": turn.role, "content": turn.content} for turn in turns],
    )


def _requester_key() -> str:
    environ = getattr(context.session, "environ", {}) or {}
    return str(
        environ.get("HTTP_X_MS_CLIENT_PRINCIPAL_ID")
        or getattr(context.session, "id", "unknown")
    )


def _read_attachment(element) -> Attachment:
    path_value = getattr(element, "path", None)
    if not path_value:
        raise ValueError("Uploaded attachment has no local path.")
    path = Path(path_value)
    mime_type = getattr(element, "mime", None) or "application/octet-stream"
    return Attachment(
        name=getattr(element, "name", None) or path.name,
        mime_type=mime_type,
        data_base64=base64.b64encode(path.read_bytes()).decode("ascii"),
    )


async def _send_result(result: CompletionResult) -> None:
    text = result.answer
    if result.citations:
        sources = "\n".join(
            f"{index}. [{citation.title}]({citation.url})"
            for index, citation in enumerate(result.citations, start=1)
        )
        text = f"{text}\n\n**Sources**\n{sources}"

    answer = cl.Message(content="")
    await answer.send()
    for token in re.split(r"(\s+)", text):
        if token:
            await answer.stream_token(token)
    await answer.update()