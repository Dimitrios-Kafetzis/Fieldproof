"""Tests of the timeline DSL (task P2-T03): the triggers of an entry, the `when` conditions,
and what the scenario schema checks across entries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from fieldproof.dsl import METRICS, TimelineBase, When, entry_label, parse_duration
from fieldproof.events.container import ContainerPause, ContainerResume
from fieldproof.events.param import ParamSet
from fieldproof.scenario import ScenarioError, validate_data

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/scenarios/ugv_events.yaml"


@pytest.fixture
def data() -> dict[str, Any]:
    return dict(yaml.safe_load(EXAMPLE.read_text()))


def entry(**fields: Any) -> TimelineBase:
    return ContainerResume.model_validate(
        {"vehicle": "ugv1", "action": "container.resume", **fields}
    )


# ---------------------------------------------------------------------------------- triggers
def test_defaults() -> None:
    e = entry()
    assert e.at == 0.0
    assert (e.after, e.when, e.every) == (None, None, None)
    assert e.repeated is False
    assert e.trigger() == {"at_s": 0.0}
    assert e.parameters() == {}
    assert entry_label(e) == "container.resume@0s"
    assert entry_label(entry(name="resume", at="1m")) == "resume"


def test_trigger_forms() -> None:
    e = entry(at="5s", after="drive")
    assert e.trigger() == {"at_s": 5.0, "after": "drive"}
    e = entry(every="5s", count=3, at=2)
    assert e.every == 5.0
    assert e.trigger() == {"at_s": 2.0, "every_s": 5.0, "count": 3}
    e = entry(every=1.5, until_s="1m")
    assert e.trigger() == {"at_s": 0.0, "every_s": 1.5, "until_s": 60.0}
    e = entry(when={"metric": "sim_time", "condition": "value > 10"}, within_s="2m")
    assert e.trigger() == {
        "at_s": 0.0,
        "when": {"metric": "sim_time", "condition": "value > 10"},
        "within_s": 120.0,
    }


def test_parameters_are_the_entrys_own() -> None:
    e = ContainerPause.model_validate(
        {"vehicle": "ugv1", "action": "container.pause", "duration_s": "3s", "at": 5, "name": "p"}
    )
    assert e.parameters() == {"duration_s": 3.0}
    p = ParamSet.model_validate(
        {"vehicle": "ugv1", "action": "param.set", "node": "n", "parameter": "p", "value": True}
    )
    assert p.parameters() == {"node": "n", "parameter": "p", "value": True, "timeout_s": 30.0}


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"after": "a", "when": {"metric": "rtf", "condition": "value < 1"}}, "exclusive"),
        ({"name": "a", "after": "a"}, "cannot follow itself"),
        ({"within_s": 5}, "within_s goes with when"),
        ({"count": 2}, "count and until_s go with every"),
        ({"until_s": 2}, "count and until_s go with every"),
        ({"every": 0}, "every"),
        ({"every": "-1s"}, "every"),
        ({"every": "soon"}, "duration"),
        ({"count": 0, "every": 1}, "count"),
        ({"every": 2, "when": {"metric": "rtf", "condition": "value < 1"}}, "cannot repeat"),
        ({"at": -1}, "at"),
        ({"vehicle": "Ugv-1"}, "vehicle"),
    ],
)
def test_trigger_errors(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        entry(**fields)


# -------------------------------------------------------------------------------------- when
def test_when_forms() -> None:
    topic = When(topic="odom", field="twist.twist.linear.x", condition="value > 0.3")
    assert topic.needs_vehicle() is True
    assert topic.summary() == "odom.twist.twist.linear.x: value > 0.3"
    metric = When(metric="sim_time", condition="value >= 20")
    assert metric.needs_vehicle() is False
    assert metric.summary() == "sim_time: value >= 20"
    distance = When(metric="distance_to", target={"x": 1.0, "y": 2.0}, condition="value < 2")  # type: ignore[arg-type]
    assert distance.needs_vehicle() is True
    assert set(METRICS) == {"sim_time", "rtf", "x", "y", "z", "yaw", "distance_to"}


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"condition": "value"}, "either topic"),
        ({"topic": "odom", "metric": "rtf", "condition": "value"}, "either topic"),
        ({"topic": "odom", "condition": "value"}, "needs the field"),
        ({"metric": "rtf", "field": "data", "condition": "value"}, "go with a topic"),
        ({"metric": "rtf", "target": {"x": 1, "y": 1}, "condition": "value"}, "distance_to"),
        ({"metric": "distance_to", "condition": "value < 1"}, "needs a target"),
        ({"metric": "speed", "condition": "value"}, "metric"),
        ({"metric": "rtf", "condition": "speed > 1"}, "unknown name 'speed'"),
        ({"metric": "rtf", "condition": "import os"}, "not an expression"),
        ({"metric": "rtf", "condition": "value", "extra": 1}, "extra"),
    ],
)
def test_when_errors(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        When.model_validate(fields)


# ------------------------------------------------------------------------------ the scenario
def test_example_loads(data: dict[str, Any]) -> None:
    scenario = validate_data(data)
    labels = [entry_label(e) for e in scenario.timeline]
    assert labels == ["drive_ahead", "say_hello", "tighten_failsafe", "freeze_stack", "stop_stack"]
    hello = scenario.timeline[1]
    assert (hello.every, hello.count, hello.at) == (5.0, 3, 2.0)
    tighten = scenario.timeline[2]
    assert tighten.when is not None
    assert tighten.when.metric == "distance_to"
    assert tighten.within_s == 240.0
    assert scenario.timeline[3].after == "drive_ahead"
    assert scenario.timeline[3].at == 1.0


@pytest.mark.parametrize(
    ("index", "fields", "message"),
    [
        (3, {"after": "nowhere"}, r"timeline\[3\].after: 'nowhere' is not a timeline entry"),
        (3, {"after": "freeze_stack"}, "after: an entry cannot follow itself"),
        (3, {"after": "say_hello"}, r"timeline\[3\].after: 'say_hello' is a repeated entry"),
        (2, {"when": {"metric": "x", "vehicle": "ugv9", "condition": "value > 1"}}, "ugv9"),
        (2, {"vehicle": "ugv9"}, r"timeline\[2\].vehicle: 'ugv9' is not a vehicle"),
        (1, {"action": "teleport"}, "teleport"),
        (1, {"name": "drive_ahead"}, "timeline: names must be unique \\(drive_ahead\\)"),
        (4, {"action": "link.drop"}, "needs a network section"),
    ],
)
def test_scenario_errors(
    data: dict[str, Any], index: int, fields: dict[str, Any], message: str
) -> None:
    data["timeline"][index].update(fields)
    with pytest.raises(ScenarioError, match=message):
        validate_data(data)


def test_after_cycle(data: dict[str, Any]) -> None:
    data["timeline"][0].update(after="stop_stack")
    with pytest.raises(ScenarioError, match="after forms a cycle: drive_ahead -> stop_stack"):
        validate_data(data)


def test_unknown_action_names_the_known_ones(data: dict[str, Any]) -> None:
    data["timeline"][0]["action"] = "fly"
    with pytest.raises(ScenarioError, match=r"'fly'.*navigate_to_pose") as excinfo:
        validate_data(data)
    assert "container.pause" in str(excinfo.value)
    assert "example.announce" in str(excinfo.value)


def test_parse_duration_forms() -> None:
    assert parse_duration("1h2m3.5s") == 3723.5
    assert parse_duration(7) == 7.0
    with pytest.raises(ValueError, match="duration"):
        parse_duration(True)
