"""The spatial assertions (task P2-T05): ``geofence``, ``min_distance``, ``path_efficiency``.

All three read the simulator's ground truth (the ``pose`` samples of the observer, 10 Hz per
model). A :class:`Region` is a polygon, a circle or a box in the world, with optional height
limits; a :class:`Subject` is what a distance is measured to (another vehicle, a model of the
world, a placement). The :class:`PathTracker` and :class:`DistanceTracker` are shared with the
KPI collectors (:mod:`fieldproof.assertions.kpi`).
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveFloat, model_validator

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
from fieldproof.dsl import AssertionBase, Duration, Placement, Strict
from fieldproof.manifest import Name
from fieldproof.world import Pose

Point = tuple[float, float]


# ------------------------------------------------------------------------------ the regions
class Circle(Strict):
    """A disc in the world."""

    x: float
    y: float
    radius_m: PositiveFloat


class Box(Strict):
    """An axis-aligned rectangle in the world."""

    x_min: float
    x_max: float
    y_min: float
    y_max: float

    @model_validator(mode="after")
    def _ordered(self) -> Box:
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("x_max must be above x_min and y_max above y_min")
        return self


class Region(Strict):
    """A region of the world: a `polygon` (vertices `[x, y]`), a `circle` or a `box`.

    `z_min` and `z_max` bound the height as well (absent: any height). Exactly one shape.
    """

    polygon: list[list[float]] | None = Field(
        default=None, description="At least three vertices `[x, y]`, in order."
    )
    circle: Circle | None = None
    box: Box | None = None
    z_min: float | None = None
    z_max: float | None = None

    @model_validator(mode="after")
    def _one_shape(self) -> Region:
        shapes = [s for s in (self.polygon, self.circle, self.box) if s is not None]
        if len(shapes) != 1:
            raise ValueError("give exactly one of polygon, circle or box")
        if self.polygon is not None:
            if len(self.polygon) < 3:
                raise ValueError("a polygon needs at least three vertices")
            for vertex in self.polygon:
                if len(vertex) != 2:
                    raise ValueError("a polygon vertex is [x, y]")
        if self.z_min is not None and self.z_max is not None and self.z_max <= self.z_min:
            raise ValueError("z_max must be above z_min")
        return self

    def vertices(self) -> list[Point]:
        """The polygon's vertices (an empty list for the other shapes)."""
        return [(float(v[0]), float(v[1])) for v in self.polygon or []]

    def contains(self, x: float, y: float, z: float | None = None) -> bool:
        """Whether the point is inside (the boundary counts as inside)."""
        if z is not None:
            if self.z_min is not None and z < self.z_min:
                return False
            if self.z_max is not None and z > self.z_max:
                return False
        return self.clearance(x, y) >= 0.0

    def clearance(self, x: float, y: float) -> float:
        """The signed distance to the boundary: positive inside, negative outside."""
        if self.circle is not None:
            return self.circle.radius_m - math.hypot(x - self.circle.x, y - self.circle.y)
        if self.box is not None:
            box = self.box
            dx = min(x - box.x_min, box.x_max - x)
            dy = min(y - box.y_min, box.y_max - y)
            if dx >= 0 and dy >= 0:
                return min(dx, dy)
            return -math.hypot(min(dx, 0.0), min(dy, 0.0))
        points = self.vertices()
        distance = min(
            _segment_distance((x, y), points[i], points[(i + 1) % len(points)])
            for i in range(len(points))
        )
        return distance if _in_polygon((x, y), points) else -distance

    def describe(self) -> str:
        """`circle of 5 m around (1, 2)`, `box [0, 10] x [-5, 5]`, `polygon of 4 vertices`."""
        if self.circle is not None:
            c = self.circle
            text = f"circle of {c.radius_m:g} m around ({c.x:g}, {c.y:g})"
        elif self.box is not None:
            b = self.box
            text = f"box [{b.x_min:g}, {b.x_max:g}] x [{b.y_min:g}, {b.y_max:g}]"
        else:
            text = f"polygon of {len(self.polygon or [])} vertices"
        if self.z_min is not None or self.z_max is not None:
            text += f", z in [{self.z_min if self.z_min is not None else '-inf'}, "
            text += f"{self.z_max if self.z_max is not None else 'inf'}]"
        return text


