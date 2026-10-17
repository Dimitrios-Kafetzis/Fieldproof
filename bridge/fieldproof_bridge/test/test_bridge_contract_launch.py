"""Contract test of the bridge against the reference UGV (task P1-T03, the P1-T03 verification).

Starts a headless Gazebo server with the depot world of fieldproof_worlds, spawns the UGV at the
world's `spawn_dock` frame through the spawn launch of fieldproof_robots (without its
robot_state_publisher) and starts bridge.launch.py on a bundle generated from the UGV manifest.
From an rclpy node in the test process:

1. the contract checker passes: every topic of contract.yaml has a publisher (or subscription)
   of the declared type and QoS, carries the declared frame, publishes at the declared rate in
   simulation time, and nothing beyond the contract is visible;
2. the IMU and the GNSS fix carry the covariance and status of a real driver;
3. a ROS velocity command drives the robot (the ROS -> Gazebo direction) and the odometry follows;
4. after shutdown, the spawn succeeded and the bridge nodes exited cleanly.

The Gazebo partition and the ROS domain are private to the test process, so a stack started with
`make up` on the same host is neither disturbed nor observed. Collected by the launch_testing
pytest plugin under colcon test.
"""

# launch_testing runs unittest.TestCase classes, whose assertion methods ruff's pytest rule
# PT009 would otherwise flag.
# ruff: noqa: PT009

from __future__ import annotations

import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# Isolation from any other Gazebo server or ROS graph on the host; set before the transports load.
os.environ["GZ_PARTITION"] = f"fieldproof-bridge-test-{os.getpid()}"
os.environ["ROS_DOMAIN_ID"] = str(os.getpid() % 100 + 1)
os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"

import launch_testing
import launch_testing.actions
import launch_testing.asserts
import pytest
import rclpy
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import Twist
from launch.actions import ExecuteProcess, IncludeLaunchDescription, RegisterEventHandler
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from nav_msgs.msg import Odometry
from rclpy.qos import QoSProfile
from sensor_msgs.msg import Imu, NavSatFix, NavSatStatus

import launch
from fieldproof_bridge.contract import Contract, ContractChecker, spin_in_background
from fieldproof_bridge.generator import generate

WORLD = "depot"
SPAWN_FRAME = "spawn_dock"
WORLD_FILE = Path(get_package_share_directory("fieldproof_worlds")) / "worlds" / f"{WORLD}.sdf"
ROBOTS_SHARE = Path(get_package_share_directory("fieldproof_robots"))
BRIDGE_SHARE = Path(get_package_share_directory("fieldproof_bridge"))
MANIFEST = ROBOTS_SHARE / "models/ugv_ref/robot.yaml"
# The bundle the bridge launch starts from; generated once here so the test reads the same
# contract the launch runs.
BUNDLE = Path(tempfile.mkdtemp(prefix="fieldproof_bridge_contract_"))

# Under software rendering the server needs tens of seconds to load ogre2 and the first frames.
TIMEOUT_S = 300.0
DRIVE_TIMEOUT_S = 180.0
DRIVE_SPEED_MPS = 0.5
DRIVE_DISTANCE_M = 0.5


def frame_pose(name: str) -> tuple[str, str, str]:
    """``x y yaw`` of a named frame of the world, as the spawn launch takes them."""
    for frame in ET.parse(WORLD_FILE).getroot().iter("frame"):  # noqa: S314  (our own file)
        if frame.get("name") == name:
            x, y, _z, _roll, _pitch, yaw = (frame.findtext("pose") or "").split()
            return x, y, yaw
    raise AssertionError(f"{WORLD_FILE} has no frame {name}")


@pytest.mark.launch_test
def generate_test_description() -> tuple[Any, dict[str, Any]]:
    generate(MANIFEST, BUNDLE)
    # The worlds' own models and the Fuel models come with the sourced install space of
    # fieldproof_worlds (environment hook); the robot models are added here.
    resource_path = os.pathsep.join(
        p for p in (str(ROBOTS_SHARE / "models"), os.environ.get("GZ_SIM_RESOURCE_PATH", "")) if p
    )
    x, y, yaw = frame_pose(SPAWN_FRAME)
    server = ExecuteProcess(
        cmd=["gz", "sim", "-s", "-r", "--headless-rendering", "-v", "2", str(WORLD_FILE)],
        name="gz_server",
        output="screen",
        env={**os.environ, "GZ_SIM_RESOURCE_PATH": resource_path},
    )
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
        PythonLaunchDescriptionSource(str(ROBOTS_SHARE / "launch/spawn_ugv_ref.launch.py")),
        launch_arguments={
            "world": WORLD,
            "robot_state_publisher": "false",
            "x": x,
            "y": y,
            "yaw": yaw,
        }.items(),
    )
    bridge = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(str(BRIDGE_SHARE / "launch/bridge.launch.py")),
        launch_arguments={"bundle": str(BUNDLE)}.items(),
    )
    description = launch.LaunchDescription(
        [
            server,
            wait_for_world,
            RegisterEventHandler(
                OnProcessExit(target_action=wait_for_world, on_exit=[spawn, bridge])
            ),
            launch_testing.actions.ReadyToTest(),
        ]
    )
    return description, {"server": server}


