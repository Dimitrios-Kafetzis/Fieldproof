"""Tests of clock management (task P2-T04): the observer's clock manager (the lockstep
protocol against a fake world and fake agents), the engine's drift monitor and its side of
the protocol."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_observer import FakeClock, FakeGz, FakeRos

from fieldproof.assertions import Sample
from fieldproof.clock import ClockMonitor, LockstepControl
from fieldproof.observer import (
    ACK_TOPIC,
    CLOCK_FILE,
    CMD_LOCKSTEP,
    CMD_RELEASE,
    CMD_RUN_UNTIL,
    ClockManager,
    Sink,
    next_on_grid,
    on_grid,
    step_of,
)
from fieldproof.scenario import Time


def is_locked(manager: ClockManager) -> bool:
    """`manager.locked` through a call, so the type checker does not narrow it."""
    return manager.locked


def acknowledged(control: LockstepControl) -> bool:
    """`control.acknowledged` through a call, so the type checker does not narrow it."""
    return control.acknowledged


def has_exceeded(monitor: ClockMonitor) -> bool:
    """`monitor.exceeded` through a call, so the type checker does not narrow it."""
    return monitor.exceeded


def test_grid_helpers() -> None:
    assert on_grid(0.30000000000000004, 0.1) == 0.3
    assert next_on_grid(2.05, 0.1) == 2.1
    assert next_on_grid(2.0, 0.1) == 2.0
    assert next_on_grid(1.9999999, 0.1) == 2.0
    assert step_of(0.3, 0.1) == 3
    assert step_of(0.29999999, 0.1) == 3
    assert step_of(0.25, 0.1) == 2


# ------------------------------------------------------------------------- the clock manager
def samples_of(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def stats(sim: float, *, paused: bool, rtf: float = 1.0, iterations: int = 0) -> Any:
    return SimpleNamespace(
        sim_time=SimpleNamespace(sec=int(sim), nsec=round((sim - int(sim)) * 1e9)),
        paused=paused,
        real_time_factor=rtf,
        iterations=iterations,
    )


def ack(vehicle: str, sim: float) -> Any:
    return SimpleNamespace(data=json.dumps({"vehicle": vehicle, "sim": sim, "step": 0}))


class Rig:
    """A clock manager with a fake world, two fake agents and the engine's command file."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: str = "lockstep",
        vehicles: tuple[str, ...] = ("ugv1", "ugv2"),
    ) -> None:
        self.run_dir = tmp_path
        self.clock = FakeClock(100.0)
        self.sink = Sink(tmp_path / "samples.jsonl", clock=self.clock)
        self.gz = FakeGz()
        self.ros = FakeRos()
        self.errors: list[str] = []
        self.control_file = tmp_path / CLOCK_FILE
        self.manager = ClockManager(
            {
                "world": "depot",
                "mode": mode,
                "target_rtf": 1.0,
                "step_s": 0.1,
                "vehicles": list(vehicles),
                "ack_timeout_s": 5.0,
                "control_file": str(self.control_file),
            },
            self.sink,
            self.gz,
            self.ros,
            clock=self.clock,
            error=self.errors.append,
        )
        self.manager.start()

    def command(self, cmd: str, **fields: Any) -> None:
        with self.control_file.open("a") as file:
            file.write(json.dumps({"cmd": cmd, **fields}) + "\n")
        self.manager.poll()

    def world_reaches(self, sim: float) -> None:
        """The world ran to `sim` and paused; every agent saw the clock there."""
        self.gz.stats["/world/depot/stats"](stats(sim, paused=True))
        for vehicle in self.manager.vehicles:
            self.ros.subscriptions[ACK_TOPIC][1](ack(vehicle, sim))

    @property
    def requests(self) -> list[float | None]:
        return [r[3] for r in self.gz.controls]

    def samples(self, kind: str = "clock") -> list[dict[str, Any]]:
        return [s for s in samples_of(self.run_dir / "samples.jsonl") if s["kind"] == kind]


def test_manager_follows_the_statistics_in_every_mode(tmp_path: Path) -> None:
    rig = Rig(tmp_path, mode="realtime", vehicles=())
    assert ACK_TOPIC not in rig.ros.subscriptions, "no agents to hear from outside lockstep"
    assert rig.ros.timers == []
    rig.gz.stats["/world/depot/stats"](stats(12.5, paused=False, rtf=0.98, iterations=12500))
    state = rig.manager.state()
    assert state == {"mode": "realtime", "target_rtf": 1.0, "paused": False, "gz_rtf": 0.98}
    assert rig.manager.sim == 12.5
    rig.gz.stats["/world/depot/stats"](stats(12.6, paused=True, rtf=0.0))
    assert rig.manager.state()["paused"] is True
    assert rig.manager.state()["gz_rtf"] == 0.98, "a paused world keeps the last factor"
    assert "lockstep" not in rig.manager.state()


