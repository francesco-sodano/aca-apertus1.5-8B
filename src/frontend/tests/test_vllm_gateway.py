from __future__ import annotations

from types import SimpleNamespace

import pytest

from apertus_frontend.pipeline import (
    Attachment,
    ChatProfile,
    ChatRequest,
    ChatTurn,
    Citation,
    GroundingPacket,
)
from apertus_frontend.vllm_gateway import VllmGateway, _build_messages


class FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)

        async def chunks():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="Cited answer [1]",
                            reasoning_content="checked",
                            tool_calls=[],
                        )
                    )
                ]
            )

        return chunks()


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCompletions())


class UnexpectedToolCompletions:
    async def create(self, **kwargs):
        async def chunks():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name="search_web",
                                        arguments='{"query":"latest"}',
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )

        return chunks()


def grounding_packet():
    return GroundingPacket(
        summary="Evidence",
        citations=(Citation("Source", "https://example.com"),),
    )


async def search_web(query):
    return grounding_packet()


@pytest.mark.asyncio
async def test_profiles_use_same_endpoint_with_different_template_flags():
    client = FakeClient()
    gateway = VllmGateway(
        base_url="http://internal-apertus/v1",
        api_key="secret",
        model="swiss-ai/Apertus-v1.5-8B",
        client=client,
    )

    await gateway.complete(
        request=ChatRequest(text="Question one"),
        profile=ChatProfile.TOOLS,
        grounding=grounding_packet(),
        search_web=search_web,
    )
    await gateway.complete(
        request=ChatRequest(text="Question two"),
        profile=ChatProfile.THINKING,
        grounding=grounding_packet(),
        search_web=search_web,
    )

    tools_call, thinking_call = client.chat.completions.calls
    assert gateway.base_url == "http://internal-apertus/v1"
    assert tools_call["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert "tools" not in tools_call
    assert thinking_call["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert "tools" not in thinking_call


@pytest.mark.asyncio
async def test_stable_identity_prompt_has_no_tools_and_identifies_apertus():
    client = FakeClient()
    gateway = VllmGateway(
        base_url="http://internal-apertus/v1",
        api_key="secret",
        model="swiss-ai/Apertus-v1.5-8B",
        client=client,
    )

    await gateway.complete(
        request=ChatRequest(text="Who are you?"),
        profile=ChatProfile.TOOLS,
        grounding=GroundingPacket(summary=""),
        search_web=search_web,
    )

    request = client.chat.completions.calls[0]
    assert "tools" not in request
    assert "Apertus v1.5 8B" in request["messages"][0]["content"]
    assert "not ChatGPT" in request["messages"][0]["content"]


def test_builds_current_vllm_image_and_audio_parts():
    request = ChatRequest(
        text="Compare the attachments",
        attachments=(
            Attachment("image.png", "image/png", "aW1hZ2U="),
            Attachment("audio.wav", "audio/wav", "YXVkaW8="),
        ),
    )

    messages = _build_messages(request, grounding_packet())
    parts = messages[1]["content"]

    assert [part["type"] for part in parts] == ["text", "image_url", "audio_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2]["audio_url"]["url"].startswith("data:audio/wav;base64,")


def test_builds_recent_conversation_before_current_request():
    request = ChatRequest(
        text="and google?",
        history=(
            ChatTurn("user", "What is the current MSFT stock value?"),
            ChatTurn("assistant", "MSFT is trading at $450."),
        ),
    )

    messages = _build_messages(request, grounding_packet())

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert messages[1]["content"] == "What is the current MSFT stock value?"
    assert messages[-1]["content"][0]["text"] == "and google?"


def test_grounded_prompt_makes_completed_retrieval_authoritative():
    messages = _build_messages(
        ChatRequest(
            text="Are you sure? Double-check on the internet.",
            history=(
                ChatTurn("assistant", "Artemis III launches in 2025."),
            ),
        ),
        grounding_packet(),
    )

    system = messages[0]["content"]
    assert "already searched the live public web" in system
    assert "more authoritative" in system
    assert "cannot" in system


@pytest.mark.asyncio
async def test_unexpected_apertus_tool_call_is_rejected():
    client = FakeClient()
    client.chat.completions = UnexpectedToolCompletions()
    gateway = VllmGateway(
        base_url="http://internal-apertus/v1",
        api_key="secret",
        model="swiss-ai/Apertus-v1.5-8B",
        client=client,
    )

    with pytest.raises(RuntimeError, match="unexpected tool call"):
        await gateway.complete(
            request=ChatRequest(text="Question"),
            profile=ChatProfile.TOOLS,
            grounding=grounding_packet(),
            search_web=search_web,
        )