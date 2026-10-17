"""Unit tests for the adapter logic (task P1-T03): message filling and throttling.

They build ROS messages with the generated Python classes, so they run under colcon test in the
dev container (and are skipped where the ROS message packages are not installed).
"""

from __future__ import annotations

import pytest

pytest.importorskip("sensor_msgs.msg")

from sensor_msgs.msg import Imu, JointState, NavSatFix, NavSatStatus
from std_msgs.msg import String

from fieldproof_bridge.adapters.imu import diagonal, fill_imu
from fieldproof_bridge.adapters.navsat import fill_navsat
from fieldproof_bridge.adapters.throttle import Throttle, stamp_seconds


def test_imu_covariance_diagonals() -> None:
    msg = Imu()
    msg.header.frame_id = "imu_link"
    out = fill_imu(msg, angular_velocity_stddev=0.009, linear_acceleration_stddev=0.021)
    assert out is msg
    assert list(msg.angular_velocity_covariance) == pytest.approx(diagonal(0.009))
    assert msg.angular_velocity_covariance[0] == pytest.approx(0.009**2)
    assert msg.angular_velocity_covariance[1] == 0.0
    assert list(msg.linear_acceleration_covariance) == pytest.approx(diagonal(0.021))
    assert list(msg.orientation_covariance) == [0.0] * 9, "unknown unless a stddev is given"
    assert msg.header.frame_id == "imu_link", "an empty frame_id keeps Gazebo's"


def test_imu_relabel_and_orientation() -> None:
    msg = fill_imu(Imu(), frame_id="ugv1_imu_link", orientation_stddev=0.01)
    assert msg.header.frame_id == "ugv1_imu_link"
    assert msg.orientation_covariance[4] == pytest.approx(1e-4)
    assert list(msg.angular_velocity_covariance) == [0.0] * 9


def test_navsat_fix_fields() -> None:
    msg = NavSatFix()
    msg.latitude = 37.9
    out = fill_navsat(msg, frame_id="gps_link", horizontal_stddev=1.0, vertical_stddev=2.0)
    assert out is msg
    assert msg.header.frame_id == "gps_link"
    assert list(msg.position_covariance) == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 4.0]
    assert msg.position_covariance_type == NavSatFix.COVARIANCE_TYPE_DIAGONAL_KNOWN
    assert msg.status.status == NavSatStatus.STATUS_FIX
    assert msg.status.service == NavSatStatus.SERVICE_GPS
    assert msg.latitude == 37.9


def test_navsat_without_noise_keeps_covariance_unknown() -> None:
    msg = fill_navsat(NavSatFix(), service=NavSatStatus.SERVICE_GALILEO)
    assert msg.position_covariance_type == NavSatFix.COVARIANCE_TYPE_UNKNOWN
    assert msg.status.service == NavSatStatus.SERVICE_GALILEO
    assert msg.header.frame_id == ""


def test_throttle_passes_one_message_per_period() -> None:
    throttle = Throttle(50.0)
    stamps = [i * 0.001 for i in range(1000)]  # 1 kHz for one second
    passed = [s for s in stamps if throttle.accept(s)]
    assert len(passed) == 50
    assert passed[:3] == pytest.approx([0.0, 0.02, 0.04])


def test_throttle_passes_slower_input_untouched() -> None:
    throttle = Throttle(50.0)
    stamps = [i * 0.1 for i in range(20)]  # 10 Hz input
    assert all(throttle.accept(s) for s in stamps)


def test_throttle_restarts_when_time_goes_backwards() -> None:
    throttle = Throttle(10.0)
    assert throttle.accept(5.0)
    assert not throttle.accept(5.05)
    assert throttle.accept(0.0), "a simulation reset must not block the output"
    assert not throttle.accept(0.05)
    assert throttle.accept(0.1)


def test_throttle_rejects_bad_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        Throttle(0.0)


def test_stamp_seconds() -> None:
    msg = JointState()
    msg.header.stamp.sec = 12
    msg.header.stamp.nanosec = 500_000_000
    assert stamp_seconds(msg) == pytest.approx(12.5)
    assert stamp_seconds(String()) is None
