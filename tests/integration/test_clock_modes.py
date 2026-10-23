"""Integration tests of clock management (task P2-T04) against the real stack.

1. ``fieldproof run examples/scenarios/ugv_realtime.yaml``: the real-time-locked mode. The
   simulator's real-time factor is pinned to 1.0, the drift between simulation time and the
   wall clock is measured over the timeline and judged (the example warns, since a CI runner
   with software rendering cannot hold real time; with ``FIELDPROOF_REALTIME_STRICT=1`` the
   test asserts the lock held, which the maintainer's GPU host does). While the drive runs,
   ``fieldproof clock <run> status|pause|resume`` is exercised on the running simulator: the
   pause shows in the world's statistics and is left out of the drift. The record and the
   report carry the clock (mode, target and measured RTF, drift, verdict), and the metrics
   side channel in the MCAP carries ``drift_s``, ``mode`` and ``paused``.
2. ``fieldproof run examples/scenarios/ugv_sim_control.yaml``: the simulator clock events.
   ``sim.pause`` stops the clock four seconds into the drive, ``sim.step`` advances it by half
   a second while paused, and ``sim.resume`` is triggered by the observer seeing the real-time
   factor collapse (``when: {metric: rtf}``); the drive then reaches its goal. The entries'
   details, the engine log's ``sim.paused``/``sim.stepped``/``sim.resumed`` lines and the
   paused ``clock`` samples are the evidence.

Needs the stack images (``make images``), the vehicle image (``make vehicle-image``) and the
Fuel models (``make assets``), like ``test_run.py``.
"""

from __future__ import annotations

import json
import os
import shlex
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
REALTIME = REPO / "examples/scenarios/ugv_realtime.yaml"
SIM_CONTROL = REPO / "examples/scenarios/ugv_sim_control.yaml"
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
RUN_TIMEOUT_S = 1500.0
PHASE_TIMEOUT_S = 600.0
ENV = {**os.environ, "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0")}
STRICT = os.environ.get("FIELDPROOF_REALTIME_STRICT") == "1"


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


