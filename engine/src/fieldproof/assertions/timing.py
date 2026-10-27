"""The timing assertions (task P2-T05): ``mission_time``, ``message_latency``, ``sequence``.

``mission_time`` bounds a span of the timeline (the whole of it, or from one entry's start to
another's end); ``message_latency`` reads the observer's ``latency`` samples (the age of a
topic's messages, header stamp to receipt, in simulation time over a sliding window);
``sequence`` wants events in order: actions starting or ending, conditions over signals
first holding, other assertions passing. The :class:`LatencyTracker` and
:class:`MissionClock` are shared with the KPI collectors.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveFloat, model_validator

from fieldproof.assertions.core import (
    FAILED,
    PASSED,
    SKIPPED,
    AssertionSpec,
    AssertionType,
    Checker,
    Metric,
    PlanContext,
    PlannedAssertion,
    PlannedSignal,
    Result,
    Sample,
    Watch,
    WindowedChecker,
    compile_or_refuse,
    rounded,
)
from fieldproof.dsl import AssertionBase, Duration, Placement, ScenarioError, Signal, Strict
from fieldproof.dsl import Metric as Metric_
from fieldproof.expression import ExpressionError, compile_condition
from fieldproof.manifest import Name

STATISTICS = ("p95", "max", "mean")


# ------------------------------------------------------------------------------- the specs
class MissionTime(AssertionBase):
    """A span of the timeline completes within a duration.

    From the `start` entry's start (or the start of the timeline) to the `end` entry's
    successful end (or the end of the timeline): fails the moment `max_s` passes without the
    end, when the end entry does not end `ok`, or when the span is shorter than `min_s`.
    """

    type: Literal["mission_time"]
    max_s: Duration = Field(description="The span must complete within this long.")
    min_s: Duration | None = Field(
        default=None, description="The span must take at least this long (absent: no floor)."
    )
    start: str | None = Field(
        default=None,
        description="The timeline entry whose start opens the span (absent: the timeline's).",
    )
    end: str | None = Field(
        default=None,
        description="The timeline entry whose successful end closes the span (absent: the "
        "end of the timeline).",
    )

    def action_names(self) -> list[str]:
        """See :meth:`AssertionBase.action_names`."""
        return [a for a in (self.start, self.end) if a is not None]

    @model_validator(mode="after")
    def _ordered(self) -> MissionTime:
        if self.min_s is not None and self.min_s > self.max_s:
            raise ValueError("min_s must not be above max_s")
        return self


class MessageLatency(AssertionBase):
    """The messages of a stamped topic arrive fresh.

    The observer measures the age of every message (its header stamp against the simulation
    clock when it is received) and reports a statistic over a sliding window once a second;
    fails at the first window whose `statistic` is above `max_ms` between `after_s` and
    `within_s`, and at the end of the timeline if no window was measured.
    """

    type: Literal["message_latency"]
    topic: str = Field(
        description="The topic (relative to the vehicle's namespace when `vehicle` is given)."
    )
    vehicle: Name | None = Field(default=None, description="The vehicle the topic belongs to.")
    max_ms: PositiveFloat = Field(description="The latency never to exceed, in milliseconds.")
    statistic: Literal["p95", "max", "mean"] = Field(
        default="p95", description="Which statistic of a window is judged."
    )
    window_s: Duration = Field(default=2.0, description="Length of the sliding window.")
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")
    within_s: Duration | None = Field(
        default=None, description="End of the window (absent: the end of the timeline)."
    )
    message_type: str | None = Field(
        default=None, description="Message type (`sensor_msgs/msg/LaserScan`; absent: discovered)."
    )

    @model_validator(mode="after")
    def _window(self) -> MessageLatency:
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        return self


class Step(Strict):
    """One step of a `sequence`: an action, a condition over a signal, or an assertion.

    Exactly one of `action` (with `event`), a signal (`topic` and `field`, or `metric`, with
    `target` and `vehicle` as a `when` has them) with its `condition`, or `assertion`.
    """

    name: str | None = Field(default=None, description="A label for the evidence.")
    action: str | None = Field(default=None, description="A timeline entry.")
    event: Literal["started", "finished"] = Field(
        default="finished", description="Which moment of the action: its start or its `ok` end."
    )
    topic: str | None = Field(default=None, description="A ROS topic (a signal step).")
    field: str | None = Field(default=None, description="Field of the message.")
    message_type: str | None = Field(default=None, description="Message type (absent: discovered).")
    metric: Metric_ | None = Field(default=None, description="A metric of the run instead.")
    target: Placement | None = Field(default=None, description="Where `distance_to` measures to.")
    vehicle: Name | None = Field(default=None, description="The vehicle of the signal.")
    condition: str | None = Field(
        default=None, description="Expression over `value` for a signal step."
    )
    assertion: str | None = Field(default=None, description="Another assertion, which must pass.")

    @model_validator(mode="after")
    def _one_kind(self) -> Step:
        kinds = [k for k in ("action", "topic", "metric", "assertion") if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError("a step is an action, a topic field, a metric or an assertion")
        if kinds[0] in ("topic", "metric"):
            if self.condition is None:
                raise ValueError("a signal step needs a condition")
            compile_or_refuse(self.condition)
            self.signal()  # the signal's own checks
        else:
            if self.condition is not None:
                raise ValueError("condition goes with a topic or a metric")
            extra = [k for k in ("field", "message_type", "target", "vehicle") if getattr(self, k)]
            if extra:
                raise ValueError(f"{', '.join(extra)}: goes with a topic or a metric")
        return self

    def is_signal(self) -> bool:
        """Whether the step is a condition over a signal."""
        return self.topic is not None or self.metric is not None

    def signal(self) -> Signal:
        """The step's signal (a :class:`ValueError` if it is malformed)."""
        return Signal(
            topic=self.topic,
            field=self.field,
            message_type=self.message_type,
            metric=self.metric,
            target=self.target,
            vehicle=self.vehicle,
        )

    def label(self, index: int) -> str:
        """The step's name, or what it is."""
        if self.name:
            return self.name
        if self.action is not None:
            return f"{self.action} {self.event}"
        if self.assertion is not None:
            return f"assertion {self.assertion}"
        subject = f"{self.topic}.{self.field}" if self.topic is not None else str(self.metric)
        return f"{subject} {self.condition} (step {index + 1})"


