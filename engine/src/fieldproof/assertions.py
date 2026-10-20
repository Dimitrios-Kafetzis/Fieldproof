"""The streaming assertion evaluator (task P1-T06).

The runner resolves every assertion of a scenario into a :class:`PlannedAssertion` (topics under
the vehicle's namespace, the target as a world pose, the Gazebo contact topic) and builds an
:class:`Evaluator` from them. While the timeline runs, the observer in the recorder container
(:mod:`fieldproof.observer`) appends one JSON line per observation to ``samples.jsonl`` in the
run directory; the runner tails the file and feeds every line as a :class:`Sample`, tells the
evaluator when actions start and end, and ticks it on every poll so deadlines fire. Each
assertion resolves once, as soon as the stream decides it (a ``reach_pose`` passes at the first
pose within tolerance, a ``no_collision`` fails at the first contact); what is still pending at
the end of the timeline is settled by :meth:`Evaluator.finish`. Every verdict carries evidence:
the samples that decided it, counts and extremes, in simulation time where the stream has it.

Times: ``t`` is seconds after the timeline started, on the wall clock the engine and the
containers share; deadlines (``within_s``, ``max_s``, ``after_s``) are in this time, as the
timeline's ``at`` is in runner v0.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fieldproof.expression import ExpressionError, compile_condition
from fieldproof.network import NETWORK_FILE
from fieldproof.scenario import (
    Assertion,
    NoCollision,
    Pose,
    ReachPose,
    TopicRate,
    TopicValue,
    WithinTime,
)

PENDING = "pending"
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
# The side-channel topics the observer publishes and the recorder always records.
SIDE_CHANNELS = {
    "events": "/fieldproof/events",
    "assertions": "/fieldproof/assertions",
    "metrics": "/fieldproof/metrics",
    "network": "/fieldproof/network",
}
# Files of the run directory the observer reads and writes.
SAMPLES_FILE = "samples.jsonl"
ASSERTIONS_FILE = "assertions.jsonl"
OBSERVE_FILE = "observe.json"
RECORDING_DIR = "recording"
DEFAULT_CONTACTS_TOPIC = "contacts"


@dataclass(frozen=True)
class PlannedAssertion:
    """An assertion with its references resolved against the plan."""

    assertion: Assertion
    topic: str | None = None
    """The ROS topic (`topic_rate`, `topic_value`) or Gazebo topic (`no_collision`), absolute."""
    target: Pose | None = None
    """The `reach_pose` target in the world."""
    model: str | None = None
    """The Gazebo model name of the vehicle (`reach_pose`, `no_collision`)."""

    @property
    def name(self) -> str:
        """The assertion's name."""
        return self.assertion.name

    @property
    def type(self) -> str:
        """The assertion's type."""
        return self.assertion.type

    def to_dict(self) -> dict[str, Any]:
        """The resolved assertion as data (the plan's JSON)."""
        data: dict[str, Any] = {"resolved": {}}
        data.update(self.assertion.model_dump(exclude_none=True))
        if self.topic is not None:
            data["resolved"]["topic"] = self.topic
        if self.target is not None:
            data["resolved"]["target_world"] = {
                "x": self.target.x,
                "y": self.target.y,
                "z": self.target.z,
                "yaw": self.target.yaw,
            }
        if self.model is not None:
            data["resolved"]["model"] = self.model
        return data


@dataclass(frozen=True)
class Sample:
    """One line of the observer's stream."""

    kind: str
    wall: float
    sim: float | None
    data: Mapping[str, Any]

    @classmethod
    def from_line(cls, line: str) -> Sample:
        """Parse one JSON line (a :class:`ValueError` if it is not a sample)."""
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"not a sample: {line!r}") from error
        if not isinstance(raw, dict) or "kind" not in raw or "wall" not in raw:
            raise ValueError(f"not a sample: {line!r}")
        sim = raw.get("sim")
        return cls(
            str(raw["kind"]),
            float(raw["wall"]),
            float(sim) if sim is not None else None,
            raw,
        )


