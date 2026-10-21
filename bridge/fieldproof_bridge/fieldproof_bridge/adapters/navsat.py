"""GNSS fix adapter: frame, position covariance and status of a real receiver.

Gazebo Harmonic's NavSat system publishes the fix without a frame in the header (gz-sim issue
2236), and the ros_gz bridge converts it with an all-zero covariance of type ``UNKNOWN`` and an
empty status. A receiver driver reports the frame of its antenna, the covariance it estimates
(here ``stddev**2`` of the horizontal and vertical noise the model simulates, type
``DIAGONAL_KNOWN``), a status of ``STATUS_FIX`` and the constellation it uses, so the adapter sets
all four.

Parameters: ``input``, ``output``, ``frame_id``, ``horizontal_stddev`` [m], ``vertical_stddev``
[m], ``service`` (sensor_msgs/NavSatStatus bit field, 1 = GPS), ``qos_*``; the faults
``dropout``, ``freeze``, ``deny`` (the receiver reports no fix: `STATUS_NO_FIX`, NaN position,
unknown covariance, as under jamming; the `gps.deny` event, task P2-T03) and ``offset_enu_m``
(east, north, up metres added to every fix: the `gps.offset` event).
"""

from __future__ import annotations

import math
import sys
from typing import Any

from rclpy.node import Node
from rclpy.qos import QoSProfile
from sensor_msgs.msg import NavSatFix, NavSatStatus

from fieldproof_bridge.adapters.common import declare_io_parameters, run
from fieldproof_bridge.adapters.faults import FaultInjection, vector3

NODE_NAME = "navsat_adapter"
ZERO = (0.0, 0.0, 0.0)
# WGS84: metres per degree of latitude, and of longitude at the equator.
EARTH_RADIUS_M = 6378137.0
METRES_PER_DEGREE = math.pi * EARTH_RADIUS_M / 180.0


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


def deny_fix(msg: Any) -> Any:
    """Turn ``msg`` into the report of a receiver without a fix, in place."""
    msg.status.status = NavSatStatus.STATUS_NO_FIX
    msg.latitude = msg.longitude = msg.altitude = float("nan")
    msg.position_covariance = [0.0] * 9
    msg.position_covariance_type = NavSatFix.COVARIANCE_TYPE_UNKNOWN
    return msg


def offset_fix(msg: Any, east_m: float, north_m: float, up_m: float) -> Any:
    """Move the fix by metres east, north and up, in place (flat Earth at the fix's latitude)."""
    if math.isnan(msg.latitude) or math.isnan(msg.longitude):
        return msg
    msg.latitude += north_m / METRES_PER_DEGREE
    cos_lat = math.cos(math.radians(msg.latitude))
    if abs(cos_lat) > 1e-9:
        msg.longitude += east_m / (METRES_PER_DEGREE * cos_lat)
    msg.altitude += up_m
    return msg


class NavSatAdapter(FaultInjection, Node):
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
        self.deny = False
        self.offset = ZERO
        self.init_faults()
        self.declare_fault("deny", False, self._set_deny)
        self.declare_fault("offset_enu_m", list(ZERO), self._set_offset)
        self.publisher = self.create_publisher(NavSatFix, output_topic, qos)
        self.subscription = self.create_subscription(
            NavSatFix, input_topic, self.on_fix, QoSProfile(depth=10)
        )
        self.get_logger().info(f"{input_topic} -> {output_topic} (frame {self.frame_id or 'kept'})")

    def _set_deny(self, value: Any) -> bool:
        if not isinstance(value, bool):
            return False
        self.deny = value
        return True

    def _set_offset(self, value: Any) -> bool:
        offset = vector3(value)
        if offset is None:
            return False
        self.offset = offset
        return True

    def on_fix(self, msg: Any) -> None:
        """Fill, offset or deny, and republish one fix, unless a fault holds it."""
        fill_navsat(
            msg,
            frame_id=self.frame_id,
            horizontal_stddev=self.horizontal_stddev,
            vertical_stddev=self.vertical_stddev,
            service=self.service,
        )
        if self.deny:
            deny_fix(msg)
        elif self.offset != ZERO:
            offset_fix(msg, *self.offset)
        out = self.inject(msg)
        if out is not None:
            self.publisher.publish(out)


def main(argv: list[str] | None = None) -> int:
    """Console script entry point."""
    return run(NavSatAdapter, argv)


if __name__ == "__main__":
    sys.exit(main())
