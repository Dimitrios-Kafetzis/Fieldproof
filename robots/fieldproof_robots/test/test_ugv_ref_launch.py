"""Launch test for the reference UGV (task P1-T01): spawn, sensor rates, drive one metre.

Starts a headless Gazebo server with the empty world of fieldproof_worlds, spawns the UGV through
spawn_ugv_ref.launch.py and subscribes to the Gazebo topics from the test process with the
gz-transport Python bindings:

1. every sensor and the odometry publish on the topic declared in robot.yaml, at the declared
   rate (measured in simulation time, so a slow software renderer cannot fail it) and with the
   declared frame id;
2. a velocity command on /cmd_vel drives the robot; after one metre of odometry the odometry
   delta matches the ground-truth pose delta of the model (the P1-T01 verification);
3. robot_state_publisher publishes the static transforms of the sensor frames.

The Gazebo partition and the ROS domain are private to the test process, so a stack started with
`make up` on the same host is neither disturbed nor observed.
"""

# launch_testing runs unittest.TestCase classes, whose assertion methods ruff's pytest rule
# PT009 would otherwise flag.
# ruff: noqa: PT009

import math
import os
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Isolation from any other Gazebo server or ROS graph on the host; set before the transports load.
os.environ["GZ_PARTITION"] = f"fieldproof-test-{os.getpid()}"
os.environ["ROS_DOMAIN_ID"] = str(os.getpid() % 100 + 1)
os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"

import launch_testing
import launch_testing.actions
import launch_testing.asserts
import rclpy
import yaml
from ament_index_python.packages import get_package_share_directory
from gz.msgs10.camera_info_pb2 import CameraInfo
from gz.msgs10.image_pb2 import Image
from gz.msgs10.imu_pb2 import IMU
from gz.msgs10.laserscan_pb2 import LaserScan
from gz.msgs10.model_pb2 import Model
from gz.msgs10.navsat_pb2 import NavSat
from gz.msgs10.odometry_pb2 import Odometry
from gz.msgs10.pointcloud_packed_pb2 import PointCloudPacked
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.msgs10.twist_pb2 import Twist
from gz.transport13 import Node as GzNode
from launch.actions import (
    ExecuteProcess,
    IncludeLaunchDescription,
    RegisterEventHandler,
)
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from tf2_msgs.msg import TFMessage

import launch

WORLD = "fieldproof_empty"
MODEL_NAME = "ugv_ref"
SHARE = Path(get_package_share_directory("fieldproof_robots"))
MANIFEST: dict[str, Any] = yaml.safe_load((SHARE / "models" / "ugv_ref" / "robot.yaml").read_text())
DRIVE = MANIFEST["drive"]
GZ_TYPES = {
    "gz.msgs.LaserScan": LaserScan,
    "gz.msgs.IMU": IMU,
    "gz.msgs.Image": Image,
    "gz.msgs.CameraInfo": CameraInfo,
    "gz.msgs.PointCloudPacked": PointCloudPacked,
    "gz.msgs.NavSat": NavSat,
    "gz.msgs.Odometry": Odometry,
    "gz.msgs.Model": Model,
}
ODOM_TOPIC = "/" + DRIVE["odometry"]["gz_topic"]
JOINT_STATES_TOPIC = "/" + DRIVE["joint_states"]["gz_topic"]
POSE_TOPIC = f"/world/{WORLD}/pose/info"

# Under software rendering the server needs tens of seconds to load ogre2 and the first frames.
SPAWN_TIMEOUT_S = 300.0
RATE_TIMEOUT_S = 300.0
DRIVE_TIMEOUT_S = 180.0
RATE_SAMPLES = 10
RATE_TOLERANCE = 0.10
SETTLE_SIM_S = 1.0
DRIVE_SPEED_MPS = 0.5
DRIVE_DISTANCE_M = 1.0
# Stopping from 0.5 m/s at 2 m/s^2 adds about 6 cm; anything beyond this is a runaway.
OVERSHOOT_M = 0.25