def _in_polygon(point: Point, vertices: list[Point]) -> bool:
    """Ray casting; a point on an edge counts as inside."""
    x, y = point
    inside = False
    count = len(vertices)
    for i in range(count):
        (x1, y1), (x2, y2) = vertices[i], vertices[(i + 1) % count]
        if _segment_distance(point, (x1, y1), (x2, y2)) < 1e-9:
            return True
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing:
                inside = not inside
    return inside


def _segment_distance(point: Point, a: Point, b: Point) -> float:
    """The distance from a point to a segment."""
    (px, py), (ax, ay), (bx, by) = point, a, b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0.0:
        return math.hypot(px - ax, py - ay)
    u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + u * dx), py - (ay + u * dy))


class Subject(Strict):
    """What a distance is measured to: another `vehicle`, a `model` of the world, a `target`."""

    vehicle: Name | None = Field(default=None, description="Another vehicle of the scenario.")
    model: str | None = Field(default=None, description="A model of the world (its name).")
    target: Placement | None = Field(default=None, description="A placement in the world.")

    @model_validator(mode="after")
    def _one(self) -> Subject:
        given = [s for s in (self.vehicle, self.model, self.target) if s is not None]
        if len(given) != 1:
            raise ValueError("give exactly one of vehicle, model or target")
        return self

    def describe(self) -> str:
        """`vehicle ugv2`, `model crate`, `(10, -1)`."""
        if self.vehicle is not None:
            return f"vehicle {self.vehicle}"
        if self.model is not None:
            return f"model {self.model}"
        target = self.target
        assert target is not None  # noqa: S101  (validated)
        if target.frame is not None:
            return f"frame {target.frame}"
        return f"({target.x:g}, {target.y:g})"


# ------------------------------------------------------------------------------- the specs
class Geofence(AssertionBase):
    """The vehicle's ground truth stays inside (or outside) a region of the world.

    Fails at the first ground-truth sample on the wrong side of the boundary between
    `after_s` and `within_s`; passes when the window closes or at the end of the timeline.
    The evidence is the smallest clearance to the boundary and the violating sample.
    """

    type: Literal["geofence"]
    vehicle: Name = Field(description="The vehicle whose ground-truth pose is read.")
    region: Region = Field(description="A polygon, circle or box, with optional z limits.")
    mode: Literal["inside", "outside"] = Field(
        default="inside", description="Whether the vehicle must stay inside or outside."
    )
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")
    within_s: Duration | None = Field(
        default=None, description="End of the window (absent: the end of the timeline)."
    )

    @model_validator(mode="after")
    def _window(self) -> Geofence:
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        return self


class MinDistance(AssertionBase):
    """The vehicle keeps a minimum distance to another vehicle, a model or a point.

    Measured between ground-truth positions (the model origins) at every pose sample of
    either; fails at the first sample closer than `min_m` between `after_s` and `within_s`.
    The evidence is the closest approach: when, how close, both positions.
    """

    type: Literal["min_distance"]
    vehicle: Name = Field(description="The vehicle whose ground-truth pose is read.")
    to: Subject = Field(description="What the distance is measured to.")
    min_m: PositiveFloat = Field(description="The distance never to go below, in metres.")
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")
    within_s: Duration | None = Field(
        default=None, description="End of the window (absent: the end of the timeline)."
    )

    def vehicle_names(self) -> list[str]:
        """See :meth:`AssertionBase.vehicle_names`."""
        return [self.vehicle, *([self.to.vehicle] if self.to.vehicle else [])]

    @model_validator(mode="after")
    def _window(self) -> MinDistance:
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        if self.to.vehicle == self.vehicle:
            raise ValueError("to.vehicle must be another vehicle")
        return self


class PathEfficiency(AssertionBase):
    """The path the vehicle drove is close to the straight line.

    The path length is summed from the ground-truth poses during an `action` (from its start
    to its end) or between `after_s` and `within_s`; the straight line goes from the first
    pose of that window to the `target` (or to the last pose). The ratio of the two, the
    efficiency, must reach `min_ratio`, and the path must stay under `max_path_m` when given.
    Judged when the action ends (or the window closes, or the timeline ends); a path over
    `max_path_m` fails as soon as it is.
    """

    type: Literal["path_efficiency"]
    vehicle: Name = Field(description="The vehicle whose ground-truth pose is read.")
    action: str | None = Field(
        default=None, description="A timeline entry: the window is its run (start to end)."
    )
    target: Placement | None = Field(
        default=None, description="The straight line's end (absent: the last pose)."
    )
    min_ratio: float = Field(
        default=0.5, gt=0.0, le=1.0, description="Straight-line distance over path length."
    )
    max_path_m: PositiveFloat | None = Field(
        default=None, description="The path length never to exceed, in metres."
    )
    after_s: Duration = Field(
        default=0.0, description="Start of the window when there is no action."
    )
    within_s: Duration | None = Field(
        default=None, description="End of the window when there is no action."
    )

    def action_names(self) -> list[str]:
        """See :meth:`AssertionBase.action_names`."""
        return [self.action] if self.action else []

    @model_validator(mode="after")
    def _window(self) -> PathEfficiency:
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        if self.action is not None and (self.after_s or self.within_s is not None):
            raise ValueError("after_s and within_s go without an action (its run is the window)")
        return self


