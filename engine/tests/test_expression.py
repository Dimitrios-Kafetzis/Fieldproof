"""Tests of the condition expressions of `topic_value` assertions (task P1-T06)."""

from __future__ import annotations

import math

import pytest

from fieldproof.expression import Condition, ExpressionError, compile_condition


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [
        ("value <= 1.5", 1.2, True),
        ("value <= 1.5", 1.6, False),
        ("abs(value) < 0.2", -0.1, True),
        ("0 < value and value < 2", 1.0, True),
        ("0 < value < 2", 2.5, False),
        ("value == 'ACTIVE'", "ACTIVE", True),
        ("not value", False, True),
        ("value in (1, 2, 3)", 2, True),
        ("value is None", None, True),
        ("sqrt(value) >= 2", 4.0, True),
        ("round(value) == 3", 2.6, True),
        ("len(value) > 2", [1, 2, 3], True),
        ("value[0] > 0.5", [0.7, 0.1], True),
        ("min(value, 10) == 3", 3, True),
        ("max(value, 1) - 1 if value > 0 else value", 2.0, True),
        ("value < inf and value != nan", 1.0, True),
        ("value % 2 == 0", 4, True),
        ("value ** 2 <= pi", 1.0, True),
        ("-value > -5", 1.0, True),
        ("isnan(value)", math.nan, True),
    ],
)
def test_conditions(text: str, value: object, expected: bool) -> None:
    condition = compile_condition(text)
    assert isinstance(condition, Condition)
    assert condition(value) is expected
    assert repr(condition) == f"Condition({text!r})"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "non-empty"),
        ("   ", "non-empty"),
        ("value <", "not an expression"),
        ("import os", "not an expression"),
        ("__import__('os')", "may be called"),
        ("value.real > 0", "Attribute is not allowed"),
        ("speed < 2", "unknown name 'speed'"),
        ("open('x')", "may be called"),
        ("(lambda: 1)()", "may be called"),
        ("value + (lambda: 1)", "Lambda is not allowed"),
        ("[v for v in value]", "ListComp is not allowed"),
        ("value if value else value := 1", "not an expression"),
        ("abs(value, key=1)", "keyword arguments"),
        ("value == b'x'", "not a valid constant"),
        ("{1: 2}[value]", "Dict is not allowed"),
        ("f'{value}'", "JoinedStr is not allowed"),
    ],
)
def test_rejected(text: str, message: str) -> None:
    with pytest.raises(ExpressionError, match=message):
        compile_condition(text)


def test_evaluation_errors_are_expression_errors() -> None:
    condition = compile_condition("value + 1 > 0")
    with pytest.raises(ExpressionError, match="cannot be evaluated for value None"):
        condition(None)
    with pytest.raises(ExpressionError, match="TypeError"):
        compile_condition("value[0] > 0")(5)
    with pytest.raises(ExpressionError, match="ZeroDivisionError"):
        compile_condition("1 / value > 0")(0)


def test_no_builtins_leak() -> None:
    condition = compile_condition("value > 0")
    assert condition(1) is True
    with pytest.raises(ExpressionError):
        compile_condition("value(1)")(print)
