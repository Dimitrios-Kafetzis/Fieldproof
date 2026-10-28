"""The five assertion primitives of task P1-T06 as assertion types (task P2-T05).

``reach_pose`` (the ground truth within a tolerance of a target), ``within_time`` (a timeline
action completes in time), ``topic_rate`` (a topic keeps its rate), ``no_collision`` (the
chassis touches nothing) and ``topic_value`` (a condition over a field of a topic, always or
eventually). Each is a :class:`~fieldproof.assertions.core.AssertionType` with its spec, its
plan hook (the target in the world, the topic under the vehicle's namespace, the model) and
its streaming checker.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveFloat, model_validator

from fieldproof.assertions.core import (
    DEFAULT_CONTACTS_TOPIC,
    FAILED,
    PASSED,
    REPLAY,
    AssertionSpec,
    AssertionType,
    Checker,
    Metric,
    PlanContext,
    PlannedAssertion,
    Sample,
    Watch,
    compile_or_refuse,
)
from fieldproof.dsl import AssertionBase, Duration, Placement, ScenarioError
from fieldproof.expression import ExpressionError, compile_condition
from fieldproof.manifest import Name


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


# ----------------------------------------------------------------------------- the specs
class ReachPose(AssertionBase):
    """The vehicle's ground-truth pose comes within a tolerance of a target.

    Passes at the first ground-truth sample within `tolerance_m` of the target (and within
    `yaw_tolerance_rad` of its heading when given); fails when `within_s` passes first, or at
    the end of the timeline. The evidence is the closest sample and the final pose.
    """

    type: Literal["reach_pose"]
    vehicle: Name = Field(description="The vehicle whose ground-truth pose is read.")
    target: Placement = Field(description="The target, in the world (frame or coordinates).")
    tolerance_m: PositiveFloat = Field(
        default=0.5, description="Distance to the target, in metres."
    )
    yaw_tolerance_rad: PositiveFloat | None = Field(
        default=None, description="Heading tolerance (absent: the heading is not checked)."
    )
    within_s: Duration | None = Field(
        default=None,
        description="Deadline after the timeline starts (absent: the end of the timeline).",
    )


class WithinTime(AssertionBase):
    """A timeline action completes successfully within a duration of its start."""

    type: Literal["within_time"]
    action: str = Field(description="The `name` of a timeline entry.")
    max_s: Duration = Field(description="The action's result must arrive within this long.")

    def action_names(self) -> list[str]:
        """See :meth:`AssertionBase.action_names`."""
        return [self.action]


class TopicRate(AssertionBase):
    """A ROS topic keeps publishing at a rate.

    Measured over a sliding window once `after_s` has passed after the timeline starts; fails
    at the first window below `min_hz` (or above `max_hz`) and at the end of the timeline if
    nothing was received.
    """

    type: Literal["topic_rate"]
    topic: str = Field(
        description="The topic (relative to the vehicle's namespace when `vehicle` is given)."
    )
    vehicle: Name | None = Field(default=None, description="The vehicle the topic belongs to.")
    min_hz: float = Field(ge=0.0, description="Lowest acceptable rate over a window.")
    max_hz: PositiveFloat | None = Field(default=None, description="Highest acceptable rate.")
    window_s: Duration = Field(default=5.0, description="Length of the measurement window.")
    after_s: Duration = Field(
        default=0.0, description="Grace period after the timeline starts before windows count."
    )
    message_type: str | None = Field(
        default=None,
        description="Message type (`sensor_msgs/msg/LaserScan`; absent: discovered).",
    )

    @model_validator(mode="after")
    def _ordered(self) -> TopicRate:
        if self.max_hz is not None and self.max_hz < self.min_hz:
            raise ValueError("max_hz must not be below min_hz")
        return self


class NoCollision(AssertionBase):
    """The vehicle's chassis touches nothing during the timeline.

    Read from the model's ground-truth contact sensor in Gazebo (the reference vehicles carry
    one on the chassis, `<namespace>/contacts`); fails at the first contact.
    """

    type: Literal["no_collision"]
    vehicle: Name = Field(description="The vehicle whose contact sensor is read.")
    topic: str | None = Field(
        default=None,
        description="Gazebo topic of the `gz.msgs.Contacts` sensor (default: "
        "`<namespace>/contacts`).",
    )
    ignore: list[str] = Field(
        default_factory=list,
        description="Entities whose contacts do not count (a model or collision name).",
    )


class TopicValue(AssertionBase):
    """A condition over a field of a ROS topic's messages.

    `condition` is an expression over `value`, the field of each message (`value < 1.5`,
    `abs(value) <= 0.2`, `0 < value and value < 2`); `mode` says whether every message after
    `after_s` must satisfy it (`always`, fails at the first that does not, passes at `within_s`
    or the end) or one must (`eventually`, passes at the first that does, fails at `within_s`
    or the end). `after_s` and `within_s` make a window: "the vehicle is stopped from 12 s to
    27 s" is `mode: always`, `after_s: 12s`, `within_s: 27s`.
    """

    type: Literal["topic_value"]
    topic: str = Field(
        description="The topic (relative to the vehicle's namespace when `vehicle` is given)."
    )
    vehicle: Name | None = Field(default=None, description="The vehicle the topic belongs to.")
    field: str = Field(
        description="Field of the message (`twist.linear.x`, `ranges[0]`, `status.status`)."
    )
    condition: str = Field(description="Expression over `value` (comparisons, arithmetic, abs).")
    mode: Literal["always", "eventually"] = Field(default="always")
    after_s: Duration = Field(
        default=0.0, description="Grace period after the timeline starts before messages count."
    )
    within_s: Duration | None = Field(
        default=None,
        description="End of the window: the deadline of `eventually`, the end of `always` "
        "(absent: the end of the timeline).",
    )
    message_type: str | None = Field(
        default=None,
        description="Message type (`geometry_msgs/msg/Twist`; absent: discovered).",
    )

    @model_validator(mode="after")
    def _compiles(self) -> TopicValue:
        compile_or_refuse(self.condition)
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        return self


# ---------------------------------------------------------------------------- the checkers
class ReachPoseChecker(Checker):
    """Ground-truth pose within tolerance of the target before the deadline."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: ReachPose = planned.assertion  # type: ignore[assignment]
        if planned.target is None or planned.model is None:
            raise ValueError(f"{planned.name}: reach_pose needs a target pose and a model")
        self.target = planned.target
        self.closest: dict[str, Any] | None = None
        self.last: dict[str, Any] | None = None
        self.samples = 0

    def _pose(
        self, sample: Sample, t: float, distance: float, yaw_error: float | None
    ) -> dict[str, Any]:
        return {
            "t_s": _round(t),
            "sim_s": _round(sample.sim),
            "x": _round(float(sample.data.get("x", 0.0))),
            "y": _round(float(sample.data.get("y", 0.0))),
            "yaw": _round(float(sample.data.get("yaw", 0.0))),
            "distance_m": _round(distance),
            "yaw_error_rad": _round(yaw_error),
        }

    def metrics(self) -> dict[str, Metric]:
        """`closest_m`, `reached_at_s`, `final_distance_m`."""
        return {
            "closest_m": (self.closest["distance_m"] if self.closest else None, "m"),
            "reached_at_s": (self.result.t_s if self.result.status == PASSED else None, "s"),
            "final_distance_m": (self.last["distance_m"] if self.last else None, "m"),
        }

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if sample.kind != "pose" or sample.data.get("model") != self.planned.model:
            return
        x, y = float(sample.data.get("x", 0.0)), float(sample.data.get("y", 0.0))
        distance = math.hypot(x - self.target.x, y - self.target.y)
        yaw_error = None
        if self.assertion.yaw_tolerance_rad is not None:
            yaw = float(sample.data.get("yaw", 0.0))
            yaw_error = abs(math.remainder(yaw - self.target.yaw, math.tau))
        pose = self._pose(sample, t, distance, yaw_error)
        self.last = pose
        if t < 0 or self.done:
            return
        self.samples += 1
        self.note(t, sample, x=x, y=y, distance_m=distance)
        if self.closest is None or distance < self.closest["distance_m"]:
            self.closest = pose
        yaw_ok = (
            yaw_error is None
            or self.assertion.yaw_tolerance_rad is None
            or yaw_error <= self.assertion.yaw_tolerance_rad
        )
        within = distance <= self.assertion.tolerance_m and yaw_ok
        if within:
            reason = (
                f"{distance:.2f} m from ({self.target.x:g}, {self.target.y:g}) at {t:.1f} s "
                f"(tolerance {self.assertion.tolerance_m:g} m)"
            )
            if yaw_error is not None:
                reason += f", heading off by {yaw_error:.2f} rad"
            self.resolve(PASSED, reason, t, sample.sim, reached=pose, samples=self.samples)

    def _fail(self, t: float, why: str) -> None:
        if self.closest is None:
            self.resolve(
                FAILED,
                f"{why}: no ground-truth pose of {self.planned.model} was observed",
                t,
                samples=0,
            )
            return
        reason = (
            f"{why}: closest {self.closest['distance_m']:.2f} m from "
            f"({self.target.x:g}, {self.target.y:g}) (tolerance {self.assertion.tolerance_m:g} m)"
        )
        if self.assertion.yaw_tolerance_rad is not None and self.closest["yaw_error_rad"]:
            reason += f", heading off by {self.closest['yaw_error_rad']:.2f} rad there"
        self.resolve(FAILED, reason, t, closest=self.closest, final=self.last, samples=self.samples)

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        deadline = self.assertion.within_s
        if deadline is not None and t >= deadline:
            self._fail(t, f"not reached within {deadline:g} s")

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if self.done:
            if self.last is not None:
                self.result.evidence["final"] = self.last
                self.result.metrics = self.measured()
            return
        self._fail(t, "not reached by the end of the timeline")


