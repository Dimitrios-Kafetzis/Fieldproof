"""The built-in fault events (task P2-T03, session 2).

`gps.deny`, `gps.offset`, `imu.bias`, `sensor.dropout`, `sensor.freeze`, `battery.drain` and
`actuator.limit`.

A fault is a parameter of a **fault injector**: one of the nodes the bridge bundle runs between
the simulator's bridge and the vehicle (:mod:`fieldproof_bridge.adapters`: the IMU and GNSS
adapters, the throttle, the relays of the other sensor topics and of the drive command, the
battery). The vehicle's stack sees the injector's output as the sensor's topic, so the fault
reaches it the way a failing driver would, and the vehicle's own container is not touched (it
may be paused, killed or behind a radio link meanwhile).

Each event resolves its injector at plan time from the vehicle's bridge bundle (the same
generator the bridge container runs: `vehicle.bundle()`), refusing a sensor the bundle does not
route through an injector (the camera topics stay on the C++ path). At run time it sets the
injector's parameters with `ros2 param set` inside the bridge container
(:meth:`~fieldproof.events.EventContext.bridge_ros2`, asked again while the CLI's graph lags
behind the node, as `param.set` does), holds for `duration_s` when the entry has one, and sets
them back. An entry without `duration_s` leaves the fault in place for the rest of the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveFloat, model_validator

from fieldproof.dsl import Duration, PositiveDuration, ScenarioError, TimelineBase
from fieldproof.events import OK, ActionRecord, Event, EventContext, PlanContext, VehicleHandle
from fieldproof.events.param import set_parameter
from fieldproof.manifest import Name

Vector3 = tuple[float, float, float]
ZERO: Vector3 = (0.0, 0.0, 0.0)
PARAM_TIMEOUT_S = 30.0
"""How long an injector may take to answer one parameter (the CLI's graph lag included)."""


def vector_text(value: Vector3) -> str:
    """A double array as `ros2 param set` reads it (floats throughout, so the type is double)."""
    return "[" + ", ".join(repr(float(v)) for v in value) + "]"


def float_text(value: float) -> str:
    """A double as `ros2 param set` reads it."""
    return repr(float(value))


# ---------------------------------------------------------------------------- the entries
class FaultBase(TimelineBase):
    """What every fault entry has: the vehicle and an optional duration."""

    vehicle: Name = Field(description="The vehicle whose bridge injects the fault.")
    duration_s: Duration | None = Field(
        default=None,
        description="Lift the fault after this long (absent: it stays for the rest of the run).",
    )


class GpsDeny(FaultBase):
    """The GNSS receiver loses its fix: `STATUS_NO_FIX`, NaN position (jamming, a tunnel)."""

    action: Literal["gps.deny"]
    sensor: str | None = Field(
        default=None, description="The GNSS sensor of the manifest (the only one by default)."
    )


class GpsOffset(FaultBase):
    """The GNSS fix is moved by metres east, north and up (multipath, spoofing)."""

    action: Literal["gps.offset"]
    sensor: str | None = Field(
        default=None, description="The GNSS sensor of the manifest (the only one by default)."
    )
    east_m: float = Field(default=0.0, description="Metres east added to every fix.")
    north_m: float = Field(default=0.0, description="Metres north added to every fix.")
    up_m: float = Field(default=0.0, description="Metres up added to every fix.")


class ImuBias(FaultBase):
    """A bias is added to the IMU's angular velocity and linear acceleration."""

    action: Literal["imu.bias"]
    sensor: str | None = Field(
        default=None, description="The IMU of the manifest (the only one by default)."
    )
    gyro_radps: Vector3 = Field(
        default=ZERO, description="The bias of the angular velocity (x, y, z) in rad/s."
    )
    accel_mps2: Vector3 = Field(
        default=ZERO, description="The bias of the linear acceleration (x, y, z) in m/s²."
    )


class SensorDropout(FaultBase):
    """A sensor goes silent: its topic stops (the driver died, the cable came loose)."""

    action: Literal["sensor.dropout"]
    sensor: str = Field(
        description="A sensor of the manifest (`lidar`, `imu`, `gps`), a drive interface "
        "(`odometry`, `joint_states`, `command`) or `battery`; or one of their topics."
    )


class SensorFreeze(FaultBase):
    """A sensor hangs: its last message repeats, stamp included (a hung driver)."""

    action: Literal["sensor.freeze"]
    sensor: str = Field(
        description="A sensor of the manifest (`lidar`, `imu`, `gps`), a drive interface "
        "(`odometry`, `joint_states`, `command`) or `battery`; or one of their topics."
    )


class BatteryDrain(TimelineBase):
    """The battery drains to a percentage over a duration (in simulation time)."""

    vehicle: Name = Field(description="The vehicle whose battery drains.")
    action: Literal["battery.drain"]
    to_pct: float = Field(default=0.0, ge=0.0, le=100.0, description="The percentage reached.")
    duration_s: PositiveDuration = Field(description="How long the drain takes.")


class ActuatorLimit(FaultBase):
    """The actuators deliver no more than these speeds (a derated motor controller)."""

    action: Literal["actuator.limit"]
    max_linear_mps: PositiveFloat | None = Field(
        default=None, description="The cap on the linear speed, in m/s (absent: unchanged)."
    )
    max_angular_radps: PositiveFloat | None = Field(
        default=None, description="The cap on the angular speed, in rad/s (absent: unchanged)."
    )

    @model_validator(mode="after")
    def _one_limit(self) -> ActuatorLimit:
        if self.max_linear_mps is None and self.max_angular_radps is None:
            raise ValueError("give max_linear_mps, max_angular_radps or both")
        return self


# ----------------------------------------------------------------------------- the events
def injector_node(
    context: PlanContext, vehicle: VehicleHandle, what: str, *, kinds: tuple[str, ...] = ()
) -> str:
    """The fully qualified name of the injector serving `what`, or a :class:`ScenarioError`.

    With `kinds`, `what` may be None-like (empty) to mean the only injector of those kinds.
    """
    bundle = vehicle.bundle()
    candidates = [n for n in bundle.injectors() if not kinds or n.kind in kinds]
    if not what:
        if len(candidates) == 1:
            return bundle.fully_qualified(candidates[0].name)
        names = ", ".join(sorted(str(n.sensor) for n in candidates)) or "none"
        raise ScenarioError(
            f"{context.where}.sensor: {vehicle.name}'s bridge has "
            f"{'no' if not candidates else 'several'} injectors of the kind ({names}); name one"
        )
    node = bundle.injector(what)
    if node is None or (kinds and node.kind not in kinds):
        known = ", ".join(sorted(str(n.sensor) for n in candidates)) or "none"
        raise ScenarioError(
            f"{context.where}.sensor: {what!r} is not routed through "
            f"{'such an' if kinds else 'an'} injector of {vehicle.name}'s bridge ({known})"
        )
    return bundle.fully_qualified(node.name)


class FaultEvent(Event):
    """An event that sets parameters of an injector, holds, and sets them back."""

    kinds: ClassVar[tuple[str, ...]] = ()
    """The injector kinds the event applies to (empty: any)."""

    def sensor_of(self, entry: Any) -> str:
        """What the entry names (a sensor, an interface, a topic; empty for the default)."""
        return str(getattr(entry, "sensor", None) or "")

    def parameters(self, entry: Any) -> dict[str, str]:
        """The parameters that inject the fault, as `ros2 param set` reads them."""
        raise NotImplementedError

    def restore(self, entry: Any) -> dict[str, str]:  # noqa: ARG002
        """The parameters that lift it (set after `duration_s`)."""
        return {}

    def detail(self, entry: Any) -> str:  # noqa: ARG002
        """What the record says once the fault is in place."""
        return self.action

    def plan(self, entry: Any, context: PlanContext) -> dict[str, Any]:
        """The injector's node and the parameters, both ways."""
        context.require_simulator(self.action, "the vehicle's bridge (its fault injectors)")
        vehicle = context.vehicle(entry.vehicle)
        node = injector_node(context, vehicle, self.sensor_of(entry), kinds=self.kinds)
        return {"node": node, "set": self.parameters(entry), "restore": self.restore(entry)}

    def apply(
        self,
        entry: Any,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Set the parameters in the bridge; with a duration, hold and set them back."""
        vehicle = context.vehicle(entry.vehicle)
        node = str(resolved["node"])
        limit_s = float(resolved.get("limit_s", PARAM_TIMEOUT_S))
        deadline = context.clock() + limit_s
        ros2 = partial(context.bridge_ros2, vehicle)
        for parameter, value in dict(resolved["set"]).items():
            step = min(deadline, context.clock() + PARAM_TIMEOUT_S)
            if not set_parameter(ros2, context, node, parameter, value, step, record):
                context.log("fault.refused", record.detail, level="warning", action=record.name)
                return
        context.log("fault.injected", f"{node}: {self.detail(entry)}", action=record.name)
        record.status, record.detail = OK, self.detail(entry)
        duration = getattr(entry, "duration_s", None)
        restore = dict(resolved.get("restore") or {})
        if duration is None or not restore:
            return
        if not context.hold(min(float(duration), limit_s)):
            return
        if float(duration) > limit_s:
            record.detail = (
                f"{self.detail(entry)}; the budget ended the fault after {limit_s:.0f} s"
            )
            return
        for parameter, value in restore.items():
            step = context.clock() + PARAM_TIMEOUT_S
            if not set_parameter(ros2, context, node, parameter, value, step, record):
                context.log("fault.refused", record.detail, level="warning", action=record.name)
                return
        context.log("fault.lifted", f"{node}: {self.detail(entry)} lifted", action=record.name)
        record.status = OK
        record.detail = f"{self.detail(entry)} for {float(duration):g} s, lifted"


class GpsDenyEvent(FaultEvent):
    """`gps.deny`."""

    action: ClassVar[str] = "gps.deny"
    Entry: ClassVar[type[TimelineBase]] = GpsDeny
    summary: ClassVar[str] = "Deny the GNSS fix (STATUS_NO_FIX, NaN position), for a duration."
    kinds: ClassVar[tuple[str, ...]] = ("navsat_adapter",)

    def parameters(self, entry: GpsDeny) -> dict[str, str]:  # noqa: ARG002
        """The parameters that inject the fault."""
        return {"deny": "true"}

    def restore(self, entry: GpsDeny) -> dict[str, str]:  # noqa: ARG002
        """The parameters that lift it."""
        return {"deny": "false"}

    def detail(self, entry: GpsDeny) -> str:  # noqa: ARG002
        """What the record says once the fault is in place."""
        return "fix denied"


class GpsOffsetEvent(FaultEvent):
    """`gps.offset`."""

    action: ClassVar[str] = "gps.offset"
    Entry: ClassVar[type[TimelineBase]] = GpsOffset
    summary: ClassVar[str] = "Offset the GNSS fix by metres east, north and up, for a duration."
    kinds: ClassVar[tuple[str, ...]] = ("navsat_adapter",)

    def parameters(self, entry: GpsOffset) -> dict[str, str]:
        """The parameters that inject the fault."""
        return {"offset_enu_m": vector_text((entry.east_m, entry.north_m, entry.up_m))}

    def restore(self, entry: GpsOffset) -> dict[str, str]:  # noqa: ARG002
        """The parameters that lift it."""
        return {"offset_enu_m": vector_text(ZERO)}

    def detail(self, entry: GpsOffset) -> str:
        """What the record says once the fault is in place."""
        return f"fix offset by ({entry.east_m:g}, {entry.north_m:g}, {entry.up_m:g}) m ENU"


class ImuBiasEvent(FaultEvent):
    """`imu.bias`."""

    action: ClassVar[str] = "imu.bias"
    Entry: ClassVar[type[TimelineBase]] = ImuBias
    summary: ClassVar[str] = "Bias the IMU's angular velocity and acceleration, for a duration."
    kinds: ClassVar[tuple[str, ...]] = ("imu_adapter",)

    def parameters(self, entry: ImuBias) -> dict[str, str]:
        """The parameters that inject the fault."""
        return {
            "gyro_bias_radps": vector_text(entry.gyro_radps),
            "accel_bias_mps2": vector_text(entry.accel_mps2),
        }

    def restore(self, entry: ImuBias) -> dict[str, str]:  # noqa: ARG002
        """The parameters that lift it."""
        return {"gyro_bias_radps": vector_text(ZERO), "accel_bias_mps2": vector_text(ZERO)}

    def detail(self, entry: ImuBias) -> str:
        """What the record says once the fault is in place."""
        return (
            f"imu biased by {vector_text(entry.gyro_radps)} rad/s, "
            f"{vector_text(entry.accel_mps2)} m/s²"
        )


class SensorDropoutEvent(FaultEvent):
    """`sensor.dropout`."""

    action: ClassVar[str] = "sensor.dropout"
    Entry: ClassVar[type[TimelineBase]] = SensorDropout
    summary: ClassVar[str] = "Silence a sensor (or the drive command), for a duration."

    def parameters(self, entry: SensorDropout) -> dict[str, str]:  # noqa: ARG002
        """The parameters that inject the fault."""
        return {"dropout": "true"}

    def restore(self, entry: SensorDropout) -> dict[str, str]:  # noqa: ARG002
        """The parameters that lift it."""
        return {"dropout": "false"}

    def detail(self, entry: SensorDropout) -> str:
        """What the record says once the fault is in place."""
        return f"{entry.sensor} silent"


class SensorFreezeEvent(FaultEvent):
    """`sensor.freeze`."""

    action: ClassVar[str] = "sensor.freeze"
    Entry: ClassVar[type[TimelineBase]] = SensorFreeze
    summary: ClassVar[str] = "Freeze a sensor on its last message, for a duration."

    def parameters(self, entry: SensorFreeze) -> dict[str, str]:  # noqa: ARG002
        """The parameters that inject the fault."""
        return {"freeze": "true"}

    def restore(self, entry: SensorFreeze) -> dict[str, str]:  # noqa: ARG002
        """The parameters that lift it."""
        return {"freeze": "false"}

    def detail(self, entry: SensorFreeze) -> str:
        """What the record says once the fault is in place."""
        return f"{entry.sensor} frozen"


class BatteryDrainEvent(FaultEvent):
    """`battery.drain`: the battery node ramps on its own clock; the entry waits it out."""

    action: ClassVar[str] = "battery.drain"
    Entry: ClassVar[type[TimelineBase]] = BatteryDrain
    summary: ClassVar[str] = "Drain the battery to a percentage over a duration."
    kinds: ClassVar[tuple[str, ...]] = ("battery",)

    def sensor_of(self, entry: BatteryDrain) -> str:  # noqa: ARG002
        """What the entry names (the battery)."""
        return "battery"

    def parameters(self, entry: BatteryDrain) -> dict[str, str]:
        """The parameters that inject the fault."""
        # The target first: the node starts the ramp when the duration is set.
        return {
            "drain_to": float_text(entry.to_pct / 100.0),
            "drain_duration_s": float_text(entry.duration_s),
        }

    def detail(self, entry: BatteryDrain) -> str:
        """What the record says once the fault is in place."""
        return f"draining to {entry.to_pct:g} % over {entry.duration_s:g} s"

    def apply(
        self,
        entry: BatteryDrain,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Command the drain, then wait for it (simulation time runs no faster than the wall)."""
        super().apply(entry, resolved, context, record)
        if record.status != OK:
            return
        limit_s = float(resolved.get("limit_s", entry.duration_s))
        if context.hold(min(entry.duration_s, limit_s)) and entry.duration_s <= limit_s:
            record.detail = f"drained to {entry.to_pct:g} % over {entry.duration_s:g} s"


class ActuatorLimitEvent(FaultEvent):
    """`actuator.limit`."""

    action: ClassVar[str] = "actuator.limit"
    Entry: ClassVar[type[TimelineBase]] = ActuatorLimit
    summary: ClassVar[str] = "Cap the linear and angular speeds the actuators deliver."
    kinds: ClassVar[tuple[str, ...]] = ("command_relay",)

    def parameters(self, entry: ActuatorLimit) -> dict[str, str]:
        """The parameters that inject the fault."""
        params: dict[str, str] = {}
        if entry.max_linear_mps is not None:
            params["max_linear_mps"] = float_text(entry.max_linear_mps)
        if entry.max_angular_radps is not None:
            params["max_angular_radps"] = float_text(entry.max_angular_radps)
        return params

    def restore(self, entry: ActuatorLimit) -> dict[str, str]:
        """The parameters that lift it."""
        return dict.fromkeys(self.parameters(entry), float_text(0.0))

    def detail(self, entry: ActuatorLimit) -> str:
        """What the record says once the fault is in place."""
        parts = []
        if entry.max_linear_mps is not None:
            parts.append(f"{entry.max_linear_mps:g} m/s")
        if entry.max_angular_radps is not None:
            parts.append(f"{entry.max_angular_radps:g} rad/s")
        return "actuators limited to " + ", ".join(parts)


EVENTS = (
    GpsDenyEvent,
    GpsOffsetEvent,
    ImuBiasEvent,
    SensorDropoutEvent,
    SensorFreezeEvent,
    BatteryDrainEvent,
    ActuatorLimitEvent,
)
