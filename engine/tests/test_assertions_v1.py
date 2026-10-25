"""Tests of the assertion library v1 (task P2-T05) on synthetic streams.

Every new type (`geofence`, `min_distance`, `path_efficiency`, `mission_time`,
`message_latency`, `sequence`, `resource_usage`, `lifecycle_state`, `always`, `never`,
`eventually`) passing, failing at a sample, at a deadline and at the end, with its evidence
and metrics; the plan hooks and their refusals; the evaluator's verdict propagation, abort and
excerpts; the observation plan with the new kinds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fieldproof.assertions import (
    FAILED,
    PASSED,
    PENDING,
    SKIPPED,
    Evaluator,
    PlanContext,
    PlannedAssertion,
    Sample,
    Watch,
    observe_plan,
    plan_assertion,
    registry,
)
from fieldproof.assertions.spatial import Box, Circle, Region, Subject
from fieldproof.assertions.timing import Step
from fieldproof.dsl import ScenarioError
from fieldproof.scenario import Scenario, validate_data
from fieldproof.world import Pose, WorldInfo

T0 = 1_700_000_000.0
FRAMES = {"spawn_dock": Pose(10.0, 3.0, 0.0, -1.5708), "goal": Pose(10.0, -1.0, 0.0, 0.0)}


class Vehicle:
    """A vehicle of the plan, as the assertions' plan context sees it."""

    def __init__(self, name: str, namespace: str = "") -> None:
        self.name = name
        self.namespace = namespace
        self.container_name = f"fieldproof-x-{name}"
        self.bridge_name = f"fieldproof-x-bridge-{name}"
        self.spawn = Pose(10.0, 3.0, 0.0, -1.5708)

    def topic(self, name: str) -> str:
        if name.startswith("/"):
            return name
        return f"/{self.namespace}/{name}" if self.namespace else f"/{name}"

    def bundle(self) -> Any:
        raise NotImplementedError


VEHICLES: dict[str, Any] = {"ugv1": Vehicle("ugv1"), "ugv2": Vehicle("ugv2", "ugv2")}
WORLD = WorldInfo(Path("depot.sdf"), "depot", FRAMES)


def scenario(*assertions: dict[str, Any], kpis: list[dict[str, Any]] | None = None) -> Scenario:
    """A scenario with two vehicles, two named actions and the given assertions."""
    return validate_data(
        {
            "apiVersion": "fieldproof.dev/v0alpha1",
            "kind": "Scenario",
            "metadata": {"name": "t"},
            "world": {"file": "depot.sdf"},
            "vehicles": [
                {"name": "ugv1", "manifest": "r.yaml", "spawn": {"x": 0, "y": 0}},
                {
                    "name": "ugv2",
                    "manifest": "r.yaml",
                    "namespace": "ugv2",
                    "spawn": {"x": 5, "y": 0},
                },
            ],
            "timeline": [
                {
                    "name": "drive",
                    "vehicle": "ugv1",
                    "action": "navigate_to_pose",
                    "goal": {"x": 1, "y": 0},
                },
                {
                    "name": "park",
                    "vehicle": "ugv1",
                    "action": "navigate_to_pose",
                    "goal": {"x": 2, "y": 0},
                    "after": "drive",
                },
            ],
            "assertions": list(assertions),
            "kpis": kpis or [],
        }
    )


def plan(*assertions: dict[str, Any]) -> list[PlannedAssertion]:
    """The assertions planned through their types' hooks."""
    loaded = scenario(*assertions)
    names = tuple(a.name for a in loaded.assertions)
    return [
        plan_assertion(
            a,
            PlanContext(
                WORLD,
                VEHICLES,
                actions=("drive", "park"),
                assertions=names,
                where=f"assertions[{i}] ({a.name})",
            ),
        )
        for i, a in enumerate(loaded.assertions)
    ]


def planned(assertion: dict[str, Any]) -> PlannedAssertion:
    return plan(assertion)[0]


def evaluator(*assertions: dict[str, Any]) -> Evaluator:
    ev = Evaluator(plan(*assertions))
    ev.start(T0)
    return ev


def sample(kind: str, t: float, **data: Any) -> Sample:
    sim = data.pop("sim", t)
    return Sample.from_line(json.dumps({"kind": kind, "wall": T0 + t, "sim": sim, **data}))


def pose(t: float, x: float, y: float, model: str = "ugv1", z: float = 0.0) -> Sample:
    return sample("pose", t, model=model, x=x, y=y, z=z, yaw=0.0)


def value(t: float, topic: str, field: str, value: Any) -> Sample:
    return sample("value", t, topic=topic, field=field, value=value)


def latency(
    t: float, topic: str, p95: float, mx: float | None = None, mean: float | None = None
) -> Sample:
    return sample(
        "latency",
        t,
        topic=topic,
        p95_ms=p95,
        max_ms=mx if mx is not None else p95,
        mean_ms=mean if mean is not None else p95 / 2,
        min_ms=1.0,
        count=10,
        total=int(t * 10),
        window_s=2.0,
    )


def resource(t: float, container: str, cpu: float, memory: float) -> Sample:
    return sample("resource", t, sim=None, container=container, cpu_pct=cpu, memory_mb=memory)


def lifecycle(t: float, node: str, state: str) -> Sample:
    return sample("lifecycle", t, node=node, state=state)


def one(results: list[Any]) -> Any:
    assert len(results) == 1, results
    return results[0]


