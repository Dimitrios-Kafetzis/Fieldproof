"""Integration test of the field log replay source (task P2-T07, its verification).

``fieldproof run examples/scenarios/ugv_replay.yaml`` replays the synthetic UGV field log
(``examples/logs/ugv_dock_drive.mcap``, written by ``fieldproof log synth`` if it is missing:
a hall with crates, a driven rectangle, drifting wheel odometry, exact ground truth) into the
example vehicle ``ugv_nav2`` with no simulator and no bridge: a player container publishes
``/clock`` from the log's timestamps and plays ``/scan``, ``/odom``, ``/tf_static`` and the
``odom -> base_link`` transforms of ``/tf`` into the stack, which comes up on the moving clock
and localises by SLAM Toolbox; the observer takes the ground truth from the log's
``/ground_truth/pose`` instead of Gazebo. The definition of done: the replay scenario of the
UGV log passes with its ``localisation_error`` assertion (SLAM's ``pose`` against the ground
truth, aligned at the first estimate), the other assertions too (the replayed lidar's rate,
SLAM active throughout, the odometry fresh against the player's clock, the budget held, no
actuator command); the record says where the inputs came from, when the playback started and
ended on the timeline, the ``localisation`` KPIs hold the odometry's drift and SLAM's error,
the MCAP recording holds the replayed inputs and the stack's outputs, the player's status
file tells its story, no simulator or bridge container ran, and nothing is left behind.

Needs the stack images (``make images``, for the bridge image the player and the recorder run
in) and the vehicle image (``make vehicle-image``); no Fuel model. A loaded CI runner replays
in real time like any host, since nothing is rendered.
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
SCENARIO = REPO / "examples/scenarios/ugv_replay.yaml"
LOG = REPO / "examples/logs/ugv_dock_drive.mcap"
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
RUN_TIMEOUT_S = 1200.0
ENV = {**os.environ, "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0")}
ASSERTIONS = {"localised", "lidar_streams", "slam_active", "odom_fresh", "stack_fits"}


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
        summary = make_reader(file).get_summary()
        assert summary is not None
        return {
            channel.topic: summary.statistics.channel_message_counts.get(channel.id, 0)
            if summary.statistics
            else 0
            for channel in summary.channels.values()
        }


def fieldproof(*args: str, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    command = [*FIELDPROOF, *args]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    return subprocess.run(  # noqa: S603
        command, env=ENV, capture_output=True, text=True, timeout=timeout, check=False
    )


@pytest.fixture(scope="module")
def synthetic_log() -> Path:
    """The example's log, written if the checkout has none yet (`make replay-log`)."""
    if not LOG.is_file():
        completed = fieldproof("log", "synth", str(LOG))
        assert completed.returncode == 0, completed.stderr[-2000:]
    return LOG


