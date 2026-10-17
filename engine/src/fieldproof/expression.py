"""Conditions over a value (task P1-T06): the ``condition`` of a ``topic_value`` assertion.

A condition is a Python expression over the name ``value`` (the field of each message), limited
to what a test condition needs: comparisons, arithmetic, boolean operators, a few numeric
functions and constants. :func:`compile_condition` parses it, refuses everything else (names,
attributes, calls, imports) with an :class:`ExpressionError` at validation time, and returns a
callable the evaluator applies to each sample.
"""

from __future__ import annotations

import ast
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sqrt": math.sqrt,
    "len": len,
    "float": float,
    "int": int,
    "isnan": math.isnan,
    "isinf": math.isinf,
}
CONSTANTS: dict[str, Any] = {"pi": math.pi, "inf": math.inf, "nan": math.nan}
NAMES = {"value", *FUNCTIONS, *CONSTANTS}
NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Constant,
    ast.Subscript,
    ast.Tuple,
    ast.List,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)


class ExpressionError(ValueError):
    """The condition is not a valid expression over `value`."""


@dataclass(frozen=True)
class Condition:
    """A compiled condition: `condition(value)` is its truth for one sample."""

    text: str
    _code: Any

    def __call__(self, value: Any) -> bool:
        """Evaluate the condition for `value` (an :class:`ExpressionError` if it cannot be)."""
        try:
            result = eval(  # noqa: S307  (the AST was whitelisted by compile_condition)
                self._code, {"__builtins__": {}}, {**FUNCTIONS, **CONSTANTS, "value": value}
            )
        except Exception as error:
            raise ExpressionError(
                f"{self.text!r} cannot be evaluated for value {value!r}: "
                f"{type(error).__name__}: {error}"
            ) from error
        return bool(result)

    def __repr__(self) -> str:
        return f"Condition({self.text!r})"


def compile_condition(text: str) -> Condition:
    """Parse and check a condition; an :class:`ExpressionError` for anything but an expression."""
    if not isinstance(text, str) or not text.strip():
        raise ExpressionError("a condition is a non-empty expression over value")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as error:
        raise ExpressionError(f"{text!r} is not an expression: {error.msg}") from error
    for node in ast.walk(tree):
        if not isinstance(node, NODES):
            raise ExpressionError(
                f"{text!r}: {type(node).__name__} is not allowed in a condition "
                "(comparisons, arithmetic, and/or/not, abs, min, max, sqrt, round over value)"
            )
        if isinstance(node, ast.Name) and node.id not in NAMES:
            raise ExpressionError(
                f"{text!r}: unknown name {node.id!r} (the message field is `value`)"
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ExpressionError(f"{text!r}: only {sorted(FUNCTIONS)} may be called")
            if node.keywords:
                raise ExpressionError(f"{text!r}: keyword arguments are not allowed")
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes | complex):
            raise ExpressionError(f"{text!r}: {node.value!r} is not a valid constant")
    return Condition(text.strip(), compile(tree, "<condition>", "eval"))