# ----------------------------------------------------------------------------------- regions
def test_region_shapes() -> None:
    circle = Region(circle=Circle(x=0.0, y=0.0, radius_m=5.0))
    assert circle.contains(3.0, 4.0)
    assert not circle.contains(3.1, 4.0)
    assert circle.clearance(3.0, 0.0) == pytest.approx(2.0)
    box = Region(box=Box(x_min=0.0, x_max=10.0, y_min=-5.0, y_max=5.0), z_max=1.0)
    assert box.contains(1.0, 0.0)
    assert box.contains(0.0, 0.0)
    assert not box.contains(1.0, 0.0, z=2.0)
    assert box.clearance(1.0, 0.0) == pytest.approx(1.0)
    assert box.clearance(-3.0, 9.0) == pytest.approx(-5.0)
    polygon = Region(polygon=[[0, 0], [4, 0], [4, 4], [0, 4]])
    assert polygon.contains(2.0, 2.0)
    assert polygon.contains(4.0, 2.0)
    assert not polygon.contains(5.0, 2.0)
    assert polygon.clearance(1.0, 2.0) == pytest.approx(1.0)
    assert polygon.clearance(7.0, 2.0) == pytest.approx(-3.0)
    assert polygon.describe() == "polygon of 4 vertices"
    assert box.describe() == "box [0, 10] x [-5, 5], z in [-inf, 1.0]"
    with pytest.raises(ValueError, match="exactly one"):
        Region.model_validate(
            {
                "circle": {"x": 0, "y": 0, "radius_m": 1},
                "box": {"x_min": 0, "x_max": 1, "y_min": 0, "y_max": 1},
            }
        )
    with pytest.raises(ValueError, match="three vertices"):
        Region(polygon=[[0, 0], [1, 1]])
    with pytest.raises(ValueError, match="x_max"):
        Region.model_validate({"box": {"x_min": 1, "x_max": 0, "y_min": 0, "y_max": 1}})
    with pytest.raises(ValueError, match="exactly one"):
        Subject()


# ---------------------------------------------------------------------------------- geofence
def geofence(**extra: Any) -> dict[str, Any]:
    return {
        "name": "fenced",
        "type": "geofence",
        "vehicle": "ugv1",
        "region": {"circle": {"x": 0.0, "y": 0.0, "radius_m": 5.0}},
        **extra,
    }


def test_geofence_fails_at_the_first_pose_outside() -> None:
    ev = evaluator(geofence())
    assert ev.feed(pose(1.0, 1.0, 0.0)) == []
    assert ev.feed(pose(2.0, 3.0, 0.0, model="ugv2")) == [], "another model"
    result = one(ev.feed(pose(3.0, 6.0, 0.0)))
    assert result.status == FAILED
    assert result.reason == (
        "ugv1 left the circle of 5 m around (0, 0) at 3.0 s (at (6.00, 0.00), 1.00 m past the "
        "boundary)"
    )
    assert result.evidence["violation"] == {
        "t_s": 3.0,
        "sim_s": 3.0,
        "x": 6.0,
        "y": 0.0,
        "clearance_m": -1.0,
    }
    assert result.evidence["samples"] == 2
    assert [e["clearance_m"] for e in result.evidence["excerpt"]] == [4.0, -1.0]
    assert result.metrics["min_clearance_m"] == {"value": -1.0, "unit": "m"}


def test_geofence_passes_at_the_end_with_the_smallest_clearance() -> None:
    ev = evaluator(geofence())
    ev.feed(pose(1.0, 1.0, 0.0))
    ev.feed(pose(2.0, 3.0, 0.0))
    result = one(ev.finish(T0 + 10.0))
    assert result.status == PASSED
    assert result.reason == (
        "ugv1 stayed inside the circle of 5 m around (0, 0) during the timeline (2 sample(s), "
        "closest to the boundary 2.00 m)"
    )
    assert result.evidence["min_clearance"]["t_s"] == 2.0


def test_geofence_outside_mode_and_window() -> None:
    ev = evaluator(
        geofence(
            region={"box": {"x_min": 0, "x_max": 2, "y_min": 0, "y_max": 2}},
            mode="outside",
            after_s=5,
            within_s=10,
        )
    )
    assert ev.feed(pose(1.0, 1.0, 1.0)) == [], "before the window"
    assert ev.feed(pose(6.0, 5.0, 5.0)) == []
    result = one(ev.feed(pose(7.0, 1.0, 1.0)))
    assert result.status == FAILED
    assert result.reason.startswith("ugv1 entered the box [0, 2] x [0, 2] at 7.0 s")
    ev = evaluator(geofence(after_s=5, within_s=10))
    ev.feed(pose(6.0, 1.0, 0.0))
    result = one(ev.tick(T0 + 10.0))
    assert result.status == PASSED
    assert "from 5 s to 10 s" in result.reason
    ev = evaluator(geofence(after_s=5, within_s=10))
    ev.feed(pose(6.0, 1.0, 0.0))
    result = one(ev.feed(pose(11.0, 9.0, 9.0)))
    assert result.status == PASSED, "a pose after the window closes it, and is not judged"


def test_geofence_without_poses_fails() -> None:
    ev = evaluator(geofence())
    result = one(ev.finish(T0 + 10.0))
    assert result.status == FAILED
    assert result.reason == "no ground-truth pose of ugv1 during the timeline"
    with pytest.raises(ScenarioError, match="within_s must be later"):
        scenario(geofence(after_s=5, within_s=5))


# ------------------------------------------------------------------------------ min_distance
def min_distance(to: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "name": "apart",
        "type": "min_distance",
        "vehicle": "ugv1",
        "to": to,
        "min_m": 1.0,
        **extra,
    }


