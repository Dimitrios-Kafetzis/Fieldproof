"""Integration test of ``fieldproof run`` (tasks P1-T05 and P1-T06, their verifications).

Runs after ``test_ugv_nav2.py`` (pytest orders the files by name) against the same images:

1. ``fieldproof run examples/scenarios/ugv_nav_to_goal.yaml`` brings the simulator, the bridge,
   the recorder and the example vehicle up through the Docker API, waits for the readiness
   gates, sends the timeline's ``NavigateToPose`` goal while the observer streams the ground
   truth and the topics, and tears everything down: exit code 0, the run directory holds the
   scenario, ``run.json`` with status ``passed``, the goal ``SUCCEEDED`` and the five assertions
   ``passed`` with evidence, the observer's stream, an MCAP recording that the ``mcap`` library
   reads back with the recorded topics and the three ``/fieldproof/*`` side channels, the
   reports (the JUnit document of task P1-T07 included), ``engine.log`` and the four
   containers' logs, the video of the drive from the depot's spectator camera, and no
   container of the run is left
   (the P1-T06 definition of done: "the UGV scenario reports PASS with per-assertion evidence",
   "MCAP opens in Foxglove Studio" by way of a valid MCAP with ROS 2 schemas);
2. a second run is interrupted with SIGINT while the stack is coming up (as soon as its
   simulator container exists): exit code 130, ``run.json`` says ``interrupted``, and no
   container of the run is left behind ("a manual interrupted run leaves no containers
   behind", automated);
3. ``examples/scenarios/ugv_nav_fails_on_time.yaml``, the same run with one second for the
   drive: exit code 1, status ``failed`` naming ``arrived_in_time``, the other assertions
   passed, no recording ("forcing a short timeout yields FAIL with the right assertion named").

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
import re
import shlex
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "examples/scenarios/ugv_nav_to_goal.yaml"
FAILING_SCENARIO = REPO / "examples/scenarios/ugv_nav_fails_on_time.yaml"
SIDE_CHANNELS = {"/fieldproof/events", "/fieldproof/assertions", "/fieldproof/metrics"}
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
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


def plain(text: str) -> str:
    """The output without the colour codes Rich emits on a CI runner."""
    return ANSI.sub("", text)


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
        "recorder.log",
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
        "recorder",
        "vehicles",
        "running",
        "teardown",
        "finished",
    ]
    (action,) = record["timeline"]
    assert action["status"] == "ok"
    assert action["detail"] == "goal SUCCEEDED"
    check_assertions(record)
    check_observer_files(run_dir, record)
    mcaps = check_recording(run_dir)
    check_reports(run_dir, mcaps)
    check_video(run_dir, record)
    assert "PASS ugv_nav_to_goal" in plain(completed.stdout)
    assert f"video: {run_dir / 'video' / 'spectator.mp4'}" in plain(completed.stdout)
    events = [
        json.loads(line)["event"] for line in (run_dir / "engine.log").read_text().splitlines()
    ]
    assert events[0] == "run.start"
    assert events[-1] == "run.end"
    assert "vehicle.ready" in events
    assert "recorder.ready" in events
    assert "action.end" in events
    assert events.count("assertion.passed") == 5
    assert "recording.saved" in events
    assert "report.written" in events
    assert "teardown.done" in events
    for name in ("sim.log", "bridge-ugv1.log", "recorder.log", "vehicle-ugv1.log"):
        assert (run_dir / "logs" / name).stat().st_size > 0, f"{name} is empty"
    assert "bt_navigator" in (run_dir / "logs" / "vehicle-ugv1.log").read_text()
    assert "Recording stopped" in (run_dir / "logs" / "recorder.log").read_text()


def check_assertions(record: dict[str, Any]) -> None:
    """The five assertions passed, each with its evidence."""
    verdicts = {a["name"]: a for a in record["assertions"]}
    assert {n: a["status"] for n, a in verdicts.items()} == {
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "lidar_streams": "passed",
        "no_collision": "passed",
        "speed_limit": "passed",
    }, {n: a["reason"] for n, a in verdicts.items()}
    for name, assertion in verdicts.items():
        print(f"{name}: {assertion['reason']}", flush=True)  # noqa: T201
        assert assertion["t_s"] is not None, name
    reached = verdicts["reached_goal"]["evidence"]
    assert reached["reached"]["distance_m"] <= 0.5
    assert reached["final"]["x"] == pytest.approx(10.0, abs=0.6)
    assert reached["samples"] > 10, "ground-truth poses streamed during the drive"
    assert verdicts["arrived_in_time"]["evidence"]["duration_s"] <= 240.0
    assert verdicts["lidar_streams"]["evidence"]["lowest_hz"] >= 2.0
    assert verdicts["lidar_streams"]["evidence"]["windows"] >= 1
    assert verdicts["no_collision"]["evidence"]["messages"] == 0
    assert verdicts["speed_limit"]["evidence"]["samples"] > 10
    assert verdicts["speed_limit"]["evidence"]["highest"] <= 1.5
    assert record["observation"]["samples"] > 50
    assert record["observation"]["observer_errors"] == 0
    assert record["observation"]["sim_elapsed_s"] > 0


def check_observer_files(run_dir: Path, record: dict[str, Any]) -> None:
    """The observer's plan and stream, the verdicts file."""
    observe = json.loads((run_dir / "observe.json").read_text())
    assert observe["record"]["enabled"] is True
    kinds = {
        json.loads(line)["kind"] for line in (run_dir / "samples.jsonl").read_text().splitlines()
    }
    assert {"ready", "pose", "rate", "value", "clock", "stopped"} <= kinds
    verdict_lines = [
        json.loads(line) for line in (run_dir / "assertions.jsonl").read_text().splitlines()
    ]
    assert sorted(v["name"] for v in verdict_lines) == sorted(
        a["name"] for a in record["assertions"]
    )


