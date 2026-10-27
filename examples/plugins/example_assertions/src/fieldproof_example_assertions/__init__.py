"""An example assertion plugin (task P2-T05): the assertion type ``example.visits_waypoints``.

The assertion passes once the vehicle's ground truth has come within a tolerance of every
waypoint of a list, in any order (a patrol, a coverage check), and fails at the deadline or
the end of the timeline naming the waypoints not visited. It shows the three parts of an
assertion plugin: the spec (what the scenario file says, validated by the engine's schema
and exported to its JSON Schema), the ``plan`` hook (the waypoints placed in the world, the
vehicle's model) and the checker (the streaming logic on the observer's ``pose`` samples,
with evidence and metrics). The engine finds it through the ``fieldproof.assertions`` entry
point in ``pyproject.toml``; ``fieldproof assertions`` lists it next to the built-ins.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveFloat

from fieldproof.assertions import (
    FAILED,
    PASSED,
    AssertionSpec,
    AssertionType,
    Checker,
    Metric,
    PlanContext,
    PlannedAssertion,
    Sample,
)
from fieldproof.dsl import AssertionBase, Duration, Placement
from fieldproof.manifest import Name
from fieldproof.world import Pose


class VisitsWaypoints(AssertionBase):
    """The vehicle's ground truth visits every waypoint, in any order."""

    type: Literal["example.visits_waypoints"]
    vehicle: Name = Field(description="The vehicle whose ground-truth pose is read.")
    waypoints: list[Placement] = Field(
        min_length=1, description="The placements to visit (frames or coordinates)."
    )
    tolerance_m: PositiveFloat = Field(
        default=0.5, description="How close counts as a visit, in metres."
    )
    within_s: Duration | None = Field(
        default=None, description="Deadline after the timeline starts (absent: its end)."
    )


class VisitsWaypointsChecker(Checker):
    """Ticks off each waypoint at the first pose within tolerance."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: VisitsWaypoints = planned.assertion  # type: ignore[assignment]
        self.waypoints: list[Pose] = list(planned.resolved.get("waypoints", []))
        self.visited: dict[int, dict[str, Any]] = {}
        self.samples = 0

    def metrics(self) -> dict[str, Metric]:
        """`visited` (how many waypoints), `completed_at_s`."""
        done = len(self.visited) == len(self.waypoints)
        return {
            "visited": (len(self.visited), ""),
            "completed_at_s": (self.result.t_s if done and self.done else None, "s"),
        }

    def _missing(self) -> list[str]:
        return [
            f"({w.x:g}, {w.y:g})" for i, w in enumerate(self.waypoints) if i not in self.visited
        ]

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done or sample.kind != "pose" or sample.data.get("model") != self.planned.model:
            return
        if t < 0:
            return
        self.samples += 1
        x, y = float(sample.data.get("x", 0.0)), float(sample.data.get("y", 0.0))
        for index, waypoint in enumerate(self.waypoints):
            if index in self.visited:
                continue
            distance = math.hypot(x - waypoint.x, y - waypoint.y)
            if distance <= self.assertion.tolerance_m:
                self.visited[index] = {"t_s": round(t, 3), "distance_m": round(distance, 3)}
                self.note(t, sample, waypoint=index, distance_m=distance)
        if len(self.visited) == len(self.waypoints):
            self.resolve(
                PASSED,
                f"all {len(self.waypoints)} waypoint(s) visited by {t:.1f} s",
                t,
                sample.sim,
                visited=self.visited,
                samples=self.samples,
            )

    def _fail(self, t: float, why: str) -> None:
        missing = self._missing()
        self.resolve(
            FAILED,
            f"{why}: {len(self.visited)} of {len(self.waypoints)} waypoint(s) visited, missing "
            f"{', '.join(missing)}",
            t,
            visited=self.visited,
            missing=missing,
            samples=self.samples,
        )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        deadline = self.assertion.within_s
        if deadline is not None and t >= deadline:
            self._fail(t, f"not all visited within {deadline:g} s")

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if not self.done:
            self._fail(t, "the timeline ended")


class VisitsWaypointsType(AssertionType):
    """`example.visits_waypoints`."""

    type: ClassVar[str] = "example.visits_waypoints"
    Spec: ClassVar[AssertionSpec] = VisitsWaypoints
    summary: ClassVar[str] = "The vehicle's ground truth visits every waypoint, in any order."
    metrics: ClassVar[dict[str, str]] = {
        "visited": "waypoints visited",
        "completed_at_s": "when the last one was visited (s)",
    }

    def plan(self, spec: VisitsWaypoints, context: PlanContext) -> PlannedAssertion:
        """The waypoints in the world, the vehicle's model."""
        vehicle = context.vehicle(spec.vehicle)
        waypoints = [
            context.place(w, f"waypoints[{index}]") for index, w in enumerate(spec.waypoints)
        ]
        return PlannedAssertion(spec, model=vehicle.name, resolved={"waypoints": waypoints})

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return VisitsWaypointsChecker(planned)
