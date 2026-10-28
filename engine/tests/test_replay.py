"""Tests of the field log replay source (task P2-T07): the schema, the log inspection, the
plan, the player, the synthetic log, the localisation assertion, the observer's pose topics,
the run on a fake backend, the `fieldproof log` commands and the reports."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fakes import FakeBackend, FakeClock
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory
from test_observer import FakeClock as ObserverClock
from test_observer import FakeRos
from test_observer import config as observer_config
from test_runner import SUFFIX, T0, execute
from typer.testing import CliRunner

from fieldproof import player as player_module
from fieldproof.assertions import Evaluator, Sample, plan_assertion
from fieldproof.assertions import PlanContext as AssertionContext
from fieldproof.assertions.kpi import LocalisationKpi, collector_of, plan_kpi
from fieldproof.assertions.localisation import LocalisationError, LocalisationTracker
from fieldproof.cli import app
from fieldproof.config import Config, load_config
from fieldproof.html import render_html
from fieldproof.observer import Observer, Sink, pose_of
from fieldproof.player import (
    DONE,
    ERROR,
    PLAYING,
    READY,
    Player,
    Status,
    filter_transforms,
    frame_pairs,
    play_command,
    qos_overrides_text,
)
from fieldproof.replay import (
    GroundTruth,
    ReplaySource,
    ReplayTf,
    ReplayVehicle,
    inspect_log,
    plan_replay,
)
from fieldproof.report import build_report, render_markdown
from fieldproof.runner import NO_WORLD, Plan, plan
from fieldproof.runner import Status as RunStatus
from fieldproof.scenario import Scenario, ScenarioError, load_scenario, validate_data
from fieldproof.synthlog import Hall, ray_distance, write_synthetic_log
from fieldproof.world import Pose

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/scenarios/ugv_replay.yaml"
SIM_EXAMPLE = REPO / "examples/scenarios/ugv_nav_to_goal.yaml"
NS = 1_000_000_000
T0_NS = 1_700_000_000 * NS


# --------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def synth_log(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 15 s synthetic log: 10 s at rest, then the drive starts."""
    path = tmp_path_factory.mktemp("logs") / "ugv_dock_drive.mcap"
    write_synthetic_log(path, seconds=15.0, seed=3)
    return path


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> Config:
    fuel = tmp_path_factory.mktemp("fuel")
    for model in yaml.safe_load((REPO / "assets/fuel.lock").read_text())["models"]:
        (fuel / model["dir"]).mkdir()
    config = load_config(REPO / "fieldproof.toml", env={})
    config.stack.fuel = str(fuel)
    return config


def example_data(log: Path) -> dict[str, Any]:
    data = dict(yaml.safe_load(EXAMPLE.read_text()))
    data["source"]["log"] = str(log)
    return data


def replay_scenario(log: Path, mutate: Any = None) -> Scenario:
    """The replay example on `log`, as if loaded from the example's path."""
    data = example_data(log)
    if mutate is not None:
        mutate(data)
    return validate_data(data, EXAMPLE.resolve(), yaml.safe_dump(data))


@pytest.fixture
def resolved(synth_log: Path, config: Config, tmp_path: Path) -> Plan:
    return plan(replay_scenario(synth_log), config, runs_dir=tmp_path / "runs", token=SUFFIX)


# ------------------------------------------------------------------------------- the schema
def test_replay_models() -> None:
    vehicle = ReplayVehicle(
        topics={"/scan": "scan", "/odom": "odom"},
        tf=ReplayTf(topic="/tf", frames=["odom -> base_link", "/map->/odom"]),
        ground_truth=GroundTruth(topic="/truth"),
    )
    assert vehicle.log_topics() == ["/scan", "/odom", "/tf", "/truth"]
    assert vehicle.tf is not None
    assert vehicle.tf.pairs() == [("odom", "base_link"), ("/map", "/odom")]
    source = ReplaySource.model_validate(
        {"kind": "replay", "log": "x.mcap", "vehicles": {"ugv1": {"topics": {"/scan": "scan"}}}}
    )
    assert source.rate == 1.0
    assert source.lead_in_s == 20.0
    assert source.vehicles["ugv1"].topics == {"/scan": "scan"}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"topics": {"scan": "scan"}}, "a log topic is absolute"),
        ({"topics": {"/clock": "clock"}}, "/clock is never played"),
        ({"topics": {"/a": "scan", "/b": "scan"}}, "several log topics feed scan"),
        ({"topics": {"/scan": "scan"}, "tf": {"topic": "/scan"}}, "also under topics"),
        (
            {"topics": {"/scan": "scan"}, "ground_truth": {"topic": "/scan"}},
            "not an input of the vehicle",
        ),
        ({"topics": {}}, "gets nothing from the log"),
        ({"topics": {"/scan": "scan"}, "tf": {"frames": ["odom base_link"]}}, "parent -> child"),
        (
            {
                "topics": {"/scan": "scan"},
                "ground_truth": {"topic": "/t", "type": "std_msgs/msg/X"},
            },
            "not a pose type",
        ),
    ],
)
def test_replay_vehicle_errors(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ReplayVehicle.model_validate(data)


def test_one_vehicle_per_log_topic() -> None:
    with pytest.raises(ValueError, match="feeds both ugv1 and ugv2"):
        ReplaySource.model_validate(
            {
                "kind": "replay",
                "log": "x.mcap",
                "vehicles": {
                    "ugv1": {"topics": {"/scan": "scan"}},
                    "ugv2": {"topics": {"/scan": "scan"}},
                },
            }
        )


def test_example_loads_without_world_or_spawn() -> None:
    scenario = load_scenario(EXAMPLE)
    assert scenario.world is None
    assert scenario.source is not None
    assert scenario.source.kind == "replay"
    assert scenario.vehicles[0].spawn is None
    assert scenario.assertions[0].type == "localisation_error"
    assert {k.type for k in scenario.kpis} >= {"localisation", "latency", "resources"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("world"), "world: required"),
        (lambda d: d["vehicles"][0].pop("spawn"), r"vehicles\[ugv1\].spawn: required"),
    ],
)
def test_simulator_scenario_needs_world_and_spawn(mutate: Any, message: str) -> None:
    data = dict(yaml.safe_load(SIM_EXAMPLE.read_text()))
    mutate(data)
    with pytest.raises(ScenarioError, match=message):
        validate_data(data, SIM_EXAMPLE)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update({"network": {"profile": "wifi_good"}}), "network: radio links"),
        (lambda d: d["time"].update({"mode": "lockstep"}), "time.mode: lockstep steps"),
        (lambda d: d["time"].update({"rtf": 2.0}), "time.rtf: a replay run is paced"),
        (
            lambda d: (d["time"].update({"mode": "realtime"}), d["source"].update({"rate": 2.0})),
            "source.rate: the real-time-locked mode",
        ),
        (lambda d: d["outputs"].update({"video": True}), "outputs.video: a video"),
        (
            lambda d: d["source"]["vehicles"].update({"ugv9": {"topics": {"/x": "x"}}}),
            "'ugv9' is not a vehicle",
        ),
    ],
)
def test_replay_scenario_refusals(synth_log: Path, mutate: Any, message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        replay_scenario(synth_log, mutate)


def test_realtime_replay_at_rate_one_is_allowed(synth_log: Path) -> None:
    scenario = replay_scenario(synth_log, lambda d: d["time"].update({"mode": "realtime"}))
    assert scenario.time.mode == "realtime"


# ----------------------------------------------------------------------------- inspection
def test_inspect_mcap(synth_log: Path) -> None:
    info = inspect_log(synth_log)
    assert info.storage == "mcap"
    assert not info.directory
    assert info.start_ns == T0_NS
    assert info.duration_s == pytest.approx(15.0)
    assert info.topics == {
        "/scan": "sensor_msgs/msg/LaserScan",
        "/odom": "nav_msgs/msg/Odometry",
        "/tf": "tf2_msgs/msg/TFMessage",
        "/tf_static": "tf2_msgs/msg/TFMessage",
        "/ground_truth/pose": "geometry_msgs/msg/PoseStamped",
    }
    assert info.counts["/scan"] == 151
    assert info.counts["/tf_static"] == 1
    assert info.messages == sum(info.counts.values())
    data = info.to_dict()
    assert data["topics"]["/scan"] == {"type": "sensor_msgs/msg/LaserScan", "messages": 151}


def test_inspect_bag_directory(tmp_path: Path) -> None:
    bag = tmp_path / "field_2026_10_28"
    bag.mkdir()
    (bag / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "rosbag2_bagfile_information": {
                    "version": 9,
                    "storage_identifier": "sqlite3",
                    "starting_time": {"nanoseconds_since_epoch": 10 * NS},
                    "duration": {"nanoseconds": 30 * NS},
                    "message_count": 400,
                    "topics_with_message_count": [
                        {
                            "topic_metadata": {
                                "name": "/scan",
                                "type": "sensor_msgs/msg/LaserScan",
                            },
                            "message_count": 300,
                        },
                        {
                            "topic_metadata": {"name": "/odom", "type": "nav_msgs/msg/Odometry"},
                            "message_count": 100,
                        },
                    ],
                }
            }
        )
    )
    info = inspect_log(bag)
    assert info.directory
    assert info.storage == "sqlite3"
    assert info.start_ns == 10 * NS
    assert info.end_ns == 40 * NS
    assert info.topics == {"/scan": "sensor_msgs/msg/LaserScan", "/odom": "nav_msgs/msg/Odometry"}
    assert info.counts == {"/scan": 300, "/odom": 100}
    (bag / "metadata.yaml").write_text("rosbag2_bagfile_information: 3\n")
    with pytest.raises(ScenarioError, match="no rosbag2_bagfile_information"):
        inspect_log(bag)
    (bag / "metadata.yaml").write_text("rosbag2_bagfile_information: {starting_time: {}}\n")
    with pytest.raises(ScenarioError, match="cannot read the bag's metadata"):
        inspect_log(bag)