class WithinTimeChecker(Checker):
    """A timeline action completes within `max_s` of its start."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: WithinTime = planned.assertion  # type: ignore[assignment]
        self.started_t: float | None = None
        self.took: float | None = None

    def metrics(self) -> dict[str, Metric]:
        """`duration_s` of the action."""
        return {"duration_s": (self.took, "s")}

    def on_action(self, label: str, event: str, t: float, status: str | None = None) -> None:
        """See :meth:`Checker.on_action`."""
        if label != self.assertion.action:
            return
        if event == "started":
            self.started_t = t
            self.note(t, event="started")
            return
        if self.started_t is None:
            if status in ("skipped", "cancelled"):
                self.resolve(FAILED, f"{label} was {status} before it started", t)
            return
        took = t - self.started_t
        self.took = round(took, 3)
        self.note(t, event="finished", status=status, duration_s=took)
        evidence = {"started_t_s": _round(self.started_t), "duration_s": _round(took)}
        if status != "ok":
            self.resolve(
                FAILED,
                f"{label} ended {status} after {took:.1f} s",
                t,
                action_status=status,
                **evidence,
            )
        elif took <= self.assertion.max_s:
            self.resolve(
                PASSED,
                f"{label} completed in {took:.1f} s (limit {self.assertion.max_s:g} s)",
                t,
                action_status=status,
                **evidence,
            )
        else:
            self.resolve(
                FAILED,
                f"{label} completed in {took:.1f} s, over the limit of {self.assertion.max_s:g} s",
                t,
                action_status=status,
                **evidence,
            )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        if self.started_t is not None and t - self.started_t > self.assertion.max_s:
            self.resolve(
                FAILED,
                f"{self.assertion.action} has not completed {self.assertion.max_s:g} s after "
                "its start",
                t,
                started_t_s=_round(self.started_t),
                elapsed_s=_round(t - self.started_t),
            )

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if self.started_t is None:
            self.resolve(FAILED, f"{self.assertion.action} never started", t)
        else:
            self.on_tick(t)
            self.resolve(FAILED, f"{self.assertion.action} did not complete", t)


class TopicRateChecker(Checker):
    """A topic's rate over the observer's sliding windows."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: TopicRate = planned.assertion  # type: ignore[assignment]
        self.windows = 0
        self.lowest: float | None = None
        self.highest: float | None = None
        self.total_hz = 0.0
        self.messages = 0

    def metrics(self) -> dict[str, Metric]:
        """`mean_hz`, `lowest_hz`, `highest_hz`, `messages`."""
        return {
            "mean_hz": (self.total_hz / self.windows if self.windows else None, "Hz"),
            "lowest_hz": (self.lowest, "Hz"),
            "highest_hz": (self.highest, "Hz"),
            "messages": (self.messages, ""),
        }

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done:
            return
        if sample.kind != "rate" or sample.data.get("topic") != self.planned.topic:
            return
        self.messages = max(self.messages, int(sample.data.get("total", 0)))
        if t < self.assertion.after_s:
            return
        hz = float(sample.data.get("hz", 0.0))
        self.windows += 1
        self.total_hz += hz
        self.lowest = hz if self.lowest is None else min(self.lowest, hz)
        self.highest = hz if self.highest is None else max(self.highest, hz)
        self.note(t, sample, hz=hz)
        window = {"t_s": _round(t), "sim_s": _round(sample.sim), "hz": _round(hz, 2)}
        if hz < self.assertion.min_hz:
            self.resolve(
                FAILED,
                f"{self.planned.topic} at {hz:.1f} Hz at {t:.1f} s, below "
                f"{self.assertion.min_hz:g} Hz (window {self.assertion.window_s:g} s)",
                t,
                sample.sim,
                window=window,
                **self._stats(),
            )
        elif self.assertion.max_hz is not None and hz > self.assertion.max_hz:
            self.resolve(
                FAILED,
                f"{self.planned.topic} at {hz:.1f} Hz at {t:.1f} s, above "
                f"{self.assertion.max_hz:g} Hz (window {self.assertion.window_s:g} s)",
                t,
                sample.sim,
                window=window,
                **self._stats(),
            )

    def _stats(self) -> dict[str, Any]:
        return {
            "windows": self.windows,
            "lowest_hz": _round(self.lowest, 2),
            "highest_hz": _round(self.highest, 2),
            "messages": self.messages,
        }

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if self.windows == 0 or self.lowest is None:
            self.resolve(
                FAILED,
                f"no rate of {self.planned.topic} was measured after {self.assertion.after_s:g} s "
                f"({self.messages} message(s) seen)",
                t,
                **self._stats(),
            )
        else:
            self.resolve(
                PASSED,
                f"{self.planned.topic} stayed between {self.lowest:.1f} and "
                f"{self.highest or 0.0:.1f} Hz over {self.windows} window(s) "
                f"(minimum {self.assertion.min_hz:g} Hz)",
                t,
                **self._stats(),
            )