def test_min_distance_to_another_vehicle() -> None:
    item = planned(min_distance({"vehicle": "ugv2"}))
    assert item.model == "ugv1"
    assert item.resolved == {"other_model": "ugv2"}
    ev = Evaluator([item])
    ev.start(T0)
    assert ev.feed(pose(1.0, 0.0, 0.0)) == [], "the other's pose is not known yet"
    assert ev.feed(pose(1.5, 3.0, 0.0, model="ugv2")) == []
    result = one(ev.feed(pose(2.0, 2.5, 0.0)))
    assert result.status == FAILED
    assert result.reason == "ugv1 came within 0.50 m of vehicle ugv2 at 2.0 s (minimum 1 m)"
    assert result.evidence["closest"] == {
        "t_s": 2.0,
        "sim_s": 2.0,
        "distance_m": 0.5,
        "own": {"x": 2.5, "y": 0.0},
        "other": {"x": 3.0, "y": 0.0},
    }
    assert result.metrics["closest_m"]["value"] == 0.5


def test_min_distance_to_a_model_and_a_target() -> None:
    item = planned(min_distance({"model": "crate"}))
    assert item.resolved == {"other_model": "crate"}
    assert item.watch == (Watch("model", {"model": "crate"}),)
    ev = Evaluator([item])
    ev.start(T0)
    ev.feed(pose(1.0, 0.0, 0.0))
    ev.feed(pose(1.0, 3.0, 4.0, model="crate"))
    result = one(ev.finish(T0 + 5.0))
    assert result.status == PASSED
    assert result.reason == (
        "ugv1 stayed 5.00 m or more from model crate during the timeline (minimum 1 m, 1 sample(s))"
    )
    item = planned(min_distance({"target": {"frame": "goal"}}, after_s=2))
    assert item.resolved["other_pose"] == FRAMES["goal"]
    assert item.to_dict()["resolved"]["other_pose"]["x"] == 10.0
    ev = Evaluator([item])
    ev.start(T0)
    assert ev.feed(pose(1.0, 10.0, -1.0)) == [], "before the window"
    result = one(ev.feed(pose(3.0, 10.0, -0.5)))
    assert result.status == FAILED
    assert "frame goal" in result.reason


def test_min_distance_without_samples_and_refusals() -> None:
    ev = evaluator(min_distance({"vehicle": "ugv2"}))
    result = one(ev.finish(T0 + 5.0))
    assert result.status == FAILED
    assert result.reason == (
        "no distance between ugv1 and vehicle ugv2 was measured during the timeline"
    )
    with pytest.raises(ScenarioError, match="another vehicle"):
        scenario(min_distance({"vehicle": "ugv1"}))
    with pytest.raises(ScenarioError, match="'ugv9' is not a vehicle"):
        scenario(min_distance({"vehicle": "ugv9"}))


# --------------------------------------------------------------------------- path_efficiency
def path(**extra: Any) -> dict[str, Any]:
    return {"name": "straight", "type": "path_efficiency", "vehicle": "ugv1", **extra}


def test_path_efficiency_over_an_action() -> None:
    ev = evaluator(path(action="drive", min_ratio=0.8))
    assert ev.feed(pose(0.5, 0.0, 0.0)) == [], "before the action: not counted"
    ev.action("drive", "started", T0 + 1.0)
    ev.feed(pose(1.0, 0.0, 0.0))
    ev.feed(pose(2.0, 2.0, 0.0))
    ev.feed(pose(3.0, 4.0, 0.0))
    result = one(ev.action("drive", "finished", T0 + 4.0, "ok"))
    assert result.status == PASSED
    assert result.reason == (
        "drive ended ok: 4.00 m driven for 4.00 m straight (efficiency 1.00), at least 0.8"
    )
    assert result.evidence["path_m"] == 4.0
    assert result.evidence["max_speed_mps"] == 2.0
    assert result.evidence["mean_speed_mps"] == pytest.approx(4.0 / 3.0, abs=1e-3)
    assert result.metrics["efficiency"] == {"value": 1.0, "unit": ""}


def test_path_efficiency_fails_on_a_detour_or_a_long_path() -> None:
    ev = evaluator(path(action="drive", min_ratio=0.8, target={"x": 4.0, "y": 0.0}))
    ev.action("drive", "started", T0 + 1.0)
    for t, (x, y) in enumerate([(0, 0), (0, 3), (4, 3), (4, 0)], start=1):
        ev.feed(pose(float(t), float(x), float(y)))
    result = one(ev.action("drive", "finished", T0 + 5.0, "ok"))
    assert result.status == FAILED
    assert result.reason == (
        "drive ended ok: 10.00 m driven for 4.00 m straight (efficiency 0.40), below 0.8"
    )
    ev = evaluator(path(action="drive", max_path_m=3.0))
    ev.action("drive", "started", T0 + 1.0)
    ev.feed(pose(1.0, 0.0, 0.0))
    ev.feed(pose(2.0, 2.0, 0.0))
    result = one(ev.feed(pose(3.0, 4.0, 0.0)))
    assert result.status == FAILED
    assert result.reason == "ugv1 drove 4.00 m by 3.0 s, over the limit of 3 m"


def test_path_efficiency_window_and_edge_cases() -> None:
    ev = evaluator(path(after_s=2, within_s=5, min_ratio=0.5))
    ev.feed(pose(1.0, 0.0, 0.0))
    ev.feed(pose(2.0, 0.0, 0.0))
    ev.feed(pose(4.0, 1.0, 0.0))
    result = one(ev.tick(T0 + 5.0))
    assert result.status == PASSED
    assert result.reason.startswith("the window closed: 1.00 m driven")
    ev = evaluator(path(action="drive"))
    result = one(ev.finish(T0 + 5.0))
    assert result.status == FAILED
    assert result.reason == "drive never started"
    ev = evaluator(path(action="drive"))
    ev.action("drive", "started", T0 + 1.0)
    ev.feed(pose(1.0, 0.0, 0.0))
    result = one(ev.action("drive", "finished", T0 + 2.0, "failed"))
    assert result.status == FAILED
    assert result.reason.startswith("drive ended failed: fewer than two")
    ev = evaluator(path())
    ev.feed(pose(1.0, 0.0, 0.0))
    ev.feed(pose(2.0, 0.0, 0.0))
    result = one(ev.finish(T0 + 3.0))
    assert result.status == PASSED, "a vehicle that did not move has efficiency 1"
    assert result.evidence["efficiency"] == 1.0
    with pytest.raises(ScenarioError, match="after_s and within_s go without an action"):
        scenario(path(action="drive", after_s=1))


