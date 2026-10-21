"""Fault injection shared by the adapter nodes (task P2-T03).

Every node the bridge bundle puts between the simulator's bridge and the vehicle (the IMU and
GNSS adapters, the throttle, the relays, the battery) is a **fault injector**: it carries ROS
parameters a scenario's timeline sets at run time (`ros2 param set` from the engine, the
`sensor.*`, `gps.*`, `imu.*`, `battery.*` and `actuator.*` events of docs/reference/events.md)
and applies them to every message it republishes. The two faults every injector offers:

- ``dropout`` (bool): publish nothing while true (the sensor is silent);
- ``freeze`` (bool): repeat the last message as it was, stamp included, while true (the sensor
  hangs; a consumer sees a stale stamp, as with a hung driver).

Type-specific faults (a GNSS denial or offset, an IMU bias, an actuator limit) are parameters of
the node that knows the message type, declared the same way. All of them are *dynamic*: a
`set_parameters` callback applies a change at once, and the node starts with no fault.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from rcl_interfaces.msg import SetParametersResult
from rclpy.parameter import Parameter

Applier = Callable[[Any], bool]
"""Validates and applies one parameter's value; False refuses it."""


class FaultInjection:
    """Mixin for an rclpy ``Node``: dynamic fault parameters and the pass-through filter.

    Call :meth:`init_faults` after the node's own parameters are declared; declare further
    dynamic parameters with :meth:`declare_fault`; run each message through :meth:`inject`
    before publishing it (``None`` means publish nothing).
    """

    def init_faults(self) -> None:
        """Declare ``dropout`` and ``freeze`` and install the parameter callback."""
        self._appliers: dict[str, Applier] = {}
        self._last: Any = None
        self.dropout = False
        self.freeze = False
        self.declare_fault("dropout", False, self._set_dropout)
        self.declare_fault("freeze", False, self._set_freeze)
        self.add_on_set_parameters_callback(self._on_fault_parameters)  # type: ignore[attr-defined]

    def declare_fault(self, name: str, default: Any, apply: Applier) -> None:
        """Declare a dynamic parameter; ``apply`` validates and applies each new value."""
        self.declare_parameter(name, default)  # type: ignore[attr-defined]
        self._appliers[name] = apply
        apply(self.get_parameter(name).value)  # type: ignore[attr-defined]

    def inject(self, msg: Any) -> Any | None:
        """The message to publish for ``msg``: the message, the frozen one, or nothing."""
        if self.dropout:
            return None
        if self.freeze:
            if self._last is None:
                self._last = msg
            return self._last
        self._last = msg
        return msg

    def _set_dropout(self, value: Any) -> bool:
        if not isinstance(value, bool):
            return False
        self.dropout = value
        return True

    def _set_freeze(self, value: Any) -> bool:
        if not isinstance(value, bool):
            return False
        self.freeze = value
        return True

    def _on_fault_parameters(self, parameters: Sequence[Parameter]) -> SetParametersResult:
        for parameter in parameters:
            apply = self._appliers.get(parameter.name)
            if apply is None:
                continue
            if not apply(parameter.value):
                return SetParametersResult(
                    successful=False, reason=f"{parameter.name}: {parameter.value!r} is not valid"
                )
            self.get_logger().info(f"{parameter.name} = {parameter.value!r}")  # type: ignore[attr-defined]
        return SetParametersResult(successful=True)


def vector3(value: Any) -> tuple[float, float, float] | None:
    """``value`` as three floats, or None when it is not a list of three numbers."""
    if not isinstance(value, list | tuple) or len(value) != 3:
        return None
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError):
        return None


def non_negative(value: Any) -> float | None:
    """``value`` as a float at or above zero, or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value >= 0 else None
