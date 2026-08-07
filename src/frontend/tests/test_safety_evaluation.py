from __future__ import annotations

import json

import pytest

from apertus_frontend.pipeline import CompletionResult, SafetyBlockedError
from safety_evaluation import (
    APERTUS_BLOCKER,
    AZURE_BLOCKER,
    EvaluationResult,
    SafetyCase,
    evaluate_case,
    format_result,
    validate_cases,
)


class BlockingService:
    async def complete(self, request, profile):
        raise SafetyBlockedError("blocked", rule="Violence")


class RefusingService:
    async def complete(self, request, profile):
        return CompletionResult(
            answer="Your message was blocked by Apertus. Rule: Violence.",
            citations=(),
            correlation_id=request.correlation_id,
        )


class AnsweringService:
    async def complete(self, request, profile):
        return CompletionResult(
            answer="Generated content that must not be reported.",
            citations=(),
            correlation_id=request.correlation_id,
        )


class MimickingService:
    async def complete(self, request, profile):
        return CompletionResult(
            answer=(
                "Your message was blocked by Apertus. "
                "Rule: generated content that must not be reported."
            ),
            citations=(),
            correlation_id=request.correlation_id,
        )


def make_case(blocker: str) -> SafetyCase:
    return SafetyCase(
        case_id="case-id",
        prompt="private probe prompt",
        expected_blocker=blocker,
        expected_rule="Violence",
    )


def test_case_manifest_is_valid():
    validate_cases()


def test_duplicate_case_ids_are_rejected():
    case = make_case(AZURE_BLOCKER)

    with pytest.raises(ValueError, match="unique"):
        validate_cases((case, case))


@pytest.mark.asyncio
async def test_azure_block_is_classified_without_prompt_or_output():
    result = await evaluate_case(BlockingService(), make_case(AZURE_BLOCKER))

    assert result.passed is True
    rendered = format_result(result)
    assert "private probe prompt" not in rendered
    assert "blocked" not in rendered.lower()


@pytest.mark.asyncio
async def test_apertus_refusal_is_classified():
    result = await evaluate_case(RefusingService(), make_case(APERTUS_BLOCKER))

    assert result.passed is True
    assert result.actual_blocker == APERTUS_BLOCKER


@pytest.mark.asyncio
async def test_unexpected_answer_content_is_not_reported():
    result = await evaluate_case(AnsweringService(), make_case(APERTUS_BLOCKER))

    assert result.passed is False
    rendered = format_result(result)
    assert "Generated content" not in rendered
    assert json.loads(rendered)["actual_blocker"] == "None"


@pytest.mark.asyncio
async def test_mimicked_block_rule_is_not_reported():
    result = await evaluate_case(MimickingService(), make_case(APERTUS_BLOCKER))

    assert result.passed is False
    rendered = format_result(result)
    assert "generated content" not in rendered
    assert json.loads(rendered)["actual_rule"] == "Unrecognized"


def test_result_schema_contains_metadata_only():
    assert set(EvaluationResult.__dataclass_fields__) == {
        "case_id",
        "expected_blocker",
        "actual_blocker",
        "expected_rule",
        "actual_rule",
        "passed",
    }