def check_recording(run_dir: Path) -> list[Path]:
    """One MCAP file, read back: the allow-listed topics that were published and the side
    channels, every channel with a ROS 2 schema (what Foxglove needs)."""
    mcaps = sorted((run_dir / "recording").glob("*.mcap"))
    assert len(mcaps) == 1, list((run_dir / "recording").iterdir())
    topics = mcap_topics(mcaps[0])
    print(f"recorded {mcaps[0].stat().st_size} bytes, topics: {topics}", flush=True)  # noqa: T201
    assert {"/clock", "/odom", "/scan", "/tf", "/cmd_vel"} <= set(topics), topics
    assert set(topics) >= SIDE_CHANNELS, topics
    assert topics["/fieldproof/assertions"] == 5, "one message per verdict"
    assert topics["/fieldproof/metrics"] >= 5, "once a second while the stack ran"
    assert topics["/fieldproof/events"] >= 20, "the engine's log lines"
    assert topics["/odom"] > 100
    return mcaps


def check_reports(run_dir: Path, mcaps: list[Path]) -> None:
    """The reports name the verdict and the recording."""
    report = json.loads((run_dir / "report.json").read_text())
    assert report["verdict"] == "PASS"
    assert report["exit_code"] == 0
    assert report["assertion_counts"]["passed"] == 5
    assert report["recording"]["mcap"] == [f"recording/{mcaps[0].name}"]
    markdown = (run_dir / "report.md").read_text()
    assert markdown.startswith("# Fieldproof run report: ugv_nav_to_goal")
    assert "**PASS**" in markdown
    assert "| `reached_goal` | `reach_pose` | PASS |" in markdown
    # The JUnit document (task P1-T07): five assertions, the drive and the run, nothing failed.
    suite = ET.parse(run_dir / "junit.xml").getroot().find("testsuite")  # noqa: S314 (our own document)
    assert suite is not None
    assert suite.get("name") == "ugv_nav_to_goal"
    assert (
        suite.get("tests"),
        suite.get("failures"),
        suite.get("errors"),
        suite.get("skipped"),
    ) == (
        "7",
        "0",
        "0",
        "0",
    )
    assert {p.get("name"): p.get("value") for p in suite.find("properties")}[  # type: ignore[union-attr]
        "recording"
    ] == f"recording/{mcaps[0].name}"