# ------------------------------------------------------------------------------ mission_time
def mission(**extra: Any) -> dict[str, Any]:
    return {"name": "mission", "type": "mission_time", "max_s": 60, **extra}


def test_mission_time_of_the_whole_timeline() -> None:
    ev = evaluator(mission())
    assert ev.tick(T0 + 30.0) == []
    result = one(ev.finish(T0 + 45.0))
    assert result.status == PASSED
    assert result.reason == "from the timeline to its end took 45.0 s (limit 60 s)"
    assert (
        result.evidence
        == {
            "started_t_s": 0.0,
            "ended_t_s": 45.0,
            "duration_s": 45.0,
        }
        or result.evidence["duration_s"] == 45.0
    )
    assert result.metrics["duration_s"] == {"value": 45.0, "unit": "s"}
    ev = evaluator(mission(max_s=10))
    result = one(ev.tick(T0 + 10.5))
    assert result.status == FAILED
    assert result.reason == "from the timeline to its end has not completed 10 s after its start"


def test_mission_time_between_actions() -> None:
    ev = evaluator(mission(start="drive", end="park", max_s=20, min_s=5))
    assert ev.tick(T0 + 30.0) == [], "the span has not started"
    ev.action("drive", "started", T0 + 40.0)
    ev.action("drive", "finished", T0 + 50.0, "ok")
    ev.action("park", "started", T0 + 50.0)
    result = one(ev.action("park", "finished", T0 + 55.0, "ok"))
    assert result.status == PASSED
    assert result.reason == "from drive started to park ended took 15.0 s (limit 20 s)"
    assert [e["action"] for e in result.evidence["excerpt"]] == ["drive", "drive", "park", "park"]
    ev = evaluator(mission(start="drive", end="park", max_s=20, min_s=5))
    ev.action("drive", "started", T0 + 1.0)
    result = one(ev.action("park", "finished", T0 + 3.0, "ok"))
    assert result.status == FAILED
    assert result.reason == "from drive started to park ended took 2.0 s, under the floor of 5 s"
    ev = evaluator(mission(end="park"))
    result = one(ev.action("park", "finished", T0 + 3.0, "failed"))
    assert result.status == FAILED
    assert result.reason == "park ended failed after 3.0 s"
    ev = evaluator(mission(start="drive"))
    result = one(ev.finish(T0 + 3.0))
    assert result.reason == "drive never started"
    ev = evaluator(mission(end="park"))
    ev.action("park", "started", T0 + 1.0)
    result = one(ev.finish(T0 + 3.0))
    assert result.reason == "park did not complete"
    with pytest.raises(ScenarioError, match="min_s must not be above"):
        scenario(mission(min_s=100))
    with pytest.raises(ScenarioError, match="'fly' is not a timeline entry"):
        scenario(mission(end="fly"))


# --------------------------------------------------------------------------- message_latency
def fresh(**extra: Any) -> dict[str, Any]:
    return {
        "name": "fresh",
        "type": "message_latency",
        "topic": "scan",
        "vehicle": "ugv1",
        "max_ms": 100,
        **extra,
    }


def test_message_latency_passes_and_fails_on_the_statistic() -> None:
    item = planned(fresh())
    assert item.topic == "/scan"
    assert registry()["message_latency"].assertion_type.watches(item) == [
        Watch("latency", {"topic": "/scan", "window_s": 2.0, "type": None})
    ]
    ev = Evaluator([item])
    ev.start(T0)
    assert ev.feed(latency(1.0, "/scan", 40.0, mx=150.0)) == [], "p95 is judged, not max"
    assert ev.feed(latency(2.0, "/odom", 900.0)) == [], "another topic"
    result = one(ev.finish(T0 + 5.0))
    assert result.status == PASSED
    assert result.reason == "/scan p95 latency stayed under 40.0 ms over 1 window(s) (limit 100 ms)"
    assert result.metrics["max_ms"] == {"value": 150.0, "unit": "ms"}
    ev = evaluator(fresh(statistic="max"))
    result = one(ev.feed(latency(1.0, "/scan", 40.0, mx=150.0)))
    assert result.status == FAILED
    assert result.reason == "/scan max latency 150.0 ms at 1.0 s, over 100 ms (window 2 s)"
    assert result.evidence["window"]["max_ms"] == 150.0


def test_message_latency_errors_and_absence() -> None:
    ev = evaluator(fresh())
    result = one(
        ev.feed(sample("latency", 1.0, topic="/scan", error="the message has no header stamp"))
    )
    assert result.status == FAILED
    assert result.reason == "/scan: the message has no header stamp"
    ev = evaluator(fresh(after_s=5))
    ev.feed(latency(1.0, "/scan", 500.0))
    result = one(ev.finish(T0 + 8.0))
    assert result.status == FAILED
    assert result.reason == "no latency of /scan was measured after 5 s (10 message(s) seen)"
    with pytest.raises(ScenarioError, match="needs a vehicle or a leading /"):
        plan({**fresh(), "vehicle": None, "topic": "scan"})


# ---------------------------------------------------------------------------------- sequence
def sequence(steps: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"name": "ordered", "type": "sequence", "steps": steps, "vehicle": "ugv1", **extra}


REACHED = {
    "name": "reached",
    "type": "reach_pose",
    "vehicle": "ugv1",
    "target": {"x": 4.0, "y": 0.0},
}


