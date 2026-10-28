"""The localisation assertion (task P2-T07): ``localisation_error``.

A stack's own pose estimate (SLAM Toolbox's ``pose``, AMCL's ``amcl_pose``, a fused
``odometry/filtered``, or the raw wheel odometry) against the ground truth of the run: the
simulator's in a simulated run, the log's in a replay. The estimate lives in the stack's map
(or odometry) frame, which starts wherever the stack started; the ground truth in the world.
The :class:`LocalisationTracker` aligns the two frames once (at the first estimate it can
match to a ground-truth pose, or at the vehicle's spawn pose), interpolates the ground truth
to each estimate's stamp, and keeps the position and heading errors; the checker judges the
largest error at once and the mean and the 95th percentile at the end.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import deque
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveFloat, PositiveInt, model_validator

from fieldproof.assertions.core import (
    FAILED,
    PASSED,
    AssertionSpec,
    AssertionType,
    Checker,
    Metric,
    PlanContext,
    PlannedAssertion,
    Sample,
    Watch,
    WindowedChecker,
    rounded,
)
from fieldproof.dsl import AssertionBase, Duration, ScenarioError, Strict
from fieldproof.manifest import Name
from fieldproof.world import Pose

TRUTH_BUFFER = 600
"""Ground-truth poses kept for matching (a minute at 10 Hz)."""
Alignment = Literal["first", "spawn"]


# --------------------------------------------------------------------------------- the spec
class Estimate(Strict):
    """The stack's pose estimate: a topic carrying a pose."""

    topic: str = Field(
        description="A `geometry_msgs/msg/PoseStamped`, `PoseWithCovarianceStamped` or "
        "`nav_msgs/msg/Odometry` topic of the vehicle (`pose`, `amcl_pose`, `odom`)."
    )
    message_type: str | None = Field(
        default=None, description="Its message type (absent: discovered from the graph)."
    )


class LocalisationError(AssertionBase):
    """The vehicle's pose estimate stays close to the ground truth.

    The estimate's frame (the stack's map, or its odometry frame) is aligned to the world
    once: `align: first` at the first estimate matched to a ground-truth pose (the stack
    started there), `align: spawn` at the vehicle's spawn pose. Each estimate is compared to
    the ground truth interpolated to its stamp (an estimate without a ground-truth pose within
    `max_age_s` of it is not judged). Fails at the first error over `max_m` (or heading error
    over `max_yaw_rad`) inside the window; at the end, fails when fewer than `min_samples`
    estimates were judged or the mean or the 95th percentile is over its limit; passes
    otherwise.
    """

    type: Literal["localisation_error"]
    vehicle: Name = Field(description="The vehicle whose estimate and ground truth are read.")
    estimate: Estimate = Field(description="The estimate topic.")
    max_m: PositiveFloat = Field(description="The position error never to exceed, in metres.")
    mean_m: PositiveFloat | None = Field(
        default=None, description="The mean position error not to exceed, judged at the end."
    )
    p95_m: PositiveFloat | None = Field(
        default=None, description="The 95th percentile of the position error, judged at the end."
    )
    max_yaw_rad: PositiveFloat | None = Field(
        default=None, description="The heading error never to exceed (absent: not judged)."
    )
    align: Alignment = Field(
        default="first",
        description="How the estimate's frame is placed in the world: at the first matched "
        "estimate, or at the vehicle's spawn pose.",
    )
    max_age_s: PositiveFloat = Field(
        default=0.5,
        description="How far an estimate's stamp may be from the nearest ground-truth pose.",
    )
    min_samples: PositiveInt = Field(
        default=1, description="Estimates that must have been judged by the end."
    )
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")
    within_s: Duration | None = Field(
        default=None, description="End of the window (absent: the end of the timeline)."
    )

    @model_validator(mode="after")
    def _window(self) -> LocalisationError:
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        return self


# ------------------------------------------------------------------------------ the tracker
def _lerp_angle(a: float, b: float, u: float) -> float:
    return math.remainder(a + math.remainder(b - a, math.tau) * u, math.tau)


