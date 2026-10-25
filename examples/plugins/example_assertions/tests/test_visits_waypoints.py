"""Tests of the example assertion plugin (task P2-T05): registration, planning, the checker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fieldproof_example_assertions import VisitsWaypoints, VisitsWaypointsType

from fieldproof.assertions import (
    FAILED,
    PASSED,
    Evaluator,
    PlanContext,
    PlannedAssertion,
    Sample,
    plan_assertion,
    registry,
)
from fieldproof.scenario import validate_data
from fieldproof.world import Pose, WorldInfo

T0 = 1_700_000_000.0


class Vehicle:
    name = "ugv1"
    namespace = ""
    container_name = "fieldproof-x-ugv1"
    bridge_name = "fieldproof-x-bridge-ugv1"
    spawn = Pose(10.0, 3.0, 0.0, -1.5708)

    def topic(self, name: str) -> str:
        return name if name.startswith("/") else f"/{name}"

    def bundle(self) -> Any:
        raise NotImplementedError


def scenario(assertion: dict[str, Any]) -> Any:
    return validate_data(
        {
            "apiVersion": "fieldproof.dev/v0alpha1",
            "kind": "Scenario",
            "metadata": {"name": "t"},
            "world": {"file": "depot.sdf"},
            "vehicles": [{"name": "ugv1", "manifest": "r.yaml", "spawn": {"x": 0, "y": 0}}],
            "assertions": [assertion],
        }
    )


def pose(t: float, x: float, y: float) -> Sample:
    data = {"kind": "pose", "wall": T0 + t, "sim": t, "model": "ugv1", "x": x, "y": y}
    return Sample.from_line(json.dumps(data))


def planned(**extra: Any) -> PlannedAssertion:
    loaded = scenario(
        {
            "name": "patrolled",
            "type": "example.visits_waypoints",
            "vehicle": "ugv1",
            "waypoints": [{"frame": "spawn_dock"}, {"x": 10.0, "y": -1.0}],
            **extra,
        }
    )
    world = WorldInfo(Path("depot.sdf"), "depot", {"spawn_dock": Pose(10.0, 3.0, 0.0, -1.5708)})
    context = PlanContext(world, {"ugv1": Vehicle()}, where="assertions[0]")
    return plan_assertion(loaded.assertions[0], context)


def test_registered() -> None:
    known = registry()
    assert "example.visits_waypoints" in known
    assert type(known["example.visits_waypoints"].assertion_type) is VisitsWaypointsType
    assert known["example.visits_waypoints"].source.startswith("fieldproof-example-assertions")


def test_plan_places_the_waypoints() -> None:
    item = planned()
    assert isinstance(item.assertion, VisitsWaypoints)
    assert item.model == "ugv1"
    assert [(w.x, w.y) for w in item.resolved["waypoints"]] == [(10.0, 3.0), (10.0, -1.0)]
    assert item.to_dict()["resolved"]["waypoints"][0]["x"] == 10.0


def test_passes_once_every_waypoint_was_visited() -> None:
    evaluator = Evaluator([planned()])
    evaluator.start(T0)
    assert evaluator.feed(pose(1.0, 10.0, -0.9)) == []
    assert evaluator.feed(pose(5.0, 5.0, 5.0)) == []
    (result,) = evaluator.feed(pose(9.0, 10.2, 3.1))
    assert result.status == PASSED
    assert result.reason == "all 2 waypoint(s) visited by 9.0 s"
    assert set(result.evidence["visited"]) == {0, 1}
    assert result.metrics["visited"] == {"value": 2, "unit": ""}
    assert result.metrics["completed_at_s"] == {"value": 9.0, "unit": "s"}


def test_fails_naming_the_missing_waypoints() -> None:
    evaluator = Evaluator([planned(within_s=10)])
    evaluator.start(T0)
    evaluator.feed(pose(1.0, 10.0, -0.9))
    (result,) = evaluator.tick(T0 + 10.0)
    assert result.status == FAILED
    assert result.reason == (
        "not all visited within 10 s: 1 of 2 waypoint(s) visited, missing (10, 3)"
    )
    assert result.evidence["missing"] == ["(10, 3)"]
    evaluator = Evaluator([planned()])
    evaluator.start(T0)
    (result,) = evaluator.finish(T0 + 3.0)
    assert result.status == FAILED
    assert result.reason.startswith("the timeline ended: 0 of 2")
