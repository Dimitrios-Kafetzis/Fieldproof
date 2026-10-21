"""Integration tests of the fault injectors (task P2-T03, session 2): every fault event's
parameter against the real bridge, and the GNSS-denial scenario of the definition of done.

1. The simulator, the spawned reference UGV and its bridge (the compose ``sim`` and ``bridge``
   services, no vehicle stack): the bundle runs the injectors (``scan_relay``, ``odom_relay``,
   ``cmd_vel_relay``, ``imu_adapter``, ``gps_adapter``, ``joint_states_throttle``,
   ``battery``), and each fault is injected with ``ros2 param set`` in the bridge container,
   as the events do, and observed on the contract topic with ``ros2 topic echo``:
   ``gps.deny`` (no fix, then the fix back), ``gps.offset`` (the fix moved east by 100 m),
   ``imu.bias`` (the gyro reading biased), ``sensor.dropout`` (the scan silent, then back),
   ``sensor.freeze`` (the odometry stamp frozen, then moving), ``battery.drain`` (the state
   falling to a target), ``actuator.limit`` (a 1 m/s command leaving the relay at 0.2 m/s).
2. ``fieldproof run examples/scenarios/ugv_gps_denied.yaml``: exit code 0, ``deny_gps`` ``ok``
   with the fix denied for 20 s and lifted, both drives ``ok``, the seven assertions passed
   (the fix present, lost through the window, back for the return leg), the ``fault.injected``
   and ``fault.lifted`` lines of the log, nothing left behind.

Needs the stack images (``make images``), the vehicle image (``make vehicle-image``) and the
Fuel models (``make assets``), like the other integration tests.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_ugv_nav2 import COMPOSE, REPO, frame_pose, run, wait_until, world_ready

SCENARIO = REPO / "examples/scenarios/ugv_gps_denied.yaml"
WORLD = "depot"
SPAWN_FRAME = "spawn_dock"
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
BRIDGE_ENTRYPOINT = "/usr/local/bin/fieldproof-bridge-entrypoint"
RUN_TIMEOUT_S = 1500.0
WORLD_TIMEOUT_S = 300.0
SPAWN_TIMEOUT_S = 300.0
BRIDGE_TIMEOUT_S = 180.0
ENV = {**os.environ, "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0")}
INJECTORS = {
    "/scan_relay",
    "/odom_relay",
    "/cmd_vel_relay",
    "/imu_adapter",
    "/gps_adapter",
    "/joint_states_throttle",
    "/battery",
}


def ros2(*args: str, timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess[str]:
    """`ros2 <args>` in the bridge container, through the image's entry point."""
    return run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "bridge",
            BRIDGE_ENTRYPOINT,
            "timeout",
            "-s",
            "INT",
            f"{timeout:.0f}",
            "ros2",
            *args,
        ],
        timeout=timeout + 30,
        check=check,
        quiet=True,
    )


def set_param(node: str, parameter: str, value: str) -> None:
    """`ros2 param set`, asked again while the CLI's graph lags (as the events do)."""
    deadline = time.monotonic() + 60.0
    while True:
        completed = ros2("param", "set", node, parameter, value, timeout=30, check=False)
        if "Set parameter successful" in completed.stdout:
            return
        missing = "Node not found" in completed.stdout + completed.stderr
        if not missing or time.monotonic() > deadline:
            raise AssertionError(
                f"ros2 param set {node} {parameter} {value} failed:\n{completed.stdout}\n"
                f"{completed.stderr}"
            )
        time.sleep(1.0)


def echo_field(topic: str, field: str, timeout: float = 30.0) -> str | None:
    """The field of the next message on `topic`, or None when none arrives in time."""
    completed = ros2(
        "topic", "echo", "--once", "--field", field, topic, timeout=timeout, check=False
    )
    # The CLI interleaves "A message was lost!!!" when a reader falls behind: not the value.
    lines = [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip() and line.strip() != "---" and "message was lost" not in line
    ]
    if completed.returncode != 0 or not lines:
        return None
    return lines[0]


def stamp_of(topic: str) -> tuple[float, float]:
    """The header stamp (seconds, nanoseconds) of the next message on `topic`."""
    return (
        echo_number(topic, "header.stamp.sec"),
        echo_number(topic, "header.stamp.nanosec"),
    )


def echo_number(topic: str, field: str, timeout: float = 30.0) -> float:
    value = echo_field(topic, field, timeout)
    assert value is not None, f"no message on {topic} within {timeout:.0f} s"
    return float(value)


