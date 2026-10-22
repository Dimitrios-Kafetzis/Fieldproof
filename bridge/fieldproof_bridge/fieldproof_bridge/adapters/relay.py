"""Relay: republish a sensor topic as it is, as a fault injector (task P2-T03).

The bridge publishes the sensor on a hidden topic and the relay republishes it on the contract
topic with the declared QoS (and the contract frame when relabelled), so that the scenario's
timeline can drop or freeze the sensor between the simulator and the vehicle (`sensor.dropout`,
`sensor.freeze`). Any message type: the type name is a parameter, as for the throttle.

Parameters: ``input``, ``output``, ``type`` (ROS type, ``pkg/msg/Name``), ``frame_id`` (empty
keeps the bridge's), ``qos_*``; the faults ``dropout`` and ``freeze``.
"""

from __future__ import annotations

import sys
from typing import Any

from rclpy.node import Node
from rclpy.qos import QoSProfile
from rosidl_runtime_py.utilities import get_message

from fieldproof_bridge.adapters.common import declare_io_parameters, run
from fieldproof_bridge.adapters.faults import FaultInjection

NODE_NAME = "relay"


class RelayNode(FaultInjection, Node):
    """Republishes ``input`` on ``output``, through the fault filter."""

    def __init__(self) -> None:
        super().__init__(NODE_NAME)
        input_topic, output_topic, qos = declare_io_parameters(self)
        self.declare_parameter("type", "")
        self.declare_parameter("frame_id", "")
        type_name = str(self.get_parameter("type").value)
        self.frame_id = str(self.get_parameter("frame_id").value)
        if not type_name:
            msg = f"{self.get_name()}: the parameter type is required"
            raise ValueError(msg)
        message_type = get_message(type_name)
        self.init_faults()
        self.publisher = self.create_publisher(message_type, output_topic, qos)
        self.subscription = self.create_subscription(
            message_type, input_topic, self.on_message, QoSProfile(depth=10)
        )
        self.get_logger().info(f"{input_topic} -> {output_topic} ({type_name})")

    def on_message(self, msg: Any) -> None:
        """Relabel if asked, then publish unless a fault holds the message."""
        if self.frame_id and hasattr(msg, "header"):
            msg.header.frame_id = self.frame_id
        out = self.inject(msg)
        if out is not None:
            self.publisher.publish(out)


def main(argv: list[str] | None = None) -> int:
    """Console script entry point."""
    return run(RelayNode, argv)


if __name__ == "__main__":
    sys.exit(main())