def test_sequence_of_an_action_a_condition_and_an_assertion() -> None:
    items = plan(
        sequence(
            [
                {"action": "drive", "event": "started"},
                {"topic": "cmd_vel", "field": "linear.x", "condition": "value > 0"},
                {"assertion": "reached", "name": "arrived"},
            ]
        ),
        REACHED,
    )
    assert set(items[0].resolved["signals"]) == {1}
    assert items[0].resolved["signals"][1].topic == "/cmd_vel"
    assert items[0].to_dict()["resolved"]["signals"] == {
        1: {"topic": "/cmd_vel", "field": "linear.x"}
    }
    ev = Evaluator(items)
    ev.start(T0)
    assert ev.feed(value(0.5, "/cmd_vel", "linear.x", 0.4)) == [], "step 2 before step 1"
    assert ev.action("drive", "started", T0 + 1.0) == []
    assert ev.feed(value(1.0, "/cmd_vel", "linear.x", 0.4)) == [], "strictly after"
    assert ev.feed(value(2.0, "/cmd_vel", "linear.x", 0.0)) == []
    assert ev.feed(value(3.0, "/cmd_vel", "linear.x", 0.4)) == []
    results = ev.feed(pose(9.0, 4.0, 0.0))
    assert [(r.name, r.status) for r in results] == [("ordered", PASSED), ("reached", PASSED)]
    ordered = results[0]
    assert ordered.reason == "all 3 step(s) happened in order, the last at 9.0 s"
    assert [s["step"] for s in ordered.evidence["steps"]] == [
        "drive started",
        "cmd_vel.linear.x value > 0 (step 2)",
        "arrived",
    ]
    assert [s["t_s"] for s in ordered.evidence["steps"]] == [1.0, 3.0, 9.0]
    assert ordered.metrics["duration_s"] == {"value": 8.0, "unit": "s"}


def test_sequence_hears_a_verdict_given_earlier_and_fails_on_a_failed_one() -> None:
    ev = Evaluator(plan(sequence([{"action": "drive"}, {"assertion": "reached"}]), REACHED))
    ev.start(T0)
    assert [r.name for r in ev.feed(pose(2.0, 4.0, 0.0))] == ["reached"]
    ev.action("drive", "started", T0 + 3.0)
    result = one(ev.action("drive", "finished", T0 + 5.0, "ok"))
    assert result.status == PASSED
    assert result.evidence["steps"][1]["detail"] == "passed at 2.0 s"
    ev = Evaluator(plan(sequence([{"assertion": "reached"}]), {**REACHED, "within_s": 4}))
    ev.start(T0)
    results = ev.tick(T0 + 4.0)
    assert {(r.name, r.status) for r in results} == {("reached", FAILED), ("ordered", FAILED)}
    assert results[0].reason == (
        "assertion reached failed: 0 of 1 step(s) happened, waiting for assertion reached"
    )


def test_sequence_deadlines_gaps_and_settling() -> None:
    ev = evaluator(sequence([{"action": "drive"}, {"action": "park"}], within_s=10))
    ev.action("drive", "finished", T0 + 2.0, "ok")
    result = one(ev.tick(T0 + 10.0))
    assert result.status == FAILED
    assert result.reason == (
        "not completed within 10 s: 1 of 2 step(s) happened, waiting for park finished"
    )
    ev = evaluator(sequence([{"action": "drive"}, {"action": "park"}], max_gap_s=3))
    ev.action("drive", "finished", T0 + 2.0, "ok")
    assert ev.tick(T0 + 4.0) == []
    result = one(ev.tick(T0 + 5.5))
    assert result.reason.startswith("the next step did not happen within 3 s")
    ev = evaluator(sequence([{"action": "drive"}]))
    result = one(ev.action("drive", "finished", T0 + 2.0, "failed"))
    assert (
        result.reason == "drive ended failed: 0 of 1 step(s) happened, waiting for drive finished"
    )
    ev = evaluator(
        sequence(
            [
                {"metric": "distance_to", "target": {"x": 0, "y": 0}, "condition": "value > 2"},
                {"action": "park"},
            ]
        )
    )
    ev.feed(pose(1.0, 3.0, 0.0))
    result = one(ev.finish(T0 + 5.0))
    assert result.status == FAILED
    assert result.reason == "the timeline ended: 1 of 2 step(s) happened, waiting for park finished"
    # A sequence waiting on an assertion settled at the end hears about it first.
    ev = Evaluator(
        plan(
            sequence([{"assertion": "safe"}]),
            {"name": "safe", "type": "no_collision", "vehicle": "ugv1"},
        )
    )
    ev.start(T0)
    results = ev.finish(T0 + 5.0)
    assert {(r.name, r.status) for r in results} == {("safe", PASSED), ("ordered", PASSED)}
    ev = Evaluator(plan(sequence([{"assertion": "reached"}]), REACHED))
    ev.start(T0)
    results = ev.finish(T0 + 5.0)
    assert {(r.name, r.status) for r in results} == {("reached", FAILED), ("ordered", FAILED)}


def test_sequence_refusals() -> None:
    with pytest.raises(
        ScenarioError, match="a step is an action, a topic field, a metric or an assertion"
    ):
        scenario(sequence([{"action": "drive", "assertion": "x"}]))
    with pytest.raises(ScenarioError, match="a signal step needs a condition"):
        scenario(sequence([{"topic": "odom", "field": "x"}]))
    with pytest.raises(ScenarioError, match="goes with a topic or a metric"):
        scenario(sequence([{"action": "drive", "field": "x"}]))
    with pytest.raises(ScenarioError, match="'fly' is not a timeline entry"):
        scenario(sequence([{"action": "fly"}]))
    with pytest.raises(ScenarioError, match="is not another assertion"):
        plan(sequence([{"assertion": "nope"}]))
    with pytest.raises(ScenarioError, match="is not another assertion"):
        plan(sequence([{"assertion": "ordered"}]))
    step = Step(topic="odom", field="x", condition="value > 1")
    assert step.signal().topic == "odom"
    assert step.label(0) == "odom.x value > 1 (step 1)"


