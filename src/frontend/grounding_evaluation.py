"""Run privacy-safe release gates for grounded and non-grounded chat paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Protocol

os.environ["TRACE_CONTENT"] = "false"

from apertus_frontend.pipeline import ChatProfile, ChatRequest, CompletionResult


@dataclass(frozen=True)
class GroundingCase:
    case_id: str
    prompt: str
    profile: ChatProfile
    expected_tools: tuple[str, ...]
    minimum_citations: int = 0
    required_patterns: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    max_seconds: float = 45.0
    repeats: int = 1


@dataclass(frozen=True)
class GroundingEvaluationResult:
    case_id: str
    attempt: int
    elapsed_seconds: float
    tools: tuple[str, ...]
    citation_count: int
    required_patterns_found: bool
    forbidden_patterns_absent: bool
    passed: bool
    error_type: str = ""


class CompletionService(Protocol):
    async def complete(
        self, request: ChatRequest, profile: ChatProfile
    ) -> CompletionResult: ...


CASES = (
    GroundingCase(
        case_id="calculator-warmup",
        prompt="Calculate 18 percent of 700.",
        profile=ChatProfile.TOOLS,
        expected_tools=("calculator",),
        required_patterns=(r"\b126(?:\.0+)?\b",),
        max_seconds=900.0,
    ),
    GroundingCase(
        case_id="local-translation",
        prompt="Translate 'Good morning' into Italian.",
        profile=ChatProfile.TOOLS,
        expected_tools=(),
        required_patterns=(r"buongiorno",),
    ),
    GroundingCase(
        case_id="thinking-explanation",
        prompt="Explain briefly why the sky appears blue.",
        profile=ChatProfile.THINKING,
        expected_tools=(),
        forbidden_patterns=(r"<\|inner_(?:prefix|suffix)\|>",),
    ),
    GroundingCase(
        case_id="odyssey-romansh",
        prompt=(
            "Give me the plot of Christopher Nolan's movie 'The Odyssey'. "
            "Answer only in Rumantsch Grischun."
        ),
        profile=ChatProfile.TOOLS,
        expected_tools=("search_web",),
        minimum_citations=1,
        required_patterns=(r"(?:od[yi]s+e(?:us|u)|uliss\w*)",),
        forbidden_patterns=(
            r"does not exist|doesn't exist|no such film|"
            r"n['\u2019](?:exista|ha betg)|nagin film|betg in film",
        ),
        repeats=5,
    ),
    GroundingCase(
        case_id="current-source-french",
        prompt=(
            "Use current official sources to identify the members of the Swiss "
            "Federal Council. Answer briefly in French."
        ),
        profile=ChatProfile.TOOLS,
        expected_tools=("search_web",),
        minimum_citations=1,
    ),
)


def validate_cases(cases: tuple[GroundingCase, ...] = CASES) -> None:
    identifiers = [case.case_id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Grounding evaluation case IDs must be unique.")
    if not cases:
        raise ValueError("At least one grounding evaluation case is required.")
    for case in cases:
        if not case.case_id or not case.prompt or case.repeats < 1:
            raise ValueError("Grounding evaluation cases contain invalid fields.")
        if case.minimum_citations < 0 or case.max_seconds <= 0:
            raise ValueError("Grounding evaluation limits must be positive.")
        for pattern in (*case.required_patterns, *case.forbidden_patterns):
            re.compile(pattern, re.IGNORECASE)


async def evaluate_case(
    service: CompletionService,
    case: GroundingCase,
    attempt: int,
) -> GroundingEvaluationResult:
    started = time.perf_counter()
    tools: tuple[str, ...] = ()
    citation_count = 0
    required_patterns_found = False
    forbidden_patterns_absent = False
    error_type = ""
    try:
        result = await asyncio.wait_for(
            service.complete(
                ChatRequest(
                    text=case.prompt,
                    correlation_id=f"release-{case.case_id}-{attempt}",
                ),
                case.profile,
            ),
            timeout=max(case.max_seconds + 30.0, 60.0),
        )
        tools = result.selected_tools
        citation_count = len(result.citations)
        required_patterns_found = all(
            re.search(pattern, result.answer, re.IGNORECASE)
            for pattern in case.required_patterns
        )
        forbidden_patterns_absent = not any(
            re.search(pattern, result.answer, re.IGNORECASE)
            for pattern in case.forbidden_patterns
        )
    except Exception as error:
        error_type = type(error).__name__

    elapsed_seconds = round(time.perf_counter() - started, 3)
    passed = (
        not error_type
        and tools == case.expected_tools
        and citation_count >= case.minimum_citations
        and required_patterns_found
        and forbidden_patterns_absent
        and elapsed_seconds <= case.max_seconds
    )
    return GroundingEvaluationResult(
        case_id=case.case_id,
        attempt=attempt,
        elapsed_seconds=elapsed_seconds,
        tools=tools,
        citation_count=citation_count,
        required_patterns_found=required_patterns_found,
        forbidden_patterns_absent=forbidden_patterns_absent,
        passed=passed,
        error_type=error_type,
    )


def format_result(result: GroundingEvaluationResult) -> str:
    return json.dumps(asdict(result), sort_keys=True)


async def run_live(*, interval_seconds: float) -> int:
    from app import get_runtime

    validate_cases()
    runtime = get_runtime()
    results: list[GroundingEvaluationResult] = []
    try:
        for case in CASES:
            for attempt in range(1, case.repeats + 1):
                result = await evaluate_case(runtime.service, case, attempt)
                results.append(result)
                print(format_result(result), flush=True)
                if interval_seconds and attempt < case.repeats:
                    await asyncio.sleep(interval_seconds)
    finally:
        await runtime.aclose()

    search_times = [
        result.elapsed_seconds
        for result in results
        if result.case_id == "odyssey-romansh" and result.passed
    ]
    print(json.dumps({
        "summary": {
            "passed": sum(result.passed for result in results),
            "total": len(results),
            "odyssey_median_seconds": (
                round(statistics.median(search_times), 3) if search_times else None
            ),
        }
    }, sort_keys=True))
    return 0 if all(result.passed for result in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run privacy-safe grounding release gates."
    )
    parser.add_argument(
        "--validate-cases",
        action="store_true",
        help="Validate the static manifest without calling Azure or Apertus.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=15.0,
        help="Delay between repeated search cases to avoid burst throttling.",
    )
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    if arguments.validate_cases:
        validate_cases()
        print(json.dumps({"case_count": len(CASES), "valid": True}))
        return 0
    if arguments.interval_seconds < 0:
        raise ValueError("--interval-seconds cannot be negative.")
    return asyncio.run(run_live(interval_seconds=arguments.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