def test_inspect_errors(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="no such file"):
        inspect_log(tmp_path / "none.mcap")
    (tmp_path / "x.db3").write_bytes(b"")
    with pytest.raises(ScenarioError, match="give the bag directory"):
        inspect_log(tmp_path / "x.db3")
    (tmp_path / "x.txt").write_text("hello")
    with pytest.raises(ScenarioError, match="not an MCAP file"):
        inspect_log(tmp_path / "x.txt")
    (tmp_path / "bad.mcap").write_bytes(b"not an mcap")
    with pytest.raises(ScenarioError, match="cannot read the MCAP summary"):
        inspect_log(tmp_path / "bad.mcap")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ScenarioError, match="not a rosbag2 directory"):
        inspect_log(tmp_path / "empty")


# ------------------------------------------------------------------------------- the plan
def topics_of(namespace: str) -> Any:
    def resolve(topic: str) -> str:
        if topic.startswith("/"):
            return topic
        return f"/{namespace}/{topic}" if namespace else f"/{topic}"

    return resolve


def test_plan_replay_resolves_the_mapping(synth_log: Path) -> None:
    source = replay_scenario(synth_log).source
    assert source is not None
    replay = plan_replay(
        source,
        EXAMPLE.parent,
        {"ugv1": topics_of("ugv1")},
        tf_static_topics={"ugv1": "/ugv1/tf_static"},
    )
    assert replay.log == synth_log
    assert replay.t0_ns == T0_NS
    assert replay.play_topics == ("/scan", "/odom", "/tf_static", "/tf", "/ground_truth/pose")
    assert replay.remaps == {
        "/scan": "/ugv1/scan",
        "/odom": "/ugv1/odom",
        "/tf_static": "/ugv1/tf_static",
        "/tf": "/fieldproof/replay/ugv1/tf",
        "/ground_truth/pose": "/fieldproof/replay/ugv1/ground_truth",
    }
    assert replay.tf_filters == (
        {"from": "/fieldproof/replay/ugv1/tf", "to": "/ugv1/tf", "frames": [("odom", "base_link")]},
    )
    assert replay.qos == {
        "/tf_static": {
            "history": "keep_last",
            "depth": 100,
            "reliability": "reliable",
            "durability": "transient_local",
        }
    }
    assert replay.ground_truth == (
        {
            "model": "ugv1",
            "topic": "/fieldproof/replay/ugv1/ground_truth",
            "type": "geometry_msgs/msg/PoseStamped",
        },
    )
    assert replay.played_duration_s == pytest.approx(15.0)
    config = replay.player_config("/fieldproof/run")
    assert config["log"] == "/fieldproof/log/ugv_dock_drive.mcap"
    assert config["status_file"] == "/fieldproof/run/replay.jsonl"
    assert config["end_ns"] == T0_NS + 15 * NS
    assert config["topics"] == list(replay.play_topics)
    data = replay.to_dict()
    assert data["kind"] == "replay"
    assert data["log_duration_s"] == 15.0


def test_plan_replay_at_the_root_without_a_filter(synth_log: Path) -> None:
    source = ReplaySource.model_validate(
        {
            "kind": "replay",
            "log": str(synth_log),
            "start_s": 5,
            "duration_s": 4,
            "vehicles": {"ugv1": {"topics": {"/scan": "scan"}, "tf": {"topic": "/tf"}}},
        }
    )
    replay = plan_replay(source, EXAMPLE.parent, {"ugv1": topics_of("")})
    assert replay.remaps == {}
    assert replay.tf_filters == ()
    assert replay.t0_ns == T0_NS + 5 * NS
    assert replay.played_duration_s == 4.0
    command = play_command(replay.player_config("/run"), None)
    assert command[:5] == ["ros2", "bag", "play", "--input", str(replay.log_in_container)]
    assert "--start-offset" in command
    assert command[command.index("--start-offset") + 1] == "5"
    assert command[command.index("--playback-duration") + 1] == "4"
    assert "--remap" not in command


def test_plan_replay_duration_is_clipped_to_the_log(synth_log: Path) -> None:
    source = ReplaySource.model_validate(
        {
            "kind": "replay",
            "log": str(synth_log),
            "duration_s": 500,
            "vehicles": {"ugv1": {"topics": {"/scan": "scan"}}},
        }
    )
    replay = plan_replay(source, EXAMPLE.parent, {"ugv1": topics_of("")})
    assert replay.played_duration_s == pytest.approx(15.0)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda v: v["topics"].update({"/imu/data": "imu/data"}),
            "the log has no topic '/imu/data'",
        ),
        (lambda v: v["tf"].update({"topic": "/tf2"}), "tf.topic: the log has no topic '/tf2'"),
        (
            lambda v: v["ground_truth"].update({"topic": "/truth"}),
            "ground_truth.topic: the log has no topic '/truth'",
        ),
        (
            lambda v: v["ground_truth"].update({"type": "nav_msgs/msg/Odometry"}),
            "is a geometry_msgs/msg/PoseStamped, not a nav_msgs/msg/Odometry",
        ),
        (lambda v: v["ground_truth"].update({"topic": "/tf"}), "not a pose"),
        (
            lambda v: v["topics"].update({"/odom": "scan"}),
            "several log topics feed scan",
        ),
    ],
)
def test_plan_replay_errors(synth_log: Path, mutate: Any, message: str) -> None:
    data = example_data(synth_log)["source"]
    mutate(data["vehicles"]["ugv1"])
    with pytest.raises(ValueError, match=message):  # the schema's, or the plan's
        plan_replay(ReplaySource.model_validate(data), EXAMPLE.parent, {"ugv1": topics_of("")})