# ---------------------------------------------------------------------------- resource_usage
def footprint(**extra: Any) -> dict[str, Any]:
    return {
        "name": "footprint",
        "type": "resource_usage",
        "vehicle": "ugv1",
        "max_memory_mb": 1000,
        **extra,
    }


def test_resource_usage_memory_and_cpu_window() -> None:
    item = planned(footprint(container="bridge", max_cpu_pct=150, window_s=3))
    assert item.resolved == {"container": "fieldproof-x-bridge-ugv1"}
    ev = Evaluator([item])
    ev.start(T0)
    box = "fieldproof-x-bridge-ugv1"
    assert ev.feed(resource(1.0, "fieldproof-x-ugv1", 900.0, 5000.0)) == [], "another container"
    assert ev.feed(resource(1.0, box, 100.0, 500.0)) == []
    assert ev.feed(resource(2.0, box, 180.0, 600.0)) == [], "the window is not covered yet"
    result = one(ev.feed(resource(3.0, box, 190.0, 600.0)))
    assert result.status == FAILED
    assert result.reason == f"{box} averaged 157 % CPU over 3 s at 3.0 s, over 150 %"
    assert result.evidence["violation"]["cpu_mean_pct"] == pytest.approx(156.7, abs=0.1)
    assert result.metrics["peak_memory_mb"] == {"value": 600.0, "unit": "MiB"}
    ev = evaluator(footprint())
    ev.feed(resource(1.0, "fieldproof-x-ugv1", 100.0, 500.0))
    result = one(ev.feed(resource(2.0, "fieldproof-x-ugv1", 100.0, 1200.0)))
    assert result.status == FAILED
    assert result.reason == "fieldproof-x-ugv1 used 1200 MiB at 2.0 s, over 1000 MiB"


def test_resource_usage_passes_or_has_no_samples() -> None:
    ev = evaluator(footprint(max_cpu_pct=250))
    ev.feed(resource(1.0, "fieldproof-x-ugv1", 100.0, 500.0))
    ev.feed(resource(2.0, "fieldproof-x-ugv1", 120.0, 520.0))
    result = one(ev.finish(T0 + 5.0))
    assert result.status == PASSED
    assert result.reason == (
        "fieldproof-x-ugv1 peaked at 120 % CPU and 520 MiB over 2 sample(s) (limits: CPU 250 %, "
        "memory 1000 MiB)"
    )
    assert result.metrics["mean_cpu_pct"] == {"value": 110.0, "unit": "%"}
    ev = evaluator(footprint())
    result = one(ev.finish(T0 + 5.0))
    assert result.status == FAILED
    assert result.reason == "no resource sample of fieldproof-x-ugv1 during the timeline"
    with pytest.raises(ScenarioError, match="give max_cpu_pct or max_memory_mb"):
        scenario({"name": "f", "type": "resource_usage", "vehicle": "ugv1"})


# --------------------------------------------------------------------------- lifecycle_state
def managed(**extra: Any) -> dict[str, Any]:
    return {
        "name": "nav_active",
        "type": "lifecycle_state",
        "vehicle": "ugv2",
        "node": "bt_navigator",
        "state": "active",
        **extra,
    }


def test_lifecycle_state_eventually_and_always() -> None:
    item = planned(managed())
    assert item.resolved == {"node": "/ugv2/bt_navigator"}
    assert item.watch == (Watch("lifecycle", {"node": "/ugv2/bt_navigator"}),)
    ev = Evaluator([item])
    ev.start(T0)
    assert ev.feed(lifecycle(1.0, "/ugv2/bt_navigator", "inactive")) == []
    assert ev.feed(lifecycle(1.5, "/bt_navigator", "active")) == [], "another node"
    result = one(ev.feed(lifecycle(2.0, "/ugv2/bt_navigator", "active")))
    assert result.status == PASSED
    assert result.reason == "/ugv2/bt_navigator was active at 2.0 s"
    assert result.evidence["states"] == {"inactive": 1, "active": 1}
    assert result.metrics["reached_at_s"] == {"value": 2.0, "unit": "s"}
    ev = evaluator(managed(mode="always", after_s=1))
    assert ev.feed(lifecycle(0.5, "/ugv2/bt_navigator", "inactive")) == [], "before the window"
    ev.feed(lifecycle(2.0, "/ugv2/bt_navigator", "active"))
    result = one(ev.feed(lifecycle(3.0, "/ugv2/bt_navigator", "unavailable")))
    assert result.status == FAILED
    assert result.reason == "/ugv2/bt_navigator was unavailable at 3.0 s, not active"
    ev = evaluator(managed(mode="always", within_s=5))
    ev.feed(lifecycle(2.0, "/ugv2/bt_navigator", "active"))
    result = one(ev.tick(T0 + 5.0))
    assert result.status == PASSED
    assert result.reason == "/ugv2/bt_navigator was active in all 1 sample(s) from 0 s to 5 s"


def test_lifecycle_state_never_reached() -> None:
    ev = evaluator(managed(within_s=5))
    ev.feed(lifecycle(2.0, "/ugv2/bt_navigator", "inactive"))
    result = one(ev.tick(T0 + 5.0))
    assert result.status == FAILED
    assert (
        result.reason == "/ugv2/bt_navigator never reached active from 0 s to 5 s (last inactive)"
    )
    ev = evaluator(managed())
    result = one(ev.finish(T0 + 5.0))
    assert (
        result.reason == "no lifecycle state of /ugv2/bt_navigator was observed during the timeline"
    )


