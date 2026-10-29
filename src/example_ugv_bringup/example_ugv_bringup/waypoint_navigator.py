"""Waypoint navigator of the example UGV: a `NavigateToPose` action server on wheel odometry.

The example vehicle's autonomy, as a field team's repository would ship it: no Nav2, no SLAM, a
few hundred lines of rclpy written for a yard robot. The node offers the `navigate_to_pose`
action (`nav2_msgs/action/NavigateToPose`, the interface an operator's tablet and Fieldproof's
`navigate_to_pose` event both speak), drives the differential base with a proportional heading
controller on the wheel odometry (`odom`), and holds position while the lidar (`scan`) sees an
obstacle in its front sector, publishing zero velocity until the sector is clear again. Its
localisation is the identity `map -> odom` transform it broadcasts once: the map frame starts
where the vehicle started. `mission/state` (`std_msgs/msg/String`: `idle`, `driving`,
`holding`, `done`, `aborted`) reports what it does. Every name is relative, so the node runs
under a fleet namespace unchanged.

Nothing here knows about Fieldproof: in the field the node reads the odometry of the motor
controller and the scan of the lidar driver; in Fieldproof the bridge publishes the same topics
from the simulator (robot.yaml, `runtime.drivers`) and the clock is simulated (`use_sim_time`).
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import rclpy
from geometry_msgs.msg import PoseStamped, TransformStamped, Twist
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import Odometry
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from tf2_ros import StaticTransformBroadcaster

IDLE, DRIVING, HOLDING, DONE, ABORTED = "idle", "driving", "holding", "done", "aborted"
# Goal frames the navigator follows: its map frame is its odom frame, so both are the same.
GOAL_FRAMES = ("map", "odom", "")
PARAMETERS: dict[str, float] = {
    "max_linear_mps": 0.5,
    "max_angular_radps": 1.0,
    "yaw_gain": 1.5,
    "slow_angle_rad": 0.5,
    "goal_tolerance_m": 0.2,
    "approach_distance_m": 0.5,
    "stop_distance_m": 0.6,
    "clear_distance_m": 0.8,
    "front_sector_rad": 0.5,
    "control_rate_hz": 20.0,
    "odom_timeout_s": 10.0,
}


def yaw_of(orientation: Any) -> float:
    """The yaw of a quaternion message."""
    q = orientation
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def wrap(angle: float) -> float:
    """An angle folded into [-pi, pi]."""
    return math.atan2(math.sin(angle), math.cos(angle))


def clamp(value: float, limit: float) -> float:
    """`value` within [-limit, limit]."""
    return max(-limit, min(limit, value))


class WaypointNavigator(Node):  # type: ignore[misc]
    """Drive to a goal on the odometry, hold on obstacles, report the state."""

    def __init__(self) -> None:
        super().__init__("waypoint_navigator")
        self.p = {
            name: float(self.declare_parameter(name, default).value)
            for name, default in PARAMETERS.items()
        }
        self.map_frame = str(self.declare_parameter("map_frame", "map").value)
        self.odom_frame = str(self.declare_parameter("odom_frame", "odom").value)
        self.lock = threading.Lock()
        self.pose: tuple[float, float, float] | None = None
        self.front_range = math.inf
        self.state = IDLE
        self.busy = False
        group = ReentrantCallbackGroup()
        # The odometry is reliable (a motor controller's default); the lidar driver publishes
        # best effort, as the bridge does when the manifest says so: a sensor-data subscriber
        # matches either.
        self.create_subscription(Odometry, "odom", self.on_odom, 10, callback_group=group)
        self.create_subscription(
            LaserScan, "scan", self.on_scan, qos_profile_sensor_data, callback_group=group
        )
        self.command = self.create_publisher(Twist, "cmd_vel", 10)
        self.status = self.create_publisher(
            String,
            "mission/state",
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
        )
        self.create_timer(1.0, self.publish_state, callback_group=group)
        self.server = ActionServer(
            self,
            NavigateToPose,
            "navigate_to_pose",
            execute_callback=self.execute,
            goal_callback=self.on_goal,
            cancel_callback=self.on_cancel,
            callback_group=group,
        )
        self.broadcaster = StaticTransformBroadcaster(self)
        self.broadcast_map()
        self.publish_state()
        self.get_logger().info(
            "waypoint navigator up: navigate_to_pose on wheel odometry, holding on obstacles"
        )

    # ------------------------------------------------------------------------ the inputs
    def on_odom(self, message: Odometry) -> None:
        """The wheel odometry: the pose the controller drives on."""
        pose = message.pose.pose
        with self.lock:
            self.pose = (pose.position.x, pose.position.y, yaw_of(pose.orientation))

    def on_scan(self, scan: LaserScan) -> None:
        """The nearest return in the front sector."""
        nearest = math.inf
        angle = scan.angle_min
        for value in scan.ranges:
            if (
                abs(wrap(angle)) <= self.p["front_sector_rad"]
                and math.isfinite(value)
                and scan.range_min <= value <= scan.range_max
            ):
                nearest = min(nearest, value)
            angle += scan.angle_increment
        with self.lock:
            self.front_range = nearest

    # ------------------------------------------------------------------------ the outputs
    def broadcast_map(self) -> None:
        """The identity `map -> odom`: the map frame starts where the vehicle started."""
        transform = TransformStamped()
        transform.header.stamp = self.get_clock().now().to_msg()
        transform.header.frame_id = self.map_frame
        transform.child_frame_id = self.odom_frame
        transform.transform.rotation.w = 1.0
        self.broadcaster.sendTransform(transform)

    def set_state(self, state: str) -> None:
        """Change the mission state and publish it."""
        if state != self.state:
            self.get_logger().info(f"mission {state}")
        self.state = state
        self.publish_state()

    def publish_state(self) -> None:
        """`mission/state`, on every change and once a second."""
        self.status.publish(String(data=self.state))

    def stop(self) -> None:
        """Zero velocity."""
        self.command.publish(Twist())

    # ------------------------------------------------------------------------- the action
    def on_goal(self, request: Any) -> GoalResponse:
        """One goal at a time, in the map (or odom) frame."""
        frame = request.pose.header.frame_id
        if frame not in GOAL_FRAMES:
            self.get_logger().warning(f"goal in frame {frame!r} rejected (not {self.map_frame})")
            return GoalResponse.REJECT
        if self.busy:
            self.get_logger().warning("goal rejected: a goal is being followed")
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def on_cancel(self, _goal_handle: Any) -> CancelResponse:
        """A goal may always be cancelled."""
        return CancelResponse.ACCEPT

    def execute(self, goal_handle: Any) -> Any:
        """Follow the goal at the control rate until it is reached, cancelled or aborted."""
        self.busy = True
        try:
            return self._follow(goal_handle)
        finally:
            self.busy = False

    def _follow(self, goal_handle: Any) -> Any:  # noqa: PLR0915
        goal = goal_handle.request.pose.pose
        target = (goal.position.x, goal.position.y)
        period = 1.0 / self.p["control_rate_hz"]
        started = self.get_clock().now()
        self.get_logger().info(f"goal ({target[0]:.2f}, {target[1]:.2f}) accepted")
        waited = 0.0
        while self.pose is None and waited < self.p["odom_timeout_s"]:
            time.sleep(period)
            waited += period
        if self.pose is None:
            self.get_logger().error("no odometry: goal aborted")
            self.set_state(ABORTED)
            goal_handle.abort()
            return NavigateToPose.Result()
        feedback = NavigateToPose.Feedback()
        holding = False
        self.set_state(DRIVING)
        while rclpy.ok():
            if goal_handle.is_cancel_requested:
                self.stop()
                goal_handle.canceled()
                self.set_state(IDLE)
                self.get_logger().info("goal cancelled")
                return NavigateToPose.Result()
            with self.lock:
                x, y, yaw = self.pose
                front = self.front_range
            dx, dy = target[0] - x, target[1] - y
            distance = math.hypot(dx, dy)
            if distance < self.p["goal_tolerance_m"]:
                break
            if holding and front > self.p["clear_distance_m"]:
                holding = False
                self.set_state(DRIVING)
            elif not holding and front < self.p["stop_distance_m"]:
                holding = True
                self.get_logger().info(f"obstacle {front:.2f} m ahead: holding")
                self.set_state(HOLDING)
            twist = Twist()
            if not holding:
                error = wrap(math.atan2(dy, dx) - yaw)
                twist.angular.z = clamp(self.p["yaw_gain"] * error, self.p["max_angular_radps"])
                if abs(error) < self.p["slow_angle_rad"]:
                    scale = min(1.0, distance / self.p["approach_distance_m"])
                    twist.linear.x = self.p["max_linear_mps"] * scale
            self.command.publish(twist)
            current = PoseStamped()
            current.header.stamp = self.get_clock().now().to_msg()
            current.header.frame_id = self.map_frame
            current.pose.position.x, current.pose.position.y = x, y
            current.pose.orientation.z = math.sin(yaw / 2)
            current.pose.orientation.w = math.cos(yaw / 2)
            feedback.current_pose = current
            feedback.distance_remaining = float(distance)
            feedback.navigation_time = (self.get_clock().now() - started).to_msg()
            goal_handle.publish_feedback(feedback)
            time.sleep(period)
        self.stop()
        goal_handle.succeed()
        self.set_state(DONE)
        elapsed = (self.get_clock().now() - started).nanoseconds / 1e9
        self.get_logger().info(f"goal reached after {elapsed:.1f} s")
        return NavigateToPose.Result()


def main() -> None:
    """Spin the navigator on a few threads (the action runs while the inputs keep arriving)."""
    rclpy.init()
    node = WaypointNavigator()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