class Sequence(AssertionBase):
    """Events happen in order.

    Each step is watched only once the previous one has happened: the first message after
    it that satisfies a signal step's condition, the action's start or `ok` end, the other
    assertion's verdict (a failed or skipped one fails the sequence; so does an action step
    whose entry ends with another status). Passes when the last step happens; fails at
    `within_s`, when a step takes longer than `max_gap_s` after the previous, or at the end
    of the timeline, naming the step that did not happen.
    """

    type: Literal["sequence"]
    steps: list[Step] = Field(min_length=1, description="The steps, in order.")
    vehicle: Name | None = Field(
        default=None, description="The vehicle the signal steps default to."
    )
    within_s: Duration | None = Field(
        default=None, description="The last step must happen by then (absent: the end)."
    )
    max_gap_s: Duration | None = Field(
        default=None, description="Each step must happen within this long after the previous."
    )

    def vehicle_names(self) -> list[str]:
        """See :meth:`AssertionBase.vehicle_names`."""
        names = [self.vehicle] if self.vehicle else []
        names += [s.vehicle for s in self.steps if s.vehicle is not None]
        return names

    def action_names(self) -> list[str]:
        """See :meth:`AssertionBase.action_names`."""
        return [s.action for s in self.steps if s.action is not None]

    def assertion_names(self) -> list[str]:
        """The assertions the steps wait for."""
        return [s.assertion for s in self.steps if s.assertion is not None]


