"""Tests of the scenario runner v0 (task P1-T05): the plan, the state machine on a fake backend."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes import FakeBackend, FakeClock, FakeContainer, v0_text
from rich.console import Console

from fieldproof.assertions import ASSERTIONS_FILE, OBSERVE_FILE, SAMPLES_FILE
from fieldproof.backend import (
    BackendError,
    ContainerSpec,
    ContainerState,
    ExecResult,
    ResourceUsage,
)
from fieldproof.config import Config, load_config
from fieldproof.report import REPORT_JSON, REPORT_MD
from fieldproof.resources import MIB
from fieldproof.runner import (
    DOMAIN_LABEL,
    EXIT_CODES,
    LOG_FILE,
    OBSERVER_FILE,
    OBSERVER_IN_CONTAINER,
    RECORD_FILE,
    ROLE_LABEL,
    RUN_DIR_IN_CONTAINER,
    RUN_LABEL,
    SCENARIO_COPY,
    VEHICLE_LABEL,
    Phase,
    Plan,
    Run,
    RunLog,
    Status,
    auto_domain_id,
    detect_render,
    goal_status,
    leftovers,
    make_run_id,
    plan,
)
from fieldproof.scenario import Scenario, ScenarioError, load_scenario, validate_data

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/scenarios/ugv_nav_to_goal.yaml"
UGV_MANIFEST = REPO / "robots/fieldproof_robots/models/ugv_ref/robot.yaml"
NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
SUFFIX = "abc123"


T0 = 1_700_000_000.0
# What the observer streams while the goal runs in the fake: the vehicle drives to the goal
# (4 m ahead of spawn_dock, (10, -1) in the world), the lidar runs at 10 Hz, Nav2 commands
# 0.4 m/s, nothing is touched.
GOOD_SAMPLES = [
    {"kind": "pose", "sim": 1.0, "model": "ugv1", "x": 10.0, "y": 3.0, "z": 0.1, "yaw": -1.57},
    {"kind": "pose", "sim": 5.0, "model": "ugv1", "x": 10.0, "y": 1.0, "z": 0.1, "yaw": -1.57},
    {"kind": "rate", "sim": 6.0, "topic": "/scan", "hz": 10.0, "window_s": 5.0, "total": 60},
    {"kind": "value", "sim": 6.0, "topic": "/cmd_vel", "field": "linear.x", "value": 0.4},
    {"kind": "pose", "sim": 9.0, "model": "ugv1", "x": 10.0, "y": -0.8, "z": 0.1, "yaw": -1.57},
    {"kind": "clock", "sim": 9.5, "rtf": 1.0},
]


def ready_backend(resolved: Plan, clock: FakeClock | None = None) -> FakeBackend:
    backend = FakeBackend()
    backend.images = {s.image for s in resolved.containers()} | {
        v.spawn_job.image for v in resolved.vehicles
    }
    backend.samples_on_goal = list(GOOD_SAMPLES)
    if clock is not None:
        backend.wall = lambda: T0 + (clock.now() - 1000.0)
    return backend


# --------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def example() -> Scenario:
    return scenario_variant(lambda _data: None)


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> Config:
    """The repository's configuration with the Fuel models "fetched" into a temporary directory."""
    fuel = tmp_path_factory.mktemp("fuel")
    for model in yaml.safe_load((REPO / "assets/fuel.lock").read_text())["models"]:
        (fuel / model["dir"]).mkdir()
    config = load_config(REPO / "fieldproof.toml", env={})
    config.stack.fuel = str(fuel)
    return config


