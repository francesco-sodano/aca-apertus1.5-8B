import asyncio
import json
from dataclasses import replace

import pytest

from apertus_frontend.tools import (
    ToolCall,
    ToolRegistry,
    ToolValidationError,
    calculator_spec,
    current_time_spec,
)


@pytest.mark.asyncio
async def test_calculator_executes_validated_arithmetic():
    registry = ToolRegistry((calculator_spec(),))

    result = await registry.execute(
        ToolCall("call-1", "calculator", '{"expression":"(37 * 19) + 4"}')
    )

    assert json.loads(result.content)["result"] == 707


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression",
    ["__import__('os').system('whoami')", "2 ** 100", "1 / 0", "True + 1", "(-1) ** 0.5"],
)
async def test_calculator_rejects_unsafe_or_unbounded_input(expression):
    registry = ToolRegistry((calculator_spec(),))

    with pytest.raises(ToolValidationError):
        await registry.execute(
            ToolCall(
                "call-1",
                "calculator",
                json.dumps({"expression": expression}),
            )
        )


@pytest.mark.asyncio
async def test_current_time_returns_requested_iana_timezone():
    registry = ToolRegistry((current_time_spec(),))

    result = await registry.execute(
        ToolCall("call-1", "get_current_time", '{"timezone":"Europe/Zurich"}')
    )

    payload = json.loads(result.content)
    assert payload["timezone"] == "Europe/Zurich"
    assert "T" in payload["iso8601"]


@pytest.mark.asyncio
async def test_registry_rejects_unknown_tool_and_invalid_arguments():
    registry = ToolRegistry((calculator_spec(),))

    with pytest.raises(ToolValidationError, match="unknown tool"):
        await registry.execute(ToolCall("call-1", "delete_all", "{}"))
    with pytest.raises(ToolValidationError, match="required property"):
        await registry.execute(ToolCall("call-2", "calculator", "{}"))


@pytest.mark.asyncio
async def test_registry_enforces_total_budget_across_tools():
    registry = ToolRegistry((calculator_spec(), current_time_spec()))
    await registry.execute(ToolCall("first", "calculator", '{"expression":"1+1"}'))

    with pytest.raises(ToolValidationError, match="budget"):
        await registry.execute(ToolCall("second", "get_current_time", '{"timezone":"UTC"}'))


@pytest.mark.asyncio
async def test_registry_enforces_per_tool_budget_for_concurrent_calls():
    registry = ToolRegistry((replace(calculator_spec(), max_calls=1),), max_total_calls=2)
    results = await asyncio.gather(
        registry.execute(ToolCall("first", "calculator", '{"expression":"1+1"}')),
        registry.execute(ToolCall("second", "calculator", '{"expression":"2+2"}')),
        return_exceptions=True,
    )

    assert sum(isinstance(result, ToolValidationError) for result in results) == 1


@pytest.mark.asyncio
async def test_failed_tool_execution_still_consumes_budget():
    registry = ToolRegistry((calculator_spec(),))
    with pytest.raises(ToolValidationError):
        await registry.execute(ToolCall("first", "calculator", '{"expression":"1/0"}'))
    with pytest.raises(ToolValidationError, match="budget"):
        await registry.execute(ToolCall("second", "calculator", '{"expression":"1+1"}'))