"""Local-only Chainlit entrypoint; deliberately does not import the cloud app."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import chainlit as cl
import httpx
from chainlit.server import app
from fastapi import Request, Response

from apertus_local.media import MAX_UPLOAD_BYTES, prepare_attachment
from apertus_local.runtime import (
    MAX_HISTORY_MESSAGES,
    LocalCompletionService,
    LocalProfile,
    LocalSettings,
)
from apertus_frontend.tools import ToolError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("apertus.local")
_runtime: LocalCompletionService | None = None


@app.middleware("http")
async def local_browser_policy(
    request: Request, call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
        "img-src 'self' data: blob:; media-src 'self' data: blob:; "
        "connect-src 'self' ws://127.0.0.1:8080 ws://localhost:8080; "
        "worker-src 'self' blob:; object-src 'none'; frame-ancestors 'self'"
    )
    return response


def get_runtime() -> LocalCompletionService:
    global _runtime
    if _runtime is None:
        _runtime = LocalCompletionService(LocalSettings.from_env())
    return _runtime


@cl.on_app_shutdown
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
        await get_runtime().ready()
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        logger.error("local_model_not_ready", extra={"error_type": type(exc).__name__})
        response.status_code = 503
        return {"status": "not-ready"}
    return {"status": "ready"}


# The CLI registers Chainlit's SPA catch-all before loading this entrypoint.
for route in reversed(tuple(app.routes)):
    if getattr(route, "endpoint", None) in {healthz, readiness}:
        app.routes.remove(route)
        app.routes.insert(0, route)


@cl.set_chat_profiles
async def chat_profiles(user: cl.User | None = None) -> list[cl.ChatProfile]:
    return [
        cl.ChatProfile(name=LocalProfile.CHAT, markdown_description="Local text, image and audio chat."),
        cl.ChatProfile(name=LocalProfile.TOOLS, markdown_description="Local calculator and clock; no web search."),
        cl.ChatProfile(name=LocalProfile.THINKING, markdown_description="Apertus thinking mode; no tools or exposed reasoning."),
    ]


@cl.on_chat_start
async def chat_start() -> None:
    await cl.Message(content=(
        "Running locally: no login, Azure services, web search, or cloud moderation. "
        "Image/audio understanding is enabled, but contextual audio accuracy is "
        "not fully validated. Verify important media answers. Output is text only."
    )).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    answer = cl.Message(content="")
    await answer.send()
    try:
        elements = message.elements or []
        if len(elements) > 2:
            raise ValueError("Send at most one image and one audio file.")
        attachments = []
        for element in elements:
            if not element.path:
                raise ValueError("The uploaded attachment has no local file.")
            path = Path(element.path)
            if path.stat().st_size > MAX_UPLOAD_BYTES:
                raise ValueError("Attachments must be at most 20 MiB.")
            attachments.append(await asyncio.to_thread(
                prepare_attachment, element.name, element.mime or "application/octet-stream", path.read_bytes(),
            ))
        history = cl.user_session.get("local_history") or []

        async def send_token(token: str) -> None:
            await answer.stream_token(token)

        result = await get_runtime().complete(
            text=message.content or "", attachments=tuple(attachments), history=history,
            profile=LocalProfile(cl.user_session.get("chat_profile") or LocalProfile.CHAT),
            on_token=send_token,
        )
        answer.content = result.answer
        await answer.update()
        user_text = message.content or "[User supplied local image/audio attachments.]"
        cl.user_session.set("local_history", [
            *history,
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result.answer},
        ][-MAX_HISTORY_MESSAGES:])
        if result.selected_tools:
            await cl.Message(content="Local tool used: " + ", ".join(result.selected_tools)).send()
        if result.trimmed_history_messages:
            await cl.Message(content="Older conversation pairs were omitted to fit the context budget.").send()
        if result.output_limited:
            await cl.Message(content="The output token limit was reached; ask Apertus to continue.").send()
    except (ValueError, OSError, httpx.HTTPError, RuntimeError, ToolError) as exc:
        logger.error("local_request_failed", extra={"error_type": type(exc).__name__})
        detail = str(exc) if isinstance(exc, ValueError) else (
            "The local model request failed. Check that the NVIDIA GPU and inference container are available."
        )
        answer.content += "\n\nRequest failed: " + detail
        await answer.update()
