from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import pytest

from apertus_frontend.pipeline import (
    Attachment,
    ChatProfile,
    ChatRequest,
    ChatTurn,
    Citation,
    GroundedCompletionService,
    GroundingPacket,
    GroundingUnavailableError,
    MAX_IMAGE_BYTES,
    ModelCompletion,
    ProgressStage,
    SafetyAssessment,
    SafetyBlockedError,
    grounding_query,
    grounding_route_hint,
)
from apertus_frontend.tools import ToolCall


@dataclass
class FakeSafety:
    blocked_purpose: str = ""
    grounded: bool | None = True
    grounded_results: list[bool | None] = field(default_factory=list)
    text_purposes: list[str] = field(default_factory=list)
    image_calls: int = 0
    grounding_calls: int = 0
    groundedness_sources: tuple[str, ...] = ()
    assessment: SafetyAssessment | None = None

    async def screen_text(
        self, text: str, *, purpose: str
    ) -> SafetyAssessment | None:
        self.text_purposes.append(purpose)
        if purpose == self.blocked_purpose:
            raise SafetyBlockedError(purpose)
        return self.assessment if purpose == "user-input" else None

    async def screen_image(self, attachment: Attachment) -> None:
        self.image_calls += 1
        if self.blocked_purpose == "image":
            raise SafetyBlockedError("image")

    async def screen_grounding(self, prompt: str, packet: GroundingPacket) -> None:
        self.grounding_calls += 1
        if self.blocked_purpose == "grounding":
            raise SafetyBlockedError("grounding")

    async def is_grounded(
        self, *, query: str, answer: str, sources: tuple[str, ...]
    ) -> bool | None:
        self.groundedness_sources = sources
        if self.grounded_results:
            return self.grounded_results.pop(0)
        return self.grounded


@dataclass
class FakeGrounding:
    packet: GroundingPacket
    calls: int = 0
    queries: list[str] = field(default_factory=list)

    async def search(self, query: str) -> GroundingPacket:
        self.calls += 1
        self.queries.append(query)
        return self.packet


@dataclass
class FakeModel:
    calls: int = 0
    profiles: list[ChatProfile] = field(default_factory=list)
    requests: list[ChatRequest] = field(default_factory=list)
    selected_tool: str | None = None
    tool_arguments_json: str = ""
    answer: str = "Grounded answer"

    async def complete(
        self,
        *,
        request,
        profile,
        grounding,
        tools,
        execute_tool,
        require_tool,
    ):
        self.calls += 1
        self.profiles.append(profile)
        self.requests.append(request)
        selected_tool = self.selected_tool or (
            "search_web" if require_tool else None
        )
        if not selected_tool or not tools:
            return ModelCompletion(answer=self.answer)
        arguments_json = self.tool_arguments_json
        if not arguments_json:
            arguments_json = json.dumps(
                {
                    "search_web": {"query": request.text},
                    "calculator": {"expression": "2 + 2"},
                    "get_current_time": {"timezone": "Europe/Zurich"},
                }[selected_tool]
            )
        result = await execute_tool(
            ToolCall("call-1", selected_tool, arguments_json)
        )
        return ModelCompletion(
            answer=self.answer,
            citations=tuple(
                Citation(item.title, item.url) for item in result.citations
            ),
            grounding_sources=result.grounding_sources,
            selected_tools=(selected_tool,),
        )


def safe_packet() -> GroundingPacket:
    return GroundingPacket(
        summary="Verified evidence",
        citations=(Citation(title="Source", url="https://example.com/source"),),
    )


def make_service(*, safety=None, grounding=None, model=None):
    safety = safety or FakeSafety()
    grounding = grounding or FakeGrounding(safe_packet())
    model = model or FakeModel()
    return (
        GroundedCompletionService(
            safety=safety,
            grounding=grounding,
            model=model,
        ),
        safety,
        grounding,
        model,
    )


@pytest.mark.asyncio
async def test_unsafe_input_never_calls_apertus():
    service, _, _, model = make_service(
        safety=FakeSafety(blocked_purpose="user-input")
    )

    with pytest.raises(SafetyBlockedError):
        await service.complete(ChatRequest(text="blocked"), ChatProfile.TOOLS)

    assert model.calls == 0


