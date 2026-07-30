import json

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
    ["__import__('os').system('whoami')", "2 ** 100", "1 / 0"],
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