# ---------------------------------------------------------------------------- the trackers
class MissionClock:
    """A span of the timeline: from an entry's start (or the timeline's) to an entry's end."""

    def __init__(self, start: str | None, end: str | None) -> None:
        self.start = start
        self.end = end
        self.started_t: float | None = 0.0 if start is None else None
        self.ended_t: float | None = None
        self.end_status: str | None = None

    def on_action(self, label: str, event: str, t: float, status: str | None) -> bool:
        """Track the entries; True when the span just closed."""
        if label == self.start and event == "started" and self.started_t is None:
            self.started_t = t
        if label == self.end and event == "finished" and self.ended_t is None:
            if self.started_t is None:
                self.started_t = t
            self.ended_t, self.end_status = t, status
            return True
        return False

    def close(self, t: float) -> None:
        """The timeline ended: a span without an end entry closes now."""
        if self.end is None and self.ended_t is None:
            self.ended_t, self.end_status = t, "ok"

    def elapsed(self, t: float) -> float | None:
        """How long the span has lasted (or lasted)."""
        if self.started_t is None:
            return None
        return (self.ended_t if self.ended_t is not None else t) - self.started_t

    def metrics(self, t: float) -> dict[str, Metric]:
        """`duration_s`."""
        return {"duration_s": (self.elapsed(t), "s")}


class LatencyTracker:
    """The observer's `latency` windows of a topic."""

    def __init__(self, topic: str | None, after_s: float = 0.0, within_s: float | None = None):
        self.topic = topic
        self.after_s = after_s
        self.within_s = within_s
        self.windows = 0
        self.messages = 0
        self.worst: dict[str, float | None] = {"p95": None, "max": None, "mean": None}
        self.mean_total = 0.0

    def on_sample(self, sample: Sample, t: float) -> dict[str, float] | None:
        """A latency window of the topic inside the window of time, as its statistics."""
        if sample.kind != "latency" or sample.data.get("topic") != self.topic:
            return None
        if "error" in sample.data:
            return None
        self.messages = max(self.messages, int(sample.data.get("total", 0)))
        if t < self.after_s or (self.within_s is not None and t > self.within_s):
            return None
        stats = {key: float(sample.data.get(f"{key}_ms", 0.0)) for key in ("p95", "max", "mean")}
        self.windows += 1
        self.mean_total += stats["mean"]
        for key, value in stats.items():
            worst = self.worst[key]
            self.worst[key] = value if worst is None else max(worst, value)
        return stats

    def metrics(self) -> dict[str, Metric]:
        """`p95_ms`, `max_ms` (the worst windows), `mean_ms` (over the windows), `messages`."""
        return {
            "p95_ms": (self.worst["p95"], "ms"),
            "max_ms": (self.worst["max"], "ms"),
            "mean_ms": (self.mean_total / self.windows if self.windows else None, "ms"),
            "messages": (self.messages, ""),
        }