@pytest.mark.integration
def test_01_replay_scenario_passes(synthetic_log: Path, tmp_path: Path) -> None:  # noqa: PLR0915
    info = fieldproof("log", "info", str(synthetic_log), "--json")
    assert info.returncode == 0, info.stderr[-2000:]
    topics = json.loads(info.stdout)["topics"]
    assert set(topics) == {"/scan", "/odom", "/tf", "/tf_static", "/ground_truth/pose"}

    run_id = f"it-replay-{os.getpid()}"
    started = time.monotonic()
    completed = fieldproof(
        "run", str(SCENARIO), "--runs-dir", str(tmp_path), "--run-id", run_id, timeout=RUN_TIMEOUT_S
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
    assert containers_of(run_id) == [], "the run left containers behind"
    record = json.loads((run_dir / "run.json").read_text())
    assert record["status"] == "passed", record["reason"]

    # No simulator, no spawn, no bridge: the player, the recorder and the vehicle.
    assert [p["name"] for p in record["phases"]] == [
        "preparing",
        "replay",
        "recorder",
        "vehicles",
        "running",
        "teardown",
        "finished",
    ]
    assert [c["role"] for c in record["containers"]] == ["player", "recorder", "vehicle"]
    assert sorted(p.name for p in (run_dir / "logs").iterdir()) == [
        "player.log",
        "recorder.log",
        "vehicle-ugv1.log",
    ]
    assert record["stack"]["world"] is None

    # The source: the log, its pace, and when the playback started and ended on the timeline.
    source = record["source"]
    assert source["kind"] == "replay"
    assert source["log"] == str(synthetic_log)
    assert source["rate"] == 1.0
    assert source["lead_in_s"] == 20.0
    assert source["duration_s"] == 120.0
    assert source["remaps"]["/tf"] == "/fieldproof/replay/ugv1/tf"
    assert source["remaps"]["/ground_truth/pose"] == "/fieldproof/replay/ugv1/ground_truth"
    # The playback starts 20 s into the run, before or after the vehicle is ready (Nav2 activates
    # once the log's odometry transform flows; a workstation is ready a few seconds later, a
    # loaded runner may take longer); the log then plays for its 120 s.
    assert source["playing_t_s"] is not None
    assert source["done_t_s"] == pytest.approx(source["playing_t_s"] + 120.0, abs=2.0)
    assert source["played_s"] == pytest.approx(120.0, abs=2.0)
    assert source["playing_t_s"] < 30.0, source

    # Every assertion passed; the localisation error is the definition of done.
    verdicts = {a["name"]: a for a in record["assertions"]}
    assert set(verdicts) == ASSERTIONS
    assert {a["status"] for a in verdicts.values()} == {"passed"}, {
        n: a["reason"] for n, a in verdicts.items() if a["status"] != "passed"
    }
    localised = verdicts["localised"]
    assert localised["type"] == "localisation_error"
    assert localised["evidence"]["samples"] >= 5
    assert localised["evidence"]["alignment"]["mode"] == "first"
    assert localised["metrics"]["max_error_m"]["value"] <= 0.5
    assert localised["metrics"]["p95_error_m"]["value"] <= 0.3
    assert localised["metrics"]["samples"]["value"] >= 5
    print("localisation:", json.dumps(localised["metrics"]), flush=True)  # noqa: T201

    # The KPIs: SLAM's error, the odometry's drift (larger: the scale error and the yaw bias
    # over a 24 m loop) and the rest measured.
    kpis = record["kpis"]
    assert kpis["slam.samples"]["value"] >= 5
    assert kpis["odom_drift.samples"]["value"] >= 10
    assert kpis["odom_drift.max_error_m"]["value"] > kpis["slam.max_error_m"]["value"]
    print("odometry drift:", kpis["odom_drift.max_error_m"], flush=True)  # noqa: T201
    assert kpis["scan_age.p95_ms"]["value"] < 250.0
    assert verdicts["lidar_streams"]["metrics"]["lowest_hz"]["value"] > 5.0
    assert "stack.peak_cpu_pct" in kpis
    collectors = {c["name"]: c["status"] for c in record["kpi_collectors"]}
    assert collectors == {
        "commanded": "measured",
        "odom_drift": "measured",
        "slam": "measured",
        "scan_age": "measured",
        "stack": "measured",
    }
    # Nothing commanded the actuators: no goal, so no cmd_vel at all.
    assert kpis["commanded.samples"]["value"] == 0
    assert kpis["commanded.count"]["value"] == 0

    # The clock: the player's, at the log's pace; nothing was paused.
    clock = record["clock"]
    assert clock["mode"] == "sim"
    assert clock["target_rtf"] == 1.0
    assert clock["rtf"]["windows"] > 30
    assert 0.9 < clock["rtf"]["mean"] < 1.1, clock
    assert clock["paused_s"] == 0.0

    # The engine log tells the story: the source, the player ready, playing, done, no simulator.
    events = [e["event"] for e in log_events(run_dir)]
    for event in ("source.replay", "player.start", "replay.ready", "replay.playing", "replay.done"):
        assert event in events, event
    assert events.index("replay.done") < events.index("timeline.done")
    if source["playing_t_s"] < 0:
        assert "replay.missed" in events
    else:
        assert "replay.missed" not in events
    assert "sim.start" not in events
    assert "bridge.start" not in events
    assert "spawn.start" not in events
    assert "recording.saved" in events

    # The player's own account, in the run directory.
    status = [json.loads(line) for line in (run_dir / "replay.jsonl").read_text().splitlines()]
    assert [s["event"] for s in status][:3] == ["started", "ready", "playing"]
    assert any(s["event"] == "done" and s["exit_code"] == 0 for s in status)
    assert next(s for s in status if s["event"] == "playing")["late_s"] < 0.5
    assert (run_dir / "replay.json").is_file()
    player_log = (run_dir / "logs" / "player.log").read_text()
    assert '"event": "playing"' in player_log

    # The recording: the replayed inputs under the vehicle's topics, SLAM's outputs, the
    # ground truth kept hidden, the side channels.
    mcaps = sorted((run_dir / "recording").glob("*.mcap"))
    assert mcaps, "no MCAP in the run directory"
    counts = mcap_topics(mcaps[0])
    assert counts["/scan"] > 500
    assert counts["/odom"] > 1000
    assert counts["/tf_static"] >= 1
    assert counts["/pose"] >= 5
    assert counts["/map"] >= 1
    assert "/fieldproof/replay/ugv1/ground_truth" not in counts
    assert "/fieldproof/replay/ugv1/tf" not in counts
    for channel in ("/fieldproof/events", "/fieldproof/assertions", "/fieldproof/kpis"):
        assert channel in counts, channel

    # The reports carry the source.
    report = (run_dir / "report.md").read_text()
    assert "- Source: replay of" in report
    assert "played by" in report
    assert "`localised` | `localisation_error` |" in report
    assert "Source: replay of" in (run_dir / "report.html").read_text()
    assert "PASS ugv_replay" in completed.stdout


@pytest.mark.integration
def test_02_replay_refuses_the_simulator_clock(synthetic_log: Path, tmp_path: Path) -> None:
    """`fieldproof clock` has nothing to act on, and a mapping to a missing topic is refused."""
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "r", "stack": {"world": None}, "source": {"kind": "replay"}})
    )
    completed = fieldproof("clock", str(run_dir), "status")
    assert completed.returncode == 2
    assert "a replay run has no simulator clock" in completed.stderr + completed.stdout
    completed = fieldproof("validate", str(SCENARIO), "--set", "source.start_s=500")
    assert completed.returncode == 2
    assert "source.start_s: 500 s is past the end of the log" in completed.stderr + completed.stdout
    completed = fieldproof(
        "validate", str(SCENARIO), "--set", "source.vehicles.ugv1.ground_truth.topic=/tf"
    )
    assert completed.returncode == 2
    assert "is a tf2_msgs/msg/TFMessage, not a pose" in completed.stderr + completed.stdout