@dataclass
class Result:
    """The verdict of one assertion, with its evidence."""

    name: str
    type: str
    status: str = PENDING
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    t_s: float | None = None
    """Seconds after the timeline started when the assertion resolved."""
    sim_s: float | None = None
    """Simulation time of the sample that resolved it, when the stream had one."""

    @property
    def resolved(self) -> bool:
        """Whether the assertion has a verdict."""
        return self.status != PENDING

    def to_dict(self) -> dict[str, Any]:
        """The result as data (`run.json`, the report, `/fieldproof/assertions`)."""
        return {
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "reason": self.reason,
            "t_s": self.t_s,
            "sim_s": self.sim_s,
            "evidence": self.evidence,
        }


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


# ----------------------------------------------------------------------------------- checkers
class Checker:
    """One assertion's streaming logic; subclasses override the `on_*` hooks."""

    def __init__(self, planned: PlannedAssertion) -> None:
        self.planned = planned
        self.result = Result(planned.name, planned.type)

    @property
    def done(self) -> bool:
        """Whether the assertion has a verdict."""
        return self.result.resolved

    def resolve(
        self, status: str, reason: str, t: float | None, sim: float | None = None, **evidence: Any
    ) -> None:
        """Give the verdict (the first one sticks)."""
        if self.done:
            return
        self.result.status = status
        self.result.reason = reason
        self.result.t_s = _round(t)
        self.result.sim_s = _round(sim)
        self.result.evidence.update(evidence)

    def on_sample(self, sample: Sample, t: float) -> None:
        """A new observation at `t` seconds into the timeline (negative before it started)."""

    def on_tick(self, t: float) -> None:
        """Time passed."""

    def on_action(self, label: str, event: str, t: float, status: str | None = None) -> None:
        """A timeline action `started` or `finished` (with its status)."""

    def on_finish(self, t: float) -> None:
        """The timeline completed; settle a pending verdict."""


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
            return
        self._fail(t, "not reached by the end of the timeline")


class WithinTimeChecker(Checker):
    """A timeline action completes within `max_s` of its start."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: WithinTime = planned.assertion  # type: ignore[assignment]
        self.started_t: float | None = None

    def on_action(self, label: str, event: str, t: float, status: str | None = None) -> None:
        """See :meth:`Checker.on_action`."""
        if label != self.assertion.action:
            return
        if event == "started":
            self.started_t = t
            return
        if self.started_t is None:
            if status in ("skipped", "cancelled"):
                self.resolve(FAILED, f"{label} was {status} before it started", t)
            return
        took = t - self.started_t
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
        self.messages = 0

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
        self.lowest = hz if self.lowest is None else min(self.lowest, hz)
        self.highest = hz if self.highest is None else max(self.highest, hz)
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


CHECKERS: dict[str, type[Checker]] = {
    "reach_pose": ReachPoseChecker,
    "within_time": WithinTimeChecker,
    "topic_rate": TopicRateChecker,
    "no_collision": NoCollisionChecker,
    "topic_value": TopicValueChecker,
}


# ---------------------------------------------------------------------------------- evaluator
class Evaluator:
    """Feeds the observer's stream and the timeline's events to every assertion's checker."""

    def __init__(self, planned: Iterable[PlannedAssertion]) -> None:
        self.checkers = [CHECKERS[p.type](p) for p in planned]
        self.start_wall: float | None = None
        self.samples = 0
        self.last_sim: float | None = None
        self.first_sim: float | None = None
        self.bad_lines = 0

    @property
    def results(self) -> list[Result]:
        """Every assertion's result, in the scenario's order."""
        return [c.result for c in self.checkers]

    @property
    def failed(self) -> list[Result]:
        """The failed assertions."""
        return [r for r in self.results if r.status == FAILED]

    @property
    def pending(self) -> list[Result]:
        """The assertions without a verdict yet."""
        return [r for r in self.results if r.status == PENDING]

    def elapsed(self, wall: float) -> float:
        """Seconds since the timeline started (negative before, 0 when it has not)."""
        return wall - self.start_wall if self.start_wall is not None else -math.inf

    def start(self, wall: float) -> None:
        """The timeline starts now."""
        self.start_wall = wall

    def _collect(self, before: list[str]) -> list[Result]:
        return [
            c.result
            for c, status in zip(self.checkers, before, strict=True)
            if c.result.status != status
        ]

    def feed(self, sample: Sample) -> list[Result]:
        """One observation; returns the assertions it resolved."""
        before = [c.result.status for c in self.checkers]
        self.samples += 1
        if sample.sim is not None:
            self.last_sim = sample.sim
            if (
                self.first_sim is None
                and self.start_wall is not None
                and sample.wall >= self.start_wall
            ):
                self.first_sim = sample.sim
        t = self.elapsed(sample.wall)
        for checker in self.checkers:
            checker.on_sample(sample, t)  # a resolved checker keeps what it tracks for evidence
        return self._collect(before)

    def feed_lines(self, lines: Iterable[str]) -> list[Result]:
        """Feed every line of the stream that parses as a sample."""
        resolved: list[Result] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                sample = Sample.from_line(line)
            except ValueError:
                self.bad_lines += 1
                continue
            resolved += self.feed(sample)
        return resolved

    def tick(self, wall: float) -> list[Result]:
        """Time passed: deadlines."""
        if self.start_wall is None:
            return []
        before = [c.result.status for c in self.checkers]
        t = self.elapsed(wall)
        for checker in self.checkers:
            if not checker.done:
                checker.on_tick(t)
        return self._collect(before)

    def action(
        self, label: str, event: str, wall: float, status: str | None = None
    ) -> list[Result]:
        """A timeline action `started` or `finished` with `status`."""
        before = [c.result.status for c in self.checkers]
        t = self.elapsed(wall)
        for checker in self.checkers:
            if not checker.done:
                checker.on_action(label, event, t, status)
        return self._collect(before)

    def finish(self, wall: float) -> list[Result]:
        """The timeline completed: settle every pending assertion."""
        before = [c.result.status for c in self.checkers]
        t = self.elapsed(wall) if self.start_wall is not None else 0.0
        for checker in self.checkers:
            if not checker.done:
                checker.on_tick(t)
            checker.on_finish(t)  # a resolved checker may add evidence of the end
        return self._collect(before)

    def skip_pending(self, reason: str) -> list[Result]:
        """The run ended before the timeline completed: the pending assertions are skipped."""
        before = [c.result.status for c in self.checkers]
        for checker in self.checkers:
            if not checker.done:
                checker.resolve(SKIPPED, reason, None)
        return self._collect(before)

    def sim_elapsed(self) -> float | None:
        """Simulation seconds covered by the stream since the timeline started."""
        if self.first_sim is None or self.last_sim is None:
            return None
        return round(self.last_sim - self.first_sim, 3)