def test_plan_replay_vehicles_and_log(synth_log: Path, tmp_path: Path) -> None:
    data = example_data(synth_log)["source"]
    source = ReplaySource.model_validate(data)
    with pytest.raises(ScenarioError, match="'ugv1' is not a vehicle"):
        plan_replay(source, EXAMPLE.parent, {"ugv2": topics_of("")})
    with pytest.raises(ScenarioError, match="'ugv2' gets nothing from the log"):
        plan_replay(source, EXAMPLE.parent, {"ugv1": topics_of(""), "ugv2": topics_of("ugv2")})
    data["log"] = str(tmp_path / "none.mcap")
    with pytest.raises(ScenarioError, match="does not exist"):
        plan_replay(ReplaySource.model_validate(data), EXAMPLE.parent, {"ugv1": topics_of("")})
    data["log"] = str(synth_log)
    data["start_s"] = 20
    with pytest.raises(ScenarioError, match="past the end of the log"):
        plan_replay(ReplaySource.model_validate(data), EXAMPLE.parent, {"ugv1": topics_of("")})
    # The same output topic from two log topics (one of them through a remap).
    data["start_s"] = 0
    data["vehicles"]["ugv1"]["topics"]["/odom"] = "fieldproof/replay/ugv1/ground_truth"
    with pytest.raises(ScenarioError, match="published as"):
        plan_replay(ReplaySource.model_validate(data), EXAMPLE.parent, {"ugv1": topics_of("")})


def test_plan_of_the_replay_example(resolved: Plan, synth_log: Path) -> None:
    assert resolved.replay is not None
    assert resolved.sim is None
    assert resolved.source == "replay"
    assert resolved.pace == 1.0
    assert resolved.world is NO_WORLD
    player = resolved.player
    assert player is not None
    assert player.role == "player"
    assert player.image == resolved.recorder.image
    assert player.command == (
        "python3",
        "/opt/fieldproof/player.py",
        "--config",
        "/fieldproof/run/replay.json",
    )
    assert [m.target for m in player.mounts] == [
        "/opt/fieldproof/player.py",
        "/fieldproof/log/ugv_dock_drive.mcap",
        "/fieldproof/run",
    ]
    assert player.mounts[1].source == synth_log
    assert player.mounts[1].read_only
    assert not player.mounts[2].read_only
    assert player.user == resolved.recorder.user
    assert [s.role for s in resolved.containers()] == ["player", "recorder", "vehicle"]
    assert "--no-gazebo" in resolved.recorder.command
    observe = resolved.observe
    assert observe["world"] == ""
    assert observe["ground_truth"] == [
        {
            "model": "ugv1",
            "topic": "/fieldproof/replay/ugv1/ground_truth",
            "type": "geometry_msgs/msg/PoseStamped",
        }
    ]
    estimates = {e["topic"] for e in observe["estimates"]}
    assert estimates == {"/pose", "/odom"}
    assert observe["clock"]["source"] == "log"
    assert observe["clock"]["world"] == ""
    assert observe["clock"]["target_rtf"] == 1.0
    assert observe["models"] == ["ugv1"]
    vehicle = resolved.vehicles[0]
    assert not vehicle.has_spawn
    assert vehicle.spawn == Pose(0.0, 0.0, 0.0, 0.0)
    data = resolved.to_dict()
    assert data["world"] is None
    assert data["source"]["kind"] == "replay"
    assert "/scan" not in data["source"]["remaps"]
    assert data["source"]["remaps"]["/ground_truth/pose"] == "/fieldproof/replay/ugv1/ground_truth"
    assert data["vehicles"][0]["spawn"] is None
    localised = next(a for a in resolved.assertions if a.name == "localised")
    assert localised.topic == "/pose"
    assert localised.model == "ugv1"
    assert [w.kind for w in localised.watch] == ["model", "estimate"]


def test_plan_replay_with_a_world_and_a_spawn(
    synth_log: Path, config: Config, tmp_path: Path
) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["world"] = {"file": "depot.sdf"}
        d["vehicles"][0]["spawn"] = {"frame": "spawn_dock"}
        d["timeline"] = [
            {
                "name": "drive",
                "vehicle": "ugv1",
                "action": "navigate_to_pose",
                "goal": {"x": 10.0, "y": -1.0},
            }
        ]
        d["assertions"][0]["align"] = "spawn"

    resolved = plan(replay_scenario(synth_log, mutate), config, runs_dir=tmp_path, token=SUFFIX)
    assert resolved.world.name == "depot"
    assert resolved.vehicles[0].has_spawn
    assert resolved.vehicles[0].spawn.x == 10.0
    (drive,) = resolved.actions
    assert drive.goal_map is not None
    assert drive.goal_map.x == pytest.approx(4.0)
    assert resolved.to_dict()["world"]["name"] == "depot"
    assert resolved.sim is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: d["assertions"].append(
                {"name": "no_touch", "type": "no_collision", "vehicle": "ugv1"}
            ),
            "no_collision reads the simulator's contact sensor",
        ),
        (
            lambda d: d.update({"timeline": [{"action": "sim.pause", "at": 1, "duration_s": 2}]}),
            "sim.pause needs the simulator",
        ),
        (
            lambda d: d.update(
                {
                    "timeline": [
                        {
                            "action": "spawn_object",
                            "model": "crate",
                            "pose": {"x": 1.0, "y": 1.0},
                            "shape": "box",
                            "size": [0.5, 0.5, 0.5],
                        }
                    ]
                }
            ),
            "spawn_object needs the simulator's world",
        ),
        (
            lambda d: d.update({"timeline": [{"action": "set_wind", "velocity": [1, 0, 0]}]}),
            "set_wind needs the simulator's world",
        ),
        (
            lambda d: d.update(
                {"timeline": [{"action": "gps.deny", "vehicle": "ugv1", "duration_s": 5}]}
            ),
            "gps.deny needs the vehicle's bridge",
        ),
        (
            lambda d: d.update(
                {
                    "timeline": [
                        {
                            "action": "navigate_to_pose",
                            "vehicle": "ugv1",
                            "goal": {"x": 1.0, "y": 0.0},
                        }
                    ]
                }
            ),
            "give the vehicle a spawn",
        ),
        (
            lambda d: d["assertions"][0].update({"align": "spawn"}),
            "align: spawn needs the vehicle's spawn pose",
        ),
        (
            lambda d: next(k for k in d["kpis"] if k["type"] == "localisation").update(
                {"align": "spawn"}
            ),
            "align: spawn needs the vehicle's spawn pose",
        ),
    ],
)
def test_plan_refuses_what_a_replay_cannot_do(
    synth_log: Path, config: Config, tmp_path: Path, mutate: Any, message: str
) -> None:
    with pytest.raises(ScenarioError, match=message):
        plan(replay_scenario(synth_log, mutate), config, runs_dir=tmp_path, token=SUFFIX)


# --------------------------------------------------------------------------------- the run
TRUTH: list[dict[str, Any]] = [
    {"kind": "pose", "sim": 1700000030.0, "model": "ugv1", "x": 1.0, "y": 2.0, "yaw": 0.5},
    {"kind": "pose", "sim": 1700000031.0, "model": "ugv1", "x": 1.5, "y": 2.0, "yaw": 0.5},
    {"kind": "pose", "sim": 1700000032.0, "model": "ugv1", "x": 2.0, "y": 2.0, "yaw": 0.5},
]
"""Ground truth: the vehicle at (1, 2) heading 0.5 rad, moving 0.5 m/s along x."""