def test_manager_refused_statistics_is_an_error(tmp_path: Path) -> None:
    rig = Rig.__new__(Rig)
    gz = FakeGz(refuse={"/world/depot/stats"})
    errors: list[str] = []
    manager = ClockManager(
        {"world": "depot"}, Sink(tmp_path / "s.jsonl"), gz, FakeRos(), error=errors.append
    )
    manager.start()
    assert errors == ["cannot subscribe to /world/depot/stats"]
    del rig


def test_lockstep_locks_at_the_next_boundary_and_steps_to_the_hold(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert ACK_TOPIC in rig.ros.subscriptions
    assert [period for period, _ in rig.ros.timers] == [0.05]
    rig.gz.stats["/world/depot/stats"](stats(12.34, paused=False))
    rig.command(CMD_LOCKSTEP)
    # The origin is the first grid point half a second ahead; the world is asked to run there.
    assert rig.requests == [12.9]
    assert rig.manager.origin == 12.9
    assert not is_locked(rig.manager)
    # One agent is not enough.
    rig.ros.subscriptions[ACK_TOPIC][1](ack("ugv1", 12.9))
    assert not is_locked(rig.manager)
    assert rig.manager.missing() == ["ugv2"]
    rig.world_reaches(12.9)
    assert is_locked(rig.manager)
    locked = [s for s in rig.samples() if s.get("locked")]
    assert len(locked) == 1
    assert locked[0]["sim"] == 12.9
    assert locked[0]["lockstep"]["origin"] == 12.9
    assert locked[0]["acks"] == {"ugv1": 12.9, "ugv2": 12.9}
    # Held at the origin until the engine says how far to go (hold 0).
    assert rig.manager.held is True
    assert rig.requests == [12.9]
    held = [s for s in rig.samples() if s.get("held")]
    assert len(held) == 1
    # The engine lets it run to 0.25 s: rounded up to the grid, three steps, then a hold.
    rig.command(CMD_RUN_UNTIL, t_s=0.25)
    assert rig.requests == [12.9, 13.0]
    rig.world_reaches(13.0)
    assert rig.requests == [12.9, 13.0, 13.1]
    rig.world_reaches(13.1)
    rig.world_reaches(13.2)
    assert rig.requests == [12.9, 13.0, 13.1, 13.2]
    assert rig.manager.held is True
    assert rig.manager.steps == 3
    assert rig.manager.holds == 2
    steps = [
        s
        for s in rig.samples()
        if s["lockstep"]["t_s"] and not s.get("held") and "waiting" not in s
    ]
    assert [s["sim"] for s in steps] == [13.0, 13.1, 13.2]
    assert steps[-1]["lockstep"]["step"] == 3
    assert steps[-1]["lockstep"]["t_s"] == pytest.approx(0.3)
    # No limit: it keeps stepping as the acknowledgements come.
    rig.command(CMD_RUN_UNTIL, t_s=None)
    assert rig.requests[-1] == 13.3
    rig.world_reaches(13.3)
    assert rig.requests[-1] == 13.4
    # An acknowledgement of an older time changes nothing.
    rig.ros.subscriptions[ACK_TOPIC][1](ack("ugv1", 13.0))
    assert rig.manager.acks["ugv1"] == 13.3
    # Release: the world runs free, nothing more is requested.
    rig.command(CMD_RELEASE)
    assert rig.gz.controls[-1] == ("/world/depot/control", False, None, None)
    assert rig.manager.state()["lockstep"]["released"] is True
    before = len(rig.gz.controls)
    rig.world_reaches(13.4)
    assert len(rig.gz.controls) == before, "nothing is requested after the release"
    assert rig.samples()[-1]["released"] is True
    assert rig.errors == []


def test_lockstep_reports_a_step_nobody_acknowledges(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.gz.stats["/world/depot/stats"](stats(1.0, paused=False))
    rig.command(CMD_LOCKSTEP)
    rig.world_reaches(1.5)
    rig.command(CMD_RUN_UNTIL, t_s=None)
    assert rig.requests[-1] == 1.6
    rig.ros.subscriptions[ACK_TOPIC][1](ack("ugv1", 1.6))
    rig.clock.t += 1.2
    rig.manager.poll()
    waiting = [s for s in rig.samples() if s.get("waiting")]
    assert waiting[-1]["waiting"] == ["ugv2"]
    assert waiting[-1]["waited_s"] == pytest.approx(1.2)
    rig.clock.t += 4.0
    rig.manager.poll()
    (timeout,) = rig.samples("lockstep.timeout")
    assert timeout["missing"] == ["ugv2"]
    assert timeout["sim"] == 1.6
    assert rig.errors == ["lockstep: step 1 (sim 1.600 s) not acknowledged by ugv2 within 5 s"]
    rig.manager.poll()
    assert len(rig.samples("lockstep.timeout")) == 1, "reported once"
    # A late acknowledgement still moves on.
    rig.ros.subscriptions[ACK_TOPIC][1](ack("ugv2", 1.6))
    assert rig.requests[-1] == 1.7


def test_lockstep_refused_request_and_bad_commands_are_errors(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.gz.refuse.add("/world/depot/control")
    rig.gz.stats["/world/depot/stats"](stats(1.0, paused=False))
    rig.command(CMD_LOCKSTEP)
    assert rig.errors == [
        "lockstep: no reply to run_to_sim_time 1.500 from /world/depot/control (the "
        "acknowledgements decide whether the step was taken)"
    ]
    with rig.control_file.open("a") as file:
        file.write("not json\n")
        file.write(json.dumps({"cmd": "dance"}) + "\n")
    rig.manager.poll()
    assert rig.errors[1:] == [
        "clock command not JSON: 'not json'",
        "unknown clock command: {'cmd': 'dance'}",
    ]
    rig.ros.subscriptions[ACK_TOPIC][1](SimpleNamespace(data="{}"))  # ignored
    assert rig.manager.acks == {}


def test_lockstep_begin_without_statistics_uses_the_ros_clock(tmp_path: Path) -> None:
    rig = Rig(tmp_path, vehicles=("ugv1",))
    rig.ros.sim = 3.02
    rig.command(CMD_LOCKSTEP)
    assert rig.requests == [3.6]
    rig.command(CMD_LOCKSTEP)
    assert rig.requests == [3.6], "a second lockstep command changes nothing"


# ------------------------------------------------------------------------------ the monitor
def clock_sample(wall: float, sim: float, rtf: float | None, *, paused: bool = False) -> Sample:
    return Sample(
        "clock",
        wall,
        sim,
        {"kind": "clock", "wall": wall, "sim": sim, "rtf": rtf, "paused": paused},
    )


def test_monitor_measures_drift_and_windows_against_the_target() -> None:
    monitor = ClockMonitor(Time(mode="realtime", tolerance=0.1, max_drift_s=1.0))
    assert monitor.target_rtf == 1.0
    assert monitor.feed(Sample("pose", 100.0, 5.0, {})) is None
    assert monitor.feed(clock_sample(100.0, 5.0, None)) is None
    assert monitor.feed(clock_sample(101.0, 5.98, 0.98)) is None
    assert monitor.drift_s == pytest.approx(-0.02)
    warning = monitor.feed(clock_sample(102.0, 6.5, 0.52))
    assert warning is not None
    assert warning.startswith("real-time factor 0.520 over the last window, target 1 ± 0.1")
    assert monitor.violations == 1
    assert not has_exceeded(monitor)
    assert monitor.feed(clock_sample(104.0, 7.0, 0.25)) is not None
    assert monitor.drift_s == pytest.approx(-2.0)
    assert has_exceeded(monitor)
    assert monitor.verdict == "drifted"
    summary = monitor.summary()
    assert summary["mode"] == "realtime"
    assert summary["origin_sim_s"] == 5.0
    assert summary["sim_elapsed_s"] == 2.0
    assert summary["wall_elapsed_s"] == 4.0
    assert summary["max_abs_drift_s"] == 2.0
    assert summary["rtf"] == {"windows": 3, "mean": 0.583, "min": 0.25, "max": 0.98}
    assert summary["violations"] == 2
    assert summary["verdict"] == "drifted"
    assert summary["max_drift_s"] == 1.0
    assert summary["on_drift"] == "fail"


def test_monitor_excludes_paused_windows_and_knows_the_world_pace() -> None:
    monitor = ClockMonitor(Time(mode="sim"), world_rtf=2.0)
    assert monitor.target_rtf == 2.0
    monitor.feed(clock_sample(100.0, 0.0, None))
    monitor.feed(clock_sample(101.0, 2.0, 2.0))
    assert monitor.drift_s == 0.0
    monitor.feed(clock_sample(102.0, 2.0, 0.0, paused=True))
    monitor.feed(clock_sample(103.0, 2.0, 0.0, paused=True))
    monitor.feed(clock_sample(104.0, 2.0, 0.0, paused=True))
    assert monitor.paused_s == 2.0, "the time between paused samples"
    assert monitor.violations == 0, "a paused window is no violation"
    monitor.feed(clock_sample(105.0, 4.0, 2.0))
    assert monitor.drift_s == pytest.approx(-2.0), (
        "the pause's first second counts (none was before)"
    )
    assert monitor.feed(clock_sample(106.0, 4.5, 0.5)) is None, "sim mode records, never warns"
    assert monitor.violations == 1
    assert monitor.verdict is None
    summary = monitor.summary()
    assert summary["paused_s"] == 2.0
    assert summary["max_drift_s"] is None
    assert summary["rtf"]["windows"] == 3
    assert summary["violations"] == 1
    assert ClockMonitor(Time(mode="sim", rtf=0.5)).target_rtf == 0.5
    assert ClockMonitor(Time(mode="lockstep")).target_rtf == 1.0
    assert ClockMonitor(Time(mode="lockstep", rtf=4.0)).target_rtf == 4.0
    empty = ClockMonitor(Time()).summary()
    assert empty["drift_s"] is None
    assert empty["rtf"] == {"windows": 0, "mean": None, "min": None, "max": None}


def test_monitor_in_lockstep_tracks_drift_but_judges_no_window() -> None:
    monitor = ClockMonitor(Time(mode="lockstep", step_s=0.1))
    monitor.feed(clock_sample(100.0, 10.0, None))
    assert monitor.feed(clock_sample(102.0, 10.5, 0.25)) is None
    assert monitor.violations == 0
    assert monitor.rtfs == []
    assert monitor.drift_s == pytest.approx(-1.5)
    assert not has_exceeded(monitor)


# ------------------------------------------------------------------------ the engine's side
def test_control_writes_commands_and_reads_the_protocol(tmp_path: Path) -> None:
    control = LockstepControl(tmp_path, ["ugv1", "ugv2"], 0.1)
    assert not acknowledged(control)
    assert control.feed(Sample("clock", 1.0, 9.0, {"acks": {"ugv1": 9.0}})) is None
    assert not acknowledged(control)
    control.feed(Sample("clock", 1.0, 9.0, {"acks": {"ugv2": 9.0}}))
    assert acknowledged(control)
    control.begin()
    assert control.run_until(2.0) is True
    assert control.run_until(2.0) is False, "unchanged"
    assert control.run_until(math.inf) is True
    assert control.run_until(math.inf) is False
    assert control.run_until(3.5) is True
    control.release()
    control.release()
    commands = [json.loads(line) for line in (tmp_path / CLOCK_FILE).read_text().splitlines()]
    assert commands == [
        {"cmd": CMD_LOCKSTEP},
        {"cmd": CMD_RUN_UNTIL, "t_s": 2.0},
        {"cmd": CMD_RUN_UNTIL, "t_s": None},
        {"cmd": CMD_RUN_UNTIL, "t_s": 3.5},
        {"cmd": CMD_RELEASE},
    ]
    assert control.commands == 5
    state = {"origin": 12.9, "step": 0, "t_s": 0.0, "steps": 0, "holds": 0, "max_wait_s": 0.0}
    assert control.feed(Sample("clock", 1.0, 12.9, {"lockstep": state, "locked": True})) == (
        "locked at simulation time 12.900 s"
    )
    assert control.origin == 12.9
    assert control.t_s == 0.0
    step = {**state, "step": 3, "t_s": 0.3, "steps": 3, "holds": 1, "max_wait_s": 0.012}
    assert control.feed(Sample("clock", 2.0, 13.2, {"lockstep": step})) is None
    assert control.t_s == pytest.approx(0.3)
    assert (control.steps, control.holds, control.max_wait_s) == (3, 1, 0.012)
    control.feed(Sample("clock", 2.5, 13.2, {"lockstep": step, "held": True}))
    assert control.held is True
    assert control.t_s == pytest.approx(0.3)
    control.feed(
        Sample("clock", 3.0, 13.3, {"lockstep": step, "waiting": ["ugv2"], "waited_s": 1.5})
    )
    assert control.waiting == ["ugv2"]
    assert control.waited_s == 1.5
    told = control.feed(
        Sample("lockstep.timeout", 9.0, 13.3, {"step": 4, "missing": ["ugv2"], "waited_s": 5.1})
    )
    assert told == "step 4 (sim 13.3) not acknowledged by ugv2 within 5.1 s"
    assert control.timeout is not None
    assert (
        control.feed(Sample("clock", 9.5, 13.3, {"lockstep": step, "released": True})) == "released"
    )
    summary = control.summary()
    assert summary["origin_sim_s"] == 12.9
    assert summary["vehicles"] == ["ugv1", "ugv2"]
    assert summary["timeout"]["missing"] == ["ugv2"]


def test_control_ignores_the_protocol_before_the_origin(tmp_path: Path) -> None:
    control = LockstepControl(tmp_path, ["ugv1"], 0.1)
    state = {"origin": None, "step": 0, "t_s": None, "steps": 0, "holds": 0, "max_wait_s": 0.0}
    assert control.feed(Sample("clock", 1.0, 5.0, {"lockstep": state, "held": True})) is None
    assert control.t_s is None
    assert control.held is False