class NoCollisionChecker(Checker):
    """No contact of the vehicle's chassis with anything but the ignored entities."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: NoCollision = planned.assertion  # type: ignore[assignment]
        self.ignored = 0
        self.messages = 0

    def metrics(self) -> dict[str, Metric]:
        """`contacts` (messages of the contact sensor, the ignored ones included)."""
        return {"contacts": (self.messages, "")}

    def _ignored(self, name: str) -> bool:
        parts = name.split("::")
        return any(
            entry == name or entry == parts[0] or entry in parts for entry in self.assertion.ignore
        )

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done:
            return
        if sample.kind != "contact" or sample.data.get("topic") != self.planned.topic:
            return
        if t < 0:
            return
        self.messages += 1
        first = str(sample.data.get("collision1", ""))
        second = str(sample.data.get("collision2", ""))
        model = self.planned.model or ""
        other = second if first.split("::", maxsplit=1)[0] == model else first
        self.note(t, sample, other=other, count=sample.data.get("count"))
        if self._ignored(other):
            self.ignored += 1
            return
        self.resolve(
            FAILED,
            f"{model} touched {other} at {t:.1f} s",
            t,
            sample.sim,
            contact={
                "t_s": _round(t),
                "sim_s": _round(sample.sim),
                "collision1": first,
                "collision2": second,
                "count": sample.data.get("count"),
            },
            ignored_contacts=self.ignored,
        )

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        reason = f"no contact of {self.planned.model} in {t:.1f} s"
        if self.ignored:
            reason += f" ({self.ignored} ignored contact message(s))"
        self.resolve(PASSED, reason, t, ignored_contacts=self.ignored, messages=self.messages)


class TopicValueChecker(Checker):
    """A condition over a field of a topic, always or eventually."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: TopicValue = planned.assertion  # type: ignore[assignment]
        self.condition = compile_condition(self.assertion.condition)
        self.count = 0
        self.lowest: Any = None
        self.highest: Any = None
        self.last: Any = None

    def metrics(self) -> dict[str, Metric]:
        """`samples`, `lowest`, `highest`, `last` of the field."""
        return {
            "samples": (self.count, ""),
            "lowest": (self.lowest, ""),
            "highest": (self.highest, ""),
            "last": (self.last if isinstance(self.last, int | float) else None, ""),
        }

    def _stats(self) -> dict[str, Any]:
        return {
            "samples": self.count,
            "lowest": self.lowest,
            "highest": self.highest,
            "last": self.last,
        }

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done:
            return
        if (
            sample.kind != "value"
            or sample.data.get("topic") != self.planned.topic
            or sample.data.get("field") != self.assertion.field
        ):
            return
        if "error" in sample.data:
            self.resolve(
                FAILED,
                f"{self.planned.topic}: field {self.assertion.field!r}: {sample.data['error']}",
                t,
                sample.sim,
                **self._stats(),
            )
            return
        if t < self.assertion.after_s:
            return
        if self.assertion.within_s is not None and t > self.assertion.within_s:
            self.on_tick(t)  # the window closed before this message
            return
        value = sample.data.get("value")
        self.count += 1
        self.last = value
        if isinstance(value, int | float) and not isinstance(value, bool):
            self.lowest = value if self.lowest is None else min(self.lowest, value)
            self.highest = value if self.highest is None else max(self.highest, value)
        self.note(t, sample, value=value)
        try:
            holds = self.condition(value)
        except ExpressionError as error:
            self.resolve(FAILED, str(error), t, sample.sim, **self._stats())
            return
        observation = {"t_s": _round(t), "sim_s": _round(sample.sim), "value": value}
        where = f"{self.planned.topic} {self.assertion.field}"
        if self.assertion.mode == "always" and not holds:
            self.resolve(
                FAILED,
                f"{where} = {value!r} at {t:.1f} s violates {self.assertion.condition!r}",
                t,
                sample.sim,
                violation=observation,
                **self._stats(),
            )
        elif self.assertion.mode == "eventually" and holds:
            self.resolve(
                PASSED,
                f"{where} = {value!r} at {t:.1f} s satisfies {self.assertion.condition!r}",
                t,
                sample.sim,
                satisfied=observation,
                **self._stats(),
            )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        deadline = self.assertion.within_s
        if self.done or deadline is None or t < deadline:
            return
        if self.assertion.mode == "always":
            self._settle_always(t, f"from {self.assertion.after_s:g} s to {deadline:g} s")
        else:
            self.resolve(
                FAILED,
                f"{self.planned.topic} {self.assertion.field} never satisfied "
                f"{self.assertion.condition!r} within {deadline:g} s ({self.count} message(s))",
                t,
                **self._stats(),
            )

    def _settle_always(self, t: float, window: str | None = None) -> None:
        """The window of an `always` closed: passed if it held for at least one message."""
        if self.count == 0:
            when = window or f"after {self.assertion.after_s:g} s"
            self.resolve(FAILED, f"no message of {self.planned.topic} {when}", t, **self._stats())
            return
        where = f"{self.planned.topic} {self.assertion.field}"
        self.resolve(
            PASSED,
            f"{where} satisfied {self.assertion.condition!r} in all {self.count} message(s)"
            + (f" {window}" if window else ""),
            t,
            **self._stats(),
        )

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if self.done:
            return
        where = f"{self.planned.topic} {self.assertion.field}"
        if self.assertion.mode == "always":
            self._settle_always(t)
        elif self.count == 0:
            self.resolve(
                FAILED,
                f"no message of {self.planned.topic} after {self.assertion.after_s:g} s",
                t,
                **self._stats(),
            )
        else:
            self.resolve(
                FAILED,
                f"{where} never satisfied {self.assertion.condition!r} ({self.count} message(s))",
                t,
                **self._stats(),
            )


