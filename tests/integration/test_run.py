"""Integration test of ``fieldproof run`` (task P1-T05, the P1-T05 verification).

Runs after ``test_ugv_nav2.py`` (pytest orders the files by name) against the same images:

1. ``fieldproof run examples/scenarios/ugv_nav_to_goal.yaml`` brings the simulator, the bridge
   and the example vehicle up through the Docker API, waits for the readiness gates, sends the
   timeline's ``NavigateToPose`` goal and tears everything down: exit code 0, the run directory
   holds the scenario, ``run.json`` with status ``passed`` and the goal ``SUCCEEDED``,
   ``engine.log`` and the three containers' logs, and no container of the run is left;
2. a second run is interrupted with SIGINT while the stack is coming up (as soon as its
   simulator container exists): exit code 130, ``run.json`` says ``interrupted``, and no
   container of the run is left behind ("a manual interrupted run leaves no containers
   behind", automated).

When the first run fails, the run directory's ``engine.log`` and the containers' logs are
printed, since CI keeps nothing else of the run.

Needs the stack images (``make images``), the vehicle image (``make vehicle-image``) and the
Fuel models (``make assets``); ``ROS_DOMAIN_ID`` (default 0) is shared with the other test.
Software rendering keeps the simulator below real time on a CI runner, so the wall-clock
limits are generous.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "examples/scenarios/ugv_nav_to_goal.yaml"
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
RUN_TIMEOUT_S = 1500.0
START_TIMEOUT_S = 300.0
STOP_TIMEOUT_S = 180.0
ENV = {**os.environ, "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0")}


def containers_of(run_id: str) -> list[str]:
    """Names of the containers carrying the run's label, running or not."""
    completed = subprocess.run(  # noqa: S603  (fixed argument list)
        ["docker", "ps", "-a", "--filter", f"label={RUN_LABEL}={run_id}", "--format", "{{.Names}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.split()


def running_of(run_id: str, role: str) -> list[str]:
    completed = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "ps",
            "--filter",
            f"label={RUN_LABEL}={run_id}",
            "--filter",
            f"label=io.fieldproof.role={role}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.split()


def record_of(runs_dir: Path, run_id: str) -> dict[str, Any]:
    return dict(json.loads((runs_dir / run_id / "run.json").read_text()))


def print_run_logs(run_dir: Path, tail: int = 120) -> None:
    """The run's engine log and the containers' logs, for a failure in CI."""
    for path in sorted(run_dir.glob("*.log")) + sorted(run_dir.glob("logs/*.log")):
        print(f"\n===== {path.relative_to(run_dir)} (last {tail} lines) =====", flush=True)  # noqa: T201
        print("\n".join(path.read_text().splitlines()[-tail:]), flush=True)  # noqa: T201


@pytest.mark.integration
def test_01_fieldproof_run_passes(tmp_path: Path) -> None:
    run_id = f"it-run-{os.getpid()}"
    command = [*FIELDPROOF, "run", str(SCENARIO), "--runs-dir", str(tmp_path), "--run-id", run_id]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603
        command, env=ENV, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
    )
    print(completed.stdout[-6000:], flush=True)  # noqa: T201
    print(f"fieldproof run took {time.monotonic() - started:.0f} s wall-clock", flush=True)  # noqa: T201
    run_dir = tmp_path / run_id
    if completed.returncode != 0:
        print_run_logs(run_dir)
    assert completed.returncode == 0, completed.stderr[-4000:]
    assert containers_of(run_id) == [], "the run left containers behind"
    assert (run_dir / "scenario.yaml").read_text() == SCENARIO.read_text()
    assert (run_dir / "engine.log").is_file()
    assert sorted(p.name for p in (run_dir / "logs").iterdir()) == [
        "bridge-ugv1.log",
        "sim.log",
        "vehicle-ugv1.log",
    ]
    record = record_of(tmp_path, run_id)
    assert record["status"] == "passed", record["reason"]
    assert record["phase"] == "finished"
    assert [p["name"] for p in record["phases"]] == [
        "preparing",
        "sim",
        "spawn",
        "bridges",
        "vehicles",
        "running",
        "teardown",
        "finished",
    ]
    (action,) = record["timeline"]
    assert action["status"] == "ok"
    assert action["detail"] == "goal SUCCEEDED"
    assert record["assertions"][0]["status"] == "skipped"
    events = [
        json.loads(line)["event"] for line in (run_dir / "engine.log").read_text().splitlines()
    ]
    assert events[0] == "run.start"
    assert events[-1] == "run.end"
    assert "vehicle.ready" in events
    assert "action.end" in events
    assert "teardown.done" in events
    for name in ("sim.log", "bridge-ugv1.log", "vehicle-ugv1.log"):
        assert (run_dir / "logs" / name).stat().st_size > 0, f"{name} is empty"
    assert "bt_navigator" in (run_dir / "logs" / "vehicle-ugv1.log").read_text()


@pytest.mark.integration
def test_02_interrupted_run_leaves_nothing_behind(tmp_path: Path) -> None:
    run_id = f"it-interrupt-{os.getpid()}"
    command = [
        *FIELDPROOF,
        "run",
        str(SCENARIO),
        "--runs-dir",
        str(tmp_path),
        "--run-id",
        run_id,
        "-q",
    ]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    with subprocess.Popen(  # noqa: S603
        command, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ) as process:
        deadline = time.monotonic() + START_TIMEOUT_S
        while not running_of(run_id, "sim"):
            if process.poll() is not None:
                early = process.stdout.read() if process.stdout else ""
                pytest.fail(f"fieldproof run exited {process.returncode} before starting:\n{early}")
            if time.monotonic() > deadline:
                process.send_signal(signal.SIGINT)
                pytest.fail("the simulator container never appeared")
            time.sleep(1.0)
        time.sleep(2.0)
        process.send_signal(signal.SIGINT)
        try:
            output, _ = process.communicate(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            pytest.fail("fieldproof run did not exit after SIGINT")
    print(output[-4000:], flush=True)  # noqa: T201
    assert process.returncode == 130, output[-2000:]
    assert containers_of(run_id) == [], "the interrupted run left containers behind"
    record = record_of(tmp_path, run_id)
    assert record["status"] == "interrupted"
    assert record["phase"] == "finished"
    assert "interrupted" in output