@pytest.fixture(scope="class")
def bridge() -> Iterator[None]:
    """The simulator with the spawned reference UGV and its bridge, torn down after the class.

    Torn down before the scenario run of ``test_06``: two stacks on the same ROS domain on
    one host cross-talk (the run saw the compose stack's ``ugv_ref`` contact and its clock).
    """
    run([sys.executable, str(REPO / "tools/assets/fetch_fuel.py"), "--quiet"], timeout=900)
    run([*COMPOSE, "up", "-d", "--wait", "--no-build", "sim"], timeout=300)
    try:
        wait_until(world_ready, WORLD_TIMEOUT_S, f"the {WORLD} world's services")
        x, y, yaw = frame_pose(SPAWN_FRAME)
        run(
            [
                *COMPOSE,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "bridge",
                "ros2",
                "launch",
                "fieldproof_robots",
                "spawn_ugv_ref.launch.py",
                f"world:={WORLD}",
                f"x:={x}",
                f"y:={y}",
                f"yaw:={yaw}",
                "robot_state_publisher:=false",
            ],
            timeout=SPAWN_TIMEOUT_S,
        )
        run([*COMPOSE, "up", "-d", "--no-build", "--no-deps", "bridge"], timeout=120)

        def injectors_up() -> bool:
            completed = ros2("node", "list", timeout=30, check=False)
            return set(completed.stdout.split()) >= INJECTORS

        wait_until(injectors_up, BRIDGE_TIMEOUT_S, "the bridge's injector nodes")
        yield
    finally:
        for name in ("bridge", "sim"):
            run([*COMPOSE, "stop", name], timeout=120, check=False, quiet=True)
            run([*COMPOSE, "rm", "-f", name], timeout=60, check=False, quiet=True)