# ------------------------------------------------------------------------------- the types
class ReachPoseType(AssertionType):
    """`reach_pose`."""

    type: ClassVar[str] = "reach_pose"
    Spec: ClassVar[AssertionSpec] = ReachPose
    summary: ClassVar[str] = "The vehicle's ground-truth pose comes within a tolerance of a target."
    metrics: ClassVar[dict[str, str]] = {
        "closest_m": "the closest the vehicle came to the target (m)",
        "reached_at_s": "when it came within tolerance (s)",
        "final_distance_m": "its distance to the target at the end (m)",
    }

    def plan(self, spec: ReachPose, context: PlanContext) -> PlannedAssertion:
        """The target in the world, the vehicle's model."""
        vehicle = context.vehicle(spec.vehicle)
        return PlannedAssertion(spec, target=context.place(spec.target), model=vehicle.name)

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return ReachPoseChecker(planned)


class WithinTimeType(AssertionType):
    """`within_time`."""

    type: ClassVar[str] = "within_time"
    Spec: ClassVar[AssertionSpec] = WithinTime
    summary: ClassVar[str] = "A timeline action completes successfully within a duration."
    metrics: ClassVar[dict[str, str]] = {"duration_s": "how long the action took (s)"}

    def plan(self, spec: WithinTime, context: PlanContext) -> PlannedAssertion:
        """The action must be a timeline entry."""
        context.action(spec.action)
        return PlannedAssertion(spec)

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return WithinTimeChecker(planned)


