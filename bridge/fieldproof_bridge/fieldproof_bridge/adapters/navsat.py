"""GNSS fix adapter: frame, position covariance and status of a real receiver.

Gazebo Harmonic's NavSat system publishes the fix without a frame in the header (gz-sim issue
2236), and the ros_gz bridge converts it with an all-zero covariance of type ``UNKNOWN`` and an
empty status. A receiver driver reports the frame of its antenna, the covariance it estimates
(here ``stddev**2`` of the horizontal and vertical noise the model simulates, type
``DIAGONAL_KNOWN``), a status of ``STATUS_FIX`` and the constellation it uses, so the adapter sets
all four.

Parameters: ``input``, ``output``, ``frame_id``, ``horizontal_stddev`` [m], ``vertical_stddev``
[m], ``service`` (sensor_msgs/NavSatStatus bit field, 1 = GPS), ``qos_*``.
"""

from __future__ import annotations

import sys
from typing import Any

from rclpy.node import Node
from rclpy.qos import QoSProfile
from sensor_msgs.msg import NavSatFix, NavSatStatus

from fieldproof_bridge.adapters.common import declare_io_parameters, run

NODE_NAME = "navsat_adapter"


def fill_navsat(
    msg: Any,
    *,
    frame_id: str = "",
    horizontal_stddev: float = 0.0,
    vertical_stddev: float = 0.0,
    service: int = NavSatStatus.SERVICE_GPS,
) -> Any:
    """Set the frame, covariance and status of ``msg`` in place and return it."""
    if frame_id:
        msg.header.frame_id = frame_id
    if horizontal_stddev > 0 or vertical_stddev > 0:
        h, v = float(horizontal_stddev) ** 2, float(vertical_stddev) ** 2
        msg.position_covariance = [h, 0.0, 0.0, 0.0, h, 0.0, 0.0, 0.0, v]
        msg.position_covariance_type = NavSatFix.COVARIANCE_TYPE_DIAGONAL_KNOWN
    msg.status.status = NavSatStatus.STATUS_FIX
    msg.status.service = int(service)
    return msg


class NavSatAdapter(Node):
    """Republishes the bridged fix with the frame, covariance and status of the contract."""

    def __init__(self) -> None:
        super().__init__(NODE_NAME)
        input_topic, output_topic, qos = declare_io_parameters(self)
        self.declare_parameter("frame_id", "")
        self.declare_parameter("horizontal_stddev", 0.0)
        self.declare_parameter("vertical_stddev", 0.0)
        self.declare_parameter("service", int(NavSatStatus.SERVICE_GPS))
        self.frame_id = str(self.get_parameter("frame_id").value)
        self.horizontal_stddev = float(self.get_parameter("horizontal_stddev").value)
        self.vertical_stddev = float(self.get_parameter("vertical_stddev").value)
        self.service = int(self.get_parameter("service").value)
        self.publisher = self.create_publisher(NavSatFix, output_topic, qos)
        self.subscription = self.create_subscription(
            NavSatFix, input_topic, self.on_fix, QoSProfile(depth=10)
        )
        self.get_logger().info(f"{input_topic} -> {output_topic} (frame {self.frame_id or 'kept'})")

    def on_fix(self, msg: Any) -> None:
        """Fill and republish one fix."""
        self.publisher.publish(
            fill_navsat(
                msg,
                frame_id=self.frame_id,
                horizontal_stddev=self.horizontal_stddev,
                vertical_stddev=self.vertical_stddev,
                service=self.service,
            )
        )


def main(argv: list[str] | None = None) -> int:
    """Console script entry point."""
    return run(NavSatAdapter, argv)


if __name__ == "__main__":
    sys.exit(main())