@pytest.mark.asyncio
async def test_model_refusal_explains_decision_source_and_safety_reason():
    safety = FakeSafety(
        assessment=SafetyAssessment(category="Violence", severity=2, threshold=4)
    )
    model = FakeModel(
        answer="I cannot provide instructions for making a Molotov cocktail."
    )
    service, _, _, _ = make_service(safety=safety, model=model)

    result = await service.complete(
        ChatRequest(
            text="Can you give me instructions for making a Molotov cocktail?",
            correlation_id="refusal-test",
        ),
        ChatProfile.TOOLS,
    )

    assert result.answer == (
        "Your message was blocked by Apertus. Rule: Violence."
    )


@pytest.mark.asyncio
async def test_normal_model_answer_is_not_annotated_as_refusal():
    service, _, _, _ = make_service(model=FakeModel(answer="Ordinary answer"))

    result = await service.complete(
        ChatRequest(text="Explain gravity."), ChatProfile.TOOLS
    )

    assert result.answer == "Ordinary answer"


@pytest.mark.asyncio
async def test_citation_free_evidence_can_still_ground_an_answer():
    service, safety, grounding, model = make_service(
        grounding=FakeGrounding(GroundingPacket(summary="No cited evidence"))
    )

    result = await service.complete(
        ChatRequest(text="What is the latest news?"), ChatProfile.TOOLS
    )

    assert result.citations == ()
    assert model.calls == 1
    assert grounding.calls == 1
    assert safety.groundedness_sources == ("No cited evidence",)


@pytest.mark.asyncio
async def test_indirect_attack_in_grounding_never_calls_apertus():
    service, _, _, model = make_service(
        safety=FakeSafety(blocked_purpose="grounding")
    )

    with pytest.raises(SafetyBlockedError):
        await service.complete(ChatRequest(text="Current news"), ChatProfile.TOOLS)

    assert model.calls == 1


@pytest.mark.asyncio
async def test_raw_audio_uses_documented_safety_exception():
    service, safety, grounding, model = make_service()
    audio = Attachment(
        name="question.wav",
        mime_type="audio/wav",
        data_base64="UklGRg==",
    )

    await service.complete(
        ChatRequest(text="", attachments=(audio,)), ChatProfile.THINKING
    )

    assert model.calls == 1
    assert safety.image_calls == 0
    assert "user-input" not in safety.text_purposes
    assert model.requests[0].attachments == (audio,)
    assert grounding.calls == 0


@pytest.mark.asyncio
async def test_image_is_moderated_before_apertus():
    service, safety, _, model = make_service()
    image = Attachment(
        name="photo.png",
        mime_type="image/png",
        data_base64="iVBORw0KGgo=",
    )

    await service.complete(
        ChatRequest(text="Describe this", attachments=(image,)), ChatProfile.TOOLS
    )

    assert safety.image_calls == 1
    assert model.calls == 1


@pytest.mark.asyncio
async def test_ungrounded_output_without_citations_is_not_returned():
    service, _, _, model = make_service(
        safety=FakeSafety(grounded=False),
        grounding=FakeGrounding(GroundingPacket(summary="Uncited evidence")),
    )

    with pytest.raises(GroundingUnavailableError):
        await service.complete(
            ChatRequest(text="What is the current score?"), ChatProfile.TOOLS
        )

    assert model.calls == 2


@pytest.mark.asyncio
async def test_grounding_error_identifies_groundedness_service():
    service, _, _, _ = make_service(
        safety=FakeSafety(grounded=False),
        grounding=FakeGrounding(GroundingPacket(summary="Uncited evidence")),
    )

    with pytest.raises(GroundingUnavailableError) as rejected:
        await service.complete(
            ChatRequest(text="What is the current score?"), ChatProfile.TOOLS
        )

    assert rejected.value.source == (
        "Azure AI Content Safety Groundedness Detection"
    )


@pytest.mark.asyncio
async def test_indeterminate_groundedness_is_not_returned():
    service, _, _, model = make_service(
        safety=FakeSafety(grounded=None),
        grounding=FakeGrounding(GroundingPacket(summary="Uncited evidence")),
    )

    with pytest.raises(GroundingUnavailableError):
        await service.complete(
            ChatRequest(text="What is the current score?"), ChatProfile.TOOLS
        )

    assert model.calls == 2


