"""Tests of the scenario schema v0 (task P1-T05): the example scenario, the errors, the world."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from fieldproof.scenario import (
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
    assert example.world.file == "depot.sdf"
    assert [v.name for v in example.vehicles] == ["ugv1"]
    assert example.vehicle("ugv1").spawn.frame == "spawn_dock"
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
    for key in ("time", "timeline", "assertions", "outputs"):
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
        ("timeline.0.action", "teleport", "action"),
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
    assert set(schema["required"]) == {"apiVersion", "kind", "metadata", "world", "vehicles"}
    assert schema["$defs"]["NavigateToPose"]["properties"]["action"]["const"] == "navigate_to_pose"
    assertion = schema["properties"]["assertions"]["items"]
    assert assertion["discriminator"]["propertyName"] == "type"
    assert {ref["$ref"].rsplit("/", 1)[1] for ref in assertion["oneOf"]} == {
        "ReachPose",
        "WithinTime",
        "TopicRate",
        "NoCollision",
        "TopicValue",
    }
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