def check_video(run_dir: Path, record: dict[str, Any]) -> None:
    """The video from the depot's spectator camera (issue #46): an MP4 file of the drive,
    written by Gazebo's CameraVideoRecorder and copied out of the simulator."""
    video = run_dir / "video" / "spectator.mp4"
    assert video.is_file(), list(run_dir.iterdir())
    size = video.stat().st_size
    print(f"video: {size} bytes", flush=True)  # noqa: T201
    assert size > 100_000, "a few seconds of 720p video"
    assert video.read_bytes()[4:8] == b"ftyp", "an ISO base media file"
    assert record["video"] == {
        "camera": "spectator",
        "file": "video/spectator.mp4",
        "bytes": size,
        "status": "saved",
    }
    report = json.loads((run_dir / "report.json").read_text())
    assert report["video"]["status"] == "saved"
    assert "- Video from camera `spectator`" in (run_dir / "report.md").read_text()


def mcap_topics(path: Path) -> dict[str, int]:
    """Topic to message count of an MCAP file; every channel must carry a ROS 2 schema."""
    from mcap.reader import make_reader  # noqa: PLC0415  (a development dependency of the engine)

    with path.open("rb") as file:
        reader = make_reader(file)
        assert reader.get_header().profile == "ros2"
        summary = reader.get_summary()
        assert summary is not None
        assert summary.statistics is not None
        counts = summary.statistics.channel_message_counts
        topics: dict[str, int] = {}
        for channel_id, channel in summary.channels.items():
            schema = summary.schemas[channel.schema_id]
            assert schema.encoding == "ros2msg", (channel.topic, schema.encoding)
            assert schema.name, channel.topic
            topics[channel.topic] = counts.get(channel_id, 0)
    return topics


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


@pytest.mark.integration
def test_03_failing_scenario_names_the_assertion(tmp_path: Path) -> None:
    run_id = f"it-fail-{os.getpid()}"
    command = [
        *FIELDPROOF,
        "run",
        str(FAILING_SCENARIO),
        "--runs-dir",
        str(tmp_path),
        "--run-id",
        run_id,
        "-q",
    ]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    completed = subprocess.run(  # noqa: S603
        command, env=ENV, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
    )
    print(completed.stdout[-4000:], flush=True)  # noqa: T201
    run_dir = tmp_path / run_id
    if completed.returncode != 1:
        print_run_logs(run_dir)
    assert completed.returncode == 1, completed.stderr[-4000:]
    assert containers_of(run_id) == [], "the failed run left containers behind"
    record = record_of(tmp_path, run_id)
    assert record["status"] == "failed"
    assert record["reason"] is not None
    assert record["reason"].startswith(
        "assertion arrived_in_time failed: drive_ahead has not completed 1 s after its start"
    ), record["reason"]
    verdicts = {a["name"]: a["status"] for a in record["assertions"]}
    assert verdicts == {
        "reached_goal": "passed",
        "arrived_in_time": "failed",
        "no_collision": "passed",
    }, record["assertions"]
    (action,) = record["timeline"]
    assert action["status"] == "ok", "the drive itself still succeeded"
    assert not (run_dir / "recording").exists(), "outputs.record is off in this scenario"
    report = json.loads((run_dir / "report.json").read_text())
    assert report["verdict"] == "FAIL"
    assert report["exit_code"] == 1
    assert report["recording"] is None
    assert "| `arrived_in_time` | `within_time` | FAIL |" in (run_dir / "report.md").read_text()
    suite = ET.parse(run_dir / "junit.xml").getroot().find("testsuite")  # noqa: S314 (our own document)
    assert suite is not None
    assert suite.get("failures") == "2", "the assertion and the run"
    assert suite.find("testcase[@name='arrived_in_time']/failure") is not None
    assert "FAIL ugv_nav_fails_on_time" in plain(completed.stdout)
    assert "FAIL arrived_in_time (within_time)" in plain(completed.stdout)
