"""Unit tests of the fault injection logic (task P2-T03): the pass-through filter, the GNSS
denial and offset, the IMU bias, the command limit and the battery model, as pure functions
and through the mixin on a stand-in node.

They build ROS messages with the generated Python classes, so they run under colcon test in the
dev container (and are skipped where the ROS message packages are not installed).
"""

from __future__ import annotations

import math
from array import array
from typing import Any

import pytest

pytest.importorskip("sensor_msgs.msg")

from geometry_msgs.msg import Twist
from rcl_interfaces.msg import SetParametersResult
from rclpy.parameter import Parameter
from sensor_msgs.msg import BatteryState, Imu, NavSatFix, NavSatStatus

from fieldproof_bridge.adapters.battery import Drain, battery_state
from fieldproof_bridge.adapters.command import clamp, limit_twist
from fieldproof_bridge.adapters.faults import FaultInjection, non_negative, vector3
from fieldproof_bridge.adapters.imu import bias_imu
from fieldproof_bridge.adapters.navsat import METRES_PER_DEGREE, deny_fix, offset_fix


class StandIn(FaultInjection):
    """What the mixin needs of an rclpy node: parameters, a logger, the callback hook."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.callbacks: list[Any] = []
        self.logged: list[str] = []

    def declare_parameter(self, name: str, default: Any) -> None:
        self.values[name] = default

    def get_parameter(self, name: str) -> Any:
        return type("P", (), {"value": self.values[name]})()

    def add_on_set_parameters_callback(self, callback: Any) -> None:
        self.callbacks.append(callback)

    def get_logger(self) -> Any:
        logged = self.logged
        return type("L", (), {"info": staticmethod(logged.append)})()

    def set(self, name: str, value: Any) -> SetParametersResult:
        """What `ros2 param set` does: the callback decides, then the value is stored."""
        result: SetParametersResult = self.callbacks[0]([Parameter(name, value=value)])
        if result.successful:
            self.values[name] = value
        return result


def test_pass_through_dropout_and_freeze() -> None:
    node = StandIn()
    node.init_faults()
    assert node.values == {"dropout": False, "freeze": False}
    first, second, third = Imu(), Imu(), Imu()
    first.header.frame_id = "first"
    second.header.frame_id = "second"
    assert node.inject(first) is first
    assert node.set("freeze", True).successful
    assert node.inject(second) is first, "the last message repeats, as it was"
    assert node.inject(third) is first
    assert node.set("freeze", False).successful
    assert node.inject(third) is third
    assert node.set("dropout", True).successful
    assert node.inject(second) is None
    assert node.set("dropout", False).successful
    assert node.inject(second) is second
    assert node.logged == ["freeze = True", "freeze = False", "dropout = True", "dropout = False"]


def test_freeze_before_any_message_holds_the_first() -> None:
    node = StandIn()
    node.init_faults()
    node.set("freeze", True)
    first, second = Imu(), Imu()
    assert node.inject(first) is first
    assert node.inject(second) is first


def test_parameter_callback_refuses_bad_values_and_ignores_others() -> None:
    node = StandIn()
    node.init_faults()
    result = node.set("dropout", "yes")
    assert not result.successful
    assert result.reason == "dropout: 'yes' is not valid"
    assert node.dropout is False
    assert node.set("unrelated", 3).successful, "not a fault: someone else's parameter"
    node.declare_fault("limit", 0.0, lambda v: non_negative(v) is not None)
    assert node.set("limit", 2.5).successful
    assert not node.set("limit", -1.0).successful


def test_vector3_and_non_negative() -> None:
    assert vector3([1, 2.5, 3]) == (1.0, 2.5, 3.0)
    assert vector3(array("d", [0.5, 0.0, 0.0])) == (0.5, 0.0, 0.0), "what rclpy delivers"
    assert vector3((0.0, 0.0, 0.0)) == (0.0, 0.0, 0.0)
    assert vector3([1, 2]) is None
    assert vector3("1 2 3") is None
    assert vector3([1, "x", 3]) is None
    assert non_negative(0) == 0.0
    assert non_negative(1.5) == 1.5
    assert non_negative(-0.1) is None
    assert non_negative(True) is None
    assert non_negative("1") is None


def test_gps_deny_reports_no_fix() -> None:
    msg = NavSatFix()
    msg.latitude, msg.longitude, msg.altitude = 37.9, 23.7, 5.0
    msg.status.status = NavSatStatus.STATUS_FIX
    msg.position_covariance_type = NavSatFix.COVARIANCE_TYPE_DIAGONAL_KNOWN
    out = deny_fix(msg)
    assert out is msg
    assert msg.status.status == NavSatStatus.STATUS_NO_FIX
    assert math.isnan(msg.latitude)
    assert math.isnan(msg.longitude)
    assert math.isnan(msg.altitude)
    assert msg.position_covariance_type == NavSatFix.COVARIANCE_TYPE_UNKNOWN
    assert list(msg.position_covariance) == [0.0] * 9


def test_gps_offset_moves_the_fix_by_metres() -> None:
    msg = NavSatFix()
    msg.latitude, msg.longitude, msg.altitude = 37.936, 23.699, 5.0
    offset_fix(msg, 100.0, 50.0, 2.0)
    assert msg.latitude == pytest.approx(37.936 + 50.0 / METRES_PER_DEGREE)
    expected_lon = 23.699 + 100.0 / (METRES_PER_DEGREE * math.cos(math.radians(msg.latitude)))
    assert msg.longitude == pytest.approx(expected_lon)
    assert msg.altitude == pytest.approx(7.0)
    # About 111 km per degree: 100 m east is roughly 0.00114 degrees at 38 degrees north.
    assert (msg.longitude - 23.699) * METRES_PER_DEGREE * math.cos(math.radians(37.936)) == (
        pytest.approx(100.0, rel=1e-3)
    )
    denied = deny_fix(NavSatFix())
    offset_fix(denied, 100.0, 0.0, 0.0)
    assert math.isnan(denied.latitude), "no fix to move"


def test_imu_bias_adds_to_rates_and_accelerations() -> None:
    msg = Imu()
    msg.angular_velocity.z = 0.1
    msg.linear_acceleration.z = 9.81
    bias_imu(msg, (0.01, -0.02, 0.03), (0.5, 0.0, -0.1))
    assert msg.angular_velocity.x == pytest.approx(0.01)
    assert msg.angular_velocity.y == pytest.approx(-0.02)
    assert msg.angular_velocity.z == pytest.approx(0.13)
    assert msg.linear_acceleration.x == pytest.approx(0.5)
    assert msg.linear_acceleration.z == pytest.approx(9.71)


def test_command_limit_clamps_both_ways() -> None:
    assert clamp(2.0, 1.5) == 1.5
    assert clamp(-2.0, 1.5) == -1.5
    assert clamp(0.7, 1.5) == 0.7
    assert clamp(2.0, 0.0) == 2.0, "0 is no limit"
    msg = Twist()
    msg.linear.x, msg.angular.z = 1.2, -2.5
    limit_twist(msg, 0.5, 1.0)
    assert msg.linear.x == 0.5
    assert msg.angular.z == -1.0
    limit_twist(msg, 0.0, 0.0)
    assert (msg.linear.x, msg.angular.z) == (0.5, -1.0)


def test_battery_drain_ramps_linearly() -> None:
    drain = Drain(start_s=100.0, from_pct=1.0, to_pct=0.2, duration_s=40.0)
    assert drain.at(90.0) == (1.0, False)
    assert drain.at(100.0) == (1.0, False)
    halfway, ended = drain.at(120.0)
    assert halfway == pytest.approx(0.6)
    assert ended is False
    assert drain.at(140.0) == (0.2, True)
    assert drain.at(500.0) == (0.2, True)
    assert Drain(0.0, 1.0, 0.0, 0.0).at(0.0) == (0.0, True), "no duration: at once"


def test_battery_state_follows_the_percentage() -> None:
    full = battery_state(
        1.0, full_voltage=29.4, empty_voltage=21.0, capacity_ah=20.0, draining=False
    )
    assert full.voltage == pytest.approx(29.4)
    assert full.percentage == 1.0
    assert full.charge == pytest.approx(20.0)
    assert full.power_supply_status == BatteryState.POWER_SUPPLY_STATUS_FULL
    assert full.present is True
    half = battery_state(
        0.5, full_voltage=29.4, empty_voltage=21.0, capacity_ah=20.0, draining=True, frame_id="b"
    )
    assert half.voltage == pytest.approx(25.2)
    assert half.power_supply_status == BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
    assert half.header.frame_id == "b"
    empty = battery_state(
        0.0, full_voltage=29.4, empty_voltage=21.0, capacity_ah=20.0, draining=False
    )
    assert empty.voltage == pytest.approx(21.0)
    assert empty.power_supply_status == BatteryState.POWER_SUPPLY_STATUS_NOT_CHARGING
    assert math.isnan(empty.current)