def estimates(offset: tuple[float, float, float], error: float = 0.0) -> list[dict[str, Any]]:
    """Estimates of the same motion in a frame placed at `offset` in the world, plus `error`."""
    ox, oy, oyaw = offset
    cos, sin = math.cos(oyaw), math.sin(oyaw)
    out = []
    for index, truth in enumerate(TRUTH):
        wx, wy = truth["x"] + (error if index == 1 else 0.0), truth["y"]
        dx, dy = wx - ox, wy - oy
        out.append(
            {
                "kind": "estimate",
                "sim": truth["sim"],
                "model": "ugv1",
                "topic": "/pose",
                "x": cos * dx + sin * dy,
                "y": -sin * dx + cos * dy,
                "yaw": truth["yaw"] - oyaw,
            }
        )
    return out


def run_samples(error: float = 0.0) -> list[dict[str, Any]]:
    """The stream the fake observer writes 60 s into the run: truth and estimates interleaved."""
    stamped = []
    for truth, estimate in zip(TRUTH, estimates((3.0, -1.0, 1.2), error), strict=True):
        stamped.append({**truth, "wall_offset_s": 60.0})
        stamped.append({**estimate, "wall_offset_s": 60.1})
    stamped.append({"kind": "clock", "sim": 1700000033.0, "rtf": 1.0, "wall_offset_s": 60.2})
    return stamped


def fake_backend(resolved: Plan, samples: list[dict[str, Any]]) -> FakeBackend:
    """A backend whose observer streams `samples` while the goal runs (inside the timeline)."""
    backend = FakeBackend()
    backend.images = {s.image for s in resolved.containers()}
    backend.samples_on_goal = list(samples)
    return backend


def reduced(synth_log: Path, config: Config, tmp_path: Path, **assertion: Any) -> Plan:
    """The example with the localisation assertion alone, and a goal for the fakes to stream on.

    The vehicle gets a spawn at the origin so the goal resolves; the stack never moves for it
    in a replay (the goal is what the fake answers `SUCCEEDED` to while it writes the samples).
    """

    assertion.setdefault("min_samples", 3)

    def mutate(d: dict[str, Any]) -> None:
        d["assertions"] = [a for a in d["assertions"] if a["name"] == "localised"]
        d["assertions"][0].update(assertion)
        d["kpis"] = [k for k in d["kpis"] if k["type"] == "localisation"]
        d["vehicles"][0]["spawn"] = {"x": 0.0, "y": 0.0, "yaw": 0.0}
        d["timeline"] = [
            {
                "name": "drive",
                "vehicle": "ugv1",
                "action": "navigate_to_pose",
                "goal": {"x": 1.0, "y": 0.0},
            }
        ]

    return plan(
        replay_scenario(synth_log, mutate), config, runs_dir=tmp_path / "runs", token=SUFFIX
    )


def test_replay_run_passes(synth_log: Path, config: Config, tmp_path: Path) -> None:
    resolved = reduced(synth_log, config, tmp_path)
    backend = fake_backend(resolved, run_samples())
    clock = FakeClock()
    record, log = execute(resolved, backend, clock)
    assert record.status == RunStatus.PASSED, record.reason
    assert [p["name"] for p in record.phases] == [
        "preparing",
        "replay",
        "recorder",
        "vehicles",
        "running",
        "teardown",
        "finished",
    ]
    assert backend.started == [
        "fieldproof-abc123-player",
        "fieldproof-abc123-recorder",
        "fieldproof-abc123-ugv1",
    ]
    assert backend.jobs == []  # no spawn job: the player feeds the vehicle
    assert not any("service -l" in " ".join(a) for _n, a in backend.execs)  # no simulator polled
    run_dir = resolved.run_dir
    replay_config = json.loads((run_dir / "replay.json").read_text())
    assert replay_config["log"] == "/fieldproof/log/ugv_dock_drive.mcap"
    assert replay_config["remaps"]["/ground_truth/pose"] == "/fieldproof/replay/ugv1/ground_truth"
    events = [e["event"] for e in log.events]
    assert "source.replay" in events
    assert "replay.ready" in events
    assert "replay.playing" in events
    assert "replay.done" in events
    assert "sim.start" not in events
    assert "bridge.start" not in events
    assert record.source["kind"] == "replay"
    assert record.source["log"] == str(synth_log)
    assert record.source["played_s"] == 120.0
    assert record.stack["world"] is None
    verdicts = {a["name"]: a for a in record.assertions}
    localised = verdicts["localised"]
    assert localised["status"] == "passed", localised["reason"]
    assert localised["evidence"]["samples"] == 3
    assert localised["evidence"]["alignment"]["mode"] == "first"
    assert localised["evidence"]["alignment"]["x"] == pytest.approx(3.0, abs=1e-3)
    assert localised["evidence"]["alignment"]["yaw"] == pytest.approx(1.2, abs=1e-3)
    assert localised["metrics"]["max_error_m"]["value"] == pytest.approx(0.0, abs=1e-3)
    kpis = record.kpis
    assert kpis["slam.samples"]["value"] == 3
    assert "odom_drift.max_error_m" not in kpis  # no odometry estimates in the fake stream
    collectors = {c["name"]: c for c in record.kpi_collectors}
    assert collectors["odom_drift"]["status"] == "missing"
    assert collectors["slam"]["status"] == "measured"
    report = (run_dir / "report.md").read_text()
    assert "- Source: replay of" in report
    assert "played by 0.0 s" in report  # the fake's log ends as the timeline starts
    assert "## Assertions" in report
    html = (run_dir / "report.html").read_text()
    assert "Source: replay of" in html


def test_replay_run_fails_on_a_large_error(synth_log: Path, config: Config, tmp_path: Path) -> None:
    resolved = reduced(synth_log, config, tmp_path)
    backend = fake_backend(resolved, run_samples(error=0.8))
    record, _log = execute(resolved, backend, FakeClock())
    assert record.status == RunStatus.FAILED
    assert record.reason is not None
    assert record.reason.startswith("assertion localised failed: position error 0.80 m")
    localised = next(a for a in record.assertions if a["name"] == "localised")
    assert localised["evidence"]["violation"]["error_m"] == pytest.approx(0.8, abs=1e-3)


def test_replay_run_waits_for_the_log_to_be_played(
    synth_log: Path, config: Config, tmp_path: Path
) -> None:
    resolved = reduced(synth_log, config, tmp_path)
    backend = fake_backend(resolved, run_samples())
    backend.replay_on_start = [{"event": "ready", "after_s": 1.5}, {"event": "playing"}]
    backend.replay_on_goal = [{"event": "done", "exit_code": 0, "played_s": 15.0}]
    clock = FakeClock()
    record, log = execute(resolved, backend, clock)
    assert record.status == RunStatus.PASSED, record.reason
    assert record.source["played_s"] == 15.0
    assert record.source["done_t_s"] is not None
    assert "replay.done" in [e["event"] for e in log.events]
    assert "played by" in (resolved.run_dir / "report.md").read_text()


def test_replay_run_ends_when_the_player_fails(
    synth_log: Path, config: Config, tmp_path: Path
) -> None:
    resolved = reduced(synth_log, config, tmp_path)
    backend = fake_backend(resolved, [])
    backend.replay_on_start = [
        {"event": "ready"},
        {"event": "error", "message": "ros2 bag play exited (1) before the playback started"},
    ]
    record, _log = execute(resolved, backend, FakeClock())
    assert record.status == RunStatus.ERROR
    assert record.reason is not None
    assert "the replay player failed: ros2 bag play exited (1)" in record.reason
    assert backend.removed  # torn down


def test_replay_run_player_never_ready(synth_log: Path, config: Config, tmp_path: Path) -> None:
    resolved = reduced(synth_log, config, tmp_path)
    backend = fake_backend(resolved, [])
    backend.replay_on_start = [{"event": "started"}]
    record, _log = execute(resolved, backend, FakeClock())
    assert record.status == RunStatus.ERROR
    assert record.reason is not None
    assert "waiting for the replay player to be ready" in record.reason


