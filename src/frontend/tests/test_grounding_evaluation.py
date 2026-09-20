from __future__ import annotations

import pytest

from apertus_frontend.pipeline import ChatProfile, CompletionResult
from grounding_evaluation import (
    CASES,
    GroundingCase,
    evaluate_case,
    format_result,
    validate_cases,
)


class FakeService:
    def __init__(self, result: CompletionResult | Exception):
        self.result = result
        self.requests = []
        self.profiles = []

    async def complete(self, request, profile):
        self.requests.append(request)
        self.profiles.append(profile)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_grounding_case_manifest_is_valid_and_not_movie_only():
    validate_cases()

    assert len(CASES) == 5
    assert {case.profile for case in CASES} == {
        ChatProfile.TOOLS,
        ChatProfile.THINKING,
    }
    assert any(case.expected_tools == () for case in CASES)
    assert any(case.expected_tools == ("calculator",) for case in CASES)
    assert sum(case.expected_tools == ("search_web",) for case in CASES) == 2


@pytest.mark.asyncio
async def test_evaluator_checks_tools_citations_patterns_and_latency():
    case = GroundingCase(
        case_id="test",
        prompt="Question",
        profile=ChatProfile.TOOLS,
        expected_tools=("search_web",),
        minimum_citations=1,
        required_patterns=(r"(?:od[yi]s+e(?:us|u)|uliss\w*)",),
        forbidden_patterns=(r"does not exist",),
    )
    service = FakeService(CompletionResult(
        answer="Odisseus returns home.",
        citations=(object(),),
        correlation_id="correlation",
        selected_tools=("search_web",),
    ))

    result = await evaluate_case(service, case, 2)

    assert result.passed is True
    assert result.attempt == 2
    assert result.citation_count == 1
    assert service.requests[0].text == "Question"
    assert service.profiles == [ChatProfile.TOOLS]
    assert "Odisseus returns home" not in format_result(result)


@pytest.mark.asyncio
async def test_evaluator_reports_error_type_without_content():
    case = GroundingCase(
        case_id="failure",
        prompt="Private prompt",
        profile=ChatProfile.TOOLS,
        expected_tools=("search_web",),
    )
    service = FakeService(RuntimeError("PRIVATE RESPONSE DETAIL"))

    result = await evaluate_case(service, case, 1)

    assert result.passed is False
    assert result.error_type == "RuntimeError"
    rendered = format_result(result)
    assert "Private prompt" not in rendered
    assert "PRIVATE RESPONSE DETAIL" not in rendered
