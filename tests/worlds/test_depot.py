"""Verification of the depot world (task P1-T02): load time and real-time factor with the UGV.

Runs inside the dev container with the sim and robots install spaces sourced
(`make test-worlds`, which is `tools/ros/colcon.sh run python3 -m pytest tests/worlds`):

1. a headless server loads depot.sdf, with every model resolved, within
   FIELDPROOF_WORLD_LOAD_LIMIT_S (default 10 s, the definition of done);
2. the reference UGV spawns at the world's `spawn_dock` frame and its lidar, IMU, camera and
   odometry publish;
3. with the real-time cap lifted (set_physics), the server sustains a real-time factor of at
   least FIELDPROOF_WORLD_RTF_MIN (default 1.0) over 30 simulated seconds with the UGV and its
   sensors running, measured from the server's own sim and real time;
4. the server logged no unresolved resource.

CI runs this with software rendering and relaxed limits (it checks loading and resolution, not
speed); the numbers that count are the ones from the maintainer's machine, printed at the end.
The Gazebo partition and the ROS domain are private to the test process.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Isolation from any other Gazebo server or ROS graph on the host; set before the transports load.
os.environ["GZ_PARTITION"] = f"fieldproof-worlds-{os.getpid()}"
os.environ["ROS_DOMAIN_ID"] = str(os.getpid() % 100 + 1)
os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"

import pytest
from ament_index_python.packages import get_package_share_directory
from gz.msgs10.image_pb2 import Image
from gz.msgs10.imu_pb2 import IMU
from gz.msgs10.laserscan_pb2 import LaserScan
from gz.msgs10.odometry_pb2 import Odometry
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.msgs10.world_stats_pb2 import WorldStatistics
from gz.transport13 import Node

WORLD = "depot"
WORLD_FILE = Path(get_package_share_directory("fieldproof_worlds")) / "worlds" / f"{WORLD}.sdf"
ROBOT_SHARE = Path(get_package_share_directory("fieldproof_robots"))
LOAD_LIMIT_S = float(os.environ.get("FIELDPROOF_WORLD_LOAD_LIMIT_S", "10"))
RTF_MIN = float(os.environ.get("FIELDPROOF_WORLD_RTF_MIN", "1.0"))
MEASURE_SIM_S = 30.0
SETTLE_SIM_S = 2.0
# Generous wall-clock limits so that a software renderer in CI does not time out.
STATS_TIMEOUT_S = max(LOAD_LIMIT_S * 3, 60.0)
SPAWN_TIMEOUT_S = 300.0
MEASURE_TIMEOUT_S = 900.0
UNCAPPED_RTF = 100.0
SENSOR_TOPICS = {"/scan": LaserScan, "/imu": IMU, "/camera/image": Image, "/odom": Odometry}
RESOURCE_MARKERS = ("Unable to find", "Failed to load mesh", "Unable to load", "Missing material")


def executable(name: str) -> str:
    """Absolute path of a tool on PATH (the ROS and Gazebo environment must be sourced)."""
    path = shutil.which(name)
    assert path is not None, f"{name} is not on PATH; source the install spaces (colcon.sh run)"
    return path


@dataclass
class Stats:
    """The latest world statistics and when the first ones arrived."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    first_at: float | None = None
    sim_time: float = 0.0
    real_time: float = 0.0
    gz_rtf: float = 0.0

    def __call__(self, msg: Any) -> None:
        with self.lock:
            if self.first_at is None and msg.sim_time.sec + msg.sim_time.nsec > 0:
                self.first_at = time.monotonic()
            self.sim_time = msg.sim_time.sec + msg.sim_time.nsec * 1e-9
            self.real_time = msg.real_time.sec + msg.real_time.nsec * 1e-9
            self.gz_rtf = msg.real_time_factor

    def sample(self) -> tuple[float, float]:
        with self.lock:
            return self.sim_time, self.real_time


class Counter:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, _msg: Any) -> None:
        self.count += 1


