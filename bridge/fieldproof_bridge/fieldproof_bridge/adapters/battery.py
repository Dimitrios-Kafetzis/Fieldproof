"""Battery: the vehicle's battery state, as a battery monitor driver reports it (task P2-T03).

The reference vehicles simulate no power system, so the bridge publishes a synthetic
`sensor_msgs/msg/BatteryState` from the manifest's `power.battery` section: full (100 %, the
full voltage) until the `battery.drain` event makes it fall, linearly in simulation time, to a
target percentage over a duration (`drain_to`, `drain_duration_s`). The voltage follows the
percentage between the empty and the full voltage; the status is `DISCHARGING` while the drain
runs, `FULL` at 100 % and `NOT_CHARGING` otherwise. What the vehicle's stack does with a flat
battery is its business, as in the field.

Parameters: ``output``, ``rate_hz``, ``full_voltage``, ``empty_voltage``, ``capacity_ah``,
``frame_id``, ``qos_*``; the faults ``dropout``, ``freeze``, ``percentage`` (set the state at
once), ``drain_to`` (0 to 1) and ``drain_duration_s``.
"""

from __future__ import annotations

import sys
from typing import Any

from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile
from sensor_msgs.msg import BatteryState

from fieldproof_bridge.adapters.common import DURABILITY, RELIABILITY, run
from fieldproof_bridge.adapters.faults import FaultInjection, non_negative

NODE_NAME = "battery"


class Drain:
    """A linear ramp of the percentage from a start to a target over a duration."""

    def __init__(self, start_s: float, from_pct: float, to_pct: float, duration_s: float) -> None:
        self.start_s = start_s
        self.from_pct = from_pct
        self.to_pct = to_pct
        self.duration_s = duration_s

    def at(self, now_s: float) -> tuple[float, bool]:
        """The percentage at ``now_s`` and whether the ramp has ended."""
        if self.duration_s <= 0 or now_s >= self.start_s + self.duration_s:
            return self.to_pct, True
        if now_s <= self.start_s:
            return self.from_pct, False
        fraction = (now_s - self.start_s) / self.duration_s
        return self.from_pct + (self.to_pct - self.from_pct) * fraction, False


def battery_state(
    percentage: float,
    *,
    full_voltage: float,
    empty_voltage: float,
    capacity_ah: float,
    draining: bool,
    frame_id: str = "",
) -> Any:
    """A `BatteryState` for ``percentage`` (0 to 1)."""
    msg = BatteryState()
    msg.header.frame_id = frame_id
    msg.percentage = float(percentage)
    msg.voltage = empty_voltage + (full_voltage - empty_voltage) * percentage
    msg.current = float("nan")
    msg.charge = capacity_ah * percentage
    msg.capacity = capacity_ah
    msg.design_capacity = capacity_ah
    msg.present = True
    if draining:
        msg.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
    elif percentage >= 1.0:
        msg.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_FULL
    else:
        msg.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_NOT_CHARGING
    msg.power_supply_health = BatteryState.POWER_SUPPLY_HEALTH_GOOD
    msg.power_supply_technology = BatteryState.POWER_SUPPLY_TECHNOLOGY_LION
    return msg


class BatteryNode(FaultInjection, Node):
    """Publishes the synthetic battery state at ``rate_hz``."""

    def __init__(self) -> None:
        super().__init__(NODE_NAME)
        self.declare_parameter("output", "")
        self.declare_parameter("rate_hz", 1.0)
        self.declare_parameter("full_voltage", 0.0)
        self.declare_parameter("empty_voltage", 0.0)
        self.declare_parameter("capacity_ah", 0.0)
        self.declare_parameter("frame_id", "")
        self.declare_parameter("qos_reliability", "reliable")
        self.declare_parameter("qos_durability", "volatile")
        self.declare_parameter("qos_depth", 10)
        output = str(self.get_parameter("output").value)
        rate_hz = float(self.get_parameter("rate_hz").value)
        self.full_voltage = float(self.get_parameter("full_voltage").value)
        self.empty_voltage = float(self.get_parameter("empty_voltage").value)
        self.capacity_ah = float(self.get_parameter("capacity_ah").value)
        self.frame_id = str(self.get_parameter("frame_id").value)
        if not output or rate_hz <= 0:
            msg = f"{self.get_name()}: the parameters output and rate_hz are required"
            raise ValueError(msg)
        qos = QoSProfile(
            depth=int(self.get_parameter("qos_depth").value),
            reliability=RELIABILITY[str(self.get_parameter("qos_reliability").value)],
            durability=DURABILITY[str(self.get_parameter("qos_durability").value)],
            history=HistoryPolicy.KEEP_LAST,
        )
        self.percentage = 1.0
        self.drain: Drain | None = None
        self.drain_to = 1.0
        self.drain_duration_s = 0.0
        self.init_faults()
        self.declare_fault("percentage", 1.0, self._set_percentage)
        self.declare_fault("drain_to", 1.0, self._set_drain_to)
        self.declare_fault("drain_duration_s", 0.0, self._set_drain_duration)
        self.publisher = self.create_publisher(BatteryState, output, qos)
        self.timer = self.create_timer(1.0 / rate_hz, self.publish)
        self.get_logger().info(
            f"-> {output} at {rate_hz} Hz ({self.full_voltage} V full, {self.empty_voltage} V "
            f"empty, {self.capacity_ah} Ah)"
        )

    def now_s(self) -> float:
        """The node's clock (simulation time) in seconds."""
        return float(self.get_clock().now().nanoseconds) * 1e-9

    def _set_percentage(self, value: Any) -> bool:
        pct = non_negative(value)
        if pct is None or pct > 1.0:
            return False
        self.percentage, self.drain = pct, None
        return True

    def _set_drain_to(self, value: Any) -> bool:
        pct = non_negative(value)
        if pct is None or pct > 1.0:
            return False
        self.drain_to = pct
        self._restart_drain()
        return True

    def _set_drain_duration(self, value: Any) -> bool:
        seconds = non_negative(value)
        if seconds is None:
            return False
        self.drain_duration_s = seconds
        self._restart_drain()
        return True

    def _restart_drain(self) -> None:
        """A drain starts when its duration is set (after its target, as the event sets them)."""
        if self.drain_duration_s > 0:
            self.drain = Drain(self.now_s(), self.percentage, self.drain_to, self.drain_duration_s)
        else:
            self.drain = None

    def publish(self) -> None:
        """Advance the drain, build the state and publish it unless a fault holds it."""
        draining = False
        if self.drain is not None:
            self.percentage, ended = self.drain.at(self.now_s())
            draining = not ended
            if ended:
                self.drain = None
                self.get_logger().info(f"drained to {self.percentage * 100:.0f} %")
        msg = battery_state(
            self.percentage,
            full_voltage=self.full_voltage,
            empty_voltage=self.empty_voltage,
            capacity_ah=self.capacity_ah,
            draining=draining,
            frame_id=self.frame_id,
        )
        msg.header.stamp = self.get_clock().now().to_msg()
        out = self.inject(msg)
        if out is not None:
            self.publisher.publish(out)


def main(argv: list[str] | None = None) -> int:
    """Console script entry point."""
    return run(BatteryNode, argv)


if __name__ == "__main__":
    sys.exit(main())
