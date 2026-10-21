"""Integration test of the event injection framework (task P2-T03, session 1): the timeline DSL
and the engine-side events against the real stack.

``fieldproof run examples/scenarios/ugv_events.yaml`` drives the example vehicle 4 m ahead from
the loading dock while the timeline exercises every trigger and every engine-side event:

- ``say_hello`` (``example.announce``, the example plugin): ``at: 2s``, ``every: 5s``,
  ``count: 3``; three occurrences published a ``std_msgs/msg/String`` on ``/announce`` from
  the vehicle's container, which the recording holds;
- ``tighten_failsafe`` (``param.set``): ``when`` the vehicle's ground truth is more than 1 m
  from its spawn, ``ros2 param set`` on the stack's ``link_monitor`` succeeded;
- ``freeze_stack`` (``container.pause``): ``after`` the drive and the parameter change, 1 s
  later, the container was paused for 3 s and resumed (Docker refuses to pause or resume a
  container in the wrong state, so the backend's lines in the log are the evidence);
- ``stop_stack`` (``container.kill``): ``after`` the pause, the container was killed and the
  run went on to its verdict without treating the exit as an error.

The checks: exit code 0 and status ``passed``; every entry ``ok`` in ``run.json`` with its
trigger, occurrence and parameters; the seven assertions ``passed``; ``engine.log`` carries an
``action.start`` and an ``action.end`` line per occurrence, ``action.triggered`` lines for
the three triggered entries and the ``container.paused``, ``container.resumed`` and
``container.killed`` lines in that order; the MCAP recording holds ``/announce`` (three
messages) and ``/fieldproof/events``; the vehicle container's log is kept although it was
killed; nothing of the run is left behind.

Needs the stack images (``make images``), the vehicle image (``make vehicle-image``) and the
Fuel models (``make assets``), like ``test_run.py``. Wall-clock limits are generous for a CI
runner under software rendering.
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

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "examples/scenarios/ugv_events.yaml"
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
RUN_TIMEOUT_S = 1500.0
ENV = {**os.environ, "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0")}
ENTRIES = ("drive_ahead", "say_hello", "tighten_failsafe", "freeze_stack", "stop_stack")


def docker(*args: str) -> str:
    completed = subprocess.run(  # noqa: S603  (fixed argument list)
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return completed.stdout


def containers_of(run_id: str) -> list[str]:
    return docker(
        "ps", "-a", "--filter", f"label={RUN_LABEL}={run_id}", "--format", "{{.Names}}"
    ).split()


def print_run_logs(run_dir: Path, tail: int = 120) -> None:
    for path in sorted(run_dir.glob("*.log")) + sorted(run_dir.glob("logs/*.log")):
        print(f"\n===== {path.relative_to(run_dir)} (last {tail} lines) =====", flush=True)  # noqa: T201
        print("\n".join(path.read_text().splitlines()[-tail:]), flush=True)  # noqa: T201


def log_events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run_dir / "engine.log").read_text().splitlines()]


def mcap_topics(path: Path) -> dict[str, int]:
    from mcap.reader import make_reader  # noqa: PLC0415

    with path.open("rb") as file:
        reader = make_reader(file)
        summary = reader.get_summary()
        assert summary is not None
        return {
            channel.topic: summary.statistics.channel_message_counts.get(channel.id, 0)
            if summary.statistics
            else 0
            for channel in summary.channels.values()
        }


@pytest.mark.integration
def test_01_events_scenario(tmp_path: Path) -> None:  # noqa: PLR0915
    run_id = f"it-events-{os.getpid()}"
    command = [*FIELDPROOF, "run", str(SCENARIO), "--runs-dir", str(tmp_path), "--run-id", run_id]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603
        command, env=ENV, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
    )
    elapsed = time.monotonic() - started
    run_dir = tmp_path / run_id
    print(f"exit {completed.returncode} after {elapsed:.0f} s", flush=True)  # noqa: T201
    print(completed.stdout[-6000:], flush=True)  # noqa: T201
    if completed.returncode != 0:
        print(completed.stderr[-4000:], flush=True)  # noqa: T201
        if run_dir.is_dir():
            print_run_logs(run_dir)
    assert completed.returncode == 0, completed.stderr[-2000:]
    record = json.loads((run_dir / "run.json").read_text())
    assert record["status"] == "passed", record["reason"]

    # Every entry ran ok, with its trigger, occurrence and parameters in the record.
    timeline = {a["name"]: a for a in record["timeline"]}
    assert set(timeline) == {*ENTRIES, "say_hello#2", "say_hello#3"}
    assert {a["status"] for a in record["timeline"]} == {"ok"}
    assert timeline["say_hello"]["trigger"] == {"at_s": 2.0, "every_s": 5.0, "count": 3}
    assert timeline["say_hello#3"]["occurrence"] == 3
    assert timeline["say_hello"]["detail"].startswith("published 'fieldproof says hello'")
    assert timeline["tighten_failsafe"]["trigger"]["when"]["metric"] == "distance_to"
    assert timeline["tighten_failsafe"]["detail"] == "/link_monitor loss_timeout_s = 5.0"
    assert timeline["tighten_failsafe"]["params"]["parameter"] == "loss_timeout_s"
    assert timeline["freeze_stack"]["trigger"] == {
        "at_s": 1.0,
        "after": ["drive_ahead", "tighten_failsafe"],
    }
    tighten_end = timeline["tighten_failsafe"]["t_s"] + timeline["tighten_failsafe"]["duration_s"]
    assert timeline["freeze_stack"]["t_s"] >= tighten_end + 1.0
    assert timeline["freeze_stack"]["detail"] == "paused for 3 s, resumed"
    drive_t, drive_s = timeline["drive_ahead"]["t_s"], timeline["drive_ahead"]["duration_s"]
    assert timeline["freeze_stack"]["t_s"] >= drive_t + drive_s + 1.0
    assert timeline["stop_stack"]["detail"] == "SIGKILL sent"
    assert timeline["stop_stack"]["t_s"] >= timeline["freeze_stack"]["t_s"] + 3.0
    # The timeline waited for the condition, which held once the vehicle had moved.
    assert timeline["tighten_failsafe"]["t_s"] > 0.0

    # The seven assertions passed.
    verdicts = {a["name"]: a["status"] for a in record["assertions"]}
    assert verdicts == {
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "said_hello": "passed",
        "failsafe_tightened": "passed",
        "stack_frozen": "passed",
        "stack_stopped": "passed",
        "no_collision": "passed",
    }

    # The container was paused, resumed and killed (Docker refuses each on a container in the
    # wrong state, so the backend's lines are the evidence); its log was kept anyway.
    assert (run_dir / "logs" / "vehicle-ugv1.log").is_file()

    # The engine log carries the event records (and so does /fieldproof/events).
    events = log_events(run_dir)
    starts = [e for e in events if e["event"] == "action.start"]
    ends = [e for e in events if e["event"] == "action.end"]
    assert sorted(e["action"] for e in starts) == sorted(timeline)
    assert {e["action"]: e["status"] for e in ends} == dict.fromkeys(timeline, "ok")
    assert {e["event_action"] for e in starts} == {
        "navigate_to_pose",
        "example.announce",
        "param.set",
        "container.pause",
        "container.kill",
    }
    triggered = [e["action"] for e in events if e["event"] == "action.triggered"]
    assert triggered == ["tighten_failsafe", "freeze_stack", "stop_stack"]
    assert [e["event"] for e in events if e["event"].startswith("container.")] == [
        "container.paused",
        "container.resumed",
        "container.killed",
    ]

    # The recording holds the three announcements and the event lines.
    mcaps = sorted((run_dir / "recording").glob("*.mcap"))
    assert mcaps, "no MCAP recording"
    counts = mcap_topics(mcaps[0])
    assert "/fieldproof/events" in counts
    assert counts.get("/announce", 0) == 3, counts
    assert containers_of(run_id) == []