@pytest.mark.integration
class TestInjectors:
    """Each fault set in the real bridge and observed on the contract topic."""

    def test_01_gps_deny_and_offset(self, bridge: None) -> None:
        assert echo_number("/gps/fix", "status.status") == 0.0, "a fix to begin with"
        latitude = echo_number("/gps/fix", "latitude")
        longitude = echo_number("/gps/fix", "longitude")
        set_param("/gps_adapter", "deny", "true")
        time.sleep(1.0)
        assert echo_number("/gps/fix", "status.status") == -1.0, "STATUS_NO_FIX while denied"
        assert echo_field("/gps/fix", "latitude") == "nan"
        set_param("/gps_adapter", "deny", "false")
        time.sleep(1.0)
        assert echo_number("/gps/fix", "status.status") == 0.0, "the fix back"
        set_param("/gps_adapter", "offset_enu_m", "[100.0, 0.0, 0.0]")
        time.sleep(1.0)
        moved = echo_number("/gps/fix", "longitude")
        metres_per_degree = 111_319.5 * math.cos(math.radians(latitude))
        assert (moved - longitude) * metres_per_degree == pytest.approx(100.0, abs=3.0)
        assert echo_number("/gps/fix", "latitude") == pytest.approx(latitude, abs=1e-4)
        set_param("/gps_adapter", "offset_enu_m", "[0.0, 0.0, 0.0]")
        time.sleep(1.0)
        assert echo_number("/gps/fix", "longitude") == pytest.approx(longitude, abs=1e-4)

    def test_02_imu_bias(self, bridge: None) -> None:
        assert abs(echo_number("/imu/data", "angular_velocity.x")) < 0.1, "at rest"
        set_param("/imu_adapter", "gyro_bias_radps", "[0.5, 0.0, 0.0]")
        time.sleep(0.5)
        assert echo_number("/imu/data", "angular_velocity.x") == pytest.approx(0.5, abs=0.1)
        set_param("/imu_adapter", "accel_bias_mps2", "[0.0, 0.0, -2.0]")
        time.sleep(0.5)
        assert echo_number("/imu/data", "linear_acceleration.z") == pytest.approx(
            9.81 - 2.0, abs=0.3
        )
        set_param("/imu_adapter", "gyro_bias_radps", "[0.0, 0.0, 0.0]")
        set_param("/imu_adapter", "accel_bias_mps2", "[0.0, 0.0, 0.0]")
        time.sleep(0.5)
        assert abs(echo_number("/imu/data", "angular_velocity.x")) < 0.1

    def test_03_sensor_dropout_and_freeze(self, bridge: None) -> None:
        assert echo_field("/scan", "header.frame_id") == "lidar_link", "the scan through the relay"
        set_param("/scan_relay", "dropout", "true")
        time.sleep(1.0)
        assert echo_field("/scan", "header.frame_id", timeout=8.0) is None, "the scan silent"
        set_param("/scan_relay", "dropout", "false")
        assert echo_field("/scan", "header.frame_id") == "lidar_link", "the scan back"
        set_param("/odom_relay", "freeze", "true")
        time.sleep(1.0)
        first = stamp_of("/odom")
        time.sleep(1.5)
        second = stamp_of("/odom")
        assert first == second, "the odometry frozen on one message"
        set_param("/odom_relay", "freeze", "false")
        time.sleep(1.0)
        third = stamp_of("/odom")
        assert third != second, "the odometry moving again"

    def test_04_battery_drain(self, bridge: None) -> None:
        assert echo_number("/battery/state", "percentage") == 1.0
        assert echo_number("/battery/state", "voltage") == pytest.approx(29.4)
        assert echo_number("/battery/state", "power_supply_status") == 4.0, "FULL"
        # A long drain: every CLI round trip below takes seconds of simulation time.
        set_param("/battery", "drain_to", "0.5")
        set_param("/battery", "drain_duration_s", "30.0")
        assert echo_number("/battery/state", "power_supply_status") == 2.0, "DISCHARGING"
        midway = echo_number("/battery/state", "percentage")
        assert 0.5 < midway < 1.0, midway
        assert echo_number("/battery/state", "voltage") < 29.4
        time.sleep(32.0)
        assert echo_number("/battery/state", "percentage") == pytest.approx(0.5)
        assert echo_number("/battery/state", "voltage") == pytest.approx(25.2)
        assert echo_number("/battery/state", "power_supply_status") == 3.0, "NOT_CHARGING"

    def test_05_actuator_limit(self, bridge: None) -> None:
        set_param("/cmd_vel_relay", "max_linear_mps", "0.2")
        set_param("/cmd_vel_relay", "max_angular_radps", "0.5")
        publisher = subprocess.Popen(  # noqa: S603
            [
                *COMPOSE,
                "exec",
                "-T",
                "bridge",
                BRIDGE_ENTRYPOINT,
                "ros2",
                "topic",
                "pub",
                "--rate",
                "10",
                "/cmd_vel",
                "geometry_msgs/msg/Twist",
                "{linear: {x: 1.0}, angular: {z: -2.0}}",
            ],
            env=ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert echo_number("/_bridge/cmd_vel", "linear.x") == pytest.approx(0.2)
            assert echo_number("/_bridge/cmd_vel", "angular.z") == pytest.approx(-0.5)
            set_param("/cmd_vel_relay", "max_linear_mps", "0.0")
            set_param("/cmd_vel_relay", "max_angular_radps", "0.0")
            time.sleep(0.5)
            assert echo_number("/_bridge/cmd_vel", "linear.x") == pytest.approx(1.0)
        finally:
            publisher.terminate()
            publisher.wait(timeout=30)
        # A stop, so the vehicle does not drive on after the test.
        ros2("topic", "pub", "--once", "/cmd_vel", "geometry_msgs/msg/Twist", "{}", timeout=30)


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


@pytest.mark.integration
def test_06_gps_denied_scenario(tmp_path: Path) -> None:
    run_id = f"it-gps-{os.getpid()}"
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

    timeline = {a["name"]: a for a in record["timeline"]}
    assert set(timeline) == {"drive_ahead", "deny_gps", "drive_back"}
    assert {a["status"] for a in record["timeline"]} == {"ok"}
    assert timeline["deny_gps"]["detail"] == "fix denied for 20 s, lifted"
    assert timeline["deny_gps"]["trigger"] == {"at_s": 5.0}
    assert timeline["deny_gps"]["duration_s"] >= 20.0
    assert timeline["drive_back"]["trigger"] == {"at_s": 0.0, "after": ["drive_ahead", "deny_gps"]}
    deny_end = timeline["deny_gps"]["t_s"] + timeline["deny_gps"]["duration_s"]
    assert timeline["drive_back"]["t_s"] >= deny_end

    verdicts = {a["name"]: a["status"] for a in record["assertions"]}
    assert verdicts == {
        "fix_present": "passed",
        "fix_lost": "passed",
        "fix_back": "passed",
        "gps_denied": "passed",
        "reached_goal": "passed",
        "returned": "passed",
        "no_collision": "passed",
    }

    events = log_events(run_dir)
    faults = [e for e in events if e["event"].startswith("fault.")]
    assert [e["event"] for e in faults] == ["fault.injected", "fault.lifted"]
    assert faults[0]["message"] == "/gps_adapter: fix denied"
    assert containers_of(run_id) == []