@pytest.mark.asyncio
async def test_groundedness_retry_can_recover_a_false_negative():
    safety = FakeSafety(grounded_results=[False, True])
    service, _, _, model = make_service(safety=safety)

    result = await service.complete(
        ChatRequest(text="What is the current score?"), ChatProfile.TOOLS
    )

    assert result.answer == "Grounded answer"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_repeated_false_negative_returns_cited_search_summary():
    service, _, _, model = make_service(safety=FakeSafety(grounded=False))

    result = await service.complete(
        ChatRequest(text="Who are the current council members?"),
        ChatProfile.TOOLS,
    )

    assert result.answer == "Verified evidence"
    assert result.citations == safe_packet().citations
    assert model.calls == 2


@pytest.mark.asyncio
async def test_groundedness_recovery_reports_refinement_and_summary_fallback():
    service, _, _, _ = make_service(safety=FakeSafety(grounded=False))
    stages: list[ProgressStage] = []

    async def record(stage: ProgressStage, detail: str | None) -> None:
        stages.append(stage)

    await service.complete(
        ChatRequest(text="Who are the current council members?"),
        ChatProfile.TOOLS,
        on_progress=record,
    )

    assert stages[-4:] == [
        ProgressStage.CHECKING_OUTPUT,
        ProgressStage.REFINING,
        ProgressStage.CHECKING_OUTPUT,
        ProgressStage.USING_SEARCH_SUMMARY,
    ]


@pytest.mark.asyncio
async def test_tool_grounding_sources_are_included_in_output_validation():
    safety = FakeSafety()
    model = FakeModel()

    async def complete_with_tool_evidence(**kwargs):
        model.calls += 1
        return ModelCompletion(
            answer="Grounded answer",
            grounding_sources=("Additional tool evidence",),
        )

    model.complete = complete_with_tool_evidence
    service, _, _, _ = make_service(safety=safety, model=model)

    await service.complete(
        ChatRequest(text="What is the current score?"), ChatProfile.TOOLS
    )

    assert safety.groundedness_sources == (
        "Additional tool evidence",
    )


@pytest.mark.asyncio
async def test_stable_explanation_skips_web_search_and_groundedness():
    service, safety, grounding, model = make_service(safety=FakeSafety(grounded=False))

    result = await service.complete(
        ChatRequest(text="Explain gravity for a high school class."),
        ChatProfile.TOOLS,
    )

    assert result.answer == "Grounded answer"
    assert grounding.calls == 0
    assert safety.groundedness_sources == ()
    assert model.calls == 1


def test_current_follow_up_uses_recent_conversation_for_grounding():
    request = ChatRequest(
        text="and google?",
        history=(
            ChatTurn("user", "What is the current value of MSFT stock?"),
            ChatTurn("assistant", "MSFT is trading at $450."),
        ),
    )

    assert grounding_route_hint(request) is True
    query = grounding_query(request)
    assert "current value of MSFT stock" in query
    assert "Current user request: and google?" in query


def test_unrelated_turn_after_current_question_does_not_inherit_grounding():
    request = ChatRequest(
        text="Write a short poem about summer.",
        history=(
            ChatTurn("user", "What is the current value of MSFT stock?"),
            ChatTurn("assistant", "MSFT is trading at $450."),
        ),
    )

    assert grounding_route_hint(request) is False


@pytest.mark.parametrize(
    "text",
    [
        "are you sure? double check on internet",
    ],
)
def test_explicit_freshness_and_verification_route_to_web(text):
    assert grounding_route_hint(ChatRequest(text=text)) is True


@pytest.mark.asyncio
async def test_apertus_selected_web_search_is_brokered_and_cited():
    grounding = FakeGrounding(safe_packet())
    model = FakeModel(
        selected_tool="search_web",
        tool_arguments_json='{"query":"Voyager 1 communications status"}',
    )
    service, _, _, model = make_service(grounding=grounding, model=model)

    result = await service.complete(
        ChatRequest(text="Has Voyager 1 recovered communications?"),
        ChatProfile.TOOLS,
    )

    assert grounding.queries == [
        "Search objective selected by Apertus: Voyager 1 communications status\n"
        "LATEST USER REQUEST: Has Voyager 1 recovered communications?\n"
        "Conversation context for reference resolution only: Has Voyager 1 "
        "recovered communications?"
    ]
    assert result.selected_tools == ("search_web",)
    assert result.citations == safe_packet().citations
    assert model.calls == 1


@pytest.mark.asyncio
async def test_apertus_can_answer_stable_fact_without_a_tool():
    grounding = FakeGrounding(safe_packet())
    service, _, _, model = make_service(grounding=grounding)

    await service.complete(
        ChatRequest(text="What is the capital of France?"), ChatProfile.TOOLS
    )

    assert grounding.calls == 0
    assert model.calls == 1


