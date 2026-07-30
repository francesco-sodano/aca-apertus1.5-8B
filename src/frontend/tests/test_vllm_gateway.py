from __future__ import annotations

from types import SimpleNamespace

import pytest

from apertus_frontend.pipeline import (
    Attachment,
    ChatProfile,
    ChatRequest,
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
    assert "tools" in tools_call
    assert tools_call["tool_choice"] == "required"
    assert thinking_call["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert "tools" not in thinking_call


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


class ToolCallingCompletions:
    def __init__(self):
        self.calls = 0
        self.requests = []

    async def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)

        async def chunks():
            if self.calls == 1:
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                reasoning_content="not exposed",
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_1",
                                        function=SimpleNamespace(
                                            name="search_web",
                                            arguments='{"query":"latest evidence"}',
                                        ),
                                    )
                                ],
                            )
                        )
                    ]
                )
            else:
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content="Final answer",
                                reasoning_content="still not exposed",
                                tool_calls=[],
                            )
                        )
                    ]
                )

        return chunks()


@pytest.mark.asyncio
async def test_tool_evidence_is_returned_for_groundedness_validation():
    client = FakeClient()
    client.chat.completions = ToolCallingCompletions()
    gateway = VllmGateway(
        base_url="http://internal-apertus/v1",
        api_key="secret",
        model="swiss-ai/Apertus-v1.5-8B",
        client=client,
    )

    result = await gateway.complete(
        request=ChatRequest(text="Question"),
        profile=ChatProfile.TOOLS,
        grounding=grounding_packet(),
        search_web=search_web,
    )

    assert result.answer == "Final answer"
    assert result.grounding_sources == ("Evidence",)
    assert not hasattr(result, "reasoning")
    assert client.chat.completions.requests[0]["tool_choice"] == "required"
    assert client.chat.completions.requests[1]["tool_choice"] == "auto"
    tool_message = client.chat.completions.requests[1]["messages"][-1]
    assert '"index": 2' in tool_message["content"]