def test_replay_run_player_exits(synth_log: Path, config: Config, tmp_path: Path) -> None:
    resolved = reduced(synth_log, config, tmp_path)
    backend = fake_backend(resolved, [])
    backend.replay_on_start = [{"event": "started"}]
    backend.exit_on_state["fieldproof-abc123-player"] = 3
    record, _log = execute(resolved, backend, FakeClock())
    assert record.status == RunStatus.ERROR
    assert record.reason is not None
    assert "player container fieldproof-abc123-player exited" in record.reason


# ------------------------------------------------------------------------------- the player
PLAYER_CONFIG: dict[str, Any] = {
    "log": "/fieldproof/log/drive.mcap",
    "storage": "mcap",
    "rate": 2.0,
    "start_s": 5.0,
    "duration_s": 30.0,
    "lead_in_s": 10.0,
    "t0_ns": T0_NS + 5 * NS,
    "end_ns": T0_NS + 35 * NS,
    "clock_hz": 100.0,
    "topics": ["/scan", "/tf", "/truth"],
    "remaps": {"/scan": "/ugv1/scan", "/tf": "/fieldproof/replay/ugv1/tf"},
    "tf_filters": [
        {"from": "/fieldproof/replay/ugv1/tf", "to": "/ugv1/tf", "frames": [["odom", "base_link"]]}
    ],
    "qos": {"/tf_static": {"durability": "transient_local", "depth": 100}},
}


def test_play_command_and_qos_file() -> None:
    command = play_command(PLAYER_CONFIG, "/run/qos.yaml")
    assert command == [
        "ros2",
        "bag",
        "play",
        "--input",
        "/fieldproof/log/drive.mcap",
        "mcap",
        "--start-paused",
        "--disable-keyboard-controls",
        "--log-level",
        "warn",
        "--rate",
        "2",
        "--start-offset",
        "5",
        "--playback-duration",
        "30",
        "--topics",
        "/scan",
        "/tf",
        "/truth",
        "--remap",
        "/scan:=/ugv1/scan",
        "/tf:=/fieldproof/replay/ugv1/tf",
        "--qos-profile-overrides-path",
        "/run/qos.yaml",
    ]
    assert play_command({"log": "/bag", "rate": 1.0}, None) == [
        "ros2",
        "bag",
        "play",
        "/bag",
        "--start-paused",
        "--disable-keyboard-controls",
        "--log-level",
        "warn",
    ]
    assert qos_overrides_text(PLAYER_CONFIG["qos"]) == (
        "/tf_static:\n  durability: transient_local\n  depth: 100\n"
    )


def transform(parent: str, child: str) -> Any:
    return SimpleNamespace(header=SimpleNamespace(frame_id=parent), child_frame_id=child)


def test_tf_filter() -> None:
    allowed = frame_pairs([["odom", "base_link"], ("/map", "odom")])
    assert allowed == {("odom", "base_link"), ("map", "odom")}
    kept = filter_transforms(
        [transform("odom", "base_link"), transform("map", "odom"), transform("base_link", "lidar")],
        {("odom", "base_link")},
    )
    assert [(t.header.frame_id, t.child_frame_id) for t in kept] == [("odom", "base_link")]
    assert filter_transforms([transform("/odom", "/base_link")], {("odom", "base_link")})


class FakeProcess:
    def __init__(self) -> None:
        self.exit_code: int | None = None
        self.signals: list[int] = []
        self.killed = False

    def poll(self) -> int | None:
        return self.exit_code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.exit_code = 130

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code or 0

    def kill(self) -> None:
        self.killed = True


class FakePlayerRos:
    def __init__(self) -> None:
        self.clocks: list[int] = []
        self.relays: list[tuple[str, str, set[tuple[str, str]]]] = []
        self.ready = False
        self.resumed = 0
        self.spins = 0
        self.shut_down = False

    def publish_clock(self, nanoseconds: int) -> None:
        self.clocks.append(nanoseconds)

    def relay_tf(self, source: str, target: str, allowed: set[tuple[str, str]]) -> None:
        self.relays.append((source, target, allowed))

    def resume_ready(self) -> bool:
        return self.ready

    def resume(self) -> None:
        self.resumed += 1

    def spin_once(self, timeout_s: float) -> None:
        self.spins += 1

    def shutdown(self) -> None:
        self.shut_down = True


class Ticking:
    def __init__(self) -> None:
        self.t = 50.0

    def __call__(self) -> float:
        return self.t


def make_player(tmp_path: Path) -> tuple[Player, FakePlayerRos, FakeProcess, Ticking, Status]:
    ros, process, clock = FakePlayerRos(), FakeProcess(), Ticking()
    status = Status(tmp_path / "replay.jsonl", wall=lambda: 1000.0 + clock.t)
    config = {**PLAYER_CONFIG, "qos_file": str(tmp_path / "qos.yaml")}
    spawned: list[list[str]] = []

    def spawn(argv: Any) -> FakeProcess:
        spawned.append(list(argv))
        return process

    player = Player(config, ros, status, spawn=spawn, clock=clock, ready_timeout_s=20.0)
    player.spawned = spawned  # type: ignore[attr-defined]
    return player, ros, process, clock, status


def test_player_runs_the_clock_and_the_playback(tmp_path: Path) -> None:
    player, ros, process, clock, status = make_player(tmp_path)
    player.start()
    assert (tmp_path / "qos.yaml").read_text().startswith("/tf_static:\n")
    assert player.spawned[0][:6] == [  # type: ignore[attr-defined]
        "ros2",
        "bag",
        "play",
        "--input",
        "/fieldproof/log/drive.mcap",
        "mcap",
    ]
    assert ros.relays == [("/fieldproof/replay/ugv1/tf", "/ugv1/tf", {("odom", "base_link")})]
    assert status.events[0]["event"] == "started"
    assert status.events[0]["t0_ns"] == T0_NS + 5 * NS
    # The clock starts lead_in_s before t0 and runs at the rate.
    assert player.sim_ns(50.0) == T0_NS + 5 * NS - 10 * NS
    assert player.sim_ns(51.0) == T0_NS + 5 * NS - 8 * NS
    player.tick()
    assert ros.clocks == [T0_NS - 5 * NS]
    assert player.state == "starting"
    clock.t = 50.005  # within the clock period: no second clock message
    player.tick()
    assert len(ros.clocks) == 1
    clock.t = 52.0
    ros.ready = True
    player.tick()
    assert player.state == READY
    assert status.events[-1]["event"] == "ready"
    assert status.events[-1]["after_s"] == 2.0
    assert ros.resumed == 0
    clock.t = 54.9  # the clock is just short of t0
    player.tick()
    assert player.state == READY
    clock.t = 55.0  # the clock reaches t0: the playback is resumed
    player.tick()
    assert player.state == PLAYING
    assert ros.resumed == 1
    assert status.events[-1]["event"] == "playing"
    assert status.events[-1]["late_s"] == 0.0
    assert status.events[-1]["sim"] == pytest.approx((T0_NS + 5 * NS) / 1e9)
    clock.t = 70.0
    process.exit_code = 0
    player.tick()
    assert player.state == DONE
    assert status.events[-1]["event"] == "done"
    assert status.events[-1]["exit_code"] == 0
    assert status.events[-1]["played_s"] == pytest.approx(30.0)
    clock.t = 71.0
    player.tick()  # the clock goes on after the playback
    assert ros.clocks[-1] == player.sim_ns(71.0)
    assert not player.failed
    player.stop()
    assert player.run() == 0  # start again on a new process? No: run() with stopping set ends.
    assert status.events[-1]["event"] == "stopped"
    assert ros.shut_down
    lines = [json.loads(line) for line in (tmp_path / "replay.jsonl").read_text().splitlines()]
    assert [line["event"] for line in lines][:3] == ["started", "ready", "playing"]


