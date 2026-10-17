"""Tests of the scenario runner v0 (task P1-T05): the plan, the state machine on a fake backend."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes import FakeBackend, FakeClock, FakeContainer
from rich.console import Console

from fieldproof.backend import BackendError, ContainerSpec, ContainerState, ExecResult
from fieldproof.config import Config, load_config
from fieldproof.runner import (
    EXIT_CODES,
    LOG_FILE,
    RECORD_FILE,
    ROLE_LABEL,
    RUN_LABEL,
    SCENARIO_COPY,
    VEHICLE_LABEL,
    Phase,
    Plan,
    Run,
    RunLog,
    Status,
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


def ready_backend(resolved: Plan) -> FakeBackend:
    backend = FakeBackend()
    backend.images = {s.image for s in resolved.containers()} | {
        v.spawn_job.image for v in resolved.vehicles
    }
    return backend


# --------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def example() -> Scenario:
    return load_scenario(EXAMPLE)


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


def scenario_variant(mutate: Callable[[dict[str, Any]], None]) -> Scenario:
    """The example with a change, as if loaded from the example's path."""
    text = EXAMPLE.read_text()
    data = yaml.safe_load(text)
    mutate(data)
    return validate_data(data, EXAMPLE.resolve(), text)


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
    run = Run(resolved, backend, log, keep=keep, sleep=clock.sleep, clock=clock.now)
    return run.execute(), log


# ------------------------------------------------------------------------------------ plan
def test_plan_resolves_the_example(resolved: Plan, config: Config, tmp_path: Path) -> None:
    assert re.fullmatch(r"[0-9a-f]{8}-20261017T120000Z-abc123", resolved.run_id)
    assert resolved.run_dir == tmp_path / "runs" / resolved.run_id
    assert resolved.world.name == "depot"
    assert resolved.network == "fieldproof"
    assert resolved.render == "software"
    assert resolved.domain_id == 0
    sim = resolved.sim
    assert sim.name == "fieldproof-abc123-sim"
    assert sim.image == config.image("sim")
    assert sim.command == ("gz", "sim", "-s", "-r", "--headless-rendering", "depot.sdf")
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
    assert sim.labels == {RUN_LABEL: resolved.run_id, ROLE_LABEL: "sim"}
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
    assert "robot_state_publisher:=false" in job.command
    assert job.mounts[0].source == REPO
    assert job.mounts[0].target == "/workspace"
    bridge = vehicle.bridge
    assert bridge.command == ("fieldproof-bridge",)
    assert bridge.env["FIELDPROOF_MANIFEST"] == "/workspace/examples/vehicles/ugv_nav2/robot.yaml"
    assert bridge.labels[VEHICLE_LABEL] == "ugv1"
    container = vehicle.container
    assert container.name == "fieldproof-abc123-ugv1"
    assert container.image.endswith("fieldproof-vehicle-ugv-nav2:local")
    assert container.hostname == "ugv1"
    assert container.cpus == 2.0
    assert container.memory == "2g"
    assert container.pids is None
    assert container.env["ROS_DOMAIN_ID"] == "0"
    assert [s.role for s in resolved.containers()] == ["sim", "bridge", "vehicle"]
    assert vehicle.topic("navigate_to_pose") == "/navigate_to_pose"
    assert vehicle.topic("/tf") == "/tf"


