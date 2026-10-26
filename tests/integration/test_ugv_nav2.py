"""Integration test of the vertical slice (task P1-T04, the P1-T04 verification).

The simulator, the bridge and the example vehicle run as three containers on the ``fieldproof``
network, the way the stack is meant to run (ADR-0004), and the test drives Docker from the host
(``make test-integration``: the engine's environment, no ROS on the host):

1. the simulator: the compose service ``sim`` with the depot world (the pinned Fuel models are
   fetched first), until the world's services answer;
2. the vehicle model: the reference UGV spawned at the depot's ``spawn_dock`` frame by a one-shot
   container of the bridge image (the spawn launch of ``fieldproof_robots``);
3. the bridge: the compose service ``bridge`` on the example vehicle's manifest;
4. the vehicle: ``fieldproof vehicle run`` of the example image, then ``fieldproof vehicle wait``
   until ``runtime.ready`` holds (Nav2's servers and SLAM Toolbox ``active``, ``/map``, the
   ``navigate_to_pose`` action); Docker then reports the container ``healthy`` through the
   image's ``HEALTHCHECK``;
5. a ``NavigateToPose`` goal ``GOAL_AHEAD_M`` ahead in the ``map`` frame, sent from inside the
   vehicle container with ``ros2 action send_goal``: the action succeeds and the ground-truth pose
   the simulator publishes is within ``TOLERANCE_M`` of where the goal lies in the world (the map
   frame starts at the spawn pose, so the goal's world position follows from the spawn frame);
6. no node of the vehicle or of the bridge died.

Needs the stack images (``make images``) and the vehicle image (``make vehicle-image``). The
containers the test started are stopped at the end (a running dev container is left alone);
``FIELDPROOF_IT_KEEP=1`` keeps them for a look after a failure, and the logs of the three
containers are printed when a test fails. ``ROS_DOMAIN_ID`` (default 0) is shared by the three
containers, as with ``make up``. Software rendering keeps the simulator below real time on a CI
runner, so the wall-clock limits are generous; what the test asserts is in simulation time.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO / "docker/compose.yaml"
MANIFEST = REPO / "examples/vehicles/ugv_nav2/robot.yaml"
WORLD = "depot"
WORLD_FILE = REPO / "sim/fieldproof_worlds/worlds" / f"{WORLD}.sdf"
SPAWN_FRAME = "spawn_dock"
MODEL = "ugv_ref"
VEHICLE = "fieldproof-it-ugv_nav2"
GOAL_AHEAD_M = 4.0
TOLERANCE_M = 0.5
DOMAIN_ID = os.environ.get("ROS_DOMAIN_ID", "0")
KEEP = os.environ.get("FIELDPROOF_IT_KEEP") == "1"
WORLD_TIMEOUT_S = 300.0
SPAWN_TIMEOUT_S = 300.0
# The manifest's ready.timeout_s (180 s) plus the docker exec and discovery overhead.
READY_TIMEOUT_S = 240.0
HEALTH_TIMEOUT_S = 90.0
GOAL_TIMEOUT_S = 600.0
ENTRYPOINT = "/usr/local/bin/fieldproof-vehicle-entrypoint"

ENV = {**os.environ, "FIELDPROOF_WORLD": f"{WORLD}.sdf", "ROS_DOMAIN_ID": DOMAIN_ID}
COMPOSE = ["docker", "compose", "-f", str(COMPOSE_FILE), "--profile", "sim", "--profile", "bridge"]
FIELDPROOF = [sys.executable, "-m", "fieldproof"]


def run(
    command: list[str], *, timeout: float, check: bool = True, quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a command with the stack's environment, echoing it; output is captured."""
    if not quiet:
        print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    completed = subprocess.run(  # noqa: S603  (fixed command lists)
        command, env=ENV, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"{shlex.join(command)} exited {completed.returncode}\n"
            f"stdout:\n{completed.stdout[-4000:]}\nstderr:\n{completed.stderr[-4000:]}"
        )
    return completed