def generate_test_description() -> tuple[Any, dict[str, Any]]:
    worlds = Path(get_package_share_directory("fieldproof_worlds")) / "worlds"
    resource_path = os.pathsep.join(
        p for p in (str(SHARE / "models"), os.environ.get("GZ_SIM_RESOURCE_PATH", "")) if p
    )
    world_file = str(worlds / f"{WORLD}.sdf")
    server = ExecuteProcess(
        cmd=["gz", "sim", "-s", "-r", "--headless-rendering", "-v", "2", world_file],
        name="gz_server",
        output="screen",
        env={**os.environ, "GZ_SIM_RESOURCE_PATH": resource_path},
    )
    # The spawn launch needs the world's create service; wait for it instead of a fixed delay.
    wait_for_world = ExecuteProcess(
        cmd=[
            "bash",
            "-c",
            f"until gz service -l | grep -qx /world/{WORLD}/create; do sleep 0.5; done",
        ],
        name="wait_for_world",
        output="screen",
    )
    spawn = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(str(SHARE / "launch" / "spawn_ugv_ref.launch.py")),
        launch_arguments={"world": WORLD, "name": MODEL_NAME}.items(),
    )
    description = launch.LaunchDescription(
        [
            server,
            wait_for_world,
            RegisterEventHandler(OnProcessExit(target_action=wait_for_world, on_exit=[spawn])),
            launch_testing.actions.ReadyToTest(),
        ]
    )
    return description, {"server": server}