# ---------------------------------------------------------------------------------- temporal
SIGNALS = {
    "speed": {"topic": "odom", "field": "twist.twist.linear.x"},
    "lost": {"topic": "link_state", "field": "data"},
}
SPEED = {"speed": SIGNALS["speed"]}


def temporal(
    kind: str, condition: str = "not lost or abs(speed) < 0.05", **extra: Any
) -> dict[str, Any]:
    return {
        "name": kind,
        "type": kind,
        "vehicle": "ugv1",
        "signals": SIGNALS,
        "condition": condition,
        **extra,
    }


def test_always_over_two_signals() -> None:
    item = planned(temporal("always"))
    assert {n: s.topic for n, s in item.resolved["signals"].items()} == {
        "speed": "/odom",
        "lost": "/link_state",
    }
    assert [w.kind for w in item.watch] == ["value", "value"]
    ev = Evaluator([item])
    ev.start(T0)
    assert ev.feed(value(1.0, "/odom", "twist.twist.linear.x", 0.5)) == [], "lost unknown yet"
    assert ev.feed(value(1.5, "/link_state", "data", False)) == []
    assert ev.feed(value(1.8, "/odom", "twist.twist.linear.x", 0.0)) == []
    assert ev.feed(value(2.0, "/link_state", "data", True)) == []
    result = one(ev.feed(value(3.0, "/odom", "twist.twist.linear.x", 0.4)))
    assert result.status == FAILED
    assert result.reason == (
        "'not lost or abs(speed) < 0.05' did not hold at 3.0 s with speed = 0.4, lost = True"
    )
    assert result.evidence["violation"]["values"] == {"speed": 0.4, "lost": True}
    assert result.evidence["evaluations"] == 4
    assert result.metrics["evaluations"] == {"value": 4, "unit": ""}
    ev = evaluator(temporal("always"))
    ev.feed(value(1.0, "/odom", "twist.twist.linear.x", 0.5))
    ev.feed(value(1.5, "/link_state", "data", False))
    result = one(ev.finish(T0 + 5.0))
    assert result.status == PASSED
    assert result.reason == (
        "'not lost or abs(speed) < 0.05' held in all 1 evaluation(s) during the timeline"
    )


def test_never_and_eventually() -> None:
    ev = evaluator(temporal("never", "lost and speed > 0.1"))
    ev.feed(value(1.0, "/odom", "twist.twist.linear.x", 0.5))
    ev.feed(value(1.5, "/link_state", "data", False))
    result = one(ev.feed(value(2.0, "/link_state", "data", True)))
    assert result.status == FAILED
    assert result.reason == "'lost and speed > 0.1' held at 2.0 s with speed = 0.5, lost = True"
    ev = evaluator(temporal("eventually", "lost and speed < 0.05"))
    ev.feed(value(1.0, "/odom", "twist.twist.linear.x", 0.5))
    ev.feed(value(1.5, "/link_state", "data", True))
    result = one(ev.feed(value(2.0, "/odom", "twist.twist.linear.x", 0.0)))
    assert result.status == PASSED
    assert result.reason == "'lost and speed < 0.05' held at 2.0 s with speed = 0.0, lost = True"
    assert result.metrics["satisfied_at_s"] == {"value": 2.0, "unit": "s"}
    ev = evaluator(temporal("eventually", "speed < 0.05", for_s=2, signals=SPEED))
    ev.feed(value(1.0, "/odom", "twist.twist.linear.x", 0.0))
    assert ev.feed(value(2.0, "/odom", "twist.twist.linear.x", 0.0)) == []
    assert ev.feed(value(2.5, "/odom", "twist.twist.linear.x", 0.5)) == [], "the hold is broken"
    ev.feed(value(3.0, "/odom", "twist.twist.linear.x", 0.0))
    assert ev.tick(T0 + 4.0) == []
    result = one(ev.tick(T0 + 5.0))
    assert result.status == PASSED
    assert result.reason == "'speed < 0.05' held for 2.0 s by 5.0 s"
    assert result.evidence["held_since_t_s"] == 3.0
    assert result.metrics["longest_hold_s"] == {"value": 2.0, "unit": "s"}


def test_temporal_settling_errors_and_refusals() -> None:
    ev = evaluator(temporal("eventually", "speed > 9", within_s=4, signals=SPEED))
    ev.feed(value(1.0, "/odom", "twist.twist.linear.x", 0.5))
    result = one(ev.tick(T0 + 4.0))
    assert result.status == FAILED
    assert result.reason == "'speed > 9' never held from 0 s to 4 s (1 evaluation(s))"
    ev = evaluator(temporal("eventually", "speed > 9", for_s=3, signals=SPEED))
    ev.feed(value(1.0, "/odom", "twist.twist.linear.x", 10.0))
    result = one(ev.finish(T0 + 3.0))
    assert result.reason == (
        "'speed > 9' never held for 3 s during the timeline (longest 2.0 s) (1 evaluation(s))"
    )
    ev = evaluator(temporal("always"))
    ev.feed(value(1.0, "/odom", "twist.twist.linear.x", 0.5))
    result = one(ev.finish(T0 + 3.0))
    assert result.status == FAILED
    assert result.reason == "never evaluated during the timeline: no value of lost"
    ev = evaluator(temporal("always"))
    result = one(
        ev.feed(
            sample("value", 1.0, topic="/odom", field="twist.twist.linear.x", error="no field 'x'")
        )
    )
    assert result.status == FAILED
    assert result.reason == "speed (/odom.twist.twist.linear.x): no field 'x'"
    ev = evaluator(temporal("always", "speed + lost < 1"))
    ev.feed(value(1.0, "/odom", "twist.twist.linear.x", 0.5))
    result = one(ev.feed(value(1.5, "/link_state", "data", "down")))
    assert result.status == FAILED
    assert "cannot be evaluated" in result.reason
    with pytest.raises(ScenarioError, match="the signal\\(s\\) lost are not read"):
        scenario(temporal("always", "speed < 1"))
    with pytest.raises(ScenarioError, match="unknown name 'x'"):
        scenario(temporal("always", "x < 1"))
    with pytest.raises(ScenarioError, match="cannot name a signal"):
        scenario({**temporal("always", "abs < 1"), "signals": {"abs": SIGNALS["speed"]}})
    with pytest.raises(ScenarioError, match="give either topic"):
        scenario({**temporal("always", "a < 1"), "signals": {"a": {"field": "x"}}})
    # A metric signal of the vehicle's ground truth, with a target.
    item = planned(
        {
            "name": "near",
            "type": "eventually",
            "vehicle": "ugv1",
            "signals": {
                "d": {"metric": "distance_to", "target": {"frame": "goal"}},
                "t": {"metric": "sim_time"},
            },
            "condition": "d < 1 and t > 2",
        }
    )
    assert item.resolved["signals"]["d"].model == "ugv1"
    assert item.resolved["signals"]["d"].target == FRAMES["goal"]
    assert item.resolved["signals"]["d"].summary() == "ugv1 distance_to (10, -1)"
    assert item.watch == (Watch("model", {"model": "ugv1"}),)
    ev = Evaluator([item])
    ev.start(T0)
    ev.feed(sample("clock", 3.0, rtf=1.0))
    result = one(ev.feed(pose(3.5, 10.0, -0.5)))
    assert result.status == PASSED


