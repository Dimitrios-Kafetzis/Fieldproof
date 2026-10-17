"""IMU adapter: covariance from the manifest's noise figures (and the frame, if relabelled).

The ros_gz bridge converts ``gz.msgs.IMU`` to ``sensor_msgs/msg/Imu`` with every covariance left at
zero, which sensor_msgs defines as "unknown". A real IMU driver fills the diagonals from the data
sheet, and a localisation stack (robot_localization, Nav2's EKF) reads them, so the adapter sets
``stddev**2`` on the diagonals of the angular velocity and linear acceleration covariances. The
orientation covariance stays unknown unless an ``orientation_stddev`` is given (Gazebo's IMU
orientation is exact).

Parameters: ``input``, ``output``, ``frame_id`` (empty keeps Gazebo's), ``angular_velocity_stddev``
[rad/s], ``linear_acceleration_stddev`` [m/s²], ``orientation_stddev`` [rad], ``qos_*``.
"""

from __future__ import annotations

import sys
from typing import Any

from rclpy.node import Node
from rclpy.qos import QoSProfile
from sensor_msgs.msg import Imu

from fieldproof_bridge.adapters.common import declare_io_parameters, run

NODE_NAME = "imu_adapter"


def diagonal(stddev: float) -> list[float]:
    """A 3x3 covariance (row major) with ``stddev**2`` on the diagonal."""
    variance = float(stddev) ** 2
    return [variance, 0.0, 0.0, 0.0, variance, 0.0, 0.0, 0.0, variance]


def fill_imu(
    msg: Any,
    *,
    frame_id: str = "",
    angular_velocity_stddev: float = 0.0,
    linear_acceleration_stddev: float = 0.0,
    orientation_stddev: float = 0.0,
) -> Any:
    """Set the frame and the covariance diagonals of ``msg`` in place and return it."""
    if frame_id:
        msg.header.frame_id = frame_id
    if angular_velocity_stddev > 0:
        msg.angular_velocity_covariance = diagonal(angular_velocity_stddev)
    if linear_acceleration_stddev > 0:
        msg.linear_acceleration_covariance = diagonal(linear_acceleration_stddev)
    if orientation_stddev > 0:
        msg.orientation_covariance = diagonal(orientation_stddev)
    return msg


class ImuAdapter(Node):
    """Republishes the bridged IMU with the frame and covariances of the contract."""

    def __init__(self) -> None:
        super().__init__(NODE_NAME)
        input_topic, output_topic, qos = declare_io_parameters(self)
        self.declare_parameter("frame_id", "")
        self.declare_parameter("angular_velocity_stddev", 0.0)
        self.declare_parameter("linear_acceleration_stddev", 0.0)
        self.declare_parameter("orientation_stddev", 0.0)
        self.frame_id = str(self.get_parameter("frame_id").value)
        self.angular_velocity_stddev = float(self.get_parameter("angular_velocity_stddev").value)
        self.linear_acceleration_stddev = float(
            self.get_parameter("linear_acceleration_stddev").value
        )
        self.orientation_stddev = float(self.get_parameter("orientation_stddev").value)
        self.publisher = self.create_publisher(Imu, output_topic, qos)
        self.subscription = self.create_subscription(
            Imu, input_topic, self.on_imu, QoSProfile(depth=10)
        )
        self.get_logger().info(f"{input_topic} -> {output_topic} (frame {self.frame_id or 'kept'})")

    def on_imu(self, msg: Any) -> None:
        """Fill and republish one message."""
        self.publisher.publish(
            fill_imu(
                msg,
                frame_id=self.frame_id,
                angular_velocity_stddev=self.angular_velocity_stddev,
                linear_acceleration_stddev=self.linear_acceleration_stddev,
                orientation_stddev=self.orientation_stddev,
            )
        )


def main(argv: list[str] | None = None) -> int:
    """Console script entry point."""
    return run(ImuAdapter, argv)


if __name__ == "__main__":
    sys.exit(main())