class TopicRateType(AssertionType):
    """`topic_rate`."""

    type: ClassVar[str] = "topic_rate"
    Spec: ClassVar[AssertionSpec] = TopicRate
    summary: ClassVar[str] = "A ROS topic keeps publishing at a rate (in simulation time)."
    metrics: ClassVar[dict[str, str]] = {
        "mean_hz": "the mean of the windows' rates (Hz)",
        "lowest_hz": "the slowest window (Hz)",
        "highest_hz": "the fastest window (Hz)",
        "messages": "messages received",
    }

    def plan(self, spec: TopicRate, context: PlanContext) -> PlannedAssertion:
        """The topic under the vehicle's namespace."""
        return PlannedAssertion(spec, topic=context.topic(spec.vehicle, spec.topic))

    def watches(self, planned: PlannedAssertion) -> list[Watch]:
        """The rate window of the topic."""
        spec: TopicRate = planned.assertion  # type: ignore[assignment]
        return [
            Watch(
                "rate",
                {"topic": planned.topic, "window_s": spec.window_s, "type": spec.message_type},
            )
        ]

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return TopicRateChecker(planned)


class NoCollisionType(AssertionType):
    """`no_collision`."""

    type: ClassVar[str] = "no_collision"
    Spec: ClassVar[AssertionSpec] = NoCollision
    summary: ClassVar[str] = (
        "The vehicle's chassis touches nothing (the ground-truth contact sensor)."
    )
    metrics: ClassVar[dict[str, str]] = {"contacts": "contact messages seen"}

    def plan(self, spec: NoCollision, context: PlanContext) -> PlannedAssertion:
        """The contact topic under the vehicle's namespace, the vehicle's model."""
        vehicle = context.vehicle(spec.vehicle)
        if context.source == REPLAY:
            raise ScenarioError(
                f"{context.where}: no_collision reads the simulator's contact sensor, which a "
                "replay run has not"
            )
        topic = vehicle.topic(spec.topic or DEFAULT_CONTACTS_TOPIC)
        return PlannedAssertion(spec, topic=topic, model=vehicle.name)

    def watches(self, planned: PlannedAssertion) -> list[Watch]:
        """The contact sensor."""
        return [Watch("contact", {"topic": planned.topic, "model": planned.model})]

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return NoCollisionChecker(planned)


class TopicValueType(AssertionType):
    """`topic_value`."""

    type: ClassVar[str] = "topic_value"
    Spec: ClassVar[AssertionSpec] = TopicValue
    summary: ClassVar[str] = "A condition over a field of a ROS topic, always or eventually."
    metrics: ClassVar[dict[str, str]] = {
        "samples": "messages judged",
        "lowest": "the lowest value seen",
        "highest": "the highest value seen",
        "last": "the last value seen",
    }

    def plan(self, spec: TopicValue, context: PlanContext) -> PlannedAssertion:
        """The topic under the vehicle's namespace."""
        return PlannedAssertion(spec, topic=context.topic(spec.vehicle, spec.topic))

    def watches(self, planned: PlannedAssertion) -> list[Watch]:
        """The field of the topic."""
        spec: TopicValue = planned.assertion  # type: ignore[assignment]
        return [
            Watch("value", {"topic": planned.topic, "field": spec.field, "type": spec.message_type})
        ]

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return TopicValueChecker(planned)


ASSERTIONS: tuple[type[AssertionType], ...] = (
    ReachPoseType,
    WithinTimeType,
    TopicRateType,
    NoCollisionType,
    TopicValueType,
)