# ---------------------------------------------------------------------------- the trackers
def _xy(sample: Sample) -> tuple[float, float, float]:
    data = sample.data
    return float(data.get("x", 0.0)), float(data.get("y", 0.0)), float(data.get("z", 0.0))


class PathTracker:
    """The path a model travels, its speed and the straight line, over a window.

    The window is an action's run (`action`) or `after_s` to `within_s`; a pose sample inside
    it adds the distance from the previous one to the path. Speeds are over simulation time
    where the stream has it.
    """

    def __init__(
        self,
        model: str,
        *,
        action: str | None = None,
        after_s: float = 0.0,
        within_s: float | None = None,
        target: Pose | None = None,
    ) -> None:
        self.model = model
        self.action = action
        self.after_s = after_s
        self.within_s = within_s
        self.target = target
        self.started_t: float | None = None
        self.ended_t: float | None = None
        self.first: tuple[float, float] | None = None
        self.last: tuple[float, float] | None = None
        self.last_time: float | None = None
        self.path_m = 0.0
        self.max_speed: float | None = None
        self.samples = 0
        self.ended = False

    def active(self, t: float) -> bool:
        """Whether `t` is inside the window."""
        if self.action is not None:
            return self.started_t is not None and not self.ended
        if t < self.after_s:
            return False
        return self.within_s is None or t <= self.within_s

    def on_action(self, label: str, event: str, t: float) -> bool:
        """Track the action's run; True when it just ended."""
        if self.action is None or label != self.action:
            return False
        if event == "started":
            self.started_t = t
            return False
        if self.started_t is not None and not self.ended:
            self.ended, self.ended_t = True, t
            return True
        return False

    def on_sample(self, sample: Sample, t: float) -> float | None:
        """A sample; the distance added to the path, or None if it was not counted."""
        if sample.kind != "pose" or sample.data.get("model") != self.model:
            return None
        if not self.active(t):
            return None
        x, y, _z = _xy(sample)
        now = sample.sim if sample.sim is not None else t
        step = None
        if self.first is None:
            self.first = (x, y)
            self.started_t = self.started_t if self.started_t is not None else t
        if self.last is not None:
            step = math.hypot(x - self.last[0], y - self.last[1])
            self.path_m += step
            if self.last_time is not None and now > self.last_time:
                speed = step / (now - self.last_time)
                self.max_speed = speed if self.max_speed is None else max(self.max_speed, speed)
        self.last, self.last_time = (x, y), now
        self.samples += 1
        return step

    def straight_m(self) -> float | None:
        """The straight line from the first pose to the target (or the last pose)."""
        if self.first is None:
            return None
        end = (self.target.x, self.target.y) if self.target is not None else self.last
        if end is None:
            return None
        return math.hypot(end[0] - self.first[0], end[1] - self.first[1])

    def efficiency(self) -> float | None:
        """Straight line over path (1.0 for a vehicle that did not move and had no target)."""
        straight = self.straight_m()
        if straight is None:
            return None
        if self.path_m <= 0.0:
            return 1.0 if straight <= 0.0 else 0.0
        return min(straight / self.path_m, 1.0)

    def duration_s(self, t: float) -> float | None:
        """How long the window has lasted, up to `t`."""
        if self.started_t is None and self.first is None:
            return None
        start = self.started_t if self.started_t is not None else self.after_s
        end = self.ended_t if self.ended_t is not None else t
        return max(end - start, 0.0)

    def metrics(self, t: float) -> dict[str, Metric]:
        """`path_m`, `straight_m`, `efficiency`, `max_speed_mps`, `mean_speed_mps`."""
        duration = self.duration_s(t)
        mean = self.path_m / duration if duration else None
        return {
            "path_m": (self.path_m if self.samples else None, "m"),
            "straight_m": (self.straight_m(), "m"),
            "efficiency": (self.efficiency(), ""),
            "max_speed_mps": (self.max_speed, "m/s"),
            "mean_speed_mps": (mean, "m/s"),
        }


