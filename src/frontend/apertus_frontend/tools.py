"""Validated, allowlisted tools that Apertus may select through native calls."""

from __future__ import annotations

import ast
import asyncio
import json
import math
import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator


class ToolError(RuntimeError):
    """Base error for rejected or failed tool execution."""


class ToolValidationError(ToolError):
    """Raised when a model-selected tool or its arguments are not allowed."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ToolCitation:
    title: str
    url: str


@dataclass(frozen=True)
class ToolResult:
    content: str
    citations: tuple[ToolCitation, ...] = ()
    grounding_sources: tuple[str, ...] = ()
    grounding_fallback_answer: str = ""


ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]
ToolExecutor = Callable[[ToolCall], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    display_name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    timeout_seconds: float = 30.0
    max_calls: int = 1

    def openai_schema(self) -> dict[str, Any]:
        """Return the function-tool shape accepted by the vLLM OpenAI API."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Resolve and execute only registered tools after strict schema validation."""

    def __init__(self, specs: tuple[ToolSpec, ...]) -> None:
        self._specs = {spec.name: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise ValueError("Tool names must be unique.")

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolValidationError(f"Apertus selected unknown tool: {name}") from exc

    async def execute(self, call: ToolCall) -> ToolResult:
        spec = self.get(call.name)
        try:
            arguments = json.loads(call.arguments_json or "{}")
        except json.JSONDecodeError as exc:
            raise ToolValidationError(
                f"Apertus returned invalid JSON for {call.name}."
            ) from exc
        if not isinstance(arguments, dict):
            raise ToolValidationError(
                f"Apertus returned non-object arguments for {call.name}."
            )

        errors = sorted(
            Draft202012Validator(spec.parameters).iter_errors(arguments),
            key=lambda error: tuple(str(item) for item in error.path),
        )
        if errors:
            raise ToolValidationError(
                f"Invalid {call.name} arguments: {errors[0].message}"
            )

        try:
            async with asyncio.timeout(spec.timeout_seconds):
                return await spec.handler(arguments)
        except TimeoutError as exc:
            raise ToolError(f"Tool timed out: {call.name}") from exc


_BINARY_OPERATORS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


async def calculate(arguments: dict[str, Any]) -> ToolResult:
    expression = str(arguments["expression"]).strip()
    if not expression or len(expression) > 200:
        raise ToolValidationError("Calculator expression must be 1-200 characters.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolValidationError("Calculator expression is invalid.") from exc
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ToolValidationError("Calculator expression is too complex.")

    try:
        result = _evaluate_number(tree.body)
    except (OverflowError, ZeroDivisionError) as exc:
        raise ToolValidationError("Calculator expression cannot be evaluated.") from exc
    if not math.isfinite(result) or abs(result) > 1e15:
        raise ToolValidationError("Calculator result is outside the allowed range.")
    normalized: int | float = int(result) if result.is_integer() else result
    return ToolResult(
        content=json.dumps(
            {"expression": expression, "result": normalized},
            ensure_ascii=True,
        )
    )


def _evaluate_number(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate_number(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate_number(node.left)
        right = _evaluate_number(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ToolValidationError("Calculator exponent is outside the allowed range.")
        return float(_BINARY_OPERATORS[type(node.op)](left, right))
    raise ToolValidationError("Calculator allows only numeric arithmetic operators.")


async def get_current_time(arguments: dict[str, Any]) -> ToolResult:
    timezone_name = str(arguments["timezone"]).strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ToolValidationError(f"Unknown IANA timezone: {timezone_name}") from exc
    current = datetime.now(timezone)
    return ToolResult(
        content=json.dumps(
            {
                "timezone": timezone_name,
                "iso8601": current.isoformat(timespec="seconds"),
                "date": current.date().isoformat(),
                "time": current.strftime("%H:%M:%S"),
            },
            ensure_ascii=True,
        )
    )


def calculator_spec() -> ToolSpec:
    return ToolSpec(
        name="calculator",
        display_name="Calculator",
        description=(
            "Evaluate deterministic arithmetic. Use for calculations instead of "
            "calculating mentally. Do not use for factual lookup or current time."
        ),
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "Numbers and parentheses with +, -, *, /, //, %, or **."
                    ),
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        handler=calculate,
        timeout_seconds=2.0,
    )


def current_time_spec() -> ToolSpec:
    return ToolSpec(
        name="get_current_time",
        display_name="Current Time",
        description=(
            "Get the current date and time in an IANA timezone. Use only for "
            "clock time, calendar date, or timezone questions. Do not use merely "
            "because another fact is described as current."
        ),
        parameters={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                    "description": "IANA timezone such as Europe/Zurich.",
                }
            },
            "required": ["timezone"],
            "additionalProperties": False,
        },
        handler=get_current_time,
        timeout_seconds=2.0,
    )


def search_web_spec(handler: ToolHandler) -> ToolSpec:
    return ToolSpec(
        name="search_web",
        display_name="Web Search",
        description=(
            "Search current public web information. Use for current, latest, "
            "planned, upcoming, changing, status, price, news, release, or "
            "explicit internet verification requests. A current price, person, "
            "event, or status is Web Search, not Current Time. Do not use for stable "
            "facts, writing, identity, arithmetic, or current time."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": (
                        "Standalone search query that resolves conversation "
                        "references while preserving requested language and format."
                    ),
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
        timeout_seconds=30.0,
    )