def wait_until(
    predicate: Callable[[], bool], timeout_s: float, what: str, period_s: float = 2.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(period_s)


def frame_pose(name: str) -> tuple[float, float, float]:
    """``x, y, yaw`` of a named frame of the world."""
    for frame in ET.parse(WORLD_FILE).getroot().iter("frame"):  # noqa: S314  (our own file)
        if frame.get("name") == name:
            x, y, _z, _roll, _pitch, yaw = (frame.findtext("pose") or "").split()
            return float(x), float(y), float(yaw)
    raise AssertionError(f"{WORLD_FILE} has no frame {name}")


def required_images() -> dict[str, str]:
    """The image of each container the test starts, as compose and the manifest name them."""
    config = json.loads(
        run([*COMPOSE, "config", "--format", "json"], timeout=60, quiet=True).stdout
    )
    manifest = yaml.safe_load(MANIFEST.read_text())
    return {
        "sim": config["services"]["sim"]["image"],
        "bridge": config["services"]["bridge"]["image"],
        "vehicle": manifest["runtime"]["image"],
    }


def sim_exec(command: str, *, timeout: float = 60.0, check: bool = True) -> str:
    """Run a shell command in the sim container with the ROS and Gazebo environment."""
    completed = run(
        [*COMPOSE, "exec", "-T", "sim", "bash", "-lc", command],
        timeout=timeout,
        check=check,
        quiet=True,
    )
    return completed.stdout


def world_ready() -> bool:
    try:
        services = sim_exec("gz service -l", timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return False
    return f"/world/{WORLD}/create" in services.split()


def ground_truth_xy() -> tuple[float, float]:
    """The model's position in the world, from the server's pose topic."""
    text = sim_exec(f"gz topic -e -t /world/{WORLD}/pose/info -n 1 --json-output", timeout=60)
    # `-n 1` can still flush a second message under load (#65): decode the first object only.
    data, _ = json.JSONDecoder().raw_decode(text[text.index("{") :])
    for pose in data.get("pose", []):
        if pose.get("name") == MODEL:
            position = pose.get("position", {})
            # Protobuf JSON leaves zero-valued fields out.
            return float(position.get("x", 0.0)), float(position.get("y", 0.0))
    raise AssertionError(f"{MODEL} is not among the poses the server publishes")


def health_status() -> str:
    completed = run(
        ["docker", "inspect", VEHICLE, "--format", "{{.State.Health.Status}}"],
        timeout=30,
        check=False,
        quiet=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "gone"


def container_logs(name: str, tail: int = 150) -> str:
    completed = run(
        ["docker", "logs", "--tail", str(tail), name], timeout=60, check=False, quiet=True
    )
    return completed.stdout + completed.stderr


def print_logs() -> None:
    for name in (VEHICLE, "fieldproof-bridge-1", "fieldproof-sim-1"):
        print(f"\n===== {name} (last lines) =====", flush=True)  # noqa: T201
        print(container_logs(name), flush=True)  # noqa: T201


def teardown() -> None:
    run(["docker", "rm", "-f", VEHICLE], timeout=60, check=False, quiet=True)
    run([*COMPOSE, "stop", "bridge", "sim"], timeout=120, check=False, quiet=True)
    run([*COMPOSE, "rm", "-f", "bridge", "sim"], timeout=60, check=False, quiet=True)


@dataclass(frozen=True)
class Stack:
    """What the fixture started and where the vehicle stands."""

    spawn_x: float
    spawn_y: float
    spawn_yaw: float

    def goal_in_world(self, ahead_m: float) -> tuple[float, float]:
        """World position of a point ``ahead_m`` in front of the spawn pose (the map frame)."""
        return (
            self.spawn_x + ahead_m * math.cos(self.spawn_yaw),
            self.spawn_y + ahead_m * math.sin(self.spawn_yaw),
        )


def bring_up() -> Stack:
    images = required_images()
    missing = [
        f"{what}: {image}"
        for what, image in images.items()
        if run(
            ["docker", "image", "inspect", image], timeout=30, check=False, quiet=True
        ).returncode
    ]
    if missing:
        pytest.fail(
            "image(s) not built: " + ", ".join(missing) + " (make images, make vehicle-image)"
        )
    run([sys.executable, str(REPO / "tools/assets/fetch_fuel.py"), "--quiet"], timeout=900)
    run([*COMPOSE, "up", "-d", "--wait", "--no-build", "sim"], timeout=300)
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
    run(
        [
            *FIELDPROOF,
            "vehicle",
            "run",
            str(MANIFEST),
            "--name",
            VEHICLE,
            "--domain-id",
            DOMAIN_ID,
            "--detach",
        ],
        timeout=120,
    )
    return Stack(x, y, yaw)


@pytest.fixture(scope="module")
def stack(request: pytest.FixtureRequest) -> Iterator[Stack]:
    failed_before = request.session.testsfailed
    try:
        started = bring_up()
    except BaseException:
        print_logs()
        if not KEEP:
            teardown()
        raise
    try:
        yield started
    finally:
        if request.session.testsfailed > failed_before:
            print_logs()
        if KEEP:
            print(f"\nFIELDPROOF_IT_KEEP=1: {VEHICLE}, bridge and sim are left running")  # noqa: T201
        else:
            teardown()


@pytest.mark.integration
def test_01_vehicle_becomes_ready_and_healthy(stack: Stack) -> None:
    started = time.monotonic()
    completed = run(
        [*FIELDPROOF, "vehicle", "wait", str(MANIFEST), "--name", VEHICLE],
        timeout=READY_TIMEOUT_S,
        check=False,
    )
    print(completed.stdout, flush=True)  # noqa: T201
    assert completed.returncode == 0, (
        f"fieldproof vehicle wait exited {completed.returncode}:\n{completed.stdout}\n"
        f"{completed.stderr}"
    )
    assert "ready:" in completed.stdout
    print(f"ready after {time.monotonic() - started:.0f} s wall-clock", flush=True)  # noqa: T201
    wait_until(lambda: health_status() == "healthy", HEALTH_TIMEOUT_S, "Docker to report healthy")


@pytest.mark.integration
def test_02_navigate_to_pose_reaches_the_goal(stack: Stack) -> None:
    before = ground_truth_xy()
    # ros2 action send_goal takes the goal as YAML, of which JSON is a subset.
    goal = json.dumps(
        {
            "pose": {
                "header": {"frame_id": "map"},
                "pose": {
                    "position": {"x": GOAL_AHEAD_M, "y": 0.0, "z": 0.0},
                    "orientation": {"w": 1.0},
                },
            }
        }
    )
    completed = run(
        [
            "docker",
            "exec",
            VEHICLE,
            ENTRYPOINT,
            "ros2",
            "action",
            "send_goal",
            "/navigate_to_pose",
            "nav2_msgs/action/NavigateToPose",
            goal,
        ],
        timeout=GOAL_TIMEOUT_S,
        check=False,
    )
    print(completed.stdout[-2000:], flush=True)  # noqa: T201
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "Goal finished with status: SUCCEEDED" in completed.stdout, completed.stdout[-2000:]
    after = ground_truth_xy()
    expected = stack.goal_in_world(GOAL_AHEAD_M)
    error = math.dist(after, expected)
    travelled = math.dist(after, before)
    print(  # noqa: T201
        f"{MODEL} moved from ({before[0]:.2f}, {before[1]:.2f}) to ({after[0]:.2f}, "
        f"{after[1]:.2f}); goal in the world ({expected[0]:.2f}, {expected[1]:.2f}); "
        f"error {error:.2f} m (tolerance {TOLERANCE_M:.2f} m), travelled {travelled:.2f} m",
        flush=True,
    )
    assert error <= TOLERANCE_M, f"{error:.2f} m from the goal (tolerance {TOLERANCE_M} m)"
    assert travelled >= GOAL_AHEAD_M - TOLERANCE_M, f"moved only {travelled:.2f} m"


@pytest.mark.integration
def test_03_no_node_died(stack: Stack) -> None:
    for name in (VEHICLE, "fieldproof-bridge-1"):
        log = container_logs(name, tail=5000)
        died = [line for line in log.splitlines() if "process has died" in line]
        assert not died, f"{name}: {died[:5]}"


MANAGER_SERVICE = "/lifecycle_manager_navigation/manage_nodes"
MANAGED = [
    "controller_server",
    "smoother_server",
    "planner_server",
    "behavior_server",
    "bt_navigator",
    "waypoint_follower",
    "velocity_smoother",
    "collision_monitor",
]
RETRY_TIMEOUT_S = 120.0


def vehicle_ros2(*argv: str, timeout: float = 60.0) -> str:
    """`ros2 ...` inside the vehicle's container."""
    return run(
        ["docker", "exec", VEHICLE, ENTRYPOINT, "ros2", *argv], timeout=timeout, quiet=True
    ).stdout


def lifecycle_states() -> dict[str, str]:
    """The managed servers' states through their `get_state` services (what the probe polls;
    `ros2 lifecycle get` wants the node in the graph first and says `Node not found`)."""
    states = {}
    for name in MANAGED:
        text = vehicle_ros2(
            "service", "call", f"/{name}/get_state", "lifecycle_msgs/srv/GetState", timeout=30
        )
        found = re.search(r"label='(\w+)'", text)
        states[name] = found.group(1) if found else "unknown"
    return states


@pytest.mark.integration
def test_04_bringup_watchdog_retries_an_aborted_bringup(stack: Stack) -> None:
    """The watchdog brings the stack back after the manager gave up on a bringup (issue #32).

    The manager's abort leaves the servers inactive and unconfigured; `PAUSE` leaves them all
    inactive, which is the same ground for the retry (every server has to go back to
    unconfigured before the manager's `STARTUP` can configure it). The manager's abort line is
    then published on `/rosout` as the manager would, and the watchdog is expected to take the
    servers down, ask the manager for `STARTUP` and get the stack active again.
    """
    pause = vehicle_ros2(
        "service", "call", MANAGER_SERVICE, "nav2_msgs/srv/ManageLifecycleNodes", "{command: 1}"
    )
    assert "success=True" in pause, pause
    paused = lifecycle_states()
    assert set(paused.values()) == {"inactive"}, paused
    vehicle_ros2(
        "topic",
        "pub",
        "--once",
        "--keep-alive",
        "2",
        "--qos-reliability",
        "reliable",
        "/rosout",
        "rcl_interfaces/msg/Log",
        "{level: 40, name: lifecycle_manager_navigation, "
        "msg: 'Failed to bring up all requested nodes. Aborting bringup.'}",
    )
    wait_until(
        lambda: set(lifecycle_states().values()) == {"active"},
        RETRY_TIMEOUT_S,
        "the watchdog to bring the servers back to active",
        period_s=5.0,
    )
    log = container_logs(VEHICLE, tail=5000)
    assert "retrying the bringup (1 of 3)" in log, log[-3000:]
    assert "the bringup succeeded on retry" in log, log[-3000:]
    assert "Managed nodes are active" in log.split("retrying the bringup")[-1], log[-3000:]
    died = [line for line in log.splitlines() if "process has died" in line]
    assert not died, died[:5]
