"""Command relay: the vehicle's velocity command on its way to the simulator (task P2-T03).

The vehicle's stack publishes the drive command on the contract topic (`cmd_vel`); the relay
subscribes to it with the declared QoS and republishes on the hidden topic the bridge forwards to
Gazebo. In between, the `actuator.limit` event caps what the actuators deliver: the linear and
angular speeds are clamped to the limits (a motor controller derated, a wheel slipping), and the
`sensor.*` faults apply too (a drive that drops or holds its last command).

Parameters: ``input`` (the contract topic), ``output`` (the hidden topic), ``qos_*``; the
faults ``dropout``, ``freeze``, ``max_linear_mps`` and ``max_angular_radps`` (0 = no limit).
"""

from __future__ import annotations

import sys
from typing import Any

from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.qos import QoSProfile

from fieldproof_bridge.adapters.common import declare_io_parameters, run
from fieldproof_bridge.adapters.faults import FaultInjection, non_negative

NODE_NAME = "command_relay"


def clamp(value: float, limit: float) -> float:
    """``value`` within ``[-limit, limit]`` (no limit when ``limit`` is 0)."""
    if limit <= 0:
        return value
    return max(-limit, min(limit, value))


def limit_twist(msg: Any, max_linear: float, max_angular: float) -> Any:
    """Clamp the linear and angular speeds of ``msg`` in place and return it."""
    msg.linear.x = clamp(float(msg.linear.x), max_linear)
    msg.linear.y = clamp(float(msg.linear.y), max_linear)
    msg.linear.z = clamp(float(msg.linear.z), max_linear)
    msg.angular.x = clamp(float(msg.angular.x), max_angular)
    msg.angular.y = clamp(float(msg.angular.y), max_angular)
    msg.angular.z = clamp(float(msg.angular.z), max_angular)
    return msg


class CommandRelay(FaultInjection, Node):
    """Relays ``input`` (the vehicle's command) to ``output`` (the bridge's), limited."""

    def __init__(self) -> None:
        super().__init__(NODE_NAME)
        input_topic, output_topic, qos = declare_io_parameters(self)
        self.max_linear = 0.0
        self.max_angular = 0.0
        self.init_faults()
        self.declare_fault("max_linear_mps", 0.0, self._set_max_linear)
        self.declare_fault("max_angular_radps", 0.0, self._set_max_angular)
        self.publisher = self.create_publisher(Twist, output_topic, QoSProfile(depth=10))
        self.subscription = self.create_subscription(Twist, input_topic, self.on_command, qos)
        self.get_logger().info(f"{input_topic} -> {output_topic} (geometry_msgs/msg/Twist)")

    def _set_max_linear(self, value: Any) -> bool:
        limit = non_negative(value)
        if limit is None:
            return False
        self.max_linear = limit
        return True

    def _set_max_angular(self, value: Any) -> bool:
        limit = non_negative(value)
        if limit is None:
            return False
        self.max_angular = limit
        return True

    def on_command(self, msg: Any) -> None:
        """Limit, then publish unless a fault holds the command."""
        out = self.inject(limit_twist(msg, self.max_linear, self.max_angular))
        if out is not None:
            self.publisher.publish(out)


def main(argv: list[str] | None = None) -> int:
    """Console script entry point."""
    return run(CommandRelay, argv)


if __name__ == "__main__":
    sys.exit(main())