class Latest:
    """Keeps the last message of a subscription."""

    def __init__(self) -> None:
        self.msg: Any = None
        self.count = 0

    def __call__(self, msg: Any) -> None:
        self.msg = msg
        self.count += 1


def wait_until(predicate: Any, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(0.1)


class TestBridgeContract(unittest.TestCase):
    node: Any
    stop_spinning: Any
    contract: Contract
    findings: list[Any]

    @classmethod
    def setUpClass(cls) -> None:
        rclpy.init()
        cls.node = rclpy.create_node("bridge_contract_test")
        cls.stop_spinning = spin_in_background(cls.node)
        cls.contract = Contract.load(BUNDLE)
        checker = ContractChecker(cls.node, cls.contract)
        cls.findings = checker.run(TIMEOUT_S)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stop_spinning()
        cls.node.destroy_node()
        rclpy.try_shutdown()

    def test_01_contract_holds(self) -> None:
        report = "\n".join(str(f) for f in self.findings)
        failed = [f for f in self.findings if not f.ok]
        self.assertGreater(len(self.findings), 20, report)
        self.assertEqual(failed, [], f"contract violations:\n{report}")
        checked = {f.topic for f in self.findings}
        for topic in ("/scan", "/imu/data", "/odom", "/camera/image", "/camera/camera_info", "/tf"):
            self.assertIn(topic, checked)

    def test_02_imu_and_fix_look_like_a_driver(self) -> None:
        imu, fix = Latest(), Latest()
        qos = QoSProfile(depth=10)
        imu_sub = self.node.create_subscription(Imu, "/imu/data", imu, qos)
        fix_sub = self.node.create_subscription(NavSatFix, "/gps/fix", fix, qos)
        try:
            wait_until(
                lambda: imu.msg is not None and fix.msg is not None, TIMEOUT_S, "IMU and fix"
            )
        finally:
            self.node.destroy_subscription(imu_sub)
            self.node.destroy_subscription(fix_sub)
        self.assertEqual(imu.msg.header.frame_id, "imu_link")
        self.assertAlmostEqual(imu.msg.angular_velocity_covariance[0], 0.009**2)
        self.assertAlmostEqual(imu.msg.linear_acceleration_covariance[8], 0.021**2)
        self.assertEqual(fix.msg.header.frame_id, "gps_link")
        self.assertEqual(fix.msg.status.status, NavSatStatus.STATUS_FIX)
        self.assertEqual(fix.msg.status.service, NavSatStatus.SERVICE_GPS)
        self.assertEqual(fix.msg.position_covariance_type, NavSatFix.COVARIANCE_TYPE_DIAGONAL_KNOWN)
        self.assertAlmostEqual(fix.msg.position_covariance[0], 1.0)
        self.assertAlmostEqual(fix.msg.position_covariance[8], 4.0)
        self.assertGreater(fix.msg.latitude, 0.0, "a fix at the world's geodetic origin")

    def test_03_ros_command_drives_the_robot(self) -> None:
        odom = Latest()
        subscription = self.node.create_subscription(Odometry, "/odom", odom, QoSProfile(depth=10))
        publisher = self.node.create_publisher(Twist, "/cmd_vel", QoSProfile(depth=10))
        try:
            wait_until(lambda: odom.msg is not None, TIMEOUT_S, "odometry")
            wait_until(lambda: publisher.get_subscription_count() > 0, TIMEOUT_S, "the bridge")
            start = odom.msg.pose.pose.position
            forward = Twist()
            forward.linear.x = DRIVE_SPEED_MPS
            deadline = time.monotonic() + DRIVE_TIMEOUT_S

            def travelled() -> float:
                position = odom.msg.pose.pose.position
                return float(((position.x - start.x) ** 2 + (position.y - start.y) ** 2) ** 0.5)

            while travelled() < DRIVE_DISTANCE_M:
                publisher.publish(forward)
                self.assertLess(time.monotonic(), deadline, f"stuck at {travelled():.2f} m")
                time.sleep(0.1)
            self.assertGreater(odom.msg.pose.pose.position.x - start.x, 0.9 * DRIVE_DISTANCE_M)
            stop = Twist()
            for _ in range(20):
                publisher.publish(stop)
                time.sleep(0.1)
        finally:
            self.node.destroy_subscription(subscription)
            self.node.destroy_publisher(publisher)


@launch_testing.post_shutdown_test()
class TestAfterShutdown(unittest.TestCase):
    def test_spawn_succeeded(self, proc_info: Any) -> None:
        launch_testing.asserts.assertExitCodes(
            proc_info, allowable_exit_codes=[0], process="create"
        )

    def test_server_resolved_every_resource(self, proc_output: Any, server: Any) -> None:
        output = b"".join(event.text for event in proc_output[server]).decode(errors="replace")
        for marker in (
            "Unable to find",
            "Failed to load mesh",
            "Unable to load",
            "Missing material",
        ):
            self.assertNotIn(marker, output)
