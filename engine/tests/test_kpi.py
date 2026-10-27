"""Tests of the KPI collectors (task P2-T05): each type on a synthetic stream, the run's KPI
map, the scenario's `kpis` section and its refusals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fieldproof.assertions import (
    MEASURED,
    MISSING,
    Evaluator,
    PlanContext,
    PlannedAssertion,
    Sample,
    Watch,
)
from fieldproof.assertions.kpi import KPI_TYPES, KPIS, collector_of, kpi_watches, plan_kpi
from fieldproof.dsl import ScenarioError
from fieldproof.scenario import Scenario, validate_data
from fieldproof.world import Pose, WorldInfo

T0 = 1_700_000_000.0


class Vehicle:
    def __init__(self, name: str, namespace: str = "") -> None:
        self.name = name
        self.namespace = namespace
        self.container_name = f"fieldproof-x-{name}"
        self.bridge_name = f"fieldproof-x-bridge-{name}"
        self.spawn = Pose(0.0, 0.0, 0.0, 0.0)

    def topic(self, name: str) -> str:
        if name.startswith("/"):
            return name
        return f"/{self.namespace}/{name}" if self.namespace else f"/{name}"

    def bundle(self) -> Any:
        raise NotImplementedError


VEHICLES: dict[str, Any] = {"ugv1": Vehicle("ugv1"), "ugv2": Vehicle("ugv2", "ugv2")}
WORLD = WorldInfo(Path("depot.sdf"), "depot", {"goal": Pose(4.0, 0.0, 0.0, 0.0)})


def scenario(*kpis: dict[str, Any], assertions: list[dict[str, Any]] | None = None) -> Scenario:
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
            ],
            "assertions": assertions or [],
            "kpis": list(kpis),
        }
    )


def plan(*kpis: dict[str, Any]) -> list[PlannedAssertion]:
    loaded = scenario(*kpis)
    return [
        plan_kpi(k, PlanContext(WORLD, VEHICLES, actions=("drive",), where=f"kpis[{i}] ({k.name})"))
        for i, k in enumerate(loaded.kpis)
    ]


def collectors(*kpis: dict[str, Any]) -> Evaluator:
    ev = Evaluator([], [collector_of(p) for p in plan(*kpis)])
    ev.start(T0)
    return ev


def sample(kind: str, t: float, **data: Any) -> Sample:
    sim = data.pop("sim", t)
    return Sample.from_line(json.dumps({"kind": kind, "wall": T0 + t, "sim": sim, **data}))


def pose(t: float, x: float, y: float, model: str = "ugv1") -> Sample:
    return sample("pose", t, model=model, x=x, y=y, z=0.0, yaw=0.0)


def test_kpi_types_are_listed() -> None:
    assert set(KPIS) == set(KPI_TYPES)
    for kind, kpi_type in KPIS.items():
        assert kpi_type.type == kind
        assert kpi_type.summary
        assert kpi_type.metrics


def test_path_collector() -> None:
    ev = collectors(
        {
            "name": "drive",
            "type": "path",
            "vehicle": "ugv1",
            "action": "drive",
            "target": {"frame": "goal"},
        }
    )
    ev.action("drive", "started", T0 + 1.0)
    ev.feed(pose(1.0, 0.0, 0.0))
    ev.feed(pose(2.0, 0.0, 3.0))
    ev.feed(pose(3.0, 4.0, 3.0))
    ev.action("drive", "finished", T0 + 4.0, "ok")
    assert ev.finish(T0 + 5.0) == [], "a collector's result is not a verdict"
    (result,) = ev.kpi_results
    assert result.status == MEASURED
    assert result.type == "path"
    assert result.reason == "7.00 m over 3 pose(s)"
    assert ev.kpis() == {
        "drive.path_m": {"value": 7.0, "unit": "m", "source": "drive", "type": "path"},
        "drive.straight_m": {"value": 4.0, "unit": "m", "source": "drive", "type": "path"},
        "drive.efficiency": {
            "value": 0.571,
            "unit": "",
            "source": "drive",
            "type": "path",
        },
        "drive.max_speed_mps": {"value": 4.0, "unit": "m/s", "source": "drive", "type": "path"},
        "drive.mean_speed_mps": {
            "value": 2.333,
            "unit": "m/s",
            "source": "drive",
            "type": "path",
        },
    }
    assert result.metrics["path_m"] == {"value": 7.0, "unit": "m"}


def test_mission_time_and_distance_collectors() -> None:
    ev = collectors(
        {"name": "whole", "type": "mission_time"},
        {"name": "leg", "type": "mission_time", "start": "drive", "end": "drive"},
        {"name": "gap", "type": "distance", "vehicle": "ugv1", "to": {"vehicle": "ugv2"}},
        {
            "name": "to_goal",
            "type": "distance",
            "vehicle": "ugv1",
            "to": {"target": {"frame": "goal"}},
        },
    )
    ev.action("drive", "started", T0 + 2.0)
    ev.feed(pose(2.0, 0.0, 0.0))
    ev.feed(pose(2.0, 3.0, 0.0, model="ugv2"))
    ev.feed(pose(3.0, 1.0, 0.0))
    ev.action("drive", "finished", T0 + 5.0, "ok")
    ev.finish(T0 + 9.0)
    results = ev.kpi_results
    assert [r.status for r in results] == [MEASURED] * 4
    kpis = ev.kpis()
    assert kpis["whole.duration_s"]["value"] == 9.0
    assert kpis["leg.duration_s"]["value"] == 3.0
    assert kpis["gap.closest_m"]["value"] == 2.0
    assert kpis["gap.closest_at_s"]["value"] == 3.0
    assert kpis["gap.final_m"]["value"] == 2.0
    assert kpis["to_goal.closest_m"]["value"] == 3.0
    assert results[1].evidence["end_status"] == "ok"


def test_rate_latency_resources_and_value_collectors() -> None:
    items = plan(
        {"name": "scan", "type": "rate", "topic": "scan", "vehicle": "ugv1", "after_s": 1},
        {"name": "scan_age", "type": "latency", "topic": "scan", "vehicle": "ugv2"},
        {"name": "stack", "type": "resources", "vehicle": "ugv1", "container": "bridge"},
        {
            "name": "speed",
            "type": "value",
            "topic": "cmd_vel",
            "vehicle": "ugv1",
            "field": "linear.x",
            "count_if": "value > 0.3",
        },
    )
    assert [kpi_watches(i) for i in items] == [
        [Watch("rate", {"topic": "/scan", "window_s": 5.0, "type": None})],
        [Watch("latency", {"topic": "/ugv2/scan", "window_s": 2.0, "type": None})],
        [],
        [Watch("value", {"topic": "/cmd_vel", "field": "linear.x", "type": None})],
    ]
    assert items[2].resolved == {"container": "fieldproof-x-bridge-ugv1"}
    ev = Evaluator([], [collector_of(i) for i in items])
    ev.start(T0)
    ev.feed(sample("rate", 0.5, topic="/scan", hz=12.0, window_s=5.0, total=5))
    ev.feed(sample("rate", 2.0, topic="/scan", hz=10.0, window_s=5.0, total=20))
    ev.feed(sample("rate", 3.0, topic="/scan", hz=8.0, window_s=5.0, total=28))
    ev.feed(
        sample(
            "latency",
            2.0,
            topic="/ugv2/scan",
            p95_ms=30.0,
            max_ms=40.0,
            mean_ms=20.0,
            count=10,
            total=10,
        )
    )
    ev.feed(
        sample(
            "latency",
            3.0,
            topic="/ugv2/scan",
            p95_ms=50.0,
            max_ms=90.0,
            mean_ms=30.0,
            count=10,
            total=20,
        )
    )
    ev.feed(
        sample(
            "resource",
            1.0,
            sim=None,
            container="fieldproof-x-bridge-ugv1",
            cpu_pct=50.0,
            memory_mb=100.0,
        )
    )
    ev.feed(
        sample(
            "resource",
            2.0,
            sim=None,
            container="fieldproof-x-bridge-ugv1",
            cpu_pct=70.0,
            memory_mb=120.0,
        )
    )
    for t, v in enumerate([0.1, 0.5, 0.4, "x"], start=1):
        ev.feed(sample("value", float(t), topic="/cmd_vel", field="linear.x", value=v))
    ev.finish(T0 + 5.0)
    results = ev.kpi_results
    assert [r.status for r in results] == [MEASURED] * 4
    kpis = ev.kpis()
    assert kpis["scan.mean_hz"]["value"] == 9.0
    assert kpis["scan.lowest_hz"]["value"] == 8.0
    assert kpis["scan.messages"]["value"] == 28
    assert kpis["scan_age.p95_ms"]["value"] == 50.0
    assert kpis["scan_age.max_ms"]["value"] == 90.0
    assert kpis["scan_age.mean_ms"]["value"] == 25.0
    assert kpis["stack.peak_cpu_pct"]["value"] == 70.0
    assert kpis["stack.mean_memory_mb"]["value"] == 110.0
    assert kpis["speed.samples"]["value"] == 4
    assert kpis["speed.min"]["value"] == 0.1
    assert kpis["speed.max"]["value"] == 0.5
    assert kpis["speed.mean"]["value"] == 0.333
    assert kpis["speed.count"]["value"] == 2
    assert "speed.last" not in kpis, "the last value was not a number"
    assert "cannot be evaluated" in results[3].evidence["error"]


def test_missing_collectors_and_errors() -> None:
    ev = collectors(
        {"name": "drive", "type": "path", "vehicle": "ugv1"},
        {"name": "gap", "type": "distance", "vehicle": "ugv1", "to": {"model": "crate"}},
        {
            "name": "speed",
            "type": "value",
            "topic": "cmd_vel",
            "vehicle": "ugv1",
            "field": "linear.x",
        },
    )
    ev.feed(sample("value", 1.0, topic="/cmd_vel", field="linear.x", error="no field"))
    ev.finish(T0 + 5.0)
    results = ev.kpi_results
    assert [r.status for r in results] == [MISSING, MISSING, MEASURED]
    assert results[1].reason == "no distance measured"
    assert results[2].evidence["error"] == "no field"
    assert ev.kpis() == {
        "speed.samples": {"value": 0, "unit": "", "source": "speed", "type": "value"}
    }
    with pytest.raises(ScenarioError, match="kpis: names must be unique"):
        scenario({"name": "a", "type": "mission_time"}, {"name": "a", "type": "mission_time"})
    with pytest.raises(ScenarioError, match=r"kpis\[0\] \(a\).vehicle: 'ugv9' is not a vehicle"):
        scenario({"name": "a", "type": "path", "vehicle": "ugv9"})
    with pytest.raises(ScenarioError, match="'fly' is not a timeline entry"):
        scenario({"name": "a", "type": "mission_time", "end": "fly"})
    with pytest.raises(ScenarioError, match="condition: 'import os' is not an expression"):
        scenario(
            {
                "name": "a",
                "type": "value",
                "topic": "x",
                "vehicle": "ugv1",
                "field": "f",
                "count_if": "import os",
            }
        )
    context = PlanContext(WORLD, VEHICLES, where="kpis[0]")
    with pytest.raises(ScenarioError, match="needs a vehicle or a leading /"):
        plan_kpi(scenario({"name": "a", "type": "rate", "topic": "scan"}).kpis[0], context)
    bad = scenario({"name": "a", "type": "mission_time"}).kpis[0]
    object.__setattr__(bad, "type", "nope")
    with pytest.raises(ScenarioError, match="is not a KPI collector"):
        plan_kpi(bad, context)
