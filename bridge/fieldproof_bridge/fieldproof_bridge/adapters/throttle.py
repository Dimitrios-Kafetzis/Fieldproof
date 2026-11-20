"""Throttle: republish a topic at the rate the contract declares.

Gazebo's JointStatePublisher system publishes every physics step (1 kHz at the 1 ms step of the
Fieldproof worlds) and has no rate setting, while a wheel-encoder driver publishes joint states at
tens of hertz. The throttle passes one message per period, judged by the header stamps (simulation
time), so the output rate holds whatever the real-time factor is. Messages without a header are
timed with the node clock.

Parameters: ``input``, ``output``, ``type`` (ROS type, ``pkg/msg/Name``), ``rate_hz``, ``qos_*``,
``input_depth`` (the subscription's queue: the generator sets a second of the physics-rate stream,
so a stall of this node on a loaded host delays the output instead of losing messages, issue #81);
the faults ``dropout`` and ``freeze`` (task P2-T03).
"""

from __future__ import annotations

import math
import sys
from typing import Any

from rclpy.node import Node
from rclpy.qos import QoSProfile
from rosidl_runtime_py.utilities import get_message

from fieldproof_bridge.adapters.common import declare_io_parameters, run
from fieldproof_bridge.adapters.faults import FaultInjection

NODE_NAME = "throttle"
# Stamps jitter by a float rounding; a message this close to the due time passes.
EPSILON_S = 1e-6
DEFAULT_INPUT_DEPTH = 10


class Throttle:
    """Decides which messages pass: the first one at or after each slot of a ``1 / rate_hz`` grid.

    The grid is anchored to the first stamp. A message that arrives after its slot, because the
    one due at the slot was lost between the simulator and this node (issue #81), fills the slot
    without moving the grid, so the output keeps the declared rate unless a whole slot goes by
    with no message at all.
    """

    def __init__(self, rate_hz: float) -> None:
        """``rate_hz`` must be positive."""
        if rate_hz <= 0:
            msg = f"rate_hz must be positive, got {rate_hz}"
            raise ValueError(msg)
        self.period_s = 1.0 / rate_hz
        self.last_s: float | None = None  # the last stamp that passed
        self.due_s = 0.0  # the next slot of the grid

    def accept(self, stamp_s: float) -> bool:
        """Return whether a message stamped ``stamp_s`` passes, and remember it if it does."""
        if self.last_s is None or stamp_s < self.last_s:
            # The first message, or time went backwards (simulation reset): start over.
            self.last_s = stamp_s
            self.due_s = stamp_s + self.period_s
            return True
        if stamp_s < self.due_s - EPSILON_S:
            return False
        # The slot this message fills; the next due is the slot after it, on the same grid.
        slots = math.floor((stamp_s - self.due_s) / self.period_s + EPSILON_S)
        self.due_s += (slots + 1) * self.period_s
        self.last_s = stamp_s
        return True


def stamp_seconds(msg: Any) -> float | None:
    """The header stamp of ``msg`` in seconds, or None when the type has no header."""
    header = getattr(msg, "header", None)
    if header is None:
        return None
    return float(header.stamp.sec) + float(header.stamp.nanosec) * 1e-9


class ThrottleNode(FaultInjection, Node):
    """Republishes ``input`` on ``output`` at ``rate_hz``."""

    def __init__(self) -> None:
        super().__init__(NODE_NAME)
        input_topic, output_topic, qos = declare_io_parameters(self)
        self.declare_parameter("type", "")
        self.declare_parameter("rate_hz", 0.0)
        self.declare_parameter("input_depth", DEFAULT_INPUT_DEPTH)
        type_name = str(self.get_parameter("type").value)
        rate_hz = float(self.get_parameter("rate_hz").value)
        input_depth = int(self.get_parameter("input_depth").value)
        if not type_name:
            msg = f"{self.get_name()}: the parameter type is required"
            raise ValueError(msg)
        message_type = get_message(type_name)
        self.throttle = Throttle(rate_hz)
        self.init_faults()
        self.publisher = self.create_publisher(message_type, output_topic, qos)
        self.subscription = self.create_subscription(
            message_type, input_topic, self.on_message, QoSProfile(depth=input_depth)
        )
        self.get_logger().info(
            f"{input_topic} -> {output_topic} at {rate_hz} Hz ({type_name}, queue {input_depth})"
        )

    def on_message(self, msg: Any) -> None:
        """Pass the message when its period is due."""
        stamp = stamp_seconds(msg)
        if stamp is None:
            stamp = self.get_clock().now().nanoseconds * 1e-9
        if self.throttle.accept(stamp):
            out = self.inject(msg)
            if out is not None:
                self.publisher.publish(out)


def main(argv: list[str] | None = None) -> int:
    """Console script entry point."""
    return run(ThrottleNode, argv)


if __name__ == "__main__":
    sys.exit(main())
