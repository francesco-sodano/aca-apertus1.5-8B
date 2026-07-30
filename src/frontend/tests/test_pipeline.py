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
    GroundingUnavailableError,
    MAX_IMAGE_BYTES,
    ModelCompletion,
    SafetyBlockedError,
    grounding_query,
    requires_web_grounding,
)


@dataclass
class FakeSafety:
    blocked_purpose: str = ""
    grounded: bool | None = True
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
async def test_ungrounded_output_is_not_returned():
    service, _, _, model = make_service(safety=FakeSafety(grounded=False))

    with pytest.raises(GroundingUnavailableError):
        await service.complete(
            ChatRequest(text="What is the current score?"), ChatProfile.TOOLS
        )

    assert model.calls == 1


@pytest.mark.asyncio
async def test_indeterminate_groundedness_is_not_returned():
    service, _, _, model = make_service(safety=FakeSafety(grounded=None))

    with pytest.raises(GroundingUnavailableError):
        await service.complete(
            ChatRequest(text="What is the current score?"), ChatProfile.TOOLS
        )

    assert model.calls == 1


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

    assert requires_web_grounding(request) is True
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

    assert requires_web_grounding(request) is False


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