class LocalisationTracker:
    """Estimates matched to the ground truth, the frames aligned, the errors kept."""

    def __init__(
        self,
        model: str,
        *,
        topic: str | None = None,
        align: str = "first",
        spawn: Pose | None = None,
        max_age_s: float = 0.5,
        after_s: float = 0.0,
        within_s: float | None = None,
    ) -> None:
        self.model = model
        self.topic = topic
        """The estimate topic (an estimate sample of another topic is not this tracker's)."""
        self.align = align
        self.max_age_s = max_age_s
        self.after_s = after_s
        self.within_s = within_s
        self.origin: tuple[float, float, float] | None = (
            (spawn.x, spawn.y, spawn.yaw) if align == "spawn" and spawn is not None else None
        )
        """The estimate frame's origin in the world: `x`, `y`, `yaw`."""
        self.truth: deque[tuple[float, float, float, float]] = deque(maxlen=TRUTH_BUFFER)
        """Ground-truth poses: `(stamp, x, y, yaw)`, in stamp order."""
        self.pending: list[tuple[float, float, float, float, float]] = []
        """Estimates waiting for a ground-truth pose past their stamp: `(t, stamp, x, y, yaw)`."""
        self.errors: list[float] = []
        self.yaw_errors: list[float] = []
        self.sum = 0.0
        self.sum_squares = 0.0
        self.worst: dict[str, Any] | None = None
        self.worst_yaw: dict[str, Any] | None = None
        self.last: dict[str, Any] | None = None
        self.unmatched = 0
        self.truth_samples = 0

    # ---------------------------------------------------------------------------- inputs
    def in_window(self, t: float) -> bool:
        """Whether `t` is inside the window."""
        if t < self.after_s:
            return False
        return self.within_s is None or t <= self.within_s

    def on_sample(self, sample: Sample, t: float) -> list[dict[str, Any]]:
        """A sample; the estimates judged because of it (each with its error)."""
        if sample.data.get("model") != self.model:
            return []
        if sample.kind == "pose":
            return self._on_truth(sample)
        if sample.kind == "estimate":
            data = sample.data
            if self.topic is not None and data.get("topic") not in (None, self.topic):
                return []
            if sample.sim is None or t < 0 or not self.in_window(t) or "error" in data:
                return []
            self.pending.append(
                (t, sample.sim, float(data["x"]), float(data["y"]), float(data.get("yaw", 0.0)))
            )
            return self._match()
        return []

    def _on_truth(self, sample: Sample) -> list[dict[str, Any]]:
        if sample.sim is None:
            return []
        data = sample.data
        entry = (sample.sim, float(data["x"]), float(data["y"]), float(data.get("yaw", 0.0)))
        if self.truth and entry[0] < self.truth[-1][0]:
            return []  # out of order: a replayed topic restarted; keep the stream monotonic
        self.truth.append(entry)
        self.truth_samples += 1
        return self._match()

    def _match(self, *, final: bool = False) -> list[dict[str, Any]]:
        """Judge the pending estimates a ground-truth pose now covers (all of them at the end)."""
        judged = []
        kept = []
        for estimate in self.pending:
            stamp = estimate[1]
            if not final and (not self.truth or stamp > self.truth[-1][0]):
                kept.append(estimate)
                continue
            truth = self._truth_at(stamp)
            if truth is None:
                self.unmatched += 1
                continue
            judged.append(self._judge(estimate, truth))
        self.pending = kept
        return judged

    def _truth_at(self, stamp: float) -> tuple[float, float, float] | None:  # noqa: PLR0911
        """The ground truth interpolated to `stamp`, or None if none is close enough."""
        if not self.truth:
            return None
        stamps = [entry[0] for entry in self.truth]
        index = bisect_left(stamps, stamp)
        if index < len(stamps) and abs(stamps[index] - stamp) <= 1e-9:
            _s, x, y, yaw = self.truth[index]
            return x, y, yaw
        before = self.truth[index - 1] if index > 0 else None
        after = self.truth[index] if index < len(stamps) else None
        if before is not None and after is not None:
            span = after[0] - before[0]
            if span <= 0:
                return before[1:]
            u = (stamp - before[0]) / span
            if (
                span > 2 * self.max_age_s
                and min(stamp - before[0], after[0] - stamp) > self.max_age_s
            ):
                return None
            return (
                before[1] + (after[1] - before[1]) * u,
                before[2] + (after[2] - before[2]) * u,
                _lerp_angle(before[3], after[3], u),
            )
        nearest = before if before is not None else after
        if nearest is None or abs(nearest[0] - stamp) > self.max_age_s:
            return None
        return nearest[1:]

    def _judge(
        self, estimate: tuple[float, float, float, float, float], truth: tuple[float, float, float]
    ) -> dict[str, Any]:
        t, stamp, ex, ey, eyaw = estimate
        gx, gy, gyaw = truth
        if self.origin is None:
            # The estimate frame's origin: where the world's pose of this estimate puts it.
            oyaw = math.remainder(gyaw - eyaw, math.tau)
            cos, sin = math.cos(oyaw), math.sin(oyaw)
            self.origin = (gx - (cos * ex - sin * ey), gy - (sin * ex + cos * ey), oyaw)
        ox, oy, oyaw = self.origin
        cos, sin = math.cos(oyaw), math.sin(oyaw)
        wx = ox + cos * ex - sin * ey
        wy = oy + sin * ex + cos * ey
        wyaw = math.remainder(oyaw + eyaw, math.tau)
        error = math.hypot(wx - gx, wy - gy)
        yaw_error = abs(math.remainder(wyaw - gyaw, math.tau))
        self.errors.append(error)
        self.yaw_errors.append(yaw_error)
        self.sum += error
        self.sum_squares += error * error
        record = {
            "t_s": rounded(t),
            "sim_s": rounded(stamp),
            "error_m": rounded(error),
            "yaw_error_rad": rounded(yaw_error),
            "estimate": {"x": rounded(wx), "y": rounded(wy), "yaw": rounded(wyaw)},
            "truth": {"x": rounded(gx), "y": rounded(gy), "yaw": rounded(gyaw)},
        }
        self.last = record
        if self.worst is None or error > self.worst["error_m"]:
            self.worst = record
        if self.worst_yaw is None or yaw_error > self.worst_yaw["yaw_error_rad"]:
            self.worst_yaw = record
        return record

    def finish(self) -> list[dict[str, Any]]:
        """Judge what is still pending against the nearest ground truth."""
        return self._match(final=True)

    # --------------------------------------------------------------------------- outputs
    @property
    def samples(self) -> int:
        """Estimates judged."""
        return len(self.errors)

    def percentile(self, fraction: float) -> float | None:
        """A percentile of the position error."""
        if not self.errors:
            return None
        ordered = sorted(self.errors)
        index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return ordered[index]

    def metrics(self) -> dict[str, Metric]:
        """`max_error_m`, `mean_error_m`, `p95_error_m`, `rms_error_m`, `final_error_m`, ..."""
        n = len(self.errors)
        return {
            "max_error_m": (self.worst["error_m"] if self.worst else None, "m"),
            "mean_error_m": (self.sum / n if n else None, "m"),
            "p95_error_m": (self.percentile(0.95), "m"),
            "rms_error_m": (math.sqrt(self.sum_squares / n) if n else None, "m"),
            "final_error_m": (self.last["error_m"] if self.last else None, "m"),
            "max_yaw_error_rad": (
                self.worst_yaw["yaw_error_rad"] if self.worst_yaw else None,
                "rad",
            ),
            "samples": (n if n else None, ""),
            "unmatched": (self.unmatched if self.unmatched else None, ""),
        }

    def alignment(self) -> dict[str, Any] | None:
        """The estimate frame's origin in the world, as data."""
        if self.origin is None:
            return None
        return {
            "mode": self.align,
            "x": rounded(self.origin[0]),
            "y": rounded(self.origin[1]),
            "yaw": rounded(self.origin[2]),
        }


