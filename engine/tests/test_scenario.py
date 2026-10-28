"""Tests of the scenario schema v0 (task P1-T05): the example scenario, the errors, the world."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from fieldproof.assertions.temporal import Eventually
from fieldproof.scenario import (
    ASSERTION_TYPES,
    KPI_TYPES,
    NavigateToPose,
    NoCollision,
    Placement,
    Pose,
    ReachPose,
    Scenario,
    ScenarioError,
    TopicRate,
    TopicValue,
    VideoOutput,
    WithinTime,
    entry_label,
    json_schema,
    load_scenario,
    parse_duration,
    place,
    read_world,
    validate_data,
)
from fieldproof.world import parse_stats

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/scenarios/ugv_nav_to_goal.yaml"
DEPOT = REPO / "sim/fieldproof_worlds/worlds/depot.sdf"
SCHEMA_FILE = REPO / "engine/schemas/scenario.schema.json"


@pytest.fixture(scope="module")
def example() -> Scenario:
    return load_scenario(EXAMPLE)


@pytest.fixture
def data() -> dict[str, Any]:
    return dict(yaml.safe_load(EXAMPLE.read_text()))


# -------------------------------------------------------------------------------- durations
@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("90s", 90.0),
        ("1m30s", 90.0),
        ("1h", 3600.0),
        ("0.5s", 0.5),
        ("2m", 120.0),
        ("1h 2m 3.5s", 3723.5),
        (5, 5.0),
        (2.5, 2.5),
    ],
)
def test_parse_duration(value: Any, seconds: float) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["abc", "", "5", "10 s", True, None, [1]])
def test_parse_duration_rejects(value: Any) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(value)


# ------------------------------------------------------------------------------ the example
def test_example_loads(example: Scenario) -> None:
    assert example.name == "ugv_nav_to_goal"
    assert example.kind == "Scenario"
    assert example.path == EXAMPLE.resolve()
    assert example.directory == EXAMPLE.parent.resolve()
    assert len(example.digest) == 8
    assert int(example.digest, 16) >= 0
    assert example.world is not None
    assert example.world.file == "depot.sdf"
    assert example.source is None
    assert [v.name for v in example.vehicles] == ["ugv1"]
    spawn = example.vehicle("ugv1").spawn
    assert spawn is not None
    assert spawn.frame == "spawn_dock"
    assert example.time.mode == "sim"
    assert example.time.timeout_s == 600.0
    drive = example.timeline[0]
    assert isinstance(drive, NavigateToPose)
    assert drive.action == "navigate_to_pose"
    assert drive.timeout_s == 300.0
    assert drive.goal.x == 10.0
    assert [a.type for a in example.assertions] == [
        "reach_pose",
        "within_time",
        "topic_rate",
        "no_collision",
        "topic_value",
        "geofence",
        "path_efficiency",
        "mission_time",
        "message_latency",
        "resource_usage",
        "lifecycle_state",
        "sequence",
        "eventually",
    ]
    assert [k.type for k in example.kpis] == [
        "path",
        "mission_time",
        "latency",
        "resources",
        "value",
    ]
    reached = example.assertions[0]
    assert isinstance(reached, ReachPose)
    assert reached.vehicle == "ugv1"
    assert reached.target.x == 10.0
    assert reached.tolerance_m == 0.5
    assert reached.within_s is None
    in_time = example.assertions[1]
    assert isinstance(in_time, WithinTime)
    assert in_time.action == "drive_ahead"
    assert in_time.max_s == 240.0
    lidar = example.assertions[2]
    assert isinstance(lidar, TopicRate)
    assert (lidar.topic, lidar.vehicle, lidar.min_hz, lidar.window_s, lidar.after_s) == (
        "scan",
        "ugv1",
        2.0,
        5.0,
        5.0,
    )
    assert lidar.max_hz is None
    safe = example.assertions[3]
    assert isinstance(safe, NoCollision)
    assert safe.topic is None
    assert safe.ignore == []
    speed = example.assertions[4]
    assert isinstance(speed, TopicValue)
    assert (speed.topic, speed.field, speed.condition, speed.mode) == (
        "cmd_vel",
        "linear.x",
        "value <= 1.5",
        "always",
    )
    assert example.outputs.logs is True
    assert example.outputs.record is True
    assert "/clock" in example.outputs.topics
    assert example.outputs.video is None, "the demo turns it on with --video"


def test_video_output_forms(data: dict[str, Any]) -> None:
    data["outputs"]["video"] = False
    assert validate_data(data).outputs.video is None
    data["outputs"]["video"] = True
    assert validate_data(data).outputs.video == VideoOutput()
    data["outputs"]["video"] = {"camera": "roof"}
    video = validate_data(data).outputs.video
    assert isinstance(video, VideoOutput)
    assert video.camera == "roof"
    del data["outputs"]["video"]
    assert validate_data(data).outputs.video is None
    data["outputs"]["video"] = {"fps": 30}
    with pytest.raises(ScenarioError, match=r"outputs\.video\.VideoOutput\.fps"):
        validate_data(data)
    data["outputs"]["video"] = {"format": "avi"}
    with pytest.raises(ScenarioError, match=r"outputs\.video\.VideoOutput\.format"):
        validate_data(data)


def test_defaults_without_optional_sections(data: dict[str, Any]) -> None:
    for key in ("time", "timeline", "assertions", "kpis", "outputs"):
        del data[key]
    scenario = validate_data(data)
    assert scenario.time.mode == "sim"
    assert scenario.time.rtf is None
    assert scenario.timeline == []
    assert scenario.assertions == []
    assert scenario.outputs.directory is None
    assert scenario.digest == ""
    with pytest.raises(ScenarioError, match="not loaded from a file"):
        _ = scenario.directory


# ---------------------------------------------------------------------------------- errors
def set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    target: Any = data
    keys = dotted.split(".")
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    last = keys[-1]
    if isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value


@pytest.mark.parametrize(
    ("dotted", "value", "message"),
    [
        ("apiVersion", "fieldproof.dev/v1", "apiVersion"),
        ("kind", "Robot", "kind"),
        ("metadata.name", "Bad Name", "metadata.name"),
        ("extra", 1, "extra"),
        ("world.name", "depot", "world.name"),
        ("vehicles.0.namespace", "Ugv-1", "namespace"),
        ("vehicles.0.spawn", {"frame": "spawn_dock", "x": 1.0}, "either frame or x"),
        ("vehicles.0.spawn", {"x": 1.0}, "at least x and y"),
        ("vehicles.0.spawn", {"frame": "spawn_dock", "pose": {}}, "pose"),
        ("vehicles", [], "vehicles"),
        ("time.mode", "wall", "time.mode"),
        ("time.timeout_s", "-5s", "timeout_s"),
        ("time.timeout_s", "soon", "duration"),
        ("time.rtf", 0, "rtf"),
        ("timeline.0.vehicle", "ugv2", r"timeline\[0\].vehicle: 'ugv2' is not a vehicle"),
        ("timeline.0.action", "levitate", "action"),
        ("timeline.0.at", -1, "at"),
        ("assertions.0.type", "reach_goal", "assertions.0: .*'reach_goal'"),
        ("assertions.0.name", "Reached", "assertions.0.reach_pose.name"),
        ("assertions.0.tolerance_m", 0, "tolerance_m"),
        ("assertions.0.target", {"frame": "x", "x": 1.0}, "either frame or x"),
        ("assertions.0.vehicle", "ugv9", r"assertions\[0\] \(reached_goal\).vehicle: 'ugv9'"),
        ("assertions.0.within_s", "soon", "duration"),
        ("assertions.0.extra", 1, "extra"),
        ("assertions.1.action", "fly", r"assertions\[1\] \(arrived_in_time\).action: 'fly'"),
        ("assertions.1.max_s", -1, "max_s"),
        ("assertions.2.min_hz", -1, "min_hz"),
        ("assertions.2.max_hz", 1, "max_hz must not be below min_hz"),
        ("assertions.2.vehicle", "ugv9", "is not a vehicle"),
        ("assertions.3.ignore", "wall", "ignore"),
        ("assertions.4.condition", "speed < 1", "unknown name 'speed'"),
        ("assertions.4.condition", "import os", "not an expression"),
        ("assertions.4.mode", "sometimes", "mode"),
        ("assertions.4.within_s", 0, "within_s must be later than after_s"),
        ("outputs.format", "md", "outputs.format"),
    ],
)
def test_schema_errors(data: dict[str, Any], dotted: str, value: Any, message: str) -> None:
    set_path(data, dotted, value)
    with pytest.raises(ScenarioError, match=message):
        validate_data(data, EXAMPLE)


def test_duplicate_vehicles_and_assertions(data: dict[str, Any]) -> None:
    data["vehicles"].append(dict(data["vehicles"][0]))
    with pytest.raises(ScenarioError, match="names must be unique"):
        validate_data(data)
    data["vehicles"][1] = {**data["vehicles"][1], "name": "ugv2"}
    with pytest.raises(ScenarioError, match="namespaces must be unique"):
        validate_data(data)
    data["vehicles"][1]["namespace"] = "ugv2"
    validate_data(data)
    data["assertions"].append(dict(data["assertions"][0]))
    with pytest.raises(ScenarioError, match="assertions: names must be unique"):
        validate_data(data)


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="No such file"):
        load_scenario(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: [\n")
    with pytest.raises(ScenarioError, match="not valid YAML"):
        load_scenario(bad)
    bad.write_text("- a list\n")
    with pytest.raises(ScenarioError, match="a YAML mapping"):
        load_scenario(bad)
    bad.write_text("apiVersion: fieldproof.dev/v0alpha1\nkind: Scenario\n")
    with pytest.raises(ScenarioError, match=r"metadata: Field required"):
        load_scenario(bad)


def test_error_lines_name_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: Scenario\n")
    with pytest.raises(ScenarioError) as excinfo:
        load_scenario(bad)
    assert str(excinfo.value).startswith(str(bad))
    assert "apiVersion" in str(excinfo.value)


# ------------------------------------------------------------------------------- the schema
def test_json_schema_is_committed_and_current() -> None:
    schema = json_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["title"] == "Fieldproof scenario v0"
    assert set(schema["required"]) == {"apiVersion", "kind", "metadata", "vehicles"}
    assert schema["$defs"]["NavigateToPose"]["properties"]["action"]["const"] == "navigate_to_pose"
    assertion = schema["properties"]["assertions"]["items"]
    assert assertion["discriminator"]["propertyName"] == "type"
    registered = {ref["$ref"].rsplit("/", 1)[1] for ref in assertion["oneOf"]}
    assert registered >= {"ReachPose", "WithinTime", "TopicRate", "NoCollision", "TopicValue"}
    assert registered >= {"Geofence", "MinDistance", "PathEfficiency", "MissionTime", "Sequence"}
    assert registered >= {"MessageLatency", "ResourceUsage", "LifecycleState"}
    assert registered >= {"Always", "Eventually", "Never"}
    assert set(assertion["discriminator"]["mapping"]) == set(ASSERTION_TYPES)
    kpi = schema["properties"]["kpis"]["items"]
    assert set(kpi["discriminator"]["mapping"]) == set(KPI_TYPES)
    for name in ("ReachPose", "TopicValue", "Vehicle"):
        assert schema["$defs"][name]["additionalProperties"] is False
    assert schema["$defs"]["TopicValue"]["properties"]["mode"]["enum"] == ["always", "eventually"]
    assert SCHEMA_FILE.is_file(), "run `make scenario-schema`"
    assert json.loads(SCHEMA_FILE.read_text()) == schema, "stale: run `make scenario-schema`"


# -------------------------------------------------------------------------------- the world
def test_entry_label(example: Scenario) -> None:
    assert entry_label(example.timeline[0]) == "drive_ahead"
    unnamed = example.timeline[0].model_copy(update={"name": None, "at": 30.0})
    assert entry_label(unnamed) == "navigate_to_pose@30s"


def test_within_time_needs_a_named_entry(data: dict[str, Any]) -> None:
    del data["timeline"][0]["name"]
    data["assertions"] = data["assertions"][:5]  # the primitives; the library names the entry too
    del data["kpis"]
    with pytest.raises(ScenarioError, match=r"is not a timeline entry \(navigate_to_pose@0s\)"):
        validate_data(data)
    data["assertions"][1]["action"] = "navigate_to_pose@0s"
    validate_data(data)
    data["timeline"] = []
    with pytest.raises(ScenarioError, match=r"is not a timeline entry \(none\)"):
        validate_data(data)


def test_read_depot() -> None:
    world = read_world(DEPOT)
    assert world.cameras == {"spectator": "/spectator/record_video"}, (
        "the depot's spectator camera carries the CameraVideoRecorder plugin"
    )
    assert world.name == "depot"
    assert world.file == DEPOT
    assert world.frames["spawn_dock"] == Pose(10.0, 3.0, 0.0, -1.5708)
    assert world.frames["goal_plateau"].z == 0.8
    assert {"spawn_hall", "goal_office", "goal_workshop", "goal_aisle", "goal_gate"} <= set(
        world.frames
    )


def test_read_world_errors(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="cannot read"):
        read_world(tmp_path / "missing.sdf")
    bad = tmp_path / "bad.sdf"
    bad.write_text("<sdf version='1.11'><model name='x'/></sdf>")
    with pytest.raises(ScenarioError, match="no <world name"):
        read_world(bad)
    bad.write_text("<sdf><world name='w'><frame name='a'><pose>1 2</pose></frame></world></sdf>")
    assert read_world(bad).frames == {}


def test_place() -> None:
    world = read_world(DEPOT)
    assert place(Placement(frame="spawn_dock"), world, "x") == world.frames["spawn_dock"]
    assert place(Placement(x=1.0, y=2.0), world, "x") == Pose(1.0, 2.0, 0.0, 0.0)
    assert place(Placement(x=1.0, y=2.0, z=0.5, yaw=1.0), world, "x") == Pose(1.0, 2.0, 0.5, 1.0)
    with pytest.raises(ScenarioError, match="has no frame 'spawn_moon' \\(goal_aisle"):
        place(Placement(frame="spawn_moon"), world, "vehicles[ugv1].spawn")


def test_pose_relative() -> None:
    spawn = Pose(10.0, 3.0, 0.0, -math.pi / 2)
    ahead = spawn.relative(Pose(10.0, -1.0, 0.0, -math.pi / 2))
    assert ahead.x == pytest.approx(4.0)
    assert ahead.y == pytest.approx(0.0)
    assert ahead.yaw == pytest.approx(0.0)
    left = spawn.relative(Pose(14.0, 3.0, 0.5, math.pi))
    assert left.x == pytest.approx(0.0)
    assert left.y == pytest.approx(4.0)
    assert left.z == pytest.approx(0.5)
    assert left.yaw == pytest.approx(-math.pi / 2)
    assert Pose(0, 0, 0, 3.0).relative(Pose(0, 0, 0, -3.0)).yaw == pytest.approx(2 * math.pi - 6.0)


def test_parse_stats_reads_a_running_and_a_paused_world() -> None:
    running = (
        "sim_time {\n  sec: 73\n  nsec: 179000000\n}\n"
        "real_time {\n  sec: 75\n  nsec: 485178424\n}\n"
        "iterations: 73179\nreal_time_factor: 0.99996200144394509\n"
        "step_size {\n  nsec: 1000000\n}\n"
    )
    stats = parse_stats(running)
    assert stats["paused"] is False
    assert stats["sim_time_s"] == pytest.approx(73.179)
    assert stats["real_time_s"] == pytest.approx(75.485178)
    assert stats["iterations"] == 73179
    assert stats["rtf"] == pytest.approx(0.999962)
    paused = (
        "sim_time {\n  sec: 2\n}\nreal_time {\n  sec: 2\n  nsec: 1591186\n}\npaused: true\n"
        "iterations: 2000\nstep_size {\n}\n"
    )
    stats = parse_stats(paused)
    assert stats["paused"] is True
    assert stats["sim_time_s"] == 2.0
    assert stats["rtf"] == 0.0
    assert parse_stats("")["iterations"] == 0


# ---------------------------------------------------------------- the v1 assertions (P2-T05)
def test_v1_assertions_validate_in_a_scenario() -> None:
    data = {
        "apiVersion": "fieldproof.dev/v0alpha1",
        "kind": "Scenario",
        "metadata": {"name": "v1"},
        "world": {"file": "depot.sdf"},
        "vehicles": [
            {"name": "ugv1", "manifest": "r.yaml", "spawn": {"x": 0, "y": 0}},
            {"name": "ugv2", "manifest": "r.yaml", "namespace": "ugv2", "spawn": {"x": 1, "y": 0}},
        ],
        "timeline": [
            {
                "name": "drive",
                "vehicle": "ugv1",
                "action": "navigate_to_pose",
                "goal": {"x": 1, "y": 0},
            }
        ],
        "assertions": [
            {
                "name": "fenced",
                "type": "geofence",
                "vehicle": "ugv1",
                "region": {"polygon": [[0, 0], [9, 0], [9, 9]]},
            },
            {
                "name": "apart",
                "type": "min_distance",
                "vehicle": "ugv1",
                "to": {"vehicle": "ugv2"},
                "min_m": 1,
            },
            {
                "name": "straight",
                "type": "path_efficiency",
                "vehicle": "ugv1",
                "action": "drive",
                "min_ratio": 0.7,
            },
            {"name": "quick", "type": "mission_time", "max_s": "2m", "end": "drive"},
            {
                "name": "fresh",
                "type": "message_latency",
                "topic": "scan",
                "vehicle": "ugv1",
                "max_ms": 200,
            },
            {
                "name": "ordered",
                "type": "sequence",
                "steps": [{"action": "drive", "event": "started"}, {"assertion": "fenced"}],
            },
            {
                "name": "fits",
                "type": "resource_usage",
                "vehicle": "ugv1",
                "max_cpu_pct": 200,
                "on_fail": "abort",
            },
            {
                "name": "nav",
                "type": "lifecycle_state",
                "vehicle": "ugv1",
                "node": "bt_navigator",
                "state": "active",
            },
            {
                "name": "calm",
                "type": "always",
                "signals": {
                    "v": {"topic": "odom", "field": "twist.twist.linear.x", "vehicle": "ugv1"}
                },
                "condition": "v < 2",
            },
            {
                "name": "stops",
                "type": "eventually",
                "vehicle": "ugv1",
                "signals": {"v": {"topic": "odom", "field": "twist.twist.linear.x"}},
                "condition": "v == 0",
                "for_s": "2s",
            },
            {
                "name": "safe",
                "type": "never",
                "vehicle": "ugv1",
                "signals": {"d": {"metric": "distance_to", "target": {"x": 5, "y": 5}}},
                "condition": "d < 0.5",
            },
        ],
        "kpis": [
            {"name": "drive", "type": "path", "vehicle": "ugv1", "action": "drive"},
            {"name": "age", "type": "latency", "topic": "scan", "vehicle": "ugv1"},
        ],
    }
    loaded = validate_data(data)
    assert [a.type for a in loaded.assertions] == [
        "geofence",
        "min_distance",
        "path_efficiency",
        "mission_time",
        "message_latency",
        "sequence",
        "resource_usage",
        "lifecycle_state",
        "always",
        "eventually",
        "never",
    ]
    assert [k.type for k in loaded.kpis] == ["path", "latency"]
    assert loaded.assertions[6].on_fail == "abort"
    assert loaded.assertions[0].on_fail == "continue"
    assert loaded.assertions[1].vehicle_names() == ["ugv1", "ugv2"]
    assert loaded.assertions[3].action_names() == ["drive"]
    assert loaded.assertions[5].action_names() == ["drive"]
    stops = loaded.assertions[9]
    assert isinstance(stops, Eventually)
    assert stops.for_s == 2.0
    with pytest.raises(ScenarioError, match="Input tag 'nope' found using 'type'"):
        validate_data({**data, "assertions": [{"name": "x", "type": "nope"}]})
    with pytest.raises(ScenarioError, match=r"assertions\[0\] \(s\).vehicle: 'ugv9'"):
        validate_data(
            {
                **data,
                "assertions": [
                    {
                        "name": "s",
                        "type": "sequence",
                        "steps": [
                            {"topic": "x", "field": "f", "condition": "value", "vehicle": "ugv9"}
                        ],
                    }
                ],
            }
        )
    with pytest.raises(ScenarioError, match="Extra inputs are not permitted"):
        validate_data(
            {
                **data,
                "assertions": [
                    {
                        "name": "x",
                        "type": "geofence",
                        "vehicle": "ugv1",
                        "region": {"circle": {"x": 0, "y": 0, "radius_m": 1}},
                        "tolerance_m": 1,
                    }
                ],
            }
        )