class DistanceTracker:
    """The distance between a model and a subject (another model or a fixed point)."""

    def __init__(
        self,
        model: str,
        *,
        other_model: str | None = None,
        other_pose: Pose | None = None,
        after_s: float = 0.0,
        within_s: float | None = None,
    ) -> None:
        self.model = model
        self.other_model = other_model
        self.other_pose = other_pose
        self.after_s = after_s
        self.within_s = within_s
        self.own: tuple[float, float] | None = None
        self.other: tuple[float, float] | None = (
            (other_pose.x, other_pose.y) if other_pose is not None else None
        )
        self.closest: dict[str, Any] | None = None
        self.last: float | None = None
        self.samples = 0

    def on_sample(self, sample: Sample, t: float) -> float | None:
        """A sample; the distance it gives (inside the window), or None."""
        if sample.kind != "pose":
            return None
        model = sample.data.get("model")
        if model == self.model:
            self.own = _xy(sample)[:2]
        elif self.other_model is not None and model == self.other_model:
            self.other = _xy(sample)[:2]
        else:
            return None
        if self.own is None or self.other is None or t < self.after_s:
            return None
        if self.within_s is not None and t > self.within_s:
            return None
        distance = math.hypot(self.own[0] - self.other[0], self.own[1] - self.other[1])
        self.samples += 1
        self.last = distance
        if self.closest is None or distance < self.closest["distance_m"]:
            self.closest = {
                "t_s": rounded(t),
                "sim_s": rounded(sample.sim),
                "distance_m": rounded(distance),
                "own": {"x": rounded(self.own[0]), "y": rounded(self.own[1])},
                "other": {"x": rounded(self.other[0]), "y": rounded(self.other[1])},
            }
        return distance

    def metrics(self) -> dict[str, Metric]:
        """`closest_m`, `closest_at_s`, `final_m`."""
        return {
            "closest_m": (self.closest["distance_m"] if self.closest else None, "m"),
            "closest_at_s": (self.closest["t_s"] if self.closest else None, "s"),
            "final_m": (self.last, "m"),
        }