# ------------------------------------------------------------------------------ the checker
class LocalisationErrorChecker(WindowedChecker):
    """The estimate against the ground truth, judged as the stream goes and at the end."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: LocalisationError = planned.assertion  # type: ignore[assignment]
        if planned.model is None:
            raise ValueError(f"{planned.name}: localisation_error needs a model")
        spawn = planned.resolved.get("spawn")
        self.tracker = LocalisationTracker(
            planned.model,
            topic=planned.topic,
            align=self.assertion.align,
            spawn=spawn if isinstance(spawn, Pose) else None,
            max_age_s=self.assertion.max_age_s,
            after_s=self.after_s,
            within_s=self.within_s,
        )

    def metrics(self) -> dict[str, Metric]:
        """The tracker's."""
        return self.tracker.metrics()

    def _evidence(self) -> dict[str, Any]:
        tracker = self.tracker
        return {
            "worst": tracker.worst,
            "final": tracker.last,
            "alignment": tracker.alignment(),
            "samples": tracker.samples,
            "unmatched": tracker.unmatched,
            "truth_samples": tracker.truth_samples,
        }

    def _judge_now(self, judged: list[dict[str, Any]], t: float, sample: Sample | None) -> None:
        assertion = self.assertion
        for record in judged:
            self.note(t, sample, error_m=record["error_m"], yaw_error_rad=record["yaw_error_rad"])
            if record["error_m"] > assertion.max_m:
                self.resolve(
                    FAILED,
                    f"position error {record['error_m']:.2f} m at {record['t_s']:.1f} s "
                    f"(limit {assertion.max_m:g} m)",
                    t,
                    sample.sim if sample else None,
                    violation=record,
                    **self._evidence(),
                )
                return
            if (
                assertion.max_yaw_rad is not None
                and record["yaw_error_rad"] > assertion.max_yaw_rad
            ):
                self.resolve(
                    FAILED,
                    f"heading error {record['yaw_error_rad']:.2f} rad at {record['t_s']:.1f} s "
                    f"(limit {assertion.max_yaw_rad:g} rad)",
                    t,
                    sample.sim if sample else None,
                    violation=record,
                    **self._evidence(),
                )
                return

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done:
            return
        judged = self.tracker.on_sample(sample, t)
        self._judge_now(judged, t, sample)

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        if not self.done and self.window_closed(t):
            self._settle(t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if not self.done:
            self._settle(t)

    def _settle(self, t: float) -> None:
        self._judge_now(self.tracker.finish(), t, None)
        if self.done:
            return
        assertion, tracker = self.assertion, self.tracker
        n = tracker.samples
        if n < assertion.min_samples:
            why = (
                f"{n} estimate(s) judged, {assertion.min_samples} needed"
                + (
                    f" ({tracker.unmatched} without a ground-truth pose)"
                    if tracker.unmatched
                    else ""
                )
                + (
                    "; no ground-truth pose was observed"
                    if not tracker.truth_samples
                    else "; no estimate was observed"
                    if not n and not tracker.unmatched
                    else ""
                )
            )
            self.resolve(FAILED, why, t, **self._evidence())
            return
        mean = tracker.sum / n
        p95 = tracker.percentile(0.95) or 0.0
        if assertion.mean_m is not None and mean > assertion.mean_m:
            self.resolve(
                FAILED,
                f"mean position error {mean:.2f} m over {n} estimate(s) (limit "
                f"{assertion.mean_m:g} m)",
                t,
                **self._evidence(),
            )
            return
        if assertion.p95_m is not None and p95 > assertion.p95_m:
            self.resolve(
                FAILED,
                f"95th percentile of the position error {p95:.2f} m over {n} estimate(s) "
                f"(limit {assertion.p95_m:g} m)",
                t,
                **self._evidence(),
            )
            return
        worst = tracker.worst["error_m"] if tracker.worst else 0.0
        self.resolve(
            PASSED,
            f"position error at most {worst:.2f} m (mean {mean:.2f} m, p95 {p95:.2f} m) over "
            f"{n} estimate(s) {self.window_text()} (limit {assertion.max_m:g} m)",
            t,
            **self._evidence(),
        )


# --------------------------------------------------------------------------------- the type
class LocalisationErrorType(AssertionType):
    """`localisation_error`."""

    type: ClassVar[str] = "localisation_error"
    Spec: ClassVar[AssertionSpec] = LocalisationError
    summary: ClassVar[str] = (
        "The vehicle's pose estimate stays within a distance of the ground truth."
    )
    metrics: ClassVar[dict[str, str]] = {
        "max_error_m": "the largest position error (m)",
        "mean_error_m": "the mean position error (m)",
        "p95_error_m": "the 95th percentile of the position error (m)",
        "rms_error_m": "the root mean square of the position error (m)",
        "final_error_m": "the last position error (m)",
        "max_yaw_error_rad": "the largest heading error (rad)",
        "samples": "estimates judged",
        "unmatched": "estimates without a ground-truth pose close enough",
    }

    def plan(self, spec: LocalisationError, context: PlanContext) -> PlannedAssertion:
        """The vehicle's model, its spawn pose and the estimate topic."""
        vehicle = context.vehicle(spec.vehicle)
        topic = context.topic(spec.vehicle, spec.estimate.topic, "estimate.topic")
        if spec.align == "spawn" and not getattr(vehicle, "has_spawn", True):
            raise ScenarioError(
                f"{context.where}.align: spawn needs the vehicle's spawn pose, which "
                f"{spec.vehicle} has none of (give it one, or align: first)"
            )
        return PlannedAssertion(
            spec,
            topic=topic,
            model=vehicle.name,
            resolved={"spawn": vehicle.spawn, "estimate_type": spec.estimate.message_type},
            watch=(
                Watch("model", {"model": vehicle.name}),
                Watch(
                    "estimate",
                    {"model": vehicle.name, "topic": topic, "type": spec.estimate.message_type},
                ),
            ),
        )

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return LocalisationErrorChecker(planned)


ASSERTIONS: tuple[type[AssertionType], ...] = (LocalisationErrorType,)