# ------------------------------------------------------------------------------- the evaluator
def test_on_fail_abort_is_reported_by_the_evaluator() -> None:
    ev = evaluator(
        {**geofence(), "on_fail": "abort"},
        {"name": "safe", "type": "no_collision", "vehicle": "ugv1"},
    )
    assert [r.status for r in ev.results] == [PENDING, PENDING]
    ev.feed(pose(1.0, 9.0, 9.0))
    aborted = ev.abort
    assert aborted is not None
    assert aborted.name == "fenced"
    assert ev.skip_pending("aborted")[0].status == SKIPPED
    with pytest.raises(ScenarioError, match="on_fail"):
        scenario({**geofence(), "on_fail": "explode"})


def test_excerpt_is_bounded_and_on_every_verdict() -> None:
    ev = evaluator(geofence())
    for t in range(10):
        ev.feed(pose(float(t), 1.0, 0.0))
    result = one(ev.finish(T0 + 10.0))
    assert len(result.evidence["excerpt"]) == 6
    assert result.evidence["excerpt"][-1]["t_s"] == 9.0
    assert result.evidence["excerpt"][0] == {
        "t_s": 4.0,
        "sim_s": 4.0,
        "x": 1.0,
        "y": 0.0,
        "clearance_m": 4.0,
    }


def test_planned_to_dict_and_bookkeeping() -> None:
    item = planned(temporal("always"))
    data = item.to_dict()
    assert data["type"] == "always"
    assert data["resolved"]["signals"]["speed"] == {
        "topic": "/odom",
        "field": "twist.twist.linear.x",
    }
    ev = Evaluator([item])
    assert [r.status for r in ev.results] == [PENDING]
    assert ev.kpis() == {
        "always.evaluations": {"value": 0, "unit": "", "source": "always", "type": "always"}
    }
    assert ev.tick(T0) == []
    assert ev.action("drive", "started", T0) == []


def test_observe_plan_with_the_new_kinds() -> None:
    items = plan(
        fresh(),
        managed(),
        min_distance({"model": "crate"}),
        temporal("always"),
        {"name": "rate", "type": "topic_rate", "topic": "scan", "vehicle": "ugv1", "min_hz": 1},
    )
    plan_data = observe_plan(
        items,
        world="depot",
        models=["ugv1", "ugv2"],
        record=True,
        topics=["/scan"],
        run_dir="/fieldproof/run",
        watches=[Watch("value", {"topic": "/odom", "field": "twist.twist.linear.x", "type": None})],
    )
    assert plan_data["models"] == ["ugv1", "ugv2", "crate"]
    assert plan_data["latencies"] == [{"topic": "/scan", "window_s": 2.0, "type": None}]
    assert plan_data["lifecycles"] == [{"node": "/ugv2/bt_navigator"}]
    assert plan_data["values"] == [
        {"topic": "/odom", "field": "twist.twist.linear.x", "type": None},
        {"topic": "/link_state", "field": "data", "type": None},
    ]
    assert plan_data["rates"] == [{"topic": "/scan", "window_s": 5.0, "type": None}]
    assert plan_data["resources_file"] == "/fieldproof/run/resources.jsonl"
    assert plan_data["kpis_file"] == "/fieldproof/run/kpis.jsonl"
    assert plan_data["side_channels"]["resources"] == "/fieldproof/resources"
    assert "/fieldproof/kpis" in plan_data["record"]["topics"]
    with pytest.raises(ValueError, match="not a kind of observation"):
        Watch("pose", {})


def test_plan_context_refusals() -> None:
    context = PlanContext(WORLD, VEHICLES, actions=("drive",), where="assertions[0] (x)")
    with pytest.raises(
        ScenarioError, match=r"assertions\[0\] \(x\).vehicle: 'ugv9' is not a vehicle"
    ):
        context.vehicle("ugv9")
    with pytest.raises(ScenarioError, match="is not a timeline entry \\(drive\\)"):
        context.action("fly")
    assert context.topic(None, "/abs") == "/abs"
    assert context.topic("ugv2", "odom") == "/ugv2/odom"
    with pytest.raises(ScenarioError, match="is not a registered assertion type"):
        plan_assertion(scenario(geofence()).assertions[0], context, types=registry().__class__())