class Entities:
    """The names of the entities the server reports on /world/<name>/pose/info."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.names: set[str] = set()

    def __call__(self, msg: Any) -> None:
        with self.lock:
            self.names.update(pose.name for pose in msg.pose)

    def has(self, wanted: set[str]) -> bool:
        with self.lock:
            return wanted <= self.names


@dataclass
class Server:
    process: subprocess.Popen[bytes]
    started_at: float
    log: Path
    stats: Stats
    node: Node
    sensors: dict[str, Counter]
    entities: Entities
    load_time_s: float = 0.0
    spawn: subprocess.Popen[bytes] | None = None
    measured_rtf: float = 0.0
    measured_over_sim_s: float = 0.0


def wait_until(predicate: Any, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(0.1)


def frame_pose(name: str) -> list[float]:
    world = ET.parse(WORLD_FILE).getroot().find("world")  # noqa: S314
    assert world is not None
    frame = next(f for f in world.findall("frame") if f.get("name") == name)
    return [float(v) for v in (frame.findtext("pose") or "").split()]


def expected_models() -> set[str]:
    """The names of every model the world file declares, included ones with their <name>."""
    world = ET.parse(WORLD_FILE).getroot().find("world")  # noqa: S314
    assert world is not None
    names = {m.get("name", "") for m in world.findall("model")}
    names |= {(i.findtext("name") or "").strip() for i in world.findall("include")}
    return names


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    log = tmp_path_factory.mktemp("depot") / "gz-server.log"
    node = Node()
    stats = Stats()
    node.subscribe(WorldStatistics, f"/world/{WORLD}/stats", stats)
    sensors = {topic: Counter() for topic in SENSOR_TOPICS}
    for topic, msg_type in SENSOR_TOPICS.items():
        node.subscribe(msg_type, topic, sensors[topic])
    entities = Entities()
    node.subscribe(Pose_V, f"/world/{WORLD}/pose/info", entities)
    with log.open("wb") as handle:
        process = subprocess.Popen(  # noqa: S603
            [
                executable("gz"),
                "sim",
                "-s",
                "-r",
                "--headless-rendering",
                "-v",
                "2",
                str(WORLD_FILE),
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    started = Server(process, time.monotonic(), log, stats, node, sensors, entities)
    try:
        yield started
    finally:
        for proc in (started.spawn, process):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()


def test_01_world_loads_within_the_limit(server: Server) -> None:
    wait_until(lambda: server.stats.first_at is not None, STATS_TIMEOUT_S, "the first world stats")
    assert server.stats.first_at is not None
    server.load_time_s = server.stats.first_at - server.started_at
    assert server.process.poll() is None, "the server exited"
    wanted = expected_models()
    wait_until(lambda: server.entities.has(wanted), STATS_TIMEOUT_S, f"the {len(wanted)} models")
    assert server.load_time_s < LOAD_LIMIT_S, (
        f"{WORLD}.sdf took {server.load_time_s:.1f} s to load (limit {LOAD_LIMIT_S:.0f} s)"
    )


def test_02_ugv_spawns_and_its_sensors_publish(server: Server) -> None:
    x, y, _z, _r, _p, yaw = frame_pose("spawn_dock")
    server.spawn = subprocess.Popen(  # noqa: S603
        [
            executable("ros2"),
            "launch",
            "fieldproof_robots",
            "spawn_ugv_ref.launch.py",
            f"world:={WORLD}",
            f"x:={x}",
            f"y:={y}",
            f"yaw:={yaw}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    for topic, counter in server.sensors.items():
        wait_until(lambda c=counter: c.count > 0, SPAWN_TIMEOUT_S, f"a message on {topic}")
    wait_until(
        lambda: server.entities.has({"ugv_ref"}), SPAWN_TIMEOUT_S, "the UGV among the models"
    )


def test_03_real_time_factor_with_the_ugv(server: Server) -> None:
    assert server.spawn is not None, "test_02 did not spawn the UGV"
    assert server.spawn.poll() is None, "the spawn launch exited"
    # Lift the world's real-time cap so the measurement shows what the machine sustains.
    result = subprocess.run(  # noqa: S603
        [
            executable("gz"),
            "service",
            "-s",
            f"/world/{WORLD}/set_physics",
            "--reqtype",
            "gz.msgs.Physics",
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "5000",
            "--req",
            f"real_time_factor: {UNCAPPED_RTF}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "data: true" in result.stdout, result.stdout
    settle_until = server.stats.sample()[0] + SETTLE_SIM_S
    wait_until(
        lambda: server.stats.sample()[0] >= settle_until, MEASURE_TIMEOUT_S, "the cap to lift"
    )
    sim0, real0 = server.stats.sample()
    counts0 = {topic: c.count for topic, c in server.sensors.items()}
    wait_until(
        lambda: server.stats.sample()[0] >= sim0 + MEASURE_SIM_S,
        MEASURE_TIMEOUT_S,
        f"{MEASURE_SIM_S:.0f} simulated seconds",
    )
    sim1, real1 = server.stats.sample()
    server.measured_over_sim_s = sim1 - sim0
    server.measured_rtf = (sim1 - sim0) / (real1 - real0)
    for topic, counter in server.sensors.items():
        assert counter.count > counts0[topic], f"{topic} stopped publishing during the measurement"
    assert server.measured_rtf >= RTF_MIN, (
        f"real-time factor {server.measured_rtf:.2f} over {server.measured_over_sim_s:.0f} "
        f"simulated seconds with the UGV, below the minimum {RTF_MIN:.2f}"
    )


def test_04_server_resolved_every_resource_and_report(server: Server) -> None:
    text = server.log.read_text(errors="replace")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for marker in RESOURCE_MARKERS:
        assert marker not in text, [line for line in text.splitlines() if marker in line][:5]
    print(  # noqa: T201
        f"\n{WORLD}.sdf: loaded in {server.load_time_s:.1f} s (limit {LOAD_LIMIT_S:.0f} s); "
        f"real-time factor {server.measured_rtf:.2f} over {server.measured_over_sim_s:.0f} "
        f"simulated seconds with the UGV and its sensors (minimum {RTF_MIN:.2f})"
    )