def wait_until(predicate: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(0.1)


class Collector:
    """Keeps the simulation-time stamps and frame ids of the messages received on one topic."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stamps: list[float] = []
        self.frame_ids: set[str] = set()
        self.last: Any = None

    def __call__(self, msg: Any) -> None:
        with self.lock:
            self.last = msg
            self.stamps.append(msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9)
            for entry in msg.header.data:
                if entry.key == "frame_id":
                    self.frame_ids.update(entry.value)

    def count(self) -> int:
        with self.lock:
            return len(self.stamps)

    def sim_time(self) -> float:
        with self.lock:
            return self.stamps[-1] if self.stamps else 0.0

    def rate_hz(self) -> float:
        with self.lock:
            stamps = list(self.stamps)
        return (len(stamps) - 1) / (stamps[-1] - stamps[0])


def yaw_of(q: Any) -> float:
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def planar_distance(a: Any, b: Any) -> float:
    return math.hypot(b.position.x - a.position.x, b.position.y - a.position.y)


def planar_speed(twist: Any) -> float:
    return math.hypot(twist.linear.x, twist.linear.y)


def model_pose(pose_v: Any) -> Any:
    return next((p for p in pose_v.pose if p.name == MODEL_NAME), None)


class TestUgvRef(unittest.TestCase):
    node: GzNode
    collectors: dict[str, Collector]
    cmd_vel: Any
    odom: Collector

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = GzNode()
        cls.collectors = {}
        topics = {
            ODOM_TOPIC: DRIVE["odometry"]["gz_type"],
            JOINT_STATES_TOPIC: DRIVE["joint_states"]["gz_type"],
        }
        for sensor in MANIFEST["sensors"]:
            for output in sensor["outputs"]:
                topics["/" + output["gz_topic"]] = output["gz_type"]
        for topic, gz_type in topics.items():
            cls.collectors[topic] = Collector()
            cls.node.subscribe(GZ_TYPES[gz_type], topic, cls.collectors[topic])
        cls.collectors[POSE_TOPIC] = Collector()
        cls.node.subscribe(Pose_V, POSE_TOPIC, cls.collectors[POSE_TOPIC])
        cls.cmd_vel = cls.node.advertise("/" + DRIVE["command"]["gz_topic"], Twist)
        cls.odom = cls.collectors[ODOM_TOPIC]
        wait_until(lambda: cls.odom.count() > 0, SPAWN_TIMEOUT_S, "the first odometry (spawn)")

    def test_01_outputs_publish_at_declared_rates(self) -> None:
        # topic -> (rate, frame ids expected in the header; empty when Gazebo sets none)
        expected: dict[str, tuple[float, set[str]]] = {
            ODOM_TOPIC: (DRIVE["odometry"]["rate_hz"], {DRIVE["odometry"]["frame_id"]})
        }
        for sensor in MANIFEST["sensors"]:
            for output in sensor["outputs"]:
                frames = {sensor["frame_id"]} if output.get("gz_header_frame_id", True) else set()
                expected["/" + output["gz_topic"]] = (sensor["rate_hz"], frames)
        failures = []
        for topic, (rate_hz, frame_ids) in expected.items():
            collector = self.collectors[topic]
            wait_until(
                lambda: collector.count() >= RATE_SAMPLES,  # noqa: B023  (used within the step)
                RATE_TIMEOUT_S,
                f"{RATE_SAMPLES} messages on {topic}",
            )
            measured = collector.rate_hz()
            if abs(measured - rate_hz) > rate_hz * RATE_TOLERANCE:
                failures.append(f"{topic}: {measured:.2f} Hz, declared {rate_hz} Hz")
            if collector.frame_ids != frame_ids:
                failures.append(
                    f"{topic}: frame_id {sorted(collector.frame_ids)}, declared {sorted(frame_ids)}"
                )
        joint_states = self.collectors[JOINT_STATES_TOPIC]
        wait_until(lambda: joint_states.count() > 0, RATE_TIMEOUT_S, "joint states")
        names = {joint.name for joint in joint_states.last.joint}
        if set(DRIVE["joints"]) != names:
            failures.append(f"joint_states names {sorted(names)}, declared {DRIVE['joints']}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_02_drive_one_metre_and_compare_odometry(self) -> None:
        odom, truth = self.odom, self.collectors[POSE_TOPIC]
        wait_until(
            lambda: truth.last is not None and model_pose(truth.last) is not None,
            SPAWN_TIMEOUT_S,
            "the model's ground-truth pose",
        )
        settled_at = odom.sim_time() + SETTLE_SIM_S
        wait_until(lambda: odom.sim_time() >= settled_at, DRIVE_TIMEOUT_S, "the robot to settle")
        start_odom, start_truth = odom.last.pose, model_pose(truth.last)
        self.assertLess(planar_speed(odom.last.twist), 0.01, "the robot must stand still first")

        forward = Twist()
        forward.linear.x = DRIVE_SPEED_MPS
        deadline = time.monotonic() + DRIVE_TIMEOUT_S
        while planar_distance(start_odom, odom.last.pose) < DRIVE_DISTANCE_M:
            self.assertTrue(self.cmd_vel.publish(forward), "cmd_vel publish failed")
            self.assertLess(
                time.monotonic(),
                deadline,
                f"{DRIVE_DISTANCE_M} m of odometry not reached within {DRIVE_TIMEOUT_S:.0f} s "
                f"(at {planar_distance(start_odom, odom.last.pose):.2f} m)",
            )
            time.sleep(0.1)
        stop = Twist()
        stopped_by = time.monotonic() + DRIVE_TIMEOUT_S
        while planar_speed(odom.last.twist) > 0.01 or abs(odom.last.twist.angular.z) > 0.01:
            self.cmd_vel.publish(stop)
            self.assertLess(time.monotonic(), stopped_by, "the robot did not stop")
            time.sleep(0.1)
        end_odom, end_truth = odom.last.pose, model_pose(truth.last)

        travelled_odom = planar_distance(start_odom, end_odom)
        travelled_truth = planar_distance(start_truth, end_truth)
        self.assertGreaterEqual(travelled_odom, DRIVE_DISTANCE_M)
        self.assertLessEqual(travelled_odom, DRIVE_DISTANCE_M + OVERSHOOT_M)
        self.assertAlmostEqual(
            travelled_odom, travelled_truth, delta=0.10, msg="odometry drifted from the truth"
        )
        self.assertAlmostEqual(
            yaw_of(end_odom.orientation),
            yaw_of(start_odom.orientation),
            delta=0.05,
            msg="a straight command turned the robot",
        )
        self.assertGreater(
            end_odom.position.x - start_odom.position.x,
            0.9 * DRIVE_DISTANCE_M,
            "the robot must drive along +x",
        )

    def test_03_static_transforms_of_the_sensor_frames(self) -> None:
        base = MANIFEST["frames"]["base"]
        expected = {sensor["frame_id"]: base for sensor in MANIFEST["sensors"]}
        expected[MANIFEST["frames"]["footprint"]] = base
        expected["camera_optical_link"] = "camera_link"
        parents: dict[str, str] = {}
        rclpy.init()
        try:
            node = rclpy.create_node("ugv_ref_tf_check")
            qos = QoSProfile(
                depth=100,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
                history=HistoryPolicy.KEEP_LAST,
            )

            def on_tf(msg: Any) -> None:
                for transform in msg.transforms:
                    parents[transform.child_frame_id] = transform.header.frame_id

            node.create_subscription(TFMessage, "/tf_static", on_tf, qos)
            deadline = time.monotonic() + SPAWN_TIMEOUT_S
            while not set(expected) <= set(parents) and time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=0.5)
            node.destroy_node()
        finally:
            rclpy.shutdown()
        self.assertEqual({child: parents.get(child) for child in expected}, expected)


@launch_testing.post_shutdown_test()
class TestAfterShutdown(unittest.TestCase):
    def test_spawn_succeeded(self, proc_info: Any) -> None:
        # ros_gz_sim create exits non-zero when the server rejected or never answered the spawn.
        launch_testing.asserts.assertExitCodes(
            proc_info, allowable_exit_codes=[0], process="create"
        )

    def test_server_resolved_every_resource(self, proc_output: Any, server: Any) -> None:
        output = b"".join(event.text for event in proc_output[server]).decode(errors="replace")
        for marker in (
            "Unable to find file",
            "Failed to load mesh",
            "Unable to load",
            "Missing material",
        ):
            self.assertNotIn(marker, output)