def test_player_reports_a_late_start_and_an_early_exit(tmp_path: Path) -> None:
    player, ros, process, clock, status = make_player(tmp_path)
    player.start()
    clock.t = 70.0  # the clock passed t0 before the player was ready
    ros.ready = True
    player.tick()
    assert player.state == PLAYING
    assert status.events[-1]["late_s"] == pytest.approx(30.0)
    player, ros, process, clock, status = make_player(tmp_path)
    player.start()
    process.exit_code = 2
    player.tick()
    assert player.state == ERROR
    assert player.failed
    assert status.events[-1]["message"] == "ros2 bag play exited (2) before the playback started"
    player.stop()
    assert player.run() == 1


def test_player_times_out_without_a_service(tmp_path: Path) -> None:
    player, _ros, _process, clock, status = make_player(tmp_path)
    player.start()
    clock.t = 71.0
    player.tick()
    assert player.state == ERROR
    assert "offered no /rosbag2_player/resume service within 20 s" in status.events[-1]["message"]


def test_player_stop_interrupts_the_process(tmp_path: Path) -> None:
    player, ros, process, clock, _status = make_player(tmp_path)
    player.start()
    ros.ready = True
    clock.t = 55.0
    player.tick()
    player.stop()
    assert player.run() == 0
    assert process.signals == [2]
    assert not process.killed