@pytest.fixture
def resolved(example: Scenario, config: Config, tmp_path: Path) -> Plan:
    return plan(
        example, config, runs_dir=tmp_path / "runs", render="software", now=NOW, token=SUFFIX
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def filmed(config: Config, tmp_path: Path) -> Plan:
    """The example with `outputs.video: true`, as `fieldproof run --video` runs it."""
    scenario = scenario_variant(lambda d: d["outputs"].update({"video": True}))
    return plan(
        scenario, config, runs_dir=tmp_path / "runs", render="software", now=NOW, token=SUFFIX
    )


def scenario_variant(mutate: Callable[[dict[str, Any]], None]) -> Scenario:
    """The example with a change, as if loaded from the example's path."""
    text = v0_text(EXAMPLE.read_text())
    data = yaml.safe_load(text)
    mutate(data)
    return validate_data(data, EXAMPLE.resolve(), text)


class SyncExecutor:
    """Runs an action's command on the spot, so the fake clock stays deterministic."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass


def execute(
    resolved: Plan, backend: FakeBackend, clock: FakeClock, *, keep: bool = False
) -> tuple[Any, RunLog]:
    resolved.run_dir.parent.mkdir(parents=True, exist_ok=True)
    log = RunLog(
        resolved.run_id,
        file=resolved.run_dir / LOG_FILE,
        console=Console(file=(resolved.run_dir.parent / "console.txt").open("a")),
        clock=clock.now,
    )
    backend.clock = clock
    backend.wall = lambda: T0 + (clock.now() - 1000.0)
    run = Run(
        resolved,
        backend,
        log,
        keep=keep,
        sleep=clock.sleep,
        clock=clock.now,
        wall=lambda: T0 + (clock.now() - 1000.0),
        executor=SyncExecutor(),  # type: ignore[arg-type]
    )
    return run.execute(), log


# ------------------------------------------------------------------------------------ plan
def test_plan_resolves_the_example(resolved: Plan, config: Config, tmp_path: Path) -> None:
    assert re.fullmatch(r"[0-9a-f]{8}-20261017T120000Z-abc123", resolved.run_id)
    assert resolved.run_dir == tmp_path / "runs" / resolved.run_id
    assert resolved.world.name == "depot"
    assert resolved.network == "fieldproof"
    assert resolved.render == "software"
    assert resolved.domain_id == auto_domain_id(resolved.run_id), "derived from the run id"
    assert 1 <= resolved.domain_id <= 100
    assert resolved.domain_source == "auto"
    assert resolved.discovery is None
    assert resolved.discovery_mode == "multicast"
    sim = resolved.sim
    assert sim.name == "fieldproof-abc123-sim"
    assert sim.image == config.image("sim")
    assert sim.command == (
        "gz",
        "sim",
        "-s",
        "-r",
        "--headless-rendering",
        "--seed",
        str(resolved.seed),
        "depot.sdf",
    )
    assert resolved.seed == 0, "Gazebo's default seed: the example sets none"
    assert sim.env["FIELDPROOF_SEED"] == str(resolved.seed)
    assert sim.env["LIBGL_ALWAYS_SOFTWARE"] == "1"
    assert sim.env["GZ_PARTITION"] == "fieldproof"
    assert sim.devices == ()
    assert [m.target for m in sim.mounts] == [
        "/fieldproof/worlds",
        "/fieldproof/models",
        "/fieldproof/fuel",
        "/fieldproof/robots",
    ]
    assert all(m.read_only and m.source.is_absolute() for m in sim.mounts)
    assert sim.labels == {
        RUN_LABEL: resolved.run_id,
        DOMAIN_LABEL: str(resolved.domain_id),
        ROLE_LABEL: "sim",
    }
    assert sim.env["ROS_DOMAIN_ID"] == str(resolved.domain_id)
    assert "ROS_DISCOVERY_SERVER" not in sim.env
    (vehicle,) = resolved.vehicles
    assert vehicle.name == "ugv1"
    assert vehicle.manifest.name == "ugv_nav2"
    assert vehicle.spawn.x == 10.0
    assert vehicle.spawn.yaw == -1.5708
    assert vehicle.ready_timeout_s == 180.0
    job = vehicle.spawn_job
    assert job.name == "fieldproof-abc123-spawn-ugv1"
    assert job.image == config.image("bridge")
    assert job.command[:4] == ("ros2", "launch", "fieldproof_robots", "spawn_ugv_ref.launch.py")
    assert not any(a.startswith("namespace:=") for a in job.command), "empty: launch default"
    assert {"name:=ugv1", "world:=depot", "x:=10", "y:=3", "z:=0.12"} <= set(job.command)


def test_plan_resolves_the_recorder_and_assertions(
    resolved: Plan, config: Config, tmp_path: Path
) -> None:
    recorder = resolved.recorder
    assert recorder.name == "fieldproof-abc123-recorder"
    assert recorder.image == config.image("bridge")
    assert recorder.role == "recorder"
    assert recorder.command == (
        "python3",
        OBSERVER_IN_CONTAINER,
        "--config",
        f"{RUN_DIR_IN_CONTAINER}/{OBSERVE_FILE}",
    )
    assert recorder.user is not None
    assert recorder.user.count(":") == 1
    assert recorder.working_dir == RUN_DIR_IN_CONTAINER
    assert recorder.env["HOME"] == "/tmp"  # noqa: S108
    assert recorder.env["GZ_PARTITION"] == "fieldproof"
    assert [(m.source, m.target, m.read_only) for m in recorder.mounts] == [
        (OBSERVER_FILE, OBSERVER_IN_CONTAINER, True),
        (resolved.run_dir, RUN_DIR_IN_CONTAINER, False),
    ]
    assert OBSERVER_FILE.is_file()
    assert [s.role for s in resolved.containers()] == ["sim", "bridge", "recorder", "vehicle"]


def test_plan_resolves_the_assertions_and_the_observation(resolved: Plan) -> None:  # noqa: PLR0915
    recorder = resolved.recorder
    (vehicle,) = resolved.vehicles
    job = vehicle.spawn_job
    assert [a.name for a in resolved.assertions] == [
        "reached_goal",
        "arrived_in_time",
        "lidar_streams",
        "no_collision",
        "speed_limit",
    ]
    reached, _in_time, lidar, safe, speed = resolved.assertions
    assert reached.target is not None
    assert (reached.target.x, reached.target.y) == (10.0, -1.0)
    assert reached.model == "ugv1"
    assert lidar.topic == "/scan"
    assert safe.topic == "/contacts"
    assert safe.model == "ugv1"
    assert speed.topic == "/cmd_vel"
    observe = resolved.observe
    assert observe["world"] == "depot"
    assert observe["models"] == ["ugv1"]
    assert observe["contacts"] == [{"topic": "/contacts", "model": "ugv1"}]
    assert observe["rates"] == [{"topic": "/scan", "window_s": 5.0, "type": None}]
    assert observe["values"] == [{"topic": "/cmd_vel", "field": "linear.x", "type": None}]
    assert observe["record"]["enabled"] is True
    assert observe["record"]["topics"][:2] == ["/clock", "/tf"]
    assert "/fieldproof/events" in observe["record"]["topics"]
    assert observe["samples_file"] == f"{RUN_DIR_IN_CONTAINER}/{SAMPLES_FILE}"
    as_data = resolved.to_dict()
    assert as_data["assertions"][0]["resolved"]["model"] == "ugv1"
    assert as_data["assertions"][2]["resolved"]["topic"] == "/scan"
    assert as_data["observe"] == observe
    assert as_data["containers"][2]["role"] == "recorder"
    assert as_data["containers"][2]["user"] == recorder.user
    assert "robot_state_publisher:=false" in job.command
    assert job.mounts[0].source == REPO
    assert job.mounts[0].target == "/workspace"
    bridge = vehicle.bridge
    assert bridge.command == ("fieldproof-bridge", "bundle_out:=/tmp/fieldproof-bridge")
    assert vehicle.clock is True, "the first vehicle's bridge publishes /clock"
    assert bridge.env["FIELDPROOF_MANIFEST"] == "/workspace/examples/vehicles/ugv_nav2/robot.yaml"
    assert bridge.labels[VEHICLE_LABEL] == "ugv1"
    container = vehicle.container
    assert container.name == "fieldproof-abc123-ugv1"
    assert container.image.endswith("fieldproof-vehicle-ugv-nav2:local")
    assert container.hostname == "ugv1"
    assert container.command == (), "the image's own command: no namespace to pass"
    assert container.cpus == 2.0
    assert container.cpuset is None
    assert container.memory == "2g"
    assert container.pids is None
    assert vehicle.resources.to_dict() == {"cpus": 2.0, "memory": "2g"}
    assert container.env["ROS_DOMAIN_ID"] == str(resolved.domain_id)
    assert container.env["FIELDPROOF_NAMESPACE"] == ""
    assert container.env["FIELDPROOF_FRAME_PREFIX"] == ""
    assert "GZ_PARTITION" not in container.env
    # The vehicle's own DDS profile, mounted and named to its RMW (Fast DDS, the default).
    assert vehicle.dds_profile == (
        REPO / "examples/vehicles/ugv_nav2/src/ugv_nav2_bringup/config/fastdds.xml"
    )
    assert [(m.source, m.target, m.read_only) for m in container.mounts] == [
        (vehicle.dds_profile, "/opt/fieldproof/dds/profile.xml", True)
    ]
    assert container.env["FASTRTPS_DEFAULT_PROFILES_FILE"] == "/opt/fieldproof/dds/profile.xml"
    assert [s.role for s in resolved.containers()] == ["sim", "bridge", "recorder", "vehicle"]
    assert vehicle.topic("navigate_to_pose") == "/navigate_to_pose"
    assert vehicle.topic("/tf") == "/tf"
    assert vehicle.describe()["resources"] == {"cpus": 2.0, "memory": "2g"}


def test_plan_resolves_the_timeline(resolved: Plan) -> None:
    (action,) = resolved.actions
    assert action.name == "drive_ahead"
    assert action.goal_world is not None
    assert action.goal_map is not None
    assert action.goal_world.x == 10.0
    assert action.goal_world.y == -1.0
    assert action.goal_map.x == pytest.approx(4.0)
    assert action.goal_map.y == pytest.approx(0.0, abs=1e-4)
    assert action.goal_map.yaw == pytest.approx(0.0)
    data = resolved.to_dict()
    assert data["run_id"] == resolved.run_id
    assert data["containers"][0]["command"] == (
        f"gz sim -s -r --headless-rendering --seed {resolved.seed} depot.sdf"
    )
    assert data["seed"] == resolved.seed
    assert data["world"]["step_s"] == 0.001
    assert data["timeline"][0]["goal_map"]["x"] == pytest.approx(4.0)
    assert data["vehicles"][0]["spawn"]["yaw"] == -1.5708
    json.dumps(data)


def test_plan_namespace_and_overrides(config: Config, tmp_path: Path) -> None:
    scenario = scenario_variant(lambda d: d["vehicles"][0].update(namespace="ugv1", image="x:1"))
    resolved = plan(
        scenario,
        config,
        run_id="custom-id",
        runs_dir=tmp_path,
        render="software",
        domain_id=9,
        network="lab",
    )
    assert resolved.run_id == "custom-id"
    assert resolved.run_dir == tmp_path / "custom-id"
    assert resolved.sim.name == "fieldproof-id-sim"
    assert resolved.sim.network == "lab"
    assert resolved.sim.env["ROS_DOMAIN_ID"] == "9"
    assert resolved.domain_source == "explicit"
    (vehicle,) = resolved.vehicles
    assert vehicle.bridge.command == (
        "fieldproof-bridge",
        "namespace:=ugv1",
        "bundle_out:=/tmp/fieldproof-bridge",
    )
    assert "namespace:=ugv1" in vehicle.spawn_job.command
    assert vehicle.container.image == "x:1"
    assert vehicle.container.command == (
        "ros2",
        "launch",
        "ugv_nav2_bringup",
        "bringup.launch.py",
        "use_sim_time:=true",
        "drivers:=false",
        "namespace:=ugv1",
    ), "the manifest's namespace_arg carries the namespace to the vehicle's launch"
    assert vehicle.container.env["FIELDPROOF_NAMESPACE"] == "ugv1"
    assert vehicle.topic("navigate_to_pose") == "/ugv1/navigate_to_pose"
    assert vehicle.topic("tf") == "/ugv1/tf"


def test_plan_outputs_directory_wins(config: Config, tmp_path: Path) -> None:
    scenario = scenario_variant(lambda d: d["outputs"].update(directory="elsewhere"))
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    assert resolved.run_dir.parent == config.root / "elsewhere"
    resolved = plan(load_scenario(EXAMPLE), config, render="software", token=SUFFIX)
    assert resolved.run_dir.parent == config.root / "runs"


def test_plan_errors(config: Config, tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match=r"world\.file: 'moon\.sdf' is not a file"):
        plan(scenario_variant(lambda d: d["world"].update(file="moon.sdf")), config)
    with pytest.raises(ScenarioError, match=r"vehicles\[ugv1\].spawn: world depot has no frame"):
        plan(
            scenario_variant(lambda d: d["vehicles"][0].update(spawn={"frame": "nowhere"})), config
        )
    with pytest.raises(ScenarioError, match=r"timeline\[0\].goal: world depot has no frame"):
        plan(scenario_variant(lambda d: d["timeline"][0].update(goal={"frame": "nowhere"})), config)
    with pytest.raises(ScenarioError, match=r"vehicles\[ugv1\].manifest: .*no runtime"):
        plan(
            scenario_variant(
                lambda d: d["vehicles"][0].update(
                    manifest="../../robots/fieldproof_robots/models/ugv_ref/robot.yaml"
                )
            ),
            config,
        )
    with pytest.raises(ScenarioError, match=r"vehicles\[ugv1\].manifest: .*missing.yaml"):
        plan(scenario_variant(lambda d: d["vehicles"][0].update(manifest="missing.yaml")), config)
    # A manifest outside the workspace cannot be seen by the bridge container.
    outside = tmp_path / "ws"
    outside.mkdir()
    (outside / "fieldproof.toml").write_text("")
    nested = load_config(outside / "fieldproof.toml", env={})
    nested.stack.worlds = str(REPO / "sim/fieldproof_worlds/worlds")
    with pytest.raises(ScenarioError, match="outside the workspace"):
        plan(load_scenario(EXAMPLE), nested)


def test_detect_render(tmp_path: Path) -> None:
    assert detect_render("software", tmp_path) == ("software", ())
    assert detect_render("auto", tmp_path) == ("software", ())
    with pytest.raises(ScenarioError, match="no render node"):
        detect_render("gpu", tmp_path)
    node = tmp_path / "renderD128"
    node.write_text("")
    mode, groups = detect_render("auto", tmp_path)
    assert mode == "gpu"
    assert groups == (str(node.stat().st_gid),)
    assert detect_render("gpu", tmp_path)[0] == "gpu"


def test_make_run_id(example: Scenario) -> None:
    assert make_run_id(example, NOW, "zz") == f"{example.digest}-20261017T120000Z-zz"


# ------------------------------------------------------------------------------ happy path
def test_run_passes(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    record, _log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    assert record.reason is None
    assert record.phase == Phase.FINISHED
    assert [p["name"] for p in record.phases] == [
        "preparing",
        "sim",
        "spawn",
        "bridges",
        "recorder",
        "vehicles",
        "running",
        "teardown",
        "finished",
    ]
    assert all(p["finished_at"] for p in record.phases[:-1])
    assert backend.started == [
        "fieldproof-abc123-sim",
        "fieldproof-abc123-bridge-ugv1",
        "fieldproof-abc123-recorder",
        "fieldproof-abc123-ugv1",
    ]
    assert [j.role for j in backend.jobs] == ["spawn"]
    assert backend.networks == {"fieldproof"}
    # The world was polled until its services answered, the bridge until odometry appeared.
    sim_polls = [a for n, a in backend.execs if n.endswith("-sim") and "service -l" in a[2]]
    assert len(sim_polls) == 2
    assert sim_polls[0] == ("bash", "-lc", "timeout 20 gz service -l")
    assert (
        "fieldproof-abc123-bridge-ugv1",
        ("/usr/local/bin/fieldproof-bridge-entrypoint", "timeout", "20", "ros2", "topic", "list"),
    ) in backend.execs
    probe = next(
        a for n, a in backend.execs if n.endswith("-ugv1") and "fieldproof-vehicle-ready" in a[1]
    )
    assert probe[2:] == ("--timeout", "180")
    goal = next(a for n, a in backend.execs if "send_goal" in a)
    assert goal[:6] == (
        "/usr/local/bin/fieldproof-vehicle-entrypoint",
        "timeout",
        "-s",
        "INT",
        "300",
        "ros2",
    )
    assert goal[7:10] == ("send_goal", "/navigate_to_pose", "nav2_msgs/action/NavigateToPose")
    message = json.loads(goal[10])
    assert message["pose"]["header"]["frame_id"] == "map"
    assert message["pose"]["pose"]["position"]["x"] == pytest.approx(4.0)
    assert message["pose"]["pose"]["orientation"]["w"] == pytest.approx(1.0)
    assert EXIT_CODES[Status(record.status)] == 0


def test_run_record_and_files(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    # The timeline and the assertions in the record.
    (action,) = record.timeline
    assert action.name == "drive_ahead"
    assert action.status == "ok"
    assert action.detail == "goal SUCCEEDED"
    assert action.duration_s is not None
    verdicts = {a["name"]: a for a in record.assertions}
    assert {n: a["status"] for n, a in verdicts.items()} == {
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "lidar_streams": "passed",
        "no_collision": "passed",
        "speed_limit": "passed",
    }
    assert verdicts["reached_goal"]["reason"].startswith("0.20 m from (10, -1)")
    assert verdicts["reached_goal"]["evidence"]["reached"]["y"] == -0.8
    assert verdicts["reached_goal"]["sim_s"] == 9.0
    assert verdicts["arrived_in_time"]["evidence"]["action_status"] == "ok"
    assert verdicts["lidar_streams"]["evidence"]["lowest_hz"] == 10.0
    assert verdicts["no_collision"]["evidence"]["messages"] == 0
    assert verdicts["speed_limit"]["evidence"]["highest"] == 0.4
    assert record.observation["samples"] == len(GOOD_SAMPLES) + 1, "the samples and `ready`"
    assert record.observation["sim_elapsed_s"] == 8.5
    assert record.observation["observer_errors"] == 0
    (described,) = record.vehicles
    assert described["name"] == "ugv1"
    assert described["manifest"] == "ugv_nav2"
    assert described["namespace"] == ""
    assert described["frame_prefix"] == ""
    assert described["image"] == "ghcr.io/dimitrios-kafetzis/fieldproof-vehicle-ugv-nav2:local"
    assert described["resources"] == {"cpus": 2.0, "memory": "2g"}
    assert described["depends_on"] == []
    assert described["dds_profile"].endswith("config/fastdds.xml")
    assert described["contract"] is None, "no contract check unless asked for"
    assert record.stack["ros_domain_id"] == resolved.domain_id
    assert record.stack["domain_source"] == "auto"
    assert record.stack["discovery"] == "multicast"
    assert [c["role"] for c in record.containers] == ["sim", "bridge", "recorder", "vehicle"]
    assert record.stack["world"] == "depot"
    assert record.stack["render"] == "software"
    # Teardown in reverse order, nothing left, logs kept.
    assert backend.stopped == [
        "fieldproof-abc123-ugv1",
        "fieldproof-abc123-recorder",
        "fieldproof-abc123-bridge-ugv1",
        "fieldproof-abc123-sim",
    ]
    assert backend.containers == {}
    assert leftovers(backend, resolved.run_id) == []
    run_dir = resolved.run_dir
    assert (run_dir / SCENARIO_COPY).read_text() == EXAMPLE.read_text()
    assert sorted(p.name for p in (run_dir / "logs").iterdir()) == [
        "bridge-ugv1.log",
        "recorder.log",
        "sim.log",
        "vehicle-ugv1.log",
    ]
    assert (run_dir / "logs" / "sim.log").read_text() == "log of fieldproof-abc123-sim\n"
    written = json.loads((run_dir / RECORD_FILE).read_text())
    assert written["status"] == "passed"
    assert written["run_id"] == resolved.run_id
    assert written["timeline"][0]["status"] == "ok"
    assert written["finished_at"] is not None


def test_run_writes_the_observer_files_and_reports(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    run_dir = resolved.run_dir
    verdicts = {a["name"]: a for a in record.assertions}
    observe = json.loads((run_dir / OBSERVE_FILE).read_text())
    assert observe == resolved.observe
    assert len((run_dir / SAMPLES_FILE).read_text().splitlines()) == len(GOOD_SAMPLES) + 1
    verdict_lines = [
        json.loads(line) for line in (run_dir / ASSERTIONS_FILE).read_text().splitlines()
    ]
    assert [v["name"] for v in verdict_lines] == [
        "reached_goal",
        "arrived_in_time",
        "lidar_streams",
        "no_collision",
        "speed_limit",
    ], "in the order they resolved: the stream first, the action, then the end of the timeline"
    assert verdicts["arrived_in_time"]["evidence"]["duration_s"] == 10.0
    assert all(v["run_id"] == resolved.run_id for v in verdict_lines)
    report = json.loads((run_dir / REPORT_JSON).read_text())
    assert report["verdict"] == "PASS"
    assert report["exit_code"] == 0
    assert report["assertion_counts"]["passed"] == 5
    assert report["recording"] is None, "the fake records nothing"
    markdown = (run_dir / REPORT_MD).read_text()
    assert markdown.startswith("# Fieldproof run report: ugv_nav_to_goal")
    assert "| `reached_goal` | `reach_pose` | PASS |" in markdown
    assert "`logs/recorder.log`" in markdown
    lines = [json.loads(line) for line in (run_dir / LOG_FILE).read_text().splitlines()]
    assert lines[0]["event"] == "run.start"
    assert lines[-1]["event"] == "run.end"
    assert {"ts", "t", "run_id", "level", "event", "message"} <= set(lines[0])
    events = [line["event"] for line in lines]
    assert "vehicle.probe" in events
    assert "action.start" in events
    assert "teardown.done" in events
    assert "recorder.ready" in events
    assert events.count("assertion.passed") == 5
    assert "assertions.passed" in events
    assert "recording.missing" in events, "outputs.record is on, the fake wrote no MCAP"
    assert "report.written" in events
    assert lines[-1]["status"] == "passed"
    assert EXIT_CODES[Status(record.status)] == 0


def test_rtf_is_applied(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    scenario = scenario_variant(lambda d: d["time"].update(rtf=4))
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    physics = next(a for n, a in backend.execs if "set_physics" in a[2])
    assert "real_time_factor: 4" in physics[2]
    backend = ready_backend(resolved)
    backend.rtf_result = ExecResult(1, "service call timed out\n")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "set_physics" in str(record.reason)
    assert backend.containers == {}


def test_without_recording_or_logs(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    scenario = scenario_variant(lambda d: d["outputs"].update(record=False, logs=False))
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    assert resolved.observe["record"]["enabled"] is False
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    assert not (resolved.run_dir / "logs").exists()
    events = [e["event"] for e in log.events]
    assert "recording.missing" not in events
    assert next(e for e in log.events if e["event"] == "observer.start")["recording"] is False


def test_recording_is_reported(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    resolved = plan(
        scenario_variant(lambda _data: None),
        config,
        runs_dir=tmp_path,
        render="software",
        token=SUFFIX,
    )
    backend = ready_backend(resolved)
    original = backend.write_samples

    def write_and_record(samples: Any) -> None:
        original(samples)
        recording = resolved.run_dir / "recording"
        recording.mkdir(exist_ok=True)
        (recording / "recording_0.mcap").write_bytes(b"\x89MCAP0\r\n" + b"\0" * 100)

    backend.write_samples = write_and_record  # type: ignore[method-assign]
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    saved = next(e for e in log.events if e["event"] == "recording.saved")
    assert saved["files"] == ["recording/recording_0.mcap"]
    report = json.loads((resolved.run_dir / REPORT_JSON).read_text())
    assert report["recording"]["mcap"] == ["recording/recording_0.mcap"]
    assert "Recording (MCAP" in (resolved.run_dir / REPORT_MD).read_text()


# ------------------------------------------------------------------------------------ video
def test_video_is_recorded_and_saved(filmed: Plan, clock: FakeClock) -> None:
    resolved = filmed
    assert resolved.video is not None
    assert resolved.video.camera == "spectator"
    assert resolved.video.service == "/spectator/record_video"
    assert resolved.video.file == "video/spectator.mp4"
    assert resolved.video.in_sim == "/tmp/fieldproof-abc123-spectator.mp4"  # noqa: S108
    assert resolved.to_dict()["video"]["service"] == "/spectator/record_video"
    backend = ready_backend(resolved)
    backend.video_file_polls_needed = 3
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    sim = resolved.sim.name
    requests = [a[2] for n, a in backend.execs if n == sim and "record_video" in a[2]]
    assert len(requests) == 2
    assert "gz service -s /spectator/record_video --reqtype gz.msgs.VideoRecord" in requests[0]
    assert (
        '\'start: true, format: "mp4", save_filename: "/tmp/fieldproof-abc123-spectator.mp4"\''
        in requests[0]
    )
    assert requests[1].endswith("--req 'stop: true'")
    # The recording started with the timeline and stopped before the verdicts were settled.
    events = [e["event"] for e in log.events]
    assert (
        events.index("timeline.start") < events.index("video.start") < events.index("action.start")
    )
    assert events.index("action.end") < events.index("video.saved") < events.index("timeline.done")
    assert backend.video_file_polls == 3, "waited for the encoder to close the file"
    assert backend.copies == [
        (sim, "/tmp/fieldproof-abc123-spectator.mp4", resolved.run_dir / "video/spectator.mp4")  # noqa: S108
    ]
    saved = resolved.run_dir / "video/spectator.mp4"
    assert saved.read_bytes()[4:8] == b"ftyp"
    assert record.video == {
        "camera": "spectator",
        "file": "video/spectator.mp4",
        "bytes": saved.stat().st_size,
        "status": "saved",
    }
    report = json.loads((resolved.run_dir / REPORT_JSON).read_text())
    assert report["video"]["status"] == "saved"
    markdown = (resolved.run_dir / REPORT_MD).read_text()
    assert "- Video from camera `spectator` (2.0 KB): `video/spectator.mp4`" in markdown
    assert "- `video/spectator.mp4`" not in markdown, "listed once, as the video"
    assert (
        'name="video" value="video/spectator.mp4"' in (resolved.run_dir / "junit.xml").read_text()
    )


def test_video_start_refused_is_a_warning(filmed: Plan, clock: FakeClock) -> None:
    resolved = filmed
    backend = ready_backend(resolved)
    backend.video_start_result = ExecResult(1, "Service call timed out\n")
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED, "the video is best effort"
    assert record.video is not None
    assert record.video["status"] == "error"
    assert "refused to start" in record.video["reason"]
    error = next(e for e in log.events if e["event"] == "video.error")
    assert error["level"] == "warning"
    assert not [a for n, a in backend.execs if "stop: true" in a[2]], "nothing to stop"
    assert backend.copies == []
    assert (
        "- No video from camera `spectator`: the recorder refused to start"
        in (resolved.run_dir / REPORT_MD).read_text()
    )


def test_video_copy_failure_is_a_warning(filmed: Plan, clock: FakeClock) -> None:
    resolved = filmed
    backend = ready_backend(resolved)
    backend.fail_copy = True
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    assert record.video is not None
    assert record.video["status"] == "error"
    assert record.video["reason"].startswith("cannot save the recording")
    assert "video.error" in [e["event"] for e in log.events]


def test_video_never_written_is_a_warning(filmed: Plan, clock: FakeClock) -> None:
    resolved = filmed
    backend = ready_backend(resolved)
    backend.video_file_polls_needed = 10_000
    record, _log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    assert record.video is not None
    assert record.video["status"] == "error"
    assert "not written 15 s after the stop" in record.video["reason"]
    assert backend.copies == []


def test_video_stops_when_the_action_fails(filmed: Plan, clock: FakeClock) -> None:
    resolved = filmed
    backend = ready_backend(resolved)
    backend.goal_result = ExecResult(0, "Goal finished with status: ABORTED\n")
    record, _log = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.video is not None
    assert record.video["status"] == "saved", "the video of a failed run is kept"


def test_without_video(resolved: Plan, clock: FakeClock) -> None:
    assert resolved.video is None, "the example records no video"
    assert resolved.to_dict()["video"] is None
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    assert record.video is None
    assert not [a for n, a in backend.execs if "record_video" in a[2]]
    assert not [e for e in log.events if e["event"].startswith("video.")]
    markdown = (resolved.run_dir / REPORT_MD).read_text()
    assert "Video from camera" not in markdown
    assert "No video" not in markdown


def test_video_unknown_camera(config: Config, tmp_path: Path) -> None:
    scenario = scenario_variant(lambda d: d["outputs"].update({"video": {"camera": "roof"}}))
    with pytest.raises(
        ScenarioError,
        match="outputs.video: world depot has no camera sensor 'roof' with a CameraVideoRecorder "
        r"plugin \(cameras with one: spectator\)",
    ):
        plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)


# ------------------------------------------------------------------------------- assertions
def test_failed_assertion_fails_the_run(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    short: list[dict[str, Any] | str] = [
        s for s in GOOD_SAMPLES if not (s["kind"] == "pose" and s["y"] == -0.8)
    ]
    short.append({"kind": "pose", "sim": 9.0, "model": "ugv1", "x": 10.0, "y": 0.5, "yaw": -1.5})
    backend.samples_on_goal = short
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason is not None
    assert record.reason.startswith(
        "assertion reached_goal failed: not reached by the end of the timeline: closest 1.50 m"
    )
    verdicts = {a["name"]: a["status"] for a in record.assertions}
    assert verdicts == {
        "reached_goal": "failed",
        "arrived_in_time": "passed",
        "lidar_streams": "passed",
        "no_collision": "passed",
        "speed_limit": "passed",
    }
    assert backend.containers == {}, "torn down"
    assert any(e["event"] == "assertion.failed" and e["level"] == "error" for e in log.events)
    report = json.loads((resolved.run_dir / REPORT_JSON).read_text())
    assert report["verdict"] == "FAIL"
    assert report["exit_code"] == 1
    assert EXIT_CODES[Status(record.status)] == 1


def test_several_failed_assertions_name_the_first(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.samples_on_goal = [
        {"kind": "value", "sim": 2.0, "topic": "/cmd_vel", "field": "linear.x", "value": 2.5},
        {
            "kind": "contact",
            "sim": 3.0,
            "topic": "/contacts",
            "model": "ugv1",
            "collision1": "ugv1::base_link::base_link_collision",
            "collision2": "depot_building::south_wall_east::collision",
            "count": 1,
        },
        *GOOD_SAMPLES,
    ]
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason == (
        "assertion no_collision failed: ugv1 touched depot_building::south_wall_east::collision "
        "at 10.0 s (and 1 more)"
    )
    assert {a["name"] for a in record.assertions if a["status"] == "failed"} == {
        "no_collision",
        "speed_limit",
    }


def test_short_within_time_fails_fast(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    """The failing example: one second for the drive names arrived_in_time and nothing else."""
    failing = load_scenario(REPO / "examples/scenarios/ugv_nav_fails_on_time.yaml")
    resolved = plan(failing, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved)
    backend.goal_duration_s = 20.0
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason == (
        "assertion arrived_in_time failed: drive_ahead has not completed 1 s after its start"
    )
    verdicts = {a["name"]: a["status"] for a in record.assertions}
    assert verdicts == {
        "reached_goal": "passed",
        "arrived_in_time": "failed",
        "no_collision": "passed",
    }
    in_time = next(a for a in record.assertions if a["name"] == "arrived_in_time")
    assert in_time["evidence"]["elapsed_s"] == 20.0


def test_pending_assertions_are_skipped_when_the_run_ends_early(
    resolved: Plan, clock: FakeClock
) -> None:
    backend = ready_backend(resolved)
    backend.goal_result = ExecResult(0, "Goal finished with status: ABORTED\n")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason == "drive_ahead: goal ABORTED"
    verdicts = {a["name"]: a for a in record.assertions}
    assert verdicts["reached_goal"]["status"] == "passed", "the stream had decided it"
    assert verdicts["arrived_in_time"]["status"] == "failed"
    assert verdicts["arrived_in_time"]["reason"] == "drive_ahead ended failed after 10.0 s"
    assert verdicts["lidar_streams"]["status"] == "skipped"
    assert verdicts["lidar_streams"]["reason"] == "the run ended failed: drive_ahead: goal ABORTED"
    assert verdicts["no_collision"]["status"] == "skipped"


def test_observer_errors_are_logged(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.samples_on_start = [
        {"kind": "error", "message": "cannot subscribe to /ugv1/contacts"},
        {"kind": "ready", "recording": True, "subscribed": ["/cmd_vel"], "waiting": ["/scan"]},
    ]
    backend.samples_on_goal = [*GOOD_SAMPLES, "not a sample"]
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    errors = [e for e in log.events if e["event"] == "observer.error"]
    assert [e["message"] for e in errors] == ["cannot subscribe to /ugv1/contacts"]
    ready = next(e for e in log.events if e["event"] == "recorder.ready")
    assert "waiting for topic(s) /scan" in ready["message"]
    assert record.observation["observer_errors"] == 1
    assert record.observation["unparsed_lines"] == 1


def test_observer_never_ready(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.samples_on_start = []
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "timed out after 60 s waiting for the run's observer" in str(record.reason)
    assert backend.containers == {}
    assert all(a["status"] == "skipped" for a in record.assertions)


def test_recorder_exits(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.exit_on_poll["fieldproof-abc123-ugv1"] = 1  # the vehicle's probe
    backend.samples_on_start = []

    def die_on_start(spec: ContainerSpec) -> None:
        FakeBackend.start(backend, spec)
        if spec.role == "recorder":
            backend.containers[spec.name].running = False
            backend.containers[spec.name].exit_code = 1
            backend.containers[spec.name].logs = "Traceback: boom\n"

    backend.start = die_on_start  # type: ignore[method-assign]
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "recorder container fieldproof-abc123-recorder exited (1)" in str(record.reason)
    assert "boom" in str(record.reason)


def test_no_ready_conditions(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    resolved = plan(
        scenario_variant(lambda _data: None),
        config,
        runs_dir=tmp_path,
        render="software",
        token=SUFFIX,
    )
    (vehicle,) = resolved.vehicles
    quick = Plan(
        **{
            **resolved.__dict__,
            "vehicles": (vehicle.__class__(**{**vehicle.__dict__, "ready_timeout_s": 0.0}),),
        }
    )
    backend = ready_backend(quick)
    record, log = execute(quick, backend, clock)
    assert record.status == Status.PASSED
    assert not any("fieldproof-vehicle-ready" in a for _, a in backend.execs)
    assert any(e["message"].endswith("no ready conditions") for e in log.events)


# -------------------------------------------------------------------------------- failures
def test_missing_images(resolved: Plan, clock: FakeClock) -> None:
    backend = FakeBackend(images={resolved.sim.image})
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "image(s) not built" in str(record.reason)
    assert "fieldproof-bridge:local" in str(record.reason)
    assert "fieldproof-vehicle-ugv-nav2:local" in str(record.reason)
    assert backend.started == []
    assert backend.networks == set()
    assert record.phases[-1]["name"] == "finished"
    assert EXIT_CODES[Status(record.status)] == 1


def test_missing_fuel_models(example: Scenario, clock: FakeClock, tmp_path: Path) -> None:
    (tmp_path / "fieldproof.toml").write_text(f'[stack]\nfuel = "{tmp_path}/nothing"\n')
    config = load_config(tmp_path / "fieldproof.toml", env={})
    # The workspace is the temporary directory, so the manifest must be reachable from it:
    # point the stack back at the repository for everything but the Fuel models.
    config.stack.worlds = str(REPO / "sim/fieldproof_worlds/worlds")
    config.stack.fuel_lock = str(REPO / "assets/fuel.lock")
    config._root = REPO
    resolved = plan(example, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "Fuel model(s) not fetched" in str(record.reason)
    assert "pallet_rack_section" in str(record.reason)
    assert "(make assets)" in str(record.reason)


def test_realtime_run_holds_the_lock(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    """The real-time-locked mode (task P2-T04): RTF pinned to 1, the drift judged."""
    scenario = scenario_variant(lambda d: d["time"].update(mode="realtime", max_drift_s=1.0))
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved, clock)
    backend.samples_on_goal = [
        {"kind": "clock", "sim": 20.0, "rtf": None, "paused": False, "wall_offset_s": -10.0},
        *GOOD_SAMPLES[:-1],
        {"kind": "clock", "sim": 30.0, "rtf": 1.0, "paused": False},
    ]
    backend.goal_duration_s = 10.0
    record, _ = execute(resolved, backend, clock)
    log_file = resolved.run_dir / LOG_FILE
    assert record.status == Status.PASSED, record.reason
    rtf_calls = [c for _, c in backend.execs if "set_physics" in " ".join(c)]
    assert len(rtf_calls) == 1
    assert "real_time_factor: 1" in " ".join(rtf_calls[0])
    clock_record = record.clock
    assert clock_record is not None
    assert clock_record["mode"] == "realtime"
    assert clock_record["target_rtf"] == 1.0
    assert clock_record["verdict"] == "locked"
    assert clock_record["origin_sim_s"] == 20.0
    assert clock_record["sim_elapsed_s"] == 10.0
    assert clock_record["drift_s"] == pytest.approx(0.0, abs=1e-6)
    assert clock_record["violations"] == 0
    assert clock_record["rtf"]["windows"] == 1
    assert record.seed == resolved.seed
    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    by_event = {e["event"]: e for e in events}
    assert "time.mode realtime" in by_event["clock.mode"]["message"]
    assert by_event["clock.locked"]["message"].startswith("real-time lock held: drift +0.000 s")
    assert "clock.drift" not in by_event
    report = json.loads((resolved.run_dir / REPORT_JSON).read_text())
    assert report["clock"]["verdict"] == "locked"
    assert report["seed"] == resolved.seed
    markdown = (resolved.run_dir / REPORT_MD).read_text()
    assert "- Clock: mode `realtime`, target RTF 1.00, measured 1.00" in markdown
    assert "**locked** (drift budget 1.0 s, on drift: fail)" in markdown
    assert f"seed {resolved.seed}" in markdown


@pytest.mark.parametrize("on_drift", ["fail", "warn"])
def test_realtime_run_that_drifts(
    config: Config, clock: FakeClock, tmp_path: Path, on_drift: str
) -> None:
    scenario = scenario_variant(
        lambda d: d["time"].update(mode="realtime", max_drift_s=1.0, on_drift=on_drift)
    )
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved, clock)
    # Ten wall-clock seconds of goal, three of simulation: the lock is lost.
    backend.samples_on_goal = [
        {"kind": "clock", "sim": 20.0, "rtf": None, "paused": False, "wall_offset_s": -10.0},
        *GOOD_SAMPLES[:-1],
        {"kind": "clock", "sim": 23.0, "rtf": 0.3, "paused": False},
    ]
    backend.goal_duration_s = 10.0
    record, _ = execute(resolved, backend, clock)
    events = [json.loads(line) for line in (resolved.run_dir / LOG_FILE).read_text().splitlines()]
    drifts = [e for e in events if e["event"] == "clock.drift"]
    assert len(drifts) == 1
    assert drifts[0]["message"].startswith("real-time factor 0.300 over the last window")
    assert record.clock is not None
    assert record.clock["verdict"] == "drifted"
    assert record.clock["drift_s"] == pytest.approx(-7.0)
    assert record.clock["violations"] == 1
    if on_drift == "fail":
        assert record.status == Status.FAILED
        assert str(record.reason).startswith("real-time lock lost: drift -7.000 s")
        assert not any(e["event"] == "clock.drifted" for e in events)
    else:
        assert record.status == Status.PASSED, record.reason
        (warned,) = [e for e in events if e["event"] == "clock.drifted"]
        assert warned["level"] == "warning"


def test_sim_clock_events(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    """`sim.pause`, `sim.step` and `sim.resume` (task P2-T04) on the world's control service."""

    def mutate(data: dict[str, Any]) -> None:
        data["timeline"] += [
            {"name": "freeze", "at": "0s", "action": "sim.pause", "duration_s": 2},
            {"name": "nudge", "at": "0s", "after": "freeze", "action": "sim.step", "steps": 50},
            {
                "name": "blink",
                "at": "0s",
                "after": "nudge",
                "action": "sim.step",
                "duration_s": 0.25,
            },
            {"name": "thaw", "at": "0s", "after": "blink", "action": "sim.resume"},
        ]

    resolved = plan(
        scenario_variant(mutate), config, runs_dir=tmp_path, render="software", token=SUFFIX
    )
    by_name = {a.name: a for a in resolved.actions}
    assert by_name["nudge"].resolved == {
        "service": "/world/depot/control",
        "world": "depot",
        "steps": 50,
        "step_s": 0.001,
    }
    assert by_name["blink"].resolved["steps"] == 250
    backend = ready_backend(resolved, clock)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.PASSED, record.reason
    assert backend.controls == [
        "pause: true",
        "pause: false",
        "pause: true",
        "multi_step: 50 pause: true",
        "pause: true",
        "multi_step: 250 pause: true",
        "pause: false",
    ]
    details = {a.name: a.detail for a in record.timeline}
    assert details["freeze"] == "paused for 2 s, resumed"
    assert details["nudge"] == "50 step(s), 0.05 s, paused"
    assert details["blink"] == "250 step(s), 0.25 s, paused"
    assert details["thaw"] == "resumed"
    log_file = resolved.run_dir / LOG_FILE
    events = [json.loads(line)["event"] for line in log_file.read_text().splitlines()]
    assert [e for e in events if e in {"sim.paused", "sim.resumed", "sim.stepped"}] == [
        "sim.paused",
        "sim.resumed",
        "sim.stepped",
        "sim.stepped",
        "sim.resumed",
    ]
    backend.control_result = ExecResult(0, "data: false\n")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert "freeze: the world refused to pause" in str(record.reason)


def test_sim_clock_events_are_refused_in_lockstep(config: Config, tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["time"].update(mode="lockstep")
        data["timeline"].append({"at": "1s", "action": "sim.pause"})

    with pytest.raises(ScenarioError, match=r"timeline\[1\]: sim.pause does not combine"):
        plan(scenario_variant(mutate), config, runs_dir=tmp_path, render="software")


def test_world_never_ready(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.world_polls_needed = 10_000
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "timed out after 300 s waiting for the services of world depot" in str(record.reason)
    assert backend.stopped == ["fieldproof-abc123-sim"]
    assert backend.containers == {}
    assert clock.t - 1000.0 >= 300.0


def test_sim_exits(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.exit_on_poll[resolved.sim.name] = 1
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "sim container fieldproof-abc123-sim exited (137)" in str(record.reason)
    assert "log of fieldproof-abc123-sim" in str(record.reason)
    assert backend.containers == {}


def test_spawn_fails(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.spawn_result = ExecResult(1, "[create-1] no world named depot\n")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "ugv1: the spawn exited 1" in str(record.reason)
    assert "no world named" in str(record.reason)
    assert backend.started == ["fieldproof-abc123-sim"]
    assert backend.containers == {}


def test_bridge_never_ready(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.bridge_polls_needed = 10_000
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "waiting for the bridge of ugv1 (/odom)" in str(record.reason)
    assert backend.containers == {}


def test_vehicle_not_ready(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.probe_result = ExecResult(
        1,
        "waiting (7/9 ready): /map topic no publisher\n"
        "not ready after 180.0 s: /map topic no publisher\n",
    )
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason == "ugv1: not ready after 180.0 s: /map topic no publisher"
    assert backend.containers == {}
    assert EXIT_CODES[Status(record.status)] == 1


def test_probe_crashes(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.probe_result = ExecResult(2, "usage: fieldproof-vehicle-ready\n")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "readiness probe exited 2" in str(record.reason)


def test_goal_aborted(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.goal_result = ExecResult(0, "Goal accepted\nGoal finished with status: ABORTED\n")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason == "drive_ahead: goal ABORTED"
    assert record.timeline[0].status == "failed"
    assert backend.containers == {}


def test_goal_without_result(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.goal_result = ExecResult(1, "Waiting for an action server\nconnection lost\n")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert "no goal result (exit 1): " in str(record.reason)
    assert "connection lost" in str(record.reason)


def test_action_times_out(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.goal_result = ExecResult(124, "Goal accepted\n")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert "no result within 300 s" in str(record.reason)
    assert record.timeline[0].status == "timeout"


def test_budget_passes_before_an_action(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    scenario = scenario_variant(
        lambda d: d["time"].update(timeout_s="50s") or d["timeline"][0].update(at="2m")
    )
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.TIMEOUT
    assert record.reason == "budget of 50 s passed before drive_ahead"
    assert not any("send_goal" in a for _, a in backend.execs)
    assert backend.containers == {}
    assert EXIT_CODES[Status(record.status)] == 1


def test_budget_bounds_an_action(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    scenario = scenario_variant(lambda d: d["time"].update(timeout_s="40s"))
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved)
    backend.goal_result = ExecResult(124, "")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.TIMEOUT
    assert "passed during drive_ahead" in str(record.reason)
    goal = next(a for _, a in backend.execs if "send_goal" in a)
    assert goal[4] == "40"


def test_action_waits_for_its_time(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    scenario = scenario_variant(lambda d: d["timeline"][0].update(at="5s"))
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved)
    backend.goal_duration_s = 0.0
    before = clock.t
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    assert clock.t - before >= 5.0
    assert max(clock.slept) <= 2.0


def test_vehicle_dies_while_waiting(
    config: Config, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_variant(lambda d: d["timeline"][0].update(at="30s"))
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved)
    vehicle = resolved.vehicles[0].container.name
    original = backend.state

    def dying(name: str) -> ContainerState:
        if name == vehicle and clock.t - 1000.0 > 20.0 and name in backend.containers:
            backend.containers[name].running, backend.containers[name].exit_code = False, 1
        return original(name)

    monkeypatch.setattr(backend, "state", dying)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert "vehicle container fieldproof-abc123-ugv1 exited (1)" in str(record.reason)
    assert backend.containers == {}


# --------------------------------------------------------------------- interruption, teardown
def test_interrupted_during_start(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.fail_start[resolved.vehicles[0].container.name] = KeyboardInterrupt()
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.INTERRUPTED
    assert EXIT_CODES[Status(record.status)] == 130
    assert backend.stopped == [
        "fieldproof-abc123-recorder",
        "fieldproof-abc123-bridge-ugv1",
        "fieldproof-abc123-sim",
    ]
    assert backend.containers == {}
    assert leftovers(backend) == []
    assert [p["name"] for p in record.phases][-3:] == ["vehicles", "teardown", "finished"]
    assert any(e["event"] == "run.status" and e["level"] == "error" for e in log.events)


def test_backend_error_during_start(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.fail_start[resolved.vehicles[0].bridge.name] = BackendError("daemon went away")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert record.reason == "daemon went away"
    assert backend.containers == {}


def test_unexpected_exception_tears_down_and_propagates(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.fail_start[resolved.vehicles[0].container.name] = ZeroDivisionError("boom")
    log = RunLog(resolved.run_id, clock=clock.now)
    run = Run(resolved, backend, log, sleep=clock.sleep, clock=clock.now)
    with pytest.raises(ZeroDivisionError):
        run.execute()
    assert run.record.status == Status.ERROR
    assert run.record.reason == "ZeroDivisionError: boom"
    assert run.record.phase == Phase.FINISHED
    assert backend.containers == {}


def test_keep_leaves_the_containers(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend, clock, keep=True)
    assert record.status == Status.PASSED
    assert backend.stopped == []
    assert len(backend.containers) == 4
    kept = next(e for e in log.events if e["event"] == "teardown.kept")
    assert "fieldproof-abc123-sim" in kept["message"]
    assert kept["level"] == "warning"
    assert sorted(leftovers(backend, resolved.run_id)) == sorted(backend.containers)


def test_teardown_continues_past_errors_and_sweeps(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.fail_stop.add(resolved.vehicles[0].bridge.name)
    stray = ContainerSpec(
        name="fieldproof-abc123-stray",
        image="x",
        role="spawn",
        labels={RUN_LABEL: resolved.run_id, ROLE_LABEL: "spawn"},
    )
    backend.containers[stray.name] = FakeContainer(stray, running=False, exit_code=0)
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    assert backend.removed[:3] == [
        "fieldproof-abc123-ugv1",
        "fieldproof-abc123-recorder",
        "fieldproof-abc123-sim",
    ]
    assert sorted(backend.removed[3:]) == [
        "fieldproof-abc123-bridge-ugv1",
        "fieldproof-abc123-stray",
    ]
    assert backend.containers == {}
    events = [e["event"] for e in log.events]
    assert "teardown.error" in events
    assert "teardown.swept" in events
    done = next(e for e in log.events if e["event"] == "teardown.done")
    assert done["removed"] == 5
    assert not (resolved.run_dir / "logs" / "bridge-ugv1.log").exists()


def test_leftovers_of_every_run(resolved: Plan) -> None:
    backend = ready_backend(resolved)
    for spec in resolved.containers():
        backend.start(spec)
    assert sorted(leftovers(backend)) == sorted(s.name for s in resolved.containers())


# -------------------------------------------------------------------------------- the log
def test_run_log(tmp_path: Path) -> None:
    out = tmp_path / "console.txt"
    with out.open("w") as handle:
        log = RunLog(
            "r1",
            file=tmp_path / "sub" / "engine.log",
            console=Console(file=handle, width=200),
            verbose=False,
        )
        log.event("phase", "sim", phase="sim")
        log.event("vehicle.probe", "waiting", level="debug")
        log.event("warn", level="warning", count=2)
        log.close()
    lines = [
        json.loads(line) for line in (tmp_path / "sub" / "engine.log").read_text().splitlines()
    ]
    assert [line["event"] for line in lines] == ["phase", "vehicle.probe", "warn"]
    assert lines[0]["run_id"] == "r1"
    assert lines[0]["phase"] == "sim"
    assert lines[2]["count"] == 2
    console = out.read_text()
    assert "phase" in console
    assert "waiting" not in console, "debug lines are hidden when not verbose"
    assert "count=2" in console
    assert log.events == lines


@pytest.mark.parametrize(
    ("output", "status"),
    [
        ("Goal finished with status: SUCCEEDED\n", "SUCCEEDED"),
        ("x\nGoal finished with status: ABORTED\n\n", "ABORTED"),
        ("Goal accepted\n", None),
        ("", None),
    ],
)
def test_goal_status(output: str, status: str | None) -> None:
    assert goal_status(ExecResult(0, output)) == status


# ------------------------------------------------------------- KPIs, resources, abort (P2-T05)
def test_kpis_and_resources_in_the_record(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    def with_kpis(data: dict[str, Any]) -> None:
        data["kpis"] = [
            {"name": "drive", "type": "path", "vehicle": "ugv1", "action": "drive_ahead"},
            {"name": "footprint", "type": "resources", "vehicle": "ugv1"},
            {"name": "mission", "type": "mission_time"},
        ]
        data["assertions"].append(
            {"name": "fits", "type": "resource_usage", "vehicle": "ugv1", "max_memory_mb": 2048}
        )

    scenario = scenario_variant(with_kpis)
    resolved = plan(
        scenario, config, runs_dir=tmp_path / "runs", render="software", now=NOW, token=SUFFIX
    )
    assert [k.name for k in resolved.kpis] == ["drive", "footprint", "mission"]
    assert resolved.kpis[1].resolved == {"container": "fieldproof-abc123-ugv1"}
    backend = ready_backend(resolved)
    backend.usage_samples["*"] = [
        ResourceUsage(80.0, int(300 * MIB), 2 * 1024**3, 50, "t1"),
        ResourceUsage(120.0, int(320 * MIB), 2 * 1024**3, 51, "t2"),
    ]
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED, record.reason
    verdicts = {a["name"]: a for a in record.assertions}
    assert verdicts["fits"]["status"] == "passed", verdicts["fits"]["reason"]
    assert verdicts["fits"]["metrics"]["peak_memory_mb"] == {"value": 320.0, "unit": "MiB"}
    assert verdicts["reached_goal"]["metrics"]["closest_m"]["value"] == pytest.approx(0.2)
    # Every container of the run was sampled from the start of the timeline.
    assert set(backend.usage_requested) == {
        "fieldproof-abc123-ugv1",
        "fieldproof-abc123-bridge-ugv1",
        "fieldproof-abc123-sim",
        "fieldproof-abc123-recorder",
    }
    lines = [
        json.loads(line) for line in (resolved.run_dir / "resources.jsonl").read_text().splitlines()
    ]
    assert len(lines) == 8
    assert {line["role"] for line in lines} == {"vehicle", "bridge", "sim", "recorder"}
    assert record.resources["samples"] == 8
    assert record.resources["last"]["fieldproof-abc123-ugv1"] == {
        "cpu_pct": 120.0,
        "memory_mb": 320.0,
        "pids": 51,
    }
    # The KPIs: the collectors' and the assertions' metrics, flat.
    kpis = record.kpis
    assert kpis["drive.path_m"]["value"] == pytest.approx(3.8)
    assert kpis["drive.efficiency"] == {"value": 1.0, "unit": "", "source": "drive", "type": "path"}
    assert kpis["footprint.peak_cpu_pct"]["value"] == 120.0
    assert kpis["mission.duration_s"]["value"] > 0
    assert kpis["arrived_in_time.duration_s"]["value"] == pytest.approx(10.0)
    assert kpis["fits.mean_memory_mb"]["value"] == 310.0
    assert [c["status"] for c in record.kpi_collectors] == ["measured"] * 3
    kpi_lines = [
        json.loads(line) for line in (resolved.run_dir / "kpis.jsonl").read_text().splitlines()
    ]
    assert [line.get("name") for line in kpi_lines[:3]] == ["drive", "footprint", "mission"]
    assert kpi_lines[3]["kind"] == "kpis"
    assert kpi_lines[3]["kpis"] == kpis
    events = [e["event"] for e in log.events]
    assert events.count("kpi.measured") == 3
    assert "resources.start" in events
    report = json.loads((resolved.run_dir / REPORT_JSON).read_text())
    assert report["kpis"] == kpis
    markdown = (resolved.run_dir / REPORT_MD).read_text()
    assert "## KPIs" in markdown
    assert "| `drive.path_m` | 3.800 | m | `drive` (path) |" in markdown
    assert 'name="kpi.drive.path_m" value="3.8 m"' in (resolved.run_dir / "junit.xml").read_text()


def test_resource_monitor_errors_are_warnings(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)

    class Refused:
        def __init__(self, name: str) -> None:
            self.name = name

        def __iter__(self) -> Refused:
            return self

        def __next__(self) -> ResourceUsage:
            raise BackendError(f"{self.name}: stats unavailable")

    backend.usage = Refused  # type: ignore[method-assign]
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    warnings = [e for e in log.events if e["event"] == "resources.error"]
    assert len(warnings) == 4
    assert warnings[0]["level"] == "warning"
    assert record.resources == {"containers": [], "samples": 0, "last": {}}
    assert record.kpis["reached_goal.closest_m"]["value"] == pytest.approx(0.2)


def test_on_fail_abort_cancels_the_timeline(
    config: Config, clock: FakeClock, tmp_path: Path
) -> None:
    def aborting(data: dict[str, Any]) -> None:
        for assertion in data["assertions"]:
            if assertion["name"] == "speed_limit":
                assertion["on_fail"] = "abort"
        data["timeline"].append(
            {
                "name": "park",
                "vehicle": "ugv1",
                "action": "navigate_to_pose",
                "goal": {"x": 9.0, "y": -1.0},
                "after": "drive_ahead",
                "at": "5s",
            }
        )
        data["assertions"].append(
            {"name": "parked_in_time", "type": "within_time", "action": "park", "max_s": "1m"}
        )

    scenario = scenario_variant(aborting)
    resolved = plan(
        scenario, config, runs_dir=tmp_path / "runs", render="software", now=NOW, token=SUFFIX
    )
    backend = ready_backend(resolved)
    backend.samples_on_goal = [
        *GOOD_SAMPLES,
        {"kind": "value", "sim": 9.6, "topic": "/cmd_vel", "field": "linear.x", "value": 2.5},
    ]
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason == (
        "assertion speed_limit failed: /cmd_vel linear.x = 2.5 at 10.0 s violates "
        "'value <= 1.5' (on_fail: abort, the timeline was cancelled)"
    )
    assert [(a.name, a.status) for a in record.timeline] == [
        ("drive_ahead", "ok"),
        ("park", "skipped"),
    ]
    assert record.timeline[1].detail.startswith("assertion speed_limit failed")
    verdicts = {a["name"]: a["status"] for a in record.assertions}
    assert verdicts == {
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "lidar_streams": "skipped",
        "no_collision": "skipped",
        "speed_limit": "failed",
        "parked_in_time": "failed",
    }
    assert len([a for a, _ in backend.execs if "send_goal" in " ".join(_)]) == 1, "park never sent"
    assert backend.containers == {}, "torn down"
    assert [e["event"] for e in log.events if e["event"] == "action.skipped"] == ["action.skipped"]
