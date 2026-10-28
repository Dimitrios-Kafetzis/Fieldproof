"""Tests of the assertion plugin API and registry (task P2-T05): what a well-formed type is,
how the built-ins and the entry points load, the schema union, `fieldproof assertions`."""

from __future__ import annotations

import json
from importlib.metadata import EntryPoint
from typing import Any, ClassVar, Literal

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from fieldproof.assertions import (
    BUILTIN,
    AssertionSpec,
    AssertionType,
    Checker,
    PlannedAssertion,
    Registry,
    assertion_union,
    check_assertion_type,
    load_registry,
    parameters_of,
    registry,
)
from fieldproof.assertions.primitives import ReachPoseType
from fieldproof.cli import app
from fieldproof.dsl import AssertionBase
from fieldproof.scenario import ASSERTION_TYPES

BUILTINS = {
    "reach_pose",
    "within_time",
    "topic_rate",
    "no_collision",
    "topic_value",
    "geofence",
    "min_distance",
    "path_efficiency",
    "mission_time",
    "message_latency",
    "sequence",
    "resource_usage",
    "lifecycle_state",
    "always",
    "never",
    "eventually",
}


class Noop(AssertionBase):
    type: Literal["test.noop"]
    knob: int = 1


class NoopChecker(Checker):
    pass


class NoopType(AssertionType):
    type: ClassVar[str] = "test.noop"
    Spec: ClassVar[AssertionSpec] = Noop
    summary: ClassVar[str] = "Nothing."

    def checker(self, planned: PlannedAssertion) -> Checker:
        return NoopChecker(planned)


def test_the_builtins_are_registered() -> None:
    known = registry()
    assert set(known.names) >= BUILTINS
    assert "example.visits_waypoints" in known
    assert known.problems == []
    assert all(known[t].source == BUILTIN for t in BUILTINS)
    assert set(ASSERTION_TYPES) == set(known.names)
    for kind in BUILTINS:
        registered = known[kind]
        assert registered.assertion_type.summary, kind
        assert registered.assertion_type.metrics, kind
        assert parameters_of(registered.spec), kind
    assert parameters_of(ReachPoseType.Spec)["tolerance_m"] == "Distance to the target, in metres."
    assert "on_fail" not in parameters_of(ReachPoseType.Spec)


def test_check_assertion_type() -> None:
    assert check_assertion_type(NoopType) is NoopType
    with pytest.raises(TypeError, match="not an AssertionType subclass"):
        check_assertion_type(object)
    with pytest.raises(TypeError, match="`type` must be a non-empty string"):
        check_assertion_type(type("Bad", (AssertionType,), {"type": "", "Spec": Noop}))
    with pytest.raises(TypeError, match="`Spec` must derive from AssertionBase"):
        check_assertion_type(type("Bad", (AssertionType,), {"type": "x", "Spec": object}))

    class Mismatch(AssertionBase):
        type: Literal["other"]

    with pytest.raises(TypeError, match=r"Mismatch.type must be Literal\['x'\]"):
        check_assertion_type(type("Bad", (AssertionType,), {"type": "x", "Spec": Mismatch}))
    with pytest.raises(NotImplementedError):
        AssertionType().checker(PlannedAssertion(Noop(name="n", type="test.noop")))


def test_registry_add_and_load() -> None:
    known = Registry()
    known.add(NoopType, "tests")
    known.add(NoopType, "tests again")  # the same class twice is fine
    assert known.names == ["test.noop"]
    assert "test.noop" in known
    assert known["test.noop"].type == "test.noop"
    with pytest.raises(ValueError, match="already registered by tests"):
        known.add(type("Other", (NoopType,), {}), "elsewhere")
    points = [
        EntryPoint("test.noop", "test_assertion_registry:NoopType", "fieldproof.assertions"),
        EntryPoint("broken", "no_such_module:Thing", "fieldproof.assertions"),
        EntryPoint("reach_pose", "test_assertion_registry:NoopChecker", "fieldproof.assertions"),
    ]
    loaded = load_registry(points, builtin_modules=("fieldproof.assertions.primitives",))
    assert set(loaded.names) == {
        *BUILTINS
        - {
            "geofence",
            "min_distance",
            "path_efficiency",
            "mission_time",
            "message_latency",
            "sequence",
            "resource_usage",
            "lifecycle_state",
            "always",
            "never",
            "eventually",
        },
        "test.noop",
    }
    assert len(loaded.problems) == 2
    assert "ModuleNotFoundError" in loaded.problems[0]
    assert "not an AssertionType subclass" in loaded.problems[1]
    assert loaded["test.noop"].source == "an entry point"


def test_assertion_union_from_a_registry() -> None:
    known = Registry()
    known.add(NoopType, "tests")
    single = TypeAdapter(assertion_union(known))
    assert single.validate_python({"name": "n", "type": "test.noop", "knob": 3}).knob == 3
    known.add(ReachPoseType, "tests")
    both = TypeAdapter(assertion_union(known))
    parsed = both.validate_python({"name": "n", "type": "test.noop"})
    assert isinstance(parsed, Noop)
    with pytest.raises(ValueError, match=r"test\.noop"):
        both.validate_python({"name": "n", "type": "nope"})


def test_assertions_command() -> None:
    result = CliRunner().invoke(app, ["assertions"])
    assert result.exit_code == 0, result.output
    for kind in (*BUILTINS, "example.visits_waypoints", "path", "resources"):
        assert kind in result.output
    assert "built-in" in result.output
    result = CliRunner().invoke(app, ["assertions", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["problems"] == []
    by_type: dict[str, Any] = {e["type"]: e for e in data["assertions"]}
    assert (
        by_type["geofence"]["parameters"]["mode"]
        == "Whether the vehicle must stay inside or outside."
    )
    assert by_type["geofence"]["metrics"]["min_clearance_m"].startswith("the smallest clearance")
    assert by_type["example.visits_waypoints"]["source"].startswith("fieldproof-example-assertions")
    kpis = {k["type"]: k for k in data["kpis"]}
    assert set(kpis) == {
        "path",
        "mission_time",
        "distance",
        "rate",
        "latency",
        "resources",
        "value",
        "localisation",
    }
    assert "efficiency" in kpis["path"]["metrics"]


def test_assertions_command_reports_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = Registry(problems=["dist 1.0 (x = y:Z): ImportError: no"])
    monkeypatch.setattr("fieldproof.scenario_cli.assertion_registry", lambda: broken)
    result = CliRunner().invoke(app, ["assertions"])
    assert result.exit_code == 2
    assert "ImportError: no" in result.output
    result = CliRunner().invoke(app, ["assertions", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["problems"] == broken.problems
