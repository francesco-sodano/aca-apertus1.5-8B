from __future__ import annotations

import base64
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
    GroundingDecision,
    GroundingUnavailableError,
    MAX_IMAGE_BYTES,
    ModelCompletion,
    ProgressStage,
    SafetyBlockedError,
    grounding_query,
    grounding_route_hint,
)


@dataclass
class FakeSafety:
    blocked_purpose: str = ""
    grounded: bool | None = True
    grounded_results: list[bool | None] = field(default_factory=list)
    text_purposes: list[str] = field(default_factory=list)
    image_calls: int = 0
    grounding_calls: int = 0
    groundedness_sources: tuple[str, ...] = ()

    async def screen_text(self, text: str, *, purpose: str) -> None:
        self.text_purposes.append(purpose)
        if purpose == self.blocked_purpose:
            raise SafetyBlockedError(purpose)

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
    decision: GroundingDecision = GroundingDecision(use_web=False)
    calls: int = 0
    queries: list[str] = field(default_factory=list)
    route_calls: int = 0

    async def route(self, query: str) -> GroundingDecision:
        self.route_calls += 1
        return self.decision

    async def search(self, query: str) -> GroundingPacket:
        self.calls += 1
        self.queries.append(query)
        return self.packet


class FailingRouteGrounding(FakeGrounding):
    async def route(self, query: str) -> GroundingDecision:
        self.route_calls += 1
        raise RuntimeError("router unavailable")


@dataclass
class FakeModel:
    calls: int = 0
    profiles: list[ChatProfile] = field(default_factory=list)
    requests: list[ChatRequest] = field(default_factory=list)

    async def complete(self, *, request, profile, grounding, search_web):
        self.calls += 1
        self.profiles.append(profile)
        self.requests.append(request)
        return ModelCompletion(answer="Grounded answer")


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

    assert model.calls == 0


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

    async def record(stage: ProgressStage) -> None:
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
        "Verified evidence",
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
async def test_ambiguous_factual_request_uses_semantic_router():
    grounding = FakeGrounding(
        safe_packet(),
        decision=GroundingDecision(
            use_web=True,
            search_query="Voyager 1 communications status",
        ),
    )
    service, _, _, model = make_service(grounding=grounding)

    await service.complete(
        ChatRequest(text="Has Voyager 1 recovered communications?"),
        ChatProfile.TOOLS,
    )

    assert grounding.route_calls == 1
    assert grounding.queries == [
        "Search objective: Voyager 1 communications status\n"
        "User request and response requirements: Has Voyager 1 recovered "
        "communications?"
    ]
    assert model.calls == 1


@pytest.mark.asyncio
async def test_ambiguous_stable_fact_can_skip_web_after_semantic_routing():
    grounding = FakeGrounding(
        safe_packet(), decision=GroundingDecision(use_web=False)
    )
    service, _, _, model = make_service(grounding=grounding)

    await service.complete(
        ChatRequest(text="What is the capital of France?"), ChatProfile.TOOLS
    )

    assert grounding.route_calls == 1
    assert grounding.calls == 0
    assert model.calls == 1


@pytest.mark.parametrize(
    "text",
    [
        "When the Artemis III launch is planned?",
        "when is the next run of the Artemis project?",
        "What is the date of the next national election?",
        "The upcoming product release is scheduled for when?",
    ],
)
def test_temporal_freshness_phrasing_routes_directly_to_web(text):
    assert grounding_route_hint(ChatRequest(text=text)) is True


@pytest.mark.parametrize("text", ["What is a president?", "Share some good news."])
def test_ambiguous_factual_phrasing_uses_semantic_routing(text):
    assert grounding_route_hint(ChatRequest(text=text)) is None


def test_explicit_explanation_is_a_high_confidence_local_task():
    assert grounding_route_hint(ChatRequest(text="Explain stock markets.")) is False


@pytest.mark.asyncio
async def test_semantic_router_failure_defaults_safely_to_web():
    grounding = FailingRouteGrounding(safe_packet())
    service, _, _, model = make_service(grounding=grounding)

    await service.complete(
        ChatRequest(text="Has Voyager 1 recovered communications?"),
        ChatProfile.TOOLS,
    )

    assert grounding.route_calls == 1
    assert grounding.calls == 1
    assert grounding.queries == ["Has Voyager 1 recovered communications?"]
    assert model.calls == 1


@pytest.mark.asyncio
async def test_empty_semantic_search_query_preserves_original_request():
    grounding = FakeGrounding(
        safe_packet(), decision=GroundingDecision(use_web=True, search_query="")
    )
    service, _, _, _ = make_service(grounding=grounding)

    await service.complete(
        ChatRequest(text="Has Voyager 1 recovered communications?"),
        ChatProfile.TOOLS,
    )

    assert grounding.queries == ["Has Voyager 1 recovered communications?"]


@pytest.mark.asyncio
async def test_semantic_search_preserves_user_response_requirements():
    grounding = FakeGrounding(
        safe_packet(),
        decision=GroundingDecision(
            use_web=True,
            search_query="The Odyssey 2026 Christopher Nolan plot",
        ),
    )
    service, _, _, _ = make_service(grounding=grounding)

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
        "Search objective: The Odyssey 2026 Christopher Nolan plot\n"
        "User request and response requirements: give me the plot of The Odyssey "
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

    async def record(stage: ProgressStage) -> None:
        stages.append(stage)

    await service.complete(
        ChatRequest(text="What is the latest news?"),
        ChatProfile.TOOLS,
        on_progress=record,
    )

    assert stages == [
        ProgressStage.CHECKING_INPUT,
        ProgressStage.SELECTING_TOOLS,
        ProgressStage.SEARCHING_WEB,
        ProgressStage.GENERATING,
        ProgressStage.CHECKING_OUTPUT,
    ]