# ---------------------------------------------------------------------------- the checkers
class GeofenceChecker(WindowedChecker):
    """The ground truth on the right side of the region's boundary."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: Geofence = planned.assertion  # type: ignore[assignment]
        self.region = self.assertion.region
        self.samples = 0
        self.min_clearance: dict[str, Any] | None = None

    def metrics(self) -> dict[str, Metric]:
        """`min_clearance_m` (to the boundary, on the required side), `samples`."""
        return {
            "min_clearance_m": (
                self.min_clearance["clearance_m"] if self.min_clearance else None,
                "m",
            ),
            "samples": (self.samples, ""),
        }

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done or sample.kind != "pose" or sample.data.get("model") != self.planned.model:
            return
        if not self.in_window(t):
            if self.window_closed(t):
                self.on_tick(t)
            return
        x, y, z = _xy(sample)
        inside = self.region.contains(x, y, z)
        clearance = self.region.clearance(x, y)
        if self.assertion.mode == "outside":
            clearance = -clearance
        self.samples += 1
        self.note(t, sample, x=x, y=y, clearance_m=clearance)
        where = {"t_s": rounded(t), "sim_s": rounded(sample.sim), "x": rounded(x), "y": rounded(y)}
        where["clearance_m"] = rounded(clearance)
        if self.min_clearance is None or clearance < self.min_clearance["clearance_m"]:
            self.min_clearance = where
        wrong = inside if self.assertion.mode == "outside" else not inside
        if wrong:
            side = "left" if self.assertion.mode == "inside" else "entered"
            self.resolve(
                FAILED,
                f"{self.planned.model} {side} the {self.region.describe()} at {t:.1f} s "
                f"(at ({x:.2f}, {y:.2f}), {abs(clearance):.2f} m past the boundary)",
                t,
                sample.sim,
                violation=where,
                samples=self.samples,
            )

    def _settle(self, t: float) -> None:
        if self.samples == 0:
            self.resolve(
                FAILED,
                f"no ground-truth pose of {self.planned.model} {self.window_text()}",
                t,
                samples=0,
            )
            return
        assert self.min_clearance is not None  # noqa: S101  (samples > 0)
        self.resolve(
            PASSED,
            f"{self.planned.model} stayed {self.assertion.mode} the {self.region.describe()} "
            f"{self.window_text()} ({self.samples} sample(s), closest to the boundary "
            f"{self.min_clearance['clearance_m']:.2f} m)",
            t,
            min_clearance=self.min_clearance,
            samples=self.samples,
        )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        if not self.done and self.window_closed(t):
            self._settle(t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if not self.done:
            self._settle(t)


class MinDistanceChecker(WindowedChecker):
    """The distance to the subject never below the minimum."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: MinDistance = planned.assertion  # type: ignore[assignment]
        if planned.model is None:
            raise ValueError(f"{planned.name}: min_distance needs the vehicle's model")
        self.tracker = DistanceTracker(
            planned.model,
            other_model=planned.resolved.get("other_model"),
            other_pose=planned.resolved.get("other_pose"),
            after_s=self.after_s,
            within_s=self.within_s,
        )

    def metrics(self) -> dict[str, Metric]:
        """The tracker's `closest_m`, `closest_at_s`, `final_m`."""
        return self.tracker.metrics()

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done:
            return
        distance = self.tracker.on_sample(sample, t)
        if distance is None:
            if self.window_closed(t):
                self.on_tick(t)
            return
        self.note(t, sample, distance_m=distance)
        if distance < self.assertion.min_m:
            self.resolve(
                FAILED,
                f"{self.planned.model} came within {distance:.2f} m of "
                f"{self.assertion.to.describe()} at {t:.1f} s (minimum "
                f"{self.assertion.min_m:g} m)",
                t,
                sample.sim,
                closest=self.tracker.closest,
                samples=self.tracker.samples,
            )

    def _settle(self, t: float) -> None:
        closest = self.tracker.closest
        if closest is None:
            self.resolve(
                FAILED,
                f"no distance between {self.planned.model} and {self.assertion.to.describe()} "
                f"was measured {self.window_text()}",
                t,
                samples=0,
            )
            return
        self.resolve(
            PASSED,
            f"{self.planned.model} stayed {closest['distance_m']:.2f} m or more from "
            f"{self.assertion.to.describe()} {self.window_text()} (minimum "
            f"{self.assertion.min_m:g} m, {self.tracker.samples} sample(s))",
            t,
            closest=closest,
            samples=self.tracker.samples,
        )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        if not self.done and self.window_closed(t):
            self._settle(t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if not self.done:
            self._settle(t)


class PathEfficiencyChecker(Checker):
    """The path against the straight line."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: PathEfficiency = planned.assertion  # type: ignore[assignment]
        if planned.model is None:
            raise ValueError(f"{planned.name}: path_efficiency needs the vehicle's model")
        self.tracker = PathTracker(
            planned.model,
            action=self.assertion.action,
            after_s=self.assertion.after_s,
            within_s=self.assertion.within_s,
            target=planned.target,
        )
        self.last_t = 0.0

    def metrics(self) -> dict[str, Metric]:
        """The tracker's `path_m`, `straight_m`, `efficiency`, `max_speed_mps`, `mean_speed_mps`."""
        return self.tracker.metrics(self.last_t)

    def _evidence(self, t: float) -> dict[str, Any]:
        measured = self.tracker.metrics(t)
        return {key: rounded(value) for key, (value, _unit) in measured.items()} | {
            "samples": self.tracker.samples
        }

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done:
            return
        self.last_t = max(self.last_t, t)
        step = self.tracker.on_sample(sample, t)
        if step is None:
            within = self.tracker.within_s
            if self.assertion.action is None and within is not None and t > within:
                self._judge(t, "the window closed")
            return
        self.note(t, sample, path_m=self.tracker.path_m)
        limit = self.assertion.max_path_m
        if limit is not None and self.tracker.path_m > limit:
            self.resolve(
                FAILED,
                f"{self.planned.model} drove {self.tracker.path_m:.2f} m by {t:.1f} s, over "
                f"the limit of {limit:g} m",
                t,
                sample.sim,
                **self._evidence(t),
            )

    def on_action(self, label: str, event: str, t: float, status: str | None = None) -> None:
        """See :meth:`Checker.on_action`."""
        self.last_t = max(self.last_t, t)
        if self.tracker.on_action(label, event, t):
            self._judge(t, f"{label} ended {status}")

    def _judge(self, t: float, when: str) -> None:
        if self.done:
            return
        self.last_t = max(self.last_t, t)
        if self.tracker.samples < 2:
            self.resolve(
                FAILED,
                f"{when}: fewer than two ground-truth poses of {self.planned.model} in the window",
                t,
                **self._evidence(t),
            )
            return
        ratio = self.tracker.efficiency() or 0.0
        straight = self.tracker.straight_m() or 0.0
        path = self.tracker.path_m
        limit = self.assertion.max_path_m
        summary = f"{path:.2f} m driven for {straight:.2f} m straight (efficiency {ratio:.2f})"
        if limit is not None and path > limit:
            self.resolve(
                FAILED, f"{when}: {summary}, over the limit of {limit:g} m", t, **self._evidence(t)
            )
        elif ratio < self.assertion.min_ratio:
            self.resolve(
                FAILED,
                f"{when}: {summary}, below {self.assertion.min_ratio:g}",
                t,
                **self._evidence(t),
            )
        else:
            self.resolve(
                PASSED,
                f"{when}: {summary}, at least {self.assertion.min_ratio:g}",
                t,
                **self._evidence(t),
            )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        self.last_t = max(self.last_t, t)
        within = self.assertion.within_s
        if self.assertion.action is None and within is not None and t >= within:
            self._judge(t, "the window closed")

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if self.done:
            return
        if self.assertion.action is not None and self.tracker.started_t is None:
            self.resolve(FAILED, f"{self.assertion.action} never started", t, **self._evidence(t))
            return
        self._judge(t, "the timeline ended")


# ------------------------------------------------------------------------------- the types
class GeofenceType(AssertionType):
    """`geofence`."""

    type: ClassVar[str] = "geofence"
    Spec: ClassVar[AssertionSpec] = Geofence
    summary: ClassVar[str] = "The vehicle's ground truth stays inside (or outside) a region."
    metrics: ClassVar[dict[str, str]] = {
        "min_clearance_m": "the smallest clearance to the boundary, on the required side (m)",
        "samples": "ground-truth poses judged",
    }

    def plan(self, spec: Geofence, context: PlanContext) -> PlannedAssertion:
        """The vehicle's model."""
        return PlannedAssertion(spec, model=context.vehicle(spec.vehicle).name)

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return GeofenceChecker(planned)


class MinDistanceType(AssertionType):
    """`min_distance`."""

    type: ClassVar[str] = "min_distance"
    Spec: ClassVar[AssertionSpec] = MinDistance
    summary: ClassVar[str] = "The vehicle keeps a distance to another vehicle, a model or a point."
    metrics: ClassVar[dict[str, str]] = {
        "closest_m": "the closest approach (m)",
        "closest_at_s": "when it happened (s)",
        "final_m": "the distance at the end (m)",
    }

    def plan(self, spec: MinDistance, context: PlanContext) -> PlannedAssertion:
        """The vehicle's model and the subject: another model, or a pose in the world."""
        vehicle = context.vehicle(spec.vehicle)
        resolved: dict[str, Any] = {}
        watch: tuple[Watch, ...] = ()
        if spec.to.vehicle is not None:
            resolved["other_model"] = context.vehicle(spec.to.vehicle, "to.vehicle").name
        elif spec.to.model is not None:
            resolved["other_model"] = spec.to.model
            watch = (Watch("model", {"model": spec.to.model}),)
        elif spec.to.target is not None:
            resolved["other_pose"] = context.place(spec.to.target, "to.target")
        return PlannedAssertion(spec, model=vehicle.name, resolved=resolved, watch=watch)

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return MinDistanceChecker(planned)


class PathEfficiencyType(AssertionType):
    """`path_efficiency`."""

    type: ClassVar[str] = "path_efficiency"
    Spec: ClassVar[AssertionSpec] = PathEfficiency
    summary: ClassVar[str] = "The path driven is close to the straight line (and short enough)."
    metrics: ClassVar[dict[str, str]] = {
        "path_m": "the path length (m)",
        "straight_m": "the straight-line distance (m)",
        "efficiency": "straight line over path (0 to 1)",
        "max_speed_mps": "the highest ground-truth speed (m/s)",
        "mean_speed_mps": "the path over the window's duration (m/s)",
    }

    def plan(self, spec: PathEfficiency, context: PlanContext) -> PlannedAssertion:
        """The vehicle's model, the action, the target in the world."""
        vehicle = context.vehicle(spec.vehicle)
        if spec.action is not None:
            context.action(spec.action)
        target = context.place(spec.target) if spec.target is not None else None
        return PlannedAssertion(spec, model=vehicle.name, target=target)

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return PathEfficiencyChecker(planned)


ASSERTIONS: tuple[type[AssertionType], ...] = (GeofenceType, MinDistanceType, PathEfficiencyType)