@pytest.mark.parametrize(
    "text",
    [
        "When the Artemis III launch is planned?",
        "when is the next run of the Artemis project?",
        "What is the date of the next national election?",
        "The upcoming product release is scheduled for when?",
        "Give me the plot of The Odyssey (2026 film) in Romansh.",
    ],
)
def test_temporal_freshness_phrasing_routes_directly_to_web(text):
    assert grounding_route_hint(ChatRequest(text=text)) is True


@pytest.mark.parametrize("text", ["What is a president?", "Share some good news."])
def test_ambiguous_factual_phrasing_leaves_tool_choice_to_apertus(text):
    assert grounding_route_hint(ChatRequest(text=text)) is None


def test_explicit_explanation_is_a_high_confidence_local_task():
    assert grounding_route_hint(ChatRequest(text="Explain stock markets.")) is False


def test_calculation_remains_eligible_for_apertus_tool_selection():
    assert grounding_route_hint(ChatRequest(text="Calculate 18 percent of 745.")) is None


def test_stable_capital_question_is_a_high_confidence_local_task():
    assert grounding_route_hint(ChatRequest(text="What is the capital of France?")) is False


@pytest.mark.asyncio
async def test_apertus_search_query_can_preserve_response_requirements():
    grounding = FakeGrounding(safe_packet())
    model = FakeModel(
        selected_tool="search_web",
        tool_arguments_json=(
            '{"query":"The Odyssey 2026 Christopher Nolan plot in Romansh"}'
        ),
    )
    service, _, _, _ = make_service(grounding=grounding, model=model)

    await service.complete(
        ChatRequest(
            text=(
                "give me the plot of The Odyssey (2026 film by Christopher "
                "Nolan) in Romansh"
            )
        ),
        ChatProfile.TOOLS,
    )

    assert grounding.queries == [
        "Search objective selected by Apertus: The Odyssey 2026 Christopher "
        "Nolan plot in Romansh\nLATEST USER REQUEST: give me the plot of The "
        "Odyssey (2026 film by Christopher Nolan) in Romansh\nConversation "
        "context for reference resolution only: give me the plot of The Odyssey "
        "(2026 film by Christopher Nolan) in Romansh"
    ]


@pytest.mark.asyncio
async def test_profiles_share_model_gateway_and_remain_distinct():
    service, _, _, model = make_service()

    await service.complete(ChatRequest(text="One"), ChatProfile.TOOLS)
    await service.complete(ChatRequest(text="Two"), ChatProfile.THINKING)

    assert model.calls == 2
    assert model.profiles == [ChatProfile.TOOLS, ChatProfile.THINKING]


@pytest.mark.asyncio
async def test_too_many_images_never_calls_apertus():
    service, _, grounding, model = make_service()
    images = tuple(
        Attachment(
            name=f"photo-{index}.png",
            mime_type="image/png",
            data_base64="eA==",
        )
        for index in range(5)
    )

    with pytest.raises(ValueError, match="at most 4 images"):
        await service.complete(
            ChatRequest(text="Describe these", attachments=images),
            ChatProfile.TOOLS,
        )

    assert grounding.calls == 0
    assert model.calls == 0


@pytest.mark.asyncio
async def test_oversize_image_never_calls_apertus():
    service, _, grounding, model = make_service()
    image = Attachment(
        name="oversize.png",
        mime_type="image/png",
        data_base64=base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode("ascii"),
    )

    with pytest.raises(ValueError, match="exceeds"):
        await service.complete(
            ChatRequest(text="Describe this", attachments=(image,)),
            ChatProfile.TOOLS,
        )

    assert grounding.calls == 0
    assert model.calls == 0


@pytest.mark.asyncio
async def test_progress_reports_web_search_and_answer_checks_in_order():
    service, _, _, _ = make_service()
    stages: list[ProgressStage] = []

    async def record(stage: ProgressStage, detail: str | None) -> None:
        stages.append(stage)

    await service.complete(
        ChatRequest(text="What is the latest news?"),
        ChatProfile.TOOLS,
        on_progress=record,
    )

    assert stages == [
        ProgressStage.CHECKING_INPUT,
        ProgressStage.SELECTING_TOOLS,
        ProgressStage.GENERATING,
        ProgressStage.USING_TOOL,
        ProgressStage.CHECKING_OUTPUT,
    ]