def test_plan_resolves_the_timeline(resolved: Plan) -> None:
    (action,) = resolved.actions
    assert action.name == "drive_ahead"
    assert action.goal_world.x == 10.0
    assert action.goal_world.y == -1.0
    assert action.goal_map.x == pytest.approx(4.0)
    assert action.goal_map.y == pytest.approx(0.0, abs=1e-4)
    assert action.goal_map.yaw == pytest.approx(0.0)
    data = resolved.to_dict()
    assert data["run_id"] == resolved.run_id
    assert data["containers"][0]["command"] == "gz sim -s -r --headless-rendering depot.sdf"
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
    (vehicle,) = resolved.vehicles
    assert vehicle.bridge.command == ("fieldproof-bridge", "namespace:=ugv1")
    assert "namespace:=ugv1" in vehicle.spawn_job.command
    assert vehicle.container.image == "x:1"
    assert vehicle.topic("navigate_to_pose") == "/ugv1/navigate_to_pose"


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
        "vehicles",
        "running",
        "teardown",
        "finished",
    ]
    assert all(p["finished_at"] for p in record.phases[:-1])
    assert backend.started == [
        "fieldproof-abc123-sim",
        "fieldproof-abc123-bridge-ugv1",
        "fieldproof-abc123-ugv1",
    ]
    assert [j.role for j in backend.jobs] == ["spawn"]
    assert backend.networks == {"fieldproof"}
    # The world was polled until its services answered, the bridge until odometry appeared.
    sim_polls = [a for n, a in backend.execs if n.endswith("-sim")]
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
    assert record.assertions == [
        {
            "name": "reached_goal",
            "type": "reach_pose",
            "status": "skipped",
            "reason": "assertions are evaluated from task P1-T06",
        }
    ]
    assert [c["role"] for c in record.containers] == ["sim", "bridge", "vehicle"]
    assert record.stack["world"] == "depot"
    assert record.stack["render"] == "software"
    # Teardown in reverse order, nothing left, logs kept.
    assert backend.stopped == [
        "fieldproof-abc123-ugv1",
        "fieldproof-abc123-bridge-ugv1",
        "fieldproof-abc123-sim",
    ]
    assert backend.containers == {}
    assert leftovers(backend, resolved.run_id) == []
    run_dir = resolved.run_dir
    assert (run_dir / SCENARIO_COPY).read_text() == EXAMPLE.read_text()
    assert sorted(p.name for p in (run_dir / "logs").iterdir()) == [
        "bridge-ugv1.log",
        "sim.log",
        "vehicle-ugv1.log",
    ]
    assert (run_dir / "logs" / "sim.log").read_text() == "log of fieldproof-abc123-sim\n"
    written = json.loads((run_dir / RECORD_FILE).read_text())
    assert written["status"] == "passed"
    assert written["run_id"] == resolved.run_id
    assert written["timeline"][0]["status"] == "ok"
    assert written["finished_at"] is not None
    lines = [json.loads(line) for line in (run_dir / LOG_FILE).read_text().splitlines()]
    assert lines[0]["event"] == "run.start"
    assert lines[-1]["event"] == "run.end"
    assert {"ts", "t", "run_id", "level", "event", "message"} <= set(lines[0])
    events = [line["event"] for line in lines]
    assert "vehicle.probe" in events
    assert "action.start" in events
    assert "teardown.done" in events
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


def test_record_skips_the_recorder(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    scenario = scenario_variant(lambda d: d["outputs"].update(record=True, logs=False))
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    assert any(e["event"] == "outputs.record" and e["level"] == "warning" for e in log.events)
    assert not (resolved.run_dir / "logs").exists()


def test_no_ready_conditions(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    resolved = plan(
        load_scenario(EXAMPLE), config, runs_dir=tmp_path, render="software", token=SUFFIX
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


def test_realtime_is_refused(config: Config, clock: FakeClock, tmp_path: Path) -> None:
    scenario = scenario_variant(lambda d: d["time"].update(mode="realtime"))
    resolved = plan(scenario, config, runs_dir=tmp_path, render="software", token=SUFFIX)
    record, _ = execute(resolved, ready_backend(resolved), clock)
    assert record.status == Status.ERROR
    assert "P2-T04" in str(record.reason)


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
    assert backend.stopped == ["fieldproof-abc123-bridge-ugv1", "fieldproof-abc123-sim"]
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
    assert len(backend.containers) == 3
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
    assert backend.removed[:2] == ["fieldproof-abc123-ugv1", "fieldproof-abc123-sim"]
    assert sorted(backend.removed[2:]) == [
        "fieldproof-abc123-bridge-ugv1",
        "fieldproof-abc123-stray",
    ]
    assert backend.containers == {}
    events = [e["event"] for e in log.events]
    assert "teardown.error" in events
    assert "teardown.swept" in events
    done = next(e for e in log.events if e["event"] == "teardown.done")
    assert done["removed"] == 4
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
