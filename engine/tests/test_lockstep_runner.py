"""Tests of lockstep mode in the runner (task P2-T04): the example scenario stepped on the
fake backend, with a fake clock manager answering the engine's commands through the clock file,
the clock agents' containers, the timeline and the assertions in simulation time, the holds at
the due times, a vehicle that stops acknowledging, and the lockstep plan."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes import FakeBackend, FakeClock, FakeLockstep
from rich.console import Console
from test_runner import REPO, SUFFIX, T0, SyncExecutor, ready_backend, scenario_variant

from fieldproof.backend import ExecResult
from fieldproof.config import Config, load_config
from fieldproof.runner import LOG_FILE, Plan, Run, RunLog, Status, plan
from fieldproof.scenario import ScenarioError

ORIGIN = 10.0
# What the vehicle "does" once the world steps: poses in simulation time past the origin.
DRIVE = {
    1.0: [
        {"kind": "pose", "sim": 11.0, "model": "ugv1", "x": 10.0, "y": 3.0, "z": 0.1, "yaw": -1.57}
    ],
    4.0: [
        {"kind": "pose", "sim": 14.0, "model": "ugv1", "x": 10.0, "y": 1.0, "z": 0.1, "yaw": -1.57},
        {"kind": "value", "sim": 14.0, "topic": "/cmd_vel", "field": "linear.x", "value": 0.4},
    ],
    7.0: [
        {"kind": "rate", "sim": 17.0, "topic": "/scan", "hz": 10.0, "window_s": 5.0, "total": 60},
        {
            "kind": "pose",
            "sim": 17.0,
            "model": "ugv1",
            "x": 10.0,
            "y": -0.8,
            "z": 0.1,
            "yaw": -1.57,
        },
    ],
}


TRAILER = {
    "name": "tidy_up",
    "at": "8s",
    "vehicle": "ugv1",
    "action": "param.set",
    "node": "link_monitor",
    "parameter": "timeout_s",
    "value": 5.0,
}
"""A last entry at 8 s of simulation time, so the world steps on after the (instant) drive."""


def lockstep_plan(
    config: Config, tmp_path: Path, mutate: Callable[[dict[str, Any]], None] | None = None
) -> Plan:
    def change(data: dict[str, Any]) -> None:
        data["time"].update(mode="lockstep", step_s=0.1, ack_timeout_s=5)
        data["seed"] = 7
        data["timeline"].append(dict(TRAILER))
        if mutate is not None:
            mutate(data)

    return plan(
        scenario_variant(change), config, runs_dir=tmp_path, render="software", token=SUFFIX
    )


def execute(
    resolved: Plan, backend: FakeBackend, clock: FakeClock, fake: FakeLockstep
) -> tuple[Any, RunLog]:
    """Run with the fake clock manager ticking on every sleep of the engine."""
    resolved.run_dir.parent.mkdir(parents=True, exist_ok=True)
    log = RunLog(
        resolved.run_id,
        file=resolved.run_dir / LOG_FILE,
        console=Console(file=(resolved.run_dir.parent / "console.txt").open("a")),
        clock=clock.now,
    )
    backend.clock = clock
    backend.wall = lambda: T0 + (clock.now() - 1000.0)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        fake.tick()

    run = Run(
        resolved,
        backend,
        log,
        sleep=sleep,
        clock=clock.now,
        wall=lambda: T0 + (clock.now() - 1000.0),
        executor=SyncExecutor(),  # type: ignore[arg-type]
    )
    return run.execute(), log


def rig(resolved: Plan, clock: FakeClock, **options: Any) -> tuple[FakeBackend, FakeLockstep]:
    backend = ready_backend(resolved, clock)
    backend.samples_on_goal = []
    fake = FakeLockstep(backend, origin=ORIGIN, **options)
    # The agents acknowledge while the world runs free, before the timeline.
    backend.samples_on_start = [
        {"kind": "ready", "recording": True, "subscribed": [], "waiting": []},
        {"kind": "clock", "sim": 9.0, "rtf": 1.0, "mode": "lockstep", "acks": {"ugv1": 9.0}},
    ]
    fake.samples_at = dict(DRIVE)
    return backend, fake


def events_of(resolved: Plan) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (resolved.run_dir / LOG_FILE).read_text().splitlines()]


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> Config:
    fuel = tmp_path_factory.mktemp("fuel")
    for model in yaml.safe_load((REPO / "assets/fuel.lock").read_text())["models"]:
        (fuel / model["dir"]).mkdir()
    config = load_config(REPO / "fieldproof.toml", env={})
    config.stack.fuel = str(fuel)
    return config


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ------------------------------------------------------------------------------------ plan
def test_lockstep_plan(config: Config, tmp_path: Path) -> None:
    resolved = lockstep_plan(config, tmp_path)
    assert resolved.seed == 7
    assert resolved.sim.command[-3:] == ("--seed", "7", "depot.sdf")
    (vehicle,) = resolved.vehicles
    agent = vehicle.agent
    assert agent is not None
    assert agent.name == "fieldproof-abc123-clock-ugv1"
    assert agent.role == "clock"
    assert agent.image == resolved.recorder.image
    assert agent.network_mode == f"container:{vehicle.container.name}"
    assert agent.command == (
        "python3",
        "/opt/fieldproof/lockstep.py",
        "--vehicle",
        "ugv1",
        "--step-s",
        "0.1",
        "--topic",
        "/fieldproof/lockstep",
    )
    assert agent.env["ROS_DOMAIN_ID"] == str(resolved.domain_id)
    assert [m.target for m in agent.mounts] == ["/opt/fieldproof/lockstep.py"]
    assert resolved.containers()[-1] is agent, "started last, stopped first"
    clock_config = resolved.observe["clock"]
    assert clock_config == {
        "world": "depot",
        "mode": "lockstep",
        "target_rtf": 1.0,
        "step_s": 0.1,
        "vehicles": ["ugv1"],
        "ack_timeout_s": 5.0,
        "control_file": "/fieldproof/run/clock.jsonl",
    }
    data = resolved.to_dict()
    assert data["time"]["mode"] == "lockstep"
    assert data["containers"][-1]["role"] == "clock"


def test_free_running_plan_has_no_agents_but_a_clock_config(config: Config, tmp_path: Path) -> None:
    resolved = plan(scenario_variant(lambda d: None), config, runs_dir=tmp_path, render="software")
    assert all(v.agent is None for v in resolved.vehicles)
    assert resolved.observe["clock"] == {
        "world": "depot",
        "mode": "sim",
        "target_rtf": 1.0,
        "step_s": 0.1,
        "vehicles": [],
        "ack_timeout_s": 30.0,
        "control_file": None,
    }
    with_rtf = plan(
        scenario_variant(lambda d: d["time"].update(rtf=2.5)),
        config,
        runs_dir=tmp_path,
        render="software",
    )
    assert with_rtf.observe["clock"]["target_rtf"] == 2.5


def test_lockstep_step_must_be_whole_physics_steps(config: Config, tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match=r"time.step_s: 0.0015 s is not a multiple"):
        lockstep_plan(config, tmp_path, lambda d: d["time"].update(step_s=0.0015))
    with pytest.raises(ScenarioError, match="not a multiple"):
        lockstep_plan(config, tmp_path, lambda d: d["time"].update(step_s=0.0005))
    assert (
        lockstep_plan(config, tmp_path, lambda d: d["time"].update(step_s=0.3)).scenario.time.step_s
        == 0.3
    )


def test_seed_override_and_range(config: Config, tmp_path: Path) -> None:
    resolved = plan(
        scenario_variant(lambda d: None), config, runs_dir=tmp_path, render="software", seed=42
    )
    assert resolved.seed == 42
    with pytest.raises(ScenarioError, match="seed: -1 is not between"):
        plan(
            scenario_variant(lambda d: None), config, runs_dir=tmp_path, render="software", seed=-1
        )


# ------------------------------------------------------------------------------------- runs
def test_lockstep_run_steps_the_world_and_counts_simulation_time(
    config: Config, clock: FakeClock, tmp_path: Path
) -> None:
    resolved = lockstep_plan(config, tmp_path, lambda d: d["timeline"][0].update(at="2s"))
    backend, fake = rig(resolved, clock)
    record, _ = execute(resolved, backend, clock, fake)
    assert record.status == Status.PASSED, record.reason
    # The agent's container started after the vehicle was ready and was torn down first.
    agent = resolved.vehicles[0].agent
    assert agent is not None
    assert backend.started[-1] == agent.name
    assert backend.stopped[0] == agent.name
    # The protocol: lock, run to the drive's due time, then to the last entry's, release.
    commands = [
        json.loads(line) for line in (resolved.run_dir / "clock.jsonl").read_text().splitlines()
    ]
    assert commands == [
        {"cmd": "lockstep"},
        {"cmd": "run_until", "t_s": 2.0},
        {"cmd": "run_until", "t_s": 8.0},
        {"cmd": "release"},
    ]
    # The drive started at exactly 2 s of simulation time after the origin, the trailer at 8.
    drive, trailer = record.timeline
    assert drive.status == "ok"
    assert drive.t_s == 2.0
    assert drive.sim_s == 2.0
    assert trailer.status == "ok"
    assert trailer.t_s == 8.0
    # The clock of the run.
    assert record.clock is not None
    assert record.clock["mode"] == "lockstep"
    assert record.clock["origin_sim_s"] == ORIGIN
    assert record.clock["verdict"] is None
    lockstep = record.clock["lockstep"]
    assert lockstep["origin_sim_s"] == ORIGIN
    assert lockstep["step_s"] == 0.1
    assert lockstep["steps"] == 80
    assert lockstep["holds"] >= 2
    assert lockstep["timeout"] is None
    # The assertions were judged in simulation time: the lidar's window after 5 s.
    verdicts = {a["name"]: a for a in record.assertions}
    assert {n: a["status"] for n, a in verdicts.items()} == {
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "lidar_streams": "passed",
        "no_collision": "passed",
        "speed_limit": "passed",
    }, {n: a["reason"] for n, a in verdicts.items()}
    assert verdicts["reached_goal"]["t_s"] == pytest.approx(7.0)
    events = events_of(resolved)
    names = [e["event"] for e in events]
    assert "clock.agent" in names
    assert "clock.agents" in names
    locked = next(e for e in events if e["event"] == "clock.locked")
    assert locked["origin_sim_s"] == ORIGIN
    assert "paused at simulation time 10.000 s" in locked["message"]
    start = next(e for e in events if e["event"] == "timeline.start")
    assert start["time_base"] == "sim"
    assert "the timeline counts simulation time" in start["message"]
    assert "clock.hold.expired" not in names
    assert (resolved.run_dir / "clock.jsonl").is_file()
    markdown = (resolved.run_dir / "report.md").read_text()
    assert "- Clock: mode `lockstep`, target RTF 1.00" in markdown
    assert "lockstep 0.100 s steps:" in markdown
    assert "seed 7" in markdown


def test_lockstep_holds_while_a_goal_starts(
    config: Config, clock: FakeClock, tmp_path: Path
) -> None:
    """The world stays at the due time until the event says the goal was accepted."""
    resolved = lockstep_plan(config, tmp_path)
    backend, fake = rig(resolved, clock)
    accepted = (
        "Waiting for an action server...\nGoal accepted with ID: abc\n"
        "Goal finished with status: SUCCEEDED\n"
    )
    backend.goal_result = ExecResult(0, accepted)
    record, _ = execute(resolved, backend, clock, fake)
    assert record.status == Status.PASSED, record.reason
    assert fake.commands[0] == {"cmd": "lockstep"}
    assert fake.commands[1] == {"cmd": "run_until", "t_s": 8.0}
    events = events_of(resolved)
    outputs = [e["message"] for e in events if e["event"] == "action.output"]
    assert "Goal accepted with ID: abc" in outputs
    drive, _ = record.timeline
    assert drive.t_s == 0.0


def test_lockstep_fails_when_a_vehicle_stops_acknowledging(
    config: Config, clock: FakeClock, tmp_path: Path
) -> None:
    resolved = lockstep_plan(config, tmp_path, lambda d: d["timeline"][0].update(at="3s"))
    backend, fake = rig(resolved, clock, ack_timeout_s=3.0)
    fake.refuse_from = {"ugv1": 1.0}
    record, _ = execute(resolved, backend, clock, fake)
    assert record.status == Status.FAILED
    assert str(record.reason).startswith("lockstep: step 11 (sim 11.1) not acknowledged by ugv1")
    assert record.clock is not None
    assert record.clock["lockstep"]["timeout"]["missing"] == ["ugv1"]
    drive, _ = record.timeline
    assert drive.status in {"waiting", "pending"}
    last = (resolved.run_dir / "clock.jsonl").read_text().splitlines()[-1]
    assert json.loads(last) == {"cmd": "release"}, "the end of the timeline lets the world go"
    events = events_of(resolved)
    assert any(e["event"] == "teardown.done" for e in events)


def test_lockstep_waits_for_the_agents_and_the_origin(
    config: Config, clock: FakeClock, tmp_path: Path
) -> None:
    resolved = lockstep_plan(config, tmp_path)
    backend, fake = rig(resolved, clock)
    backend.samples_on_start = [
        {"kind": "ready", "recording": True, "subscribed": [], "waiting": []}
    ]
    record, _ = execute(resolved, backend, clock, fake)
    assert record.status == Status.ERROR
    assert "every vehicle's clock agent" in str(record.reason)