# ------------------------------------------------------------------------------- observation
def observe_plan(
    planned: Sequence[PlannedAssertion],
    *,
    world: str,
    models: Sequence[str],
    record: bool,
    topics: Sequence[str],
    run_dir: str,
    pose_hz: float = 10.0,
    conditions: Sequence[Any] = (),
) -> dict[str, Any]:
    """What the observer must watch and record (`observe.json`).

    From the planned assertions and the timeline's `when` conditions (task P2-T03: their topic
    fields are watched too). `run_dir` is the run directory as the recorder container sees it.
    """
    rates: list[dict[str, Any]] = []
    values: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    for condition in conditions:
        when = condition.when
        if condition.topic is not None:
            entry = {"topic": condition.topic, "field": when.field, "type": when.message_type}
            if entry not in values:
                values.append(entry)
    for item in planned:
        a = item.assertion
        if isinstance(a, TopicRate):
            entry = {"topic": item.topic, "window_s": a.window_s, "type": a.message_type}
            if entry not in rates:
                rates.append(entry)
        elif isinstance(a, TopicValue):
            entry = {"topic": item.topic, "field": a.field, "type": a.message_type}
            if entry not in values:
                values.append(entry)
        elif isinstance(a, NoCollision):
            entry = {"topic": item.topic, "model": item.model}
            if entry not in contacts:
                contacts.append(entry)
    recorded = list(dict.fromkeys([*topics, *SIDE_CHANNELS.values()])) if topics else []
    return {
        "world": world,
        "models": list(models),
        "pose_hz": pose_hz,
        "contacts": contacts,
        "rates": rates,
        "values": values,
        "record": {
            "enabled": record,
            "topics": recorded,
            "output": f"{run_dir}/{RECORDING_DIR}",
        },
        "side_channels": dict(SIDE_CHANNELS),
        "events_file": f"{run_dir}/engine.log",
        "assertions_file": f"{run_dir}/{ASSERTIONS_FILE}",
        "network_file": f"{run_dir}/{NETWORK_FILE}",
        "samples_file": f"{run_dir}/{SAMPLES_FILE}",
    }