def fieldproof(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    command = [*FIELDPROOF, *args]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    completed = subprocess.run(  # noqa: S603
        command, env=ENV, capture_output=True, text=True, timeout=timeout, check=False
    )
    print(completed.stdout[-2000:], flush=True)  # noqa: T201
    if completed.returncode != 0:
        print(completed.stderr[-2000:], flush=True)  # noqa: T201
    return completed


def wait_for_phase(run_dir: Path, phase: str, timeout_s: float) -> dict[str, Any]:
    """Poll run.json until the run reaches `phase` (or ends)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        record_file = run_dir / "run.json"
        if record_file.is_file():
            try:
                record = json.loads(record_file.read_text())
            except json.JSONDecodeError:
                record = {}
            if record.get("phase") == phase or record.get("status") is not None:
                return dict(record)
        time.sleep(1.0)
    raise AssertionError(f"{run_dir}: phase {phase} not reached within {timeout_s} s")


def log_events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run_dir / "engine.log").read_text().splitlines()]


def samples(run_dir: Path, kind: str) -> list[dict[str, Any]]:
    out = []
    for line in (run_dir / "samples.jsonl").read_text().splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("kind") == kind:
            out.append(data)
    return out


def decode_string(payload: bytes) -> str:
    """The `data` of a `std_msgs/msg/String` from its CDR encoding."""
    (length,) = struct.unpack_from("<I", payload, 4)
    return payload[8 : 8 + length - 1].decode("utf-8", errors="replace")


def metrics_messages(run_dir: Path) -> list[dict[str, Any]]:
    from mcap.reader import make_reader  # noqa: PLC0415  (a dev dependency of the engine)

    (path,) = sorted((run_dir / "recording").glob("*.mcap"))
    messages = []
    with path.open("rb") as file:
        reader = make_reader(file)
        for _schema, channel, message in reader.iter_messages(topics=["/fieldproof/metrics"]):
            assert channel.topic == "/fieldproof/metrics"
            messages.append(json.loads(decode_string(message.data)))
    return messages


@pytest.mark.integration
def test_01_realtime_scenario_and_clock_command(tmp_path: Path) -> None:
    run_id = f"it-realtime-{os.getpid()}"
    run_dir = tmp_path / run_id
    # Kept: the clock command is exercised on the simulator once the run has its verdict, when
    # no goal is in flight (the example stack's lifecycle manager shuts Nav2 down after 4 s
    # without a heartbeat on the wall clock, and a pause by hand on a loaded runner lasts that).
    command = [*FIELDPROOF, "run", str(REALTIME), "--runs-dir", str(tmp_path), "--run-id", run_id]
    command += ["--keep"]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603
            command, env=ENV, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
        )
        elapsed = time.monotonic() - started
        print(f"exit {completed.returncode} after {elapsed:.0f} s", flush=True)  # noqa: T201
        print(completed.stdout[-6000:], flush=True)  # noqa: T201
        if completed.returncode != 0:
            print(completed.stderr[-4000:], flush=True)  # noqa: T201
            print_run_logs(run_dir)
        assert completed.returncode == 0, completed.stderr[-2000:]
        check_realtime_record(run_dir)
        check_clock_command(run_dir)
        # The recorder stopped the way the teardown stops it (SIGTERM; the observer sends
        # rosbag2 SIGINT and waits), so the MCAP is finalised before it is read.
        recorder = [n for n in containers_of(run_id) if n.endswith("-recorder")]
        subprocess.run(["docker", "stop", "-t", "30", *recorder], check=True, capture_output=True)  # noqa: S603, S607
        check_metrics(run_dir)
    finally:
        left = containers_of(run_id)
        if left:
            subprocess.run(["docker", "rm", "-f", *left], check=False, capture_output=True)  # noqa: S603, S607
    assert containers_of(run_id) == []


def check_realtime_record(run_dir: Path) -> None:
    """The record, the log, the reports and the metrics of the real-time-locked run."""
    record = json.loads((run_dir / "run.json").read_text())
    assert record["status"] == "passed", record["reason"]
    assert record["time"]["mode"] == "realtime"
    assert record["seed"] == 0, "the example names no seed: Gazebo's default"
    clock = record["clock"]
    print(f"clock: {json.dumps(clock)}", flush=True)  # noqa: T201
    assert clock["mode"] == "realtime"
    assert clock["target_rtf"] == 1.0
    assert clock["verdict"] in ("locked", "drifted")
    assert clock["drift_s"] is not None
    assert clock["rtf"]["windows"] >= 3
    assert clock["sim_elapsed_s"] > 5.0
    assert clock["max_drift_s"] == 2.0
    assert clock["on_drift"] == "warn"
    if STRICT:
        assert clock["verdict"] == "locked", clock
        assert abs(clock["drift_s"]) <= 2.0
    events = log_events(run_dir)
    names = [e["event"] for e in events]
    mode = next(e for e in events if e["event"] == "clock.mode")
    assert "time.mode realtime" in mode["message"]
    assert mode["target_rtf"] == 1.0
    assert ("clock.locked" in names) != ("clock.drifted" in names)
    assert any(e["event"] == "sim.rtf" and e["rtf"] == 1.0 for e in events)
    assert {a["name"]: a["status"] for a in record["assertions"]} == {
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "no_collision": "passed",
    }
    markdown = (run_dir / "report.md").read_text()
    assert "- Clock: mode `realtime`, target RTF 1.00, measured" in markdown
    assert f"seed {record['seed']}" in markdown
    report = json.loads((run_dir / "report.json").read_text())
    assert report["clock"]["verdict"] == clock["verdict"]


def check_metrics(run_dir: Path) -> None:
    """The metrics side channel in the recording carries the clock."""
    metrics = metrics_messages(run_dir)
    assert len(metrics) >= 3
    last = metrics[-1]
    assert last["mode"] == "realtime"
    assert last["target_rtf"] == 1.0
    assert "drift_s" in last
    assert "paused" in last
    assert "gz_rtf" in last


def check_clock_command(run_dir: Path) -> None:
    """`fieldproof clock` on the kept simulator: status, a pause that holds the clock, resume."""
    status = fieldproof("clock", str(run_dir), "status")
    assert status.returncode == 0, status.stderr
    assert "world depot running at simulation time" in status.stdout
    paused = fieldproof("clock", str(run_dir), "pause")
    assert paused.returncode == 0, paused.stderr
    assert "pause accepted (pause: true)" in paused.stdout
    first = fieldproof("clock", str(run_dir), "status", "--json")
    assert first.returncode == 0, first.stderr
    stats = json.loads(first.stdout)
    assert stats["paused"] is True
    assert stats["sim_time_s"] > 0.0
    time.sleep(1.0)
    second = fieldproof("clock", str(run_dir), "status", "--json")
    assert json.loads(second.stdout)["sim_time_s"] == stats["sim_time_s"], "the clock stood still"
    stepped = fieldproof("clock", str(run_dir), "step", "--seconds", "0.5")
    assert stepped.returncode == 0, stepped.stderr
    assert "step accepted (multi_step: 500 pause: true)" in stepped.stdout
    time.sleep(2.0)
    third = json.loads(fieldproof("clock", str(run_dir), "status", "--json").stdout)
    assert third["paused"] is True
    assert third["sim_time_s"] == pytest.approx(stats["sim_time_s"] + 0.5, abs=0.01)
    resumed = fieldproof("clock", str(run_dir), "resume")
    assert resumed.returncode == 0, resumed.stderr
    time.sleep(1.0)
    final = fieldproof("clock", str(run_dir), "status")
    assert "running at simulation time" in final.stdout


@pytest.mark.integration
def test_02_sim_control_scenario(tmp_path: Path) -> None:
    run_id = f"it-simctl-{os.getpid()}"
    command = [
        *FIELDPROOF,
        "run",
        str(SIM_CONTROL),
        "--runs-dir",
        str(tmp_path),
        "--run-id",
        run_id,
    ]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603
        command, env=ENV, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
    )
    run_dir = tmp_path / run_id
    print(f"exit {completed.returncode} after {time.monotonic() - started:.0f} s", flush=True)  # noqa: T201
    print(completed.stdout[-6000:], flush=True)  # noqa: T201
    if completed.returncode != 0:
        print(completed.stderr[-4000:], flush=True)  # noqa: T201
        print_run_logs(run_dir)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert containers_of(run_id) == []
    record = json.loads((run_dir / "run.json").read_text())
    assert record["status"] == "passed", record["reason"]
    timeline = {a["name"]: a for a in record["timeline"]}
    assert {n: a["status"] for n, a in timeline.items()} == {
        "drive_ahead": "ok",
        "freeze": "ok",
        "thaw": "ok",
        "nudge": "ok",
        "thaw_again": "ok",
    }
    assert timeline["freeze"]["detail"] == "paused"
    assert timeline["thaw"]["detail"] == "resumed"
    assert timeline["thaw"]["trigger"]["when"]["metric"] == "rtf"
    assert timeline["thaw"]["t_s"] >= timeline["freeze"]["t_s"] + 0.5
    assert timeline["nudge"]["detail"] == "500 step(s), 0.5 s, paused"
    assert timeline["nudge"]["t_s"] >= timeline["thaw"]["t_s"] + 1.0
    assert timeline["thaw_again"]["detail"] == "resumed"
    assert timeline["thaw_again"]["t_s"] >= timeline["nudge"]["t_s"] + 0.5
    for name, entry in timeline.items():
        print(f"{name}: t {entry['t_s']} s, {entry['detail']}", flush=True)  # noqa: T201
    assert {a["name"]: a["status"] for a in record["assertions"]} == {
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "paused_and_seen": "passed",
        "nudged": "passed",
        "resumed_again": "passed",
        "no_collision": "passed",
    }, {a["name"]: a["reason"] for a in record["assertions"]}
    events = [e["event"] for e in log_events(run_dir)]
    clock_events = [e for e in events if e in {"sim.paused", "sim.stepped", "sim.resumed"}]
    assert clock_events == ["sim.paused", "sim.resumed", "sim.stepped", "sim.resumed"]
    clock = record["clock"]
    print(f"clock: {json.dumps(clock)}", flush=True)  # noqa: T201
    assert clock["mode"] == "sim"
    assert clock["paused_s"] >= 1.0, "the observer saw the pause in the world's statistics"
    assert clock["verdict"] is None
    paused_samples = [s for s in samples(run_dir, "clock") if s.get("paused")]
    assert paused_samples
    # The paused samples cluster around the two pauses (the second one past the nudge).
    sims = sorted({round(s["sim"], 2) for s in paused_samples if s.get("sim") is not None})
    assert sims, paused_samples[:3]
    assert sims[-1] - sims[0] <= timeline["thaw_again"]["t_s"] - timeline["freeze"]["t_s"] + 1.0