# ---------------------------------------------------------------------------- the checkers
class MissionTimeChecker(Checker):
    """The span within its bounds."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: MissionTime = planned.assertion  # type: ignore[assignment]
        self.clock = MissionClock(self.assertion.start, self.assertion.end)
        self.last_t = 0.0

    def metrics(self) -> dict[str, Metric]:
        """`duration_s`."""
        return self.clock.metrics(self.last_t)

    def _span(self) -> str:
        start = f"{self.assertion.start} started" if self.assertion.start else "the timeline"
        end = f"{self.assertion.end} ended" if self.assertion.end else "its end"
        return f"from {start} to {end}"

    def _evidence(self) -> dict[str, Any]:
        return {
            "started_t_s": rounded(self.clock.started_t),
            "ended_t_s": rounded(self.clock.ended_t),
            "duration_s": rounded(self.clock.elapsed(self.last_t)),
        }

    def on_action(self, label: str, event: str, t: float, status: str | None = None) -> None:
        """See :meth:`Checker.on_action`."""
        self.last_t = max(self.last_t, t)
        if label in (self.assertion.start, self.assertion.end):
            self.note(t, action=label, event=event, status=status)
        if self.clock.on_action(label, event, t, status):
            self._judge(t)

    def _judge(self, t: float) -> None:
        if self.done:
            return
        self.last_t = max(self.last_t, t)
        took = self.clock.elapsed(t) or 0.0
        if self.clock.end_status not in (None, "ok"):
            self.resolve(
                FAILED,
                f"{self.assertion.end} ended {self.clock.end_status} after {took:.1f} s",
                t,
                **self._evidence(),
            )
        elif took > self.assertion.max_s:
            self.resolve(
                FAILED,
                f"{self._span()} took {took:.1f} s, over the limit of {self.assertion.max_s:g} s",
                t,
                **self._evidence(),
            )
        elif self.assertion.min_s is not None and took < self.assertion.min_s:
            self.resolve(
                FAILED,
                f"{self._span()} took {took:.1f} s, under the floor of {self.assertion.min_s:g} s",
                t,
                **self._evidence(),
            )
        else:
            self.resolve(
                PASSED,
                f"{self._span()} took {took:.1f} s (limit {self.assertion.max_s:g} s)",
                t,
                **self._evidence(),
            )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        self.last_t = max(self.last_t, t)
        elapsed = self.clock.elapsed(t)
        if self.clock.ended_t is None and elapsed is not None and elapsed > self.assertion.max_s:
            self.resolve(
                FAILED,
                f"{self._span()} has not completed {self.assertion.max_s:g} s after its start",
                t,
                **self._evidence(),
            )

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if self.done:
            return
        self.last_t = max(self.last_t, t)
        if self.clock.started_t is None:
            self.resolve(FAILED, f"{self.assertion.start} never started", t, **self._evidence())
            return
        if self.assertion.end is not None and self.clock.ended_t is None:
            self.on_tick(t)
            self.resolve(FAILED, f"{self.assertion.end} did not complete", t, **self._evidence())
            return
        self.clock.close(t)
        self._judge(t)


class MessageLatencyChecker(WindowedChecker):
    """A topic's latency windows under the limit."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: MessageLatency = planned.assertion  # type: ignore[assignment]
        self.tracker = LatencyTracker(planned.topic, self.after_s, self.within_s)

    def metrics(self) -> dict[str, Metric]:
        """The tracker's `p95_ms`, `max_ms`, `mean_ms`, `messages`."""
        return self.tracker.metrics()

    def _stats(self) -> dict[str, Any]:
        return {"windows": self.tracker.windows} | {
            key: rounded(value) for key, (value, _unit) in self.tracker.metrics().items()
        }

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done:
            return
        stats = self.tracker.on_sample(sample, t)
        if stats is None:
            if (
                sample.kind == "latency"
                and "error" in sample.data
                and (sample.data.get("topic") == self.planned.topic)
            ):
                self.resolve(FAILED, f"{self.planned.topic}: {sample.data['error']}", t, sample.sim)
            elif self.window_closed(t):
                self.on_tick(t)
            return
        self.note(t, sample, **{f"{k}_ms": v for k, v in stats.items()})
        judged = stats[self.assertion.statistic]
        if judged > self.assertion.max_ms:
            self.resolve(
                FAILED,
                f"{self.planned.topic} {self.assertion.statistic} latency {judged:.1f} ms at "
                f"{t:.1f} s, over {self.assertion.max_ms:g} ms (window "
                f"{self.assertion.window_s:g} s)",
                t,
                sample.sim,
                window={"t_s": rounded(t), "sim_s": rounded(sample.sim)}
                | {f"{k}_ms": rounded(v) for k, v in stats.items()},
                **self._stats(),
            )

    def _settle(self, t: float) -> None:
        if self.tracker.windows == 0:
            self.resolve(
                FAILED,
                f"no latency of {self.planned.topic} was measured {self.window_text()} "
                f"({self.tracker.messages} message(s) seen)",
                t,
                **self._stats(),
            )
            return
        worst = self.tracker.worst[self.assertion.statistic] or 0.0
        self.resolve(
            PASSED,
            f"{self.planned.topic} {self.assertion.statistic} latency stayed under "
            f"{worst:.1f} ms over {self.tracker.windows} window(s) (limit "
            f"{self.assertion.max_ms:g} ms)",
            t,
            **self._stats(),
        )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        if not self.done and self.window_closed(t):
            self._settle(t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if not self.done:
            self._settle(t)


class SequenceChecker(Checker):
    """The steps, one after the other."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: Sequence = planned.assertion  # type: ignore[assignment]
        self.signals: dict[int, PlannedSignal] = dict(planned.resolved.get("signals", {}))
        self.index = 0
        self.occurred: list[dict[str, Any]] = []
        self.last_t: float | None = None
        self.verdicts: dict[str, tuple[str, float]] = {}
        self.conditions = {
            i: compile_condition(step.condition or "value")
            for i, step in enumerate(self.assertion.steps)
            if step.is_signal()
        }

    def depends_on(self) -> list[str]:
        """The assertions the steps wait for."""
        return self.assertion.assertion_names()

    def metrics(self) -> dict[str, Metric]:
        """`steps_done`, `completed_at_s`, `duration_s` (first to last step)."""
        first = self.occurred[0]["t_s"] if self.occurred else None
        last = self.occurred[-1]["t_s"] if self.occurred else None
        done = len(self.occurred) == len(self.assertion.steps)
        return {
            "steps_done": (len(self.occurred), ""),
            "completed_at_s": (last if done else None, "s"),
            "duration_s": (last - first if done and first is not None else None, "s"),
        }

    @property
    def current(self) -> Step | None:
        """The step waited for."""
        steps = self.assertion.steps
        return steps[self.index] if self.index < len(steps) else None

    def _evidence(self) -> dict[str, Any]:
        return {"steps": self.occurred, "steps_done": len(self.occurred)}

    def _advance(self, t: float, sim: float | None, detail: str) -> None:
        step = self.current
        assert step is not None  # noqa: S101  (called while a step is waited for)
        self.occurred.append(
            {
                "step": step.label(self.index),
                "t_s": rounded(t),
                "sim_s": rounded(sim),
                "detail": detail,
            }
        )
        self.note(t, step=step.label(self.index), detail=detail)
        self.index += 1
        self.last_t = t
        if self.current is None:
            self.resolve(
                PASSED,
                f"all {len(self.assertion.steps)} step(s) happened in order, the last at {t:.1f} s",
                t,
                sim,
                **self._evidence(),
            )
            return
        # The next step may wait on an assertion that already resolved.
        self._check_verdict(t)

    def _check_verdict(self, t: float) -> None:
        step = self.current
        if step is None or step.assertion is None or step.assertion not in self.verdicts:
            return
        status, when = self.verdicts[step.assertion]
        if status == PASSED:
            self._advance(max(t, when), None, f"passed at {when:.1f} s")
        else:
            self._fail(t, f"assertion {step.assertion} {status}")

    def _fail(self, t: float, why: str) -> None:
        step = self.current
        label = step.label(self.index) if step is not None else "-"
        self.resolve(
            FAILED,
            f"{why}: {len(self.occurred)} of {len(self.assertion.steps)} step(s) happened, "
            f"waiting for {label}",
            t,
            **self._evidence(),
        )

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        step = self.current
        if self.done or step is None or not step.is_signal():
            return
        if self.last_t is not None and t <= self.last_t:
            return  # strictly after the previous step
        planned = self.signals.get(self.index)
        if planned is None:
            return
        error = planned.error_of(sample)
        if error is not None:
            self._fail(t, f"{planned.summary()}: {error}")
            return
        value = planned.value_of(sample)
        if value is None:
            return
        try:
            holds = self.conditions[self.index](value)
        except ExpressionError as error:
            self._fail(t, str(error))
            return
        if holds:
            self._advance(t, sample.sim, f"{planned.summary()} = {value!r}")

    def on_action(self, label: str, event: str, t: float, status: str | None = None) -> None:
        """See :meth:`Checker.on_action`."""
        step = self.current
        if self.done or step is None or step.action != label:
            return
        if event == "finished" and status != "ok":
            self._fail(t, f"{label} ended {status}")
            return
        if event == step.event:
            self._advance(t, None, f"{label} {event}")

    def on_verdict(self, result: Result, t: float) -> None:
        """See :meth:`Checker.on_verdict`."""
        self.verdicts[result.name] = (result.status, t)
        if not self.done:
            self._check_verdict(t)

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        if self.done:
            return
        if self.assertion.within_s is not None and t >= self.assertion.within_s:
            self._fail(t, f"not completed within {self.assertion.within_s:g} s")
            return
        gap = self.assertion.max_gap_s
        since = self.last_t if self.last_t is not None else 0.0
        if gap is not None and t - since > gap:
            self._fail(t, f"the next step did not happen within {gap:g} s of the previous")

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if not self.done:
            step = self.current
            if (
                step is not None
                and step.assertion is not None
                and step.assertion not in (self.verdicts)
            ):
                self.verdicts[step.assertion] = (SKIPPED, t)
            self._check_verdict(t)
        if not self.done:
            self._fail(t, "the timeline ended")


# ------------------------------------------------------------------------------- the types
class MissionTimeType(AssertionType):
    """`mission_time`."""

    type: ClassVar[str] = "mission_time"
    Spec: ClassVar[AssertionSpec] = MissionTime
    summary: ClassVar[str] = "A span of the timeline completes within a duration."
    metrics: ClassVar[dict[str, str]] = {"duration_s": "how long the span took (s)"}

    def plan(self, spec: MissionTime, context: PlanContext) -> PlannedAssertion:
        """The entries must be timeline entries."""
        for what in ("start", "end"):
            name = getattr(spec, what)
            if name is not None:
                context.action(name, what)
        return PlannedAssertion(spec)

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return MissionTimeChecker(planned)


class MessageLatencyType(AssertionType):
    """`message_latency`."""

    type: ClassVar[str] = "message_latency"
    Spec: ClassVar[AssertionSpec] = MessageLatency
    summary: ClassVar[str] = "A stamped topic's messages arrive fresh (stamp to receipt)."
    metrics: ClassVar[dict[str, str]] = {
        "p95_ms": "the worst window's 95th percentile (ms)",
        "max_ms": "the worst message (ms)",
        "mean_ms": "the mean over the windows (ms)",
        "messages": "messages received",
    }

    def plan(self, spec: MessageLatency, context: PlanContext) -> PlannedAssertion:
        """The topic under the vehicle's namespace."""
        return PlannedAssertion(spec, topic=context.topic(spec.vehicle, spec.topic))

    def watches(self, planned: PlannedAssertion) -> list[Watch]:
        """The latency window of the topic."""
        spec: MessageLatency = planned.assertion  # type: ignore[assignment]
        return [
            Watch(
                "latency",
                {"topic": planned.topic, "window_s": spec.window_s, "type": spec.message_type},
            )
        ]

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return MessageLatencyChecker(planned)


class SequenceType(AssertionType):
    """`sequence`."""

    type: ClassVar[str] = "sequence"
    Spec: ClassVar[AssertionSpec] = Sequence
    summary: ClassVar[str] = "Events happen in order: actions, conditions, other assertions."
    metrics: ClassVar[dict[str, str]] = {
        "steps_done": "steps that happened",
        "completed_at_s": "when the last step happened (s)",
        "duration_s": "from the first step to the last (s)",
    }

    def plan(self, spec: Sequence, context: PlanContext) -> PlannedAssertion:
        """The actions, the signals and the assertions of the steps."""
        signals: dict[int, PlannedSignal] = {}
        for index, step in enumerate(spec.steps):
            what = f"steps[{index}]"
            if step.action is not None:
                context.action(step.action, f"{what}.action")
            elif step.assertion is not None:
                if step.assertion == spec.name or step.assertion not in context.assertions:
                    raise ScenarioError(
                        f"{context.where}.{what}.assertion: {step.assertion!r} is not another "
                        "assertion of the scenario"
                    )
            else:
                signals[index] = context.signal(step.signal(), spec.vehicle, what)
        watch = tuple(w for s in signals.values() for w in s.watches())
        return PlannedAssertion(spec, resolved={"signals": signals}, watch=watch)

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return SequenceChecker(planned)


ASSERTIONS: tuple[type[AssertionType], ...] = (MissionTimeType, MessageLatencyType, SequenceType)