def test_player_main_reads_the_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = {**PLAYER_CONFIG, "status_file": str(tmp_path / "replay.jsonl")}
    (tmp_path / "replay.json").write_text(json.dumps(config))
    built: list[Any] = []

    class StubRos(FakePlayerRos):
        def __init__(self) -> None:
            super().__init__()
            built.append(self)

    def stub_player(config: Any, ros: Any, status: Any) -> Any:
        player = Player(config, ros, status, spawn=lambda _argv: FakeProcess())
        player.stop()
        return player

    monkeypatch.setattr(player_module, "RclpyRos", StubRos)
    monkeypatch.setattr(player_module, "Player", stub_player)
    assert player_module.main(["--config", str(tmp_path / "replay.json")]) == 0
    assert built
    lines = (tmp_path / "replay.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["event"] == "started"
    (tmp_path / "bad.json").write_text("[]")
    with pytest.raises(ValueError, match="not a JSON object"):
        player_module.load_config(tmp_path / "bad.json")


# ------------------------------------------------------------------------- the synthetic log
def test_synthetic_log_is_deterministic_and_shaped(tmp_path: Path) -> None:
    first = write_synthetic_log(
        tmp_path / "a.mcap", seconds=12.0, seed=5, start=(10.0, 3.0, -1.5708)
    )
    second = write_synthetic_log(
        tmp_path / "b.mcap", seconds=12.0, seed=5, start=(10.0, 3.0, -1.5708)
    )
    assert first.messages == second.messages
    assert (tmp_path / "a.mcap").read_bytes() == (tmp_path / "b.mcap").read_bytes()
    assert first.messages == {
        "/scan": 121,
        "/odom": 241,
        "/tf": 241,
        "/tf_static": 1,
        "/ground_truth/pose": 121,
    }
    assert first.to_dict()["start"] == {"x": 10.0, "y": 3.0, "yaw": -1.5708}
    with (tmp_path / "a.mcap").open("rb") as stream:
        reader = make_reader(stream, decoder_factories=[DecoderFactory()])
        scans = [d for _s, _c, _m, d in reader.iter_decoded_messages(topics=["/scan"])]
        truths = [
            d for _s, _c, _m, d in reader.iter_decoded_messages(topics=["/ground_truth/pose"])
        ]
        odoms = [d for _s, _c, _m, d in reader.iter_decoded_messages(topics=["/odom"])]
        statics = [d for _s, _c, _m, d in reader.iter_decoded_messages(topics=["/tf_static"])]
    scan = scans[0]
    assert scan.header.frame_id == "lidar_link"
    assert len(scan.ranges) == 720
    assert scan.intensities == []
    # The lidar sits 0.2 m ahead of base_link at the hall's origin: 10.2 m to the back wall,
    # 5 m to the right wall, 9.8 m to the front wall, 7 m to the left wall (1 cm of noise).
    assert scan.ranges[0] == pytest.approx(10.2, abs=0.05)
    assert scan.ranges[180] == pytest.approx(5.0, abs=0.05)
    assert scan.ranges[360] == pytest.approx(9.8, abs=0.05)
    assert scan.ranges[540] == pytest.approx(7.0, abs=0.05)
    # The ground truth starts at `start` and heads along the hall's x after the rest.
    assert truths[0].header.frame_id == "world"
    assert (truths[0].pose.position.x, truths[0].pose.position.y) == (10.0, 3.0)
    assert truths[-1].pose.position.x == pytest.approx(10.0, abs=0.05)
    assert truths[-1].pose.position.y < 2.5  # 2 s of driving at 0.5 m/s, facing -y in the world
    # The odometry starts at its own origin and moves a little further (the scale error).
    assert odoms[0].header.frame_id == "odom"
    assert odoms[0].child_frame_id == "base_link"
    assert odoms[-1].pose.pose.position.x > 0.9
    assert odoms[-1].pose.pose.position.x > abs(truths[-1].pose.position.y - 3.0)
    assert odoms[-1].twist.twist.linear.x == pytest.approx(0.51, abs=0.05)
    assert {t.child_frame_id for t in statics[0].transforms} == {
        "base_footprint",
        "lidar_link",
        "imu_link",
        "gps_link",
    }
    assert first.bytes > 100_000


def test_hall_segments_and_rays() -> None:
    hall = Hall(crates=())
    segments = hall.segments()
    assert len(segments) == 4
    assert ray_distance(0.0, 0.0, 1.0, 0.0, segments, 25.0) == pytest.approx(10.0)
    assert ray_distance(0.0, 0.0, 0.0, 1.0, segments, 25.0) == pytest.approx(7.0)
    assert math.isinf(ray_distance(0.0, 0.0, 1.0, 0.0, segments, 5.0))
    with pytest.raises(ValueError, match="positive"):
        write_synthetic_log(Path("/nonexistent/x.mcap"), seconds=0.0)


# --------------------------------------------------------------------------- localisation
def sample(**fields: Any) -> Sample:
    sim = fields.pop("sim", None)
    return Sample(str(fields["kind"]), 0.0, sim, fields)


def test_localisation_tracker_aligns_at_the_first_estimate() -> None:
    tracker = LocalisationTracker("ugv1", max_age_s=0.5)
    judged = []
    for truth, estimate in zip(TRUTH, estimates((3.0, -1.0, 1.2)), strict=True):
        judged += tracker.on_sample(sample(**truth), 1.0)
        judged += tracker.on_sample(sample(**estimate), 1.3)
    assert tracker.finish() == []  # every estimate had its truth at once
    assert len(judged) == 3
    assert tracker.samples == 3
    assert tracker.unmatched == 0
    for record in judged:
        assert record["error_m"] == pytest.approx(0.0, abs=1e-6)
        assert record["yaw_error_rad"] == pytest.approx(0.0, abs=1e-6)
    alignment = tracker.alignment()
    assert alignment is not None
    assert (alignment["x"], alignment["y"], alignment["yaw"]) == (3.0, -1.0, 1.2)
    metrics = tracker.metrics()
    assert metrics["samples"] == (3, "")
    assert metrics["mean_error_m"][0] == pytest.approx(0.0, abs=1e-6)
    assert tracker.percentile(0.95) == pytest.approx(0.0, abs=1e-6)


def test_localisation_tracker_interpolates_and_bounds_the_age() -> None:
    tracker = LocalisationTracker(
        "ugv1", align="spawn", spawn=Pose(0.0, 0.0, 0.0, 0.0), max_age_s=0.5
    )
    tracker.on_sample(sample(kind="pose", sim=10.0, model="ugv1", x=0.0, y=0.0, yaw=0.0), 0.0)
    tracker.on_sample(sample(kind="pose", sim=11.0, model="ugv1", x=1.0, y=0.0, yaw=0.0), 1.0)
    # Stamped halfway: the truth there is (0.5, 0) by interpolation; the estimate says (0.6, 0).
    judged = tracker.on_sample(
        sample(kind="estimate", sim=10.5, model="ugv1", x=0.6, y=0.0, yaw=0.0), 0.5
    )
    assert len(judged) == 1
    assert judged[0]["error_m"] == pytest.approx(0.1)
    # Truths too far apart for the age limit: an estimate between them is not judged.
    tracker.on_sample(sample(kind="pose", sim=14.0, model="ugv1", x=4.0, y=0.0, yaw=0.0), 4.0)
    assert (
        tracker.on_sample(sample(kind="estimate", sim=12.5, model="ugv1", x=2.5, y=0.0), 2.5) == []
    )
    assert tracker.unmatched == 1
    # Too far from any truth: not judged.
    tracker.on_sample(sample(kind="estimate", sim=20.0, model="ugv1", x=5.0, y=0.0, yaw=0.0), 10.0)
    assert tracker.finish() == []
    assert tracker.unmatched == 2
    # Another model's samples, and samples before the window, are ignored.
    assert tracker.on_sample(sample(kind="pose", sim=13.0, model="other", x=9.0, y=9.0), 3.0) == []
    assert (
        tracker.on_sample(sample(kind="estimate", sim=12.0, model="ugv1", x=2.0, y=0.0), -1.0) == []
    )
    tracker = LocalisationTracker("ugv1", after_s=5.0)
    assert (
        tracker.on_sample(sample(kind="estimate", sim=12.0, model="ugv1", x=2.0, y=0.0), 1.0) == []
    )
    assert tracker.metrics()["samples"] == (None, "")


def localisation_plan(**fields: Any) -> Any:
    spec = LocalisationError.model_validate(
        {
            "name": "loc",
            "type": "localisation_error",
            "vehicle": "ugv1",
            "estimate": {"topic": "pose"},
            "max_m": 0.5,
            **fields,
        }
    )
    vehicle = SimpleNamespace(
        name="ugv1",
        namespace="",
        spawn=Pose(0.0, 0.0, 0.0, 0.0),
        topic=lambda t: f"/{t}",
        has_spawn=True,
    )
    context = AssertionContext(NO_WORLD, {"ugv1": vehicle}, where="assertions[0] (loc)")
    return plan_assertion(spec, context)


def evaluate(planned: Any, stream: list[dict[str, Any]], *, end: float = 100.0) -> Any:
    evaluator = Evaluator([planned])
    evaluator.start(T0)
    for index, item in enumerate(stream):
        fields = dict(item)
        sim = fields.pop("sim", None)
        evaluator.feed(Sample(fields["kind"], T0 + 1.0 + index, sim, fields))
    evaluator.finish(T0 + end)
    return evaluator.results[0]


def test_localisation_assertion_verdicts() -> None:
    stream = []
    for truth, estimate in zip(TRUTH, estimates((3.0, -1.0, 1.2)), strict=True):
        stream += [truth, estimate]
    result = evaluate(localisation_plan(min_samples=3, p95_m=0.1, mean_m=0.1), stream)
    assert result.status == "passed", result.reason
    assert result.reason.startswith(
        "position error at most 0.00 m (mean 0.00 m, p95 0.00 m) over 3"
    )
    assert result.metrics["samples"]["value"] == 3
    result = evaluate(localisation_plan(min_samples=4), stream)
    assert result.status == "failed"
    assert result.reason.startswith("3 estimate(s) judged, 4 needed")
    result = evaluate(localisation_plan(), [])
    assert result.status == "failed"
    assert "no ground-truth pose was observed" in result.reason
    result = evaluate(localisation_plan(), TRUTH)
    assert "no estimate was observed" in result.reason
    off = []
    for truth, estimate in zip(TRUTH, estimates((3.0, -1.0, 1.2), error=0.3), strict=True):
        off += [truth, estimate]
    result = evaluate(localisation_plan(mean_m=0.05), off)
    assert result.status == "failed"
    assert result.reason.startswith("mean position error 0.10 m over 3")
    result = evaluate(localisation_plan(p95_m=0.2), off)
    assert result.status == "failed"
    assert result.reason.startswith("95th percentile of the position error 0.30 m")
    result = evaluate(localisation_plan(max_m=0.2), off)
    assert result.status == "failed"
    assert result.reason.startswith("position error 0.30 m at")
    assert result.evidence["violation"]["error_m"] == pytest.approx(0.3, abs=1e-3)
    assert result.evidence["worst"]["error_m"] == pytest.approx(0.3, abs=1e-3)
    # A heading limit.
    twisted = [dict(s) for s in off]
    twisted[3]["yaw"] += 0.4
    result = evaluate(localisation_plan(max_yaw_rad=0.2), twisted)
    assert result.status == "failed"
    assert result.reason.startswith("heading error 0.40 rad")
    # A window: the estimate outside it is not judged, the one inside fails at once.
    result = evaluate(localisation_plan(within_s=2.5, max_m=0.2), off)
    assert result.status == "passed"
    assert result.evidence["samples"] == 1
    result = evaluate(localisation_plan(within_s=4.5, max_m=0.2), off)
    assert result.status == "failed"
    assert result.evidence["samples"] == 2


def test_localisation_kpi_collector() -> None:
    spec = LocalisationKpi.model_validate(
        {"name": "slam", "type": "localisation", "vehicle": "ugv1", "estimate": {"topic": "pose"}}
    )
    vehicle = SimpleNamespace(
        name="ugv1",
        namespace="",
        spawn=Pose(0.0, 0.0, 0.0, 0.0),
        topic=lambda t: f"/{t}",
        has_spawn=True,
    )
    planned = plan_kpi(spec, AssertionContext(NO_WORLD, {"ugv1": vehicle}, where="kpis[0] (slam)"))
    assert planned.topic == "/pose"
    assert [w.kind for w in planned.watch] == ["model", "estimate"]
    collector = collector_of(planned)
    for truth, estimate in zip(TRUTH, estimates((3.0, -1.0, 1.2), error=0.3), strict=True):
        collector.on_sample(Sample("pose", 1.0, truth["sim"], truth), 1.0)
        collector.on_sample(Sample("estimate", 1.3, estimate["sim"], estimate), 1.3)
    collector.on_finish(10.0)
    assert collector.result.status == "measured"
    assert collector.result.reason == "3 estimate(s) judged against 3 ground-truth pose(s)"
    assert collector.result.metrics["max_error_m"]["value"] == pytest.approx(0.3, abs=1e-3)
    empty = collector_of(planned)
    empty.on_finish(10.0)
    assert empty.result.status == "missing"


# ------------------------------------------------------------------------------ the observer
def pose_message(
    stamp_s: float, x: float, y: float, yaw: float, *, covariance: bool = False
) -> Any:
    pose = SimpleNamespace(
        position=SimpleNamespace(x=x, y=y, z=0.0),
        orientation=SimpleNamespace(x=0.0, y=0.0, z=math.sin(yaw / 2), w=math.cos(yaw / 2)),
    )
    return SimpleNamespace(
        header=SimpleNamespace(
            stamp=SimpleNamespace(sec=int(stamp_s), nanosec=round((stamp_s % 1) * 1e9)),
            frame_id="world",
        ),
        pose=SimpleNamespace(pose=pose, covariance=[0.0] * 36) if covariance else pose,
    )


def test_pose_of() -> None:
    assert pose_of(pose_message(1.0, 1.0, 2.0, 0.5)) == pytest.approx((1.0, 2.0, 0.0, 0.5))
    assert pose_of(pose_message(1.0, 1.0, 2.0, 0.5, covariance=True)) == pytest.approx(
        (1.0, 2.0, 0.0, 0.5)
    )
    transform = SimpleNamespace(
        transform=SimpleNamespace(
            translation=SimpleNamespace(x=3.0, y=0.0, z=1.0),
            rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
        )
    )
    assert pose_of(transform) == pytest.approx((3.0, 0.0, 1.0, 0.0))
    assert pose_of(SimpleNamespace(data=1)) is None
    assert pose_of(SimpleNamespace(pose=SimpleNamespace(position=None, orientation=None))) is None
    assert pose_of(SimpleNamespace(pose=SimpleNamespace(position="x", orientation="y"))) is None


def test_observer_streams_ground_truth_and_estimates(tmp_path: Path) -> None:
    (tmp_path / "engine.log").write_text("")
    (tmp_path / "assertions.jsonl").write_text("")
    ros = FakeRos()
    ros.types = {"/pose": "geometry_msgs/msg/PoseWithCovarianceStamped"}
    clock = ObserverClock()
    watcher = Observer(
        observer_config(
            tmp_path,
            world="",
            contacts=[],
            rates=[],
            values=[],
            ground_truth=[
                {
                    "model": "ugv1",
                    "topic": "/fieldproof/replay/ugv1/ground_truth",
                    "type": "geometry_msgs/msg/PoseStamped",
                }
            ],
            estimates=[{"model": "ugv1", "topic": "/pose", "type": None}],
            clock={"world": "", "source": "log", "mode": "sim", "target_rtf": 2.0},
        ),
        Sink(tmp_path / "samples.jsonl", clock=clock),
        ros,
        None,
        clock=clock,
    )
    watcher.start()
    assert "/fieldproof/replay/ugv1/ground_truth" in ros.subscriptions
    assert (
        ros.subscriptions["/fieldproof/replay/ugv1/ground_truth"][0]
        == "geometry_msgs/msg/PoseStamped"
    )
    assert "/pose" in ros.subscriptions  # discovered from the graph
    ros.deliver("/fieldproof/replay/ugv1/ground_truth", pose_message(1700000010.5, 1.0, 2.0, 0.5))
    ros.deliver("/fieldproof/replay/ugv1/ground_truth", pose_message(1700000010.55, 1.1, 2.0, 0.5))
    clock.t += 0.2
    ros.deliver("/pose", pose_message(1700000010.6, 0.9, 1.9, 0.4, covariance=True))
    ros.deliver("/pose", SimpleNamespace(data=3))
    ros.deliver("/pose", SimpleNamespace(data=4))
    ros.sim = 1700000011.0
    watcher.emit_metrics()
    lines = [json.loads(line) for line in (tmp_path / "samples.jsonl").read_text().splitlines()]
    kinds = [line["kind"] for line in lines]
    assert kinds.count("pose") == 1  # the second truth is within the pose period
    pose = next(line for line in lines if line["kind"] == "pose")
    assert pose["model"] == "ugv1"
    assert pose["sim"] == pytest.approx(1700000010.5)
    assert (pose["x"], pose["y"], pose["yaw"]) == (1.0, 2.0, 0.5)
    estimates_seen = [line for line in lines if line["kind"] == "estimate"]
    assert len(estimates_seen) == 2
    assert estimates_seen[0]["sim"] == pytest.approx(1700000010.6)
    assert estimates_seen[0]["x"] == 0.9
    assert estimates_seen[0]["topic"] == "/pose"
    assert estimates_seen[1]["error"] == "no pose in the message"  # reported once
    metrics = json.loads(ros.published["/fieldproof/metrics"][0])
    assert metrics["poses"]["ugv1"]["x"] == 1.1
    assert metrics["source"] == "log"
    assert metrics["paused"] is None
    clock_sample = next(line for line in lines if line["kind"] == "clock")
    assert clock_sample["source"] == "log"


# ------------------------------------------------------------------------------- the CLI
runner = CliRunner()


def test_log_info_command(synth_log: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["log", "info", str(synth_log)])
    assert result.exit_code == 0, result.output
    assert "mcap file, 15.0 s" in result.output
    assert "/ground_truth/pose" in result.output
    assert "sensor_msgs/msg/LaserScan" in result.output
    result = runner.invoke(app, ["log", "info", str(synth_log), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["storage"] == "mcap"
    assert data["topics"]["/scan"]["messages"] == 151
    result = runner.invoke(app, ["log", "info", str(tmp_path / "none.mcap")])
    assert result.exit_code == 2
    assert "no such file" in result.output


def test_log_synth_command(tmp_path: Path) -> None:
    out = tmp_path / "logs" / "short.mcap"
    result = runner.invoke(app, ["log", "synth", str(out), "--seconds", "3", "--seed", "1"])
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert "wrote" in result.output
    assert "/scan 31" in result.output
    result = runner.invoke(app, ["log", "synth", str(out), "--seconds", "3"])
    assert result.exit_code == 2
    assert "--force overwrites it" in result.output
    result = runner.invoke(
        app, ["log", "synth", str(out), "--seconds", "2", "--force", "--json", "--start-x", "4"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["start"]["x"] == 4.0
    assert data["messages"]["/scan"] == 21
    result = runner.invoke(app, ["log", "synth", str(tmp_path / "x.db3")])
    assert result.exit_code == 2
    result = runner.invoke(app, ["log", "synth", str(tmp_path / "y.mcap"), "--seconds", "0"])
    assert result.exit_code == 2


def test_validate_and_dry_run_the_replay_example(synth_log: Path, tmp_path: Path) -> None:
    override = f"source.log={synth_log}"
    result = runner.invoke(app, ["validate", str(EXAMPLE), "--set", override])
    assert result.exit_code == 0, result.output
    assert (
        "source replay of ugv_dock_drive.mcap (15 s, 5 topic(s), 1x), world none" in result.output
    )
    assert "note source replay: no simulator" in result.output
    result = runner.invoke(
        app,
        [
            "run",
            str(EXAMPLE),
            "--dry-run",
            "--runs-dir",
            str(tmp_path),
            "--render",
            "software",
            "--set",
            override,
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["source"]["kind"] == "replay"
    assert data["world"] is None
    assert [c["role"] for c in data["containers"]] == ["player", "recorder", "vehicle"]


def test_clock_command_refuses_a_replay_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "r1", "stack": {"world": None}, "source": {"kind": "replay"}})
    )
    result = runner.invoke(app, ["clock", str(run_dir), "status"])
    assert result.exit_code == 2
    assert "a replay run has no simulator clock" in result.output


def test_reports_carry_the_source() -> None:
    record: dict[str, Any] = {
        "run_id": "r1",
        "status": "passed",
        "scenario": {"name": "ugv_replay", "path": "x.yaml", "digest": "abc"},
        "source": {
            "kind": "replay",
            "log": "/logs/drive.mcap",
            "rate": 2.0,
            "start_s": 0.0,
            "duration_s": 120.0,
            "lead_in_s": 20.0,
            "topics": ["/scan", "/odom"],
            "playing_t_s": 3.5,
        },
        "assertions": [],
        "phases": [],
        "timeline": [],
    }
    report = build_report(record, Path("/nonexistent"))
    markdown = render_markdown(report)
    assert (
        "- Source: replay of `/logs/drive.mcap` at 2.00x: 120.0 s of the log from 0.0 s, 2 topic(s)"
        in markdown
    )
    assert "playing from 3.5 s on the timeline, not played to its end" in markdown
    record["source"].update({"playing_t_s": -4.2, "done_t_s": 115.8})
    before = render_markdown(build_report(record, Path("/nonexistent")))
    assert "playing from 4.2 s before the timeline, played by 115.8 s" in before
    assert "Source: replay of" in render_html(report)
    record["source"] = {"kind": "sim"}
    assert "Source:" not in render_markdown(build_report(record, Path("/nonexistent")))
