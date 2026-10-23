"""The determinism test of task P2-T04: the lockstep scenario twice, the trajectories compared.

``fieldproof run examples/scenarios/ugv_lockstep.yaml`` twice, with the scenario's seed, and
then ``fieldproof compare`` on the two runs: the ground-truth trajectories of the vehicle stay
within the tolerance, and so do the odometry streams the observer recorded (the ``odom_x`` and
``odom_y`` value samples), compared sample by sample in simulation time. Each run's record says
the world was locked and stepped (the origin, the steps, the holds, no acknowledgement
timeout), the drive started at exactly 1 s of simulation time, the clock agent's container ran
and its log is kept, and nothing is left behind.

The definition of done asks for identical odometry streams; what two runs share exactly is the
stepped clock, the seed and the moment every entry starts, and what still differs is what the
stack computed in the wall-clock time of each step (Nav2's planner and controller run free
between steps), so the test compares within a tolerance and prints the deviation
(docs/reference/time.md, "Lockstep").

Slow (two runs of the stack), so it runs only with ``FIELDPROOF_DETERMINISM=1``:
``make test-determinism`` locally, the nightly scenario workflow in CI. Needs the stack images,
the vehicle image and the Fuel models like ``test_run.py``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from fieldproof.determinism import Point, compare

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "examples/scenarios/ugv_lockstep.yaml"
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
RUN_TIMEOUT_S = 1800.0
ENV = {**os.environ, "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0")}
TOLERANCE_M = 0.25
"""The largest deviation two lockstep runs may show, in the ground truth and in the odometry."""

pytestmark = pytest.mark.skipif(
    os.environ.get("FIELDPROOF_DETERMINISM") != "1",
    reason="two runs of the stack: set FIELDPROOF_DETERMINISM=1 (make test-determinism)",
)


def containers_of(run_id: str) -> list[str]:
    completed = subprocess.run(  # noqa: S603
        ["docker", "ps", "-a", "--filter", f"label={RUN_LABEL}={run_id}", "--format", "{{.Names}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.split()


def print_run_logs(run_dir: Path, tail: int = 120) -> None:
    for path in sorted(run_dir.glob("*.log")) + sorted(run_dir.glob("logs/*.log")):
        print(f"\n===== {path.relative_to(run_dir)} (last {tail} lines) =====", flush=True)  # noqa: T201
        print("\n".join(path.read_text().splitlines()[-tail:]), flush=True)  # noqa: T201


def run_once(runs_dir: Path, run_id: str) -> dict[str, Any]:
    command = [*FIELDPROOF, "run", str(SCENARIO), "--runs-dir", str(runs_dir), "--run-id", run_id]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603
        command, env=ENV, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
    )
    run_dir = runs_dir / run_id
    print(f"exit {completed.returncode} after {time.monotonic() - started:.0f} s", flush=True)  # noqa: T201
    print(completed.stdout[-4000:], flush=True)  # noqa: T201
    if completed.returncode != 0:
        print(completed.stderr[-4000:], flush=True)  # noqa: T201
        print_run_logs(run_dir)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert containers_of(run_id) == []
    record = json.loads((run_dir / "run.json").read_text())
    assert record["status"] == "passed", record["reason"]
    return dict(record)


def check_lockstep(record: dict[str, Any], run_dir: Path) -> None:
    assert record["time"]["mode"] == "lockstep"
    assert record["seed"] == 7
    clock = record["clock"]
    print(f"clock: {json.dumps(clock)}", flush=True)  # noqa: T201
    assert clock["mode"] == "lockstep"
    assert clock["target_rtf"] == 1.0
    lockstep = clock["lockstep"]
    assert lockstep["origin_sim_s"] == clock["origin_sim_s"]
    assert lockstep["steps"] >= 50
    assert lockstep["holds"] >= 1
    assert lockstep["timeout"] is None
    assert lockstep["vehicles"] == ["ugv1"]
    assert clock["sim_elapsed_s"] >= 5.0
    (drive,) = record["timeline"]
    assert drive["status"] == "ok"
    assert drive["t_s"] == 1.0, "the goal was sent at exactly 1 s of simulation time"
    assert drive["sim_s"] == 1.0
    assert {a["name"]: a["status"] for a in record["assertions"]} == {
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "lidar_streams": "passed",
        "no_collision": "passed",
        "speed_limit": "passed",
        "odom_x": "passed",
        "odom_y": "passed",
    }, {a["name"]: a["reason"] for a in record["assertions"]}
    events = [json.loads(line) for line in (run_dir / "engine.log").read_text().splitlines()]
    names = [e["event"] for e in events]
    for name in ("clock.agent", "clock.agents", "clock.locked", "timeline.start"):
        assert name in names, name
    assert "clock.hold.expired" not in names
    assert "lockstep.timeout" not in names
    locked = next(e for e in events if e["event"] == "clock.locked")
    assert locked["origin_sim_s"] == clock["origin_sim_s"]
    commands = [json.loads(line) for line in (run_dir / "clock.jsonl").read_text().splitlines()]
    assert commands[0] == {"cmd": "lockstep"}
    assert commands[-1] == {"cmd": "release"}
    assert any(c["cmd"] == "run_until" and c.get("t_s") == 1.0 for c in commands)
    agent_log = run_dir / "logs" / "clock-ugv1.log"
    assert agent_log.is_file()
    assert "ready: ugv1 acknowledges 0.1 s steps" in agent_log.read_text()
    assert sorted(p.name for p in (run_dir / "logs").iterdir()) == [
        "bridge-ugv1.log",
        "clock-ugv1.log",
        "recorder.log",
        "sim.log",
        "vehicle-ugv1.log",
    ]


def odometry(run_dir: Path, origin: float) -> list[Point]:
    """The recorded odometry position as a trajectory in timeline seconds."""
    xs: dict[float, float] = {}
    ys: dict[float, float] = {}
    for line in (run_dir / "samples.jsonl").read_text().splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("kind") != "value" or data.get("topic") != "/odom" or data.get("sim") is None:
            continue
        t = round(float(data["sim"]) - origin, 6)
        if t < 0 or "value" not in data:
            continue
        if data["field"] == "pose.pose.position.x":
            xs[t] = float(data["value"])
        elif data["field"] == "pose.pose.position.y":
            ys[t] = float(data["value"])
    return [Point(t, xs[t], ys[t], 0.0) for t in sorted(set(xs) & set(ys))]


@pytest.mark.integration
def test_two_lockstep_runs_follow_the_same_trajectory(tmp_path: Path) -> None:
    ids = [f"det-{letter}-{os.getpid()}" for letter in "ab"]
    records = [run_once(tmp_path, run_id) for run_id in ids]
    for run_id, record in zip(ids, records, strict=True):
        check_lockstep(record, tmp_path / run_id)
    # The ground truth, through the command line.
    command = [*FIELDPROOF, "compare", str(tmp_path / ids[0]), str(tmp_path / ids[1]), "--json"]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    completed = subprocess.run(  # noqa: S603
        command, env=ENV, capture_output=True, text=True, timeout=120, check=False
    )
    print(completed.stdout, flush=True)  # noqa: T201
    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(completed.stdout)
    assert result["within_tolerance"] is True
    assert result["seeds"] == [7, 7]
    assert result["modes"] == ["lockstep", "lockstep"]
    ground = result["vehicles"]["ugv1"]
    assert ground["samples"] >= 50
    assert ground["max_m"] <= TOLERANCE_M
    # The odometry streams, sample by sample in simulation time.
    streams = [
        odometry(tmp_path / run_id, record["clock"]["origin_sim_s"])
        for run_id, record in zip(ids, records, strict=True)
    ]
    assert all(len(s) >= 100 for s in streams), [len(s) for s in streams]
    odom = compare(streams[0], streams[1])
    print(f"odometry: {json.dumps(odom)}", flush=True)  # noqa: T201
    assert odom["samples"] >= 100
    assert odom["max_m"] is not None
    assert odom["max_m"] <= TOLERANCE_M, odom
    print(  # noqa: T201
        f"ground truth deviates {ground['max_m']:.3f} m at most (mean {ground['mean_m']:.3f} m), "
        f"odometry {odom['max_m']:.3f} m (mean {odom['mean_m']:.3f} m) over {odom['span_s']:.1f} s",
        flush=True,
    )
