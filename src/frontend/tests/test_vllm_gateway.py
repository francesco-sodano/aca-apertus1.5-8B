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
from apertus_frontend.vllm_gateway import (
    VllmGateway,
    _build_messages,
    _parse_selector_call,
)
from apertus_frontend.tools import (
    ToolResult,
    ToolValidationError,
    calculator_spec,
    current_time_spec,
)


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
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                id="call_1",
                                function=SimpleNamespace(
                                    name="select_tool",
                                    arguments='{"tool":"unknown"}',
                                ),
                            )
                        ],
                    )
                )
            ]
        )


class ToolThenAnswerCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)

        if len(self.calls) == 1:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="Choosing calculator.",
                            tool_calls=[
                                SimpleNamespace(
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name="select_tool",
                                        arguments=(
                                            "Choosing calculator."
                                            '<|tools_prefix|>[{"calculator":'
                                            '{"expression":"2 + 2"}}]'
                                        ),
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )

        async def chunks():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="The result is 4.",
                            tool_calls=[],
                        )
                    )
                ]
            )

        return chunks()


class NoneThenAnswerCompletions(ToolThenAnswerCompletions):
    async def create(self, **kwargs):
        self.calls.append(kwargs)

        if len(self.calls) == 1:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[
                                SimpleNamespace(
                                    id="call_none",
                                    function=SimpleNamespace(
                                        name="select_tool",
                                        arguments="none",
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )

        async def chunks():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="I am Apertus v1.5 8B.",
                            tool_calls=[],
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


TOOLS = (calculator_spec(), current_time_spec())


async def execute_calculator(call):
    assert call.name == "calculator"
    return ToolResult(content='{"expression":"2 + 2","result":4}')


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
        tools=(),
        execute_tool=execute_calculator,
        require_tool=False,
    )
    await gateway.complete(
        request=ChatRequest(text="Question two"),
        profile=ChatProfile.THINKING,
        grounding=grounding_packet(),
        tools=(),
        execute_tool=execute_calculator,
        require_tool=False,
    )

    tools_call, thinking_call = client.chat.completions.calls
    assert gateway.base_url == "http://internal-apertus/v1"
    assert tools_call["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert "tools" not in tools_call
    assert thinking_call["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert "tools" not in thinking_call


@pytest.mark.asyncio
async def test_stable_identity_prompt_can_answer_without_selecting_a_tool():
    client = FakeClient()
    client.chat.completions = NoneThenAnswerCompletions()
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
        tools=TOOLS,
        execute_tool=execute_calculator,
        require_tool=False,
    )

    request = client.chat.completions.calls[0]
    selector, answer = client.chat.completions.calls
    assert selector["tool_choice"]["function"]["name"] == "select_tool"
    assert "tools" not in answer
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
async def test_unknown_apertus_tool_call_is_rejected_by_broker():
    client = FakeClient()
    client.chat.completions = UnexpectedToolCompletions()
    gateway = VllmGateway(
        base_url="http://internal-apertus/v1",
        api_key="secret",
        model="swiss-ai/Apertus-v1.5-8B",
        client=client,
    )

    async def reject_unknown(call):
        raise ToolValidationError(f"Apertus selected unknown tool: {call.name}")

    with pytest.raises(ToolValidationError, match="unknown tool"):
        await gateway.complete(
            request=ChatRequest(text="Question"),
            profile=ChatProfile.TOOLS,
            grounding=grounding_packet(),
            tools=TOOLS,
            execute_tool=reject_unknown,
            require_tool=False,
        )


@pytest.mark.asyncio
async def test_native_tool_call_executes_once_then_tools_are_bounded():
    client = FakeClient()
    client.chat.completions = ToolThenAnswerCompletions()
    gateway = VllmGateway(
        base_url="http://internal-apertus/v1",
        api_key="secret",
        model="swiss-ai/Apertus-v1.5-8B",
        client=client,
    )

    result = await gateway.complete(
        request=ChatRequest(text="What is 2 + 2?"),
        profile=ChatProfile.TOOLS,
        grounding=GroundingPacket(summary=""),
        tools=TOOLS,
        execute_tool=execute_calculator,
        require_tool=True,
    )

    first, second = client.chat.completions.calls
    assert first["tool_choice"]["function"]["name"] == "select_tool"
    assert len(first["tools"]) == 1
    assert "tools" not in second
    assert second["messages"][-1]["role"] == "tool"
    assert result.answer == "The result is 4."
    assert result.selected_tools == ("calculator",)


def test_apertus_wrapper_normalizes_real_tool_name_and_arguments():
    call = _parse_selector_call(
        {
            "id": "call_1",
            "name": "select_tool",
            "arguments": (
                "I will calculate it.<|tools_prefix|>"
                '[{"calculator":{"expression":"37*19+4"}}]'
            ),
        },
        TOOLS,
        require_tool=True,
    )

    assert call is not None
    assert call.name == "calculator"
    assert call.arguments_json == '{"expression": "37*19+4"}'


def test_selector_none_is_allowed_only_when_tool_is_optional():
    raw = {"id": "call_none", "name": "select_tool", "arguments": "none"}

    assert _parse_selector_call(raw, TOOLS, require_tool=False) is None
    with pytest.raises(ToolValidationError, match="required"):
        _parse_selector_call(raw, TOOLS, require_tool=True)