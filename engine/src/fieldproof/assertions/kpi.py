"""The KPI collectors (task P2-T05): the scenario's ``kpis`` section.

A collector measures what an assertion would judge, without a verdict: ``path`` (the path
driven, the straight line, the efficiency, the speeds), ``mission_time`` (a span of the
timeline), ``distance`` (the closest approach to a vehicle, a model or a point), ``rate`` (a
topic's rate), ``latency`` (a stamped topic's age), ``resources`` (a container's CPU and
memory) and ``value`` (the statistics of a field, and how often a condition held). The
collectors share the trackers of the assertion types and run next to the checkers in the
:class:`~fieldproof.assertions.core.Evaluator`; their measurements join every assertion's
metrics in the run's ``kpis`` (``run.json``, the report, ``/fieldproof/kpis``), one flat map
of ``<name>.<metric>`` to a value and a unit, which ``fieldproof runs diff`` (task P2-T06)
compares between runs.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import Field, model_validator

from fieldproof.assertions.core import (
    MEASURED,
    MISSING,
    Checker,
    KpiBase,
    Metric,
    PlanContext,
    PlannedAssertion,
    Sample,
    Watch,
    compile_or_refuse,
    rounded,
)
from fieldproof.assertions.spatial import DistanceTracker, PathTracker, Subject
from fieldproof.assertions.system import ResourceTracker
from fieldproof.assertions.timing import LatencyTracker, MissionClock
from fieldproof.dsl import Duration, Placement, ScenarioError
from fieldproof.expression import ExpressionError, compile_condition
from fieldproof.manifest import Name


# ------------------------------------------------------------------------------- the specs
class PathKpi(KpiBase):
    """The path the vehicle drove: length, straight line, efficiency, speeds."""

    type: Literal["path"]
    vehicle: Name = Field(description="The vehicle whose ground-truth pose is read.")
    action: str | None = Field(default=None, description="A timeline entry: its run is the window.")
    target: Placement | None = Field(
        default=None, description="The straight line's end (absent: the last pose)."
    )
    after_s: Duration = Field(default=0.0, description="Start of the window without an action.")
    within_s: Duration | None = Field(default=None, description="End of the window without one.")

    def action_names(self) -> list[str]:
        """See :meth:`KpiBase.action_names`."""
        return [self.action] if self.action else []


class MissionTimeKpi(KpiBase):
    """How long a span of the timeline took."""

    type: Literal["mission_time"]
    start: str | None = Field(default=None, description="The entry whose start opens the span.")
    end: str | None = Field(default=None, description="The entry whose end closes the span.")

    def action_names(self) -> list[str]:
        """See :meth:`KpiBase.action_names`."""
        return [a for a in (self.start, self.end) if a is not None]


class DistanceKpi(KpiBase):
    """The closest the vehicle came to another vehicle, a model or a point."""

    type: Literal["distance"]
    vehicle: Name = Field(description="The vehicle whose ground-truth pose is read.")
    to: Subject = Field(description="What the distance is measured to.")
    after_s: Duration = Field(default=0.0, description="Start of the window.")
    within_s: Duration | None = Field(default=None, description="End of the window.")

    def vehicle_names(self) -> list[str]:
        """See :meth:`KpiBase.vehicle_names`."""
        return [self.vehicle, *([self.to.vehicle] if self.to.vehicle else [])]


class RateKpi(KpiBase):
    """A topic's rate over the run (in simulation time)."""

    type: Literal["rate"]
    topic: str = Field(description="The topic (relative to the vehicle's namespace).")
    vehicle: Name | None = Field(default=None, description="The vehicle the topic belongs to.")
    window_s: Duration = Field(default=5.0, description="Length of the measurement window.")
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")
    message_type: str | None = Field(default=None, description="Message type (absent: discovered).")


class LatencyKpi(KpiBase):
    """A stamped topic's age (header stamp to receipt)."""

    type: Literal["latency"]
    topic: str = Field(description="The topic (relative to the vehicle's namespace).")
    vehicle: Name | None = Field(default=None, description="The vehicle the topic belongs to.")
    window_s: Duration = Field(default=2.0, description="Length of the sliding window.")
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")
    message_type: str | None = Field(default=None, description="Message type (absent: discovered).")


class ResourcesKpi(KpiBase):
    """A container's CPU and memory over the run."""

    type: Literal["resources"]
    vehicle: Name = Field(description="The vehicle whose container is measured.")
    container: Literal["vehicle", "bridge"] = Field(default="vehicle")
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")


class ValueKpi(KpiBase):
    """The statistics of a field of a topic, and how often a condition held."""

    type: Literal["value"]
    topic: str = Field(description="The topic (relative to the vehicle's namespace).")
    vehicle: Name | None = Field(default=None, description="The vehicle the topic belongs to.")
    field: str = Field(description="Field of the message.")
    count_if: str | None = Field(
        default=None, description="A condition over `value`; `count` is how often it held."
    )
    after_s: Duration = Field(default=0.0, description="Start of the window.")
    within_s: Duration | None = Field(default=None, description="End of the window.")
    message_type: str | None = Field(default=None, description="Message type (absent: discovered).")

    @model_validator(mode="after")
    def _compiles(self) -> ValueKpi:
        if self.count_if is not None:
            compile_or_refuse(self.count_if)
        return self


Kpi = Annotated[
    Union[PathKpi, MissionTimeKpi, DistanceKpi, RateKpi, LatencyKpi, ResourcesKpi, ValueKpi],  # noqa: UP007
    Field(discriminator="type"),
]
KPI_TYPES = ("path", "mission_time", "distance", "rate", "latency", "resources", "value")


# ------------------------------------------------------------------------- the collectors
class Collector(Checker):
    """A KPI collector: a checker that measures and never fails."""

    def settle(self, t: float, detail: str, **evidence: Any) -> None:
        """The timeline ended: `measured`, or `missing` when nothing was measured."""
        measured = {k: v for k, (v, _u) in self.metrics().items() if v is not None}
        status = MEASURED if measured else MISSING
        self.resolve(status, detail, t, **evidence)


class PathCollector(Collector):
    """`path`."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        spec: PathKpi = planned.assertion  # type: ignore[assignment]
        self.tracker = PathTracker(
            planned.model or spec.vehicle,
            action=spec.action,
            after_s=spec.after_s,
            within_s=spec.within_s,
            target=planned.target,
        )
        self.last_t = 0.0

    def metrics(self) -> dict[str, Metric]:
        """The path tracker's."""
        return self.tracker.metrics(self.last_t)

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        self.last_t = max(self.last_t, t)
        self.tracker.on_sample(sample, t)

    def on_action(
        self,
        label: str,
        event: str,
        t: float,
        status: str | None = None,  # noqa: ARG002  (the path does not care how it ended)
    ) -> None:
        """See :meth:`Checker.on_action`."""
        self.last_t = max(self.last_t, t)
        self.tracker.on_action(label, event, t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        self.last_t = max(self.last_t, t)
        self.settle(
            t,
            f"{self.tracker.path_m:.2f} m over {self.tracker.samples} pose(s)",
            samples=self.tracker.samples,
        )


class MissionTimeCollector(Collector):
    """`mission_time`."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        spec: MissionTimeKpi = planned.assertion  # type: ignore[assignment]
        self.clock = MissionClock(spec.start, spec.end)
        self.last_t = 0.0

    def metrics(self) -> dict[str, Metric]:
        """`duration_s`."""
        return self.clock.metrics(self.last_t)

    def on_action(self, label: str, event: str, t: float, status: str | None = None) -> None:
        """See :meth:`Checker.on_action`."""
        self.last_t = max(self.last_t, t)
        self.clock.on_action(label, event, t, status)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        self.last_t = max(self.last_t, t)
        self.clock.close(t)
        took = self.clock.elapsed(t)
        self.settle(
            t,
            f"{took:.1f} s" if took is not None else "the span never started",
            started_t_s=rounded(self.clock.started_t),
            ended_t_s=rounded(self.clock.ended_t),
            end_status=self.clock.end_status,
        )


class DistanceCollector(Collector):
    """`distance`."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        spec: DistanceKpi = planned.assertion  # type: ignore[assignment]
        self.tracker = DistanceTracker(
            planned.model or spec.vehicle,
            other_model=planned.resolved.get("other_model"),
            other_pose=planned.resolved.get("other_pose"),
            after_s=spec.after_s,
            within_s=spec.within_s,
        )

    def metrics(self) -> dict[str, Metric]:
        """The distance tracker's."""
        return self.tracker.metrics()

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        self.tracker.on_sample(sample, t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        closest = self.tracker.closest
        self.settle(
            t,
            f"closest {closest['distance_m']:.2f} m" if closest else "no distance measured",
            closest=closest,
            samples=self.tracker.samples,
        )


class RateCollector(Collector):
    """`rate`."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.spec: RateKpi = planned.assertion  # type: ignore[assignment]
        self.windows = 0
        self.total = 0.0
        self.lowest: float | None = None
        self.highest: float | None = None
        self.messages = 0

    def metrics(self) -> dict[str, Metric]:
        """`mean_hz`, `lowest_hz`, `highest_hz`, `messages`."""
        return {
            "mean_hz": (self.total / self.windows if self.windows else None, "Hz"),
            "lowest_hz": (self.lowest, "Hz"),
            "highest_hz": (self.highest, "Hz"),
            "messages": (self.messages, ""),
        }

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if sample.kind != "rate" or sample.data.get("topic") != self.planned.topic:
            return
        self.messages = max(self.messages, int(sample.data.get("total", 0)))
        if t < self.spec.after_s:
            return
        hz = float(sample.data.get("hz", 0.0))
        self.windows += 1
        self.total += hz
        self.lowest = hz if self.lowest is None else min(self.lowest, hz)
        self.highest = hz if self.highest is None else max(self.highest, hz)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        self.settle(t, f"{self.windows} window(s)", windows=self.windows)


class LatencyCollector(Collector):
    """`latency`."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        spec: LatencyKpi = planned.assertion  # type: ignore[assignment]
        self.tracker = LatencyTracker(planned.topic, spec.after_s)

    def metrics(self) -> dict[str, Metric]:
        """The latency tracker's."""
        return self.tracker.metrics()

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        self.tracker.on_sample(sample, t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        self.settle(t, f"{self.tracker.windows} window(s)", windows=self.tracker.windows)


class ResourcesCollector(Collector):
    """`resources`."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        spec: ResourcesKpi = planned.assertion  # type: ignore[assignment]
        self.container = str(planned.resolved.get("container", ""))
        self.tracker = ResourceTracker(self.container, after_s=spec.after_s)

    def metrics(self) -> dict[str, Metric]:
        """The resource tracker's."""
        return self.tracker.metrics()

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        self.tracker.on_sample(sample, t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        self.settle(t, f"{self.tracker.samples} sample(s) of {self.container}")


class ValueCollector(Collector):
    """`value`."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.spec: ValueKpi = planned.assertion  # type: ignore[assignment]
        self.condition = compile_condition(self.spec.count_if) if self.spec.count_if else None
        self.samples = 0
        self.count = 0
        self.total = 0.0
        self.numeric = 0
        self.lowest: float | None = None
        self.highest: float | None = None
        self.last: Any = None
        self.error: str | None = None

    def metrics(self) -> dict[str, Metric]:
        """`samples`, `min`, `max`, `mean`, `last`, `count`."""
        return {
            "samples": (self.samples, ""),
            "min": (self.lowest, ""),
            "max": (self.highest, ""),
            "mean": (self.total / self.numeric if self.numeric else None, ""),
            "last": (self.last if isinstance(self.last, int | float) else None, ""),
            "count": (self.count if self.condition is not None else None, ""),
        }

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if (
            sample.kind != "value"
            or sample.data.get("topic") != self.planned.topic
            or sample.data.get("field") != self.spec.field
        ):
            return
        if "error" in sample.data:
            self.error = str(sample.data["error"])
            return
        if t < self.spec.after_s or (self.spec.within_s is not None and t > self.spec.within_s):
            return
        value = sample.data.get("value")
        self.samples += 1
        self.last = value
        if isinstance(value, int | float) and not isinstance(value, bool):
            self.numeric += 1
            self.total += float(value)
            self.lowest = float(value) if self.lowest is None else min(self.lowest, float(value))
            self.highest = float(value) if self.highest is None else max(self.highest, float(value))
        if self.condition is not None:
            try:
                if self.condition(value):
                    self.count += 1
            except ExpressionError as error:
                self.error = str(error)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        detail = f"{self.samples} message(s)"
        if self.error:
            detail += f"; {self.error}"
        self.settle(t, detail, samples=self.samples, error=self.error)


# --------------------------------------------------------------------------------- planning
KpiSpec = type[KpiBase]
CollectorClass = type[Collector]


class KpiType:
    """How a collector type is planned and built."""

    type: ClassVar[str]
    Spec: ClassVar[KpiSpec]
    summary: ClassVar[str] = ""
    metrics: ClassVar[dict[str, str]] = {}
    collector: ClassVar[CollectorClass]

    def plan(self, spec: Any, context: PlanContext) -> PlannedAssertion:  # noqa: ARG002
        """Resolve the collector's references against the plan."""
        return PlannedAssertion(spec)

    def watches(self, planned: PlannedAssertion) -> list[Watch]:
        """What the observer must watch for it."""
        return list(planned.watch)


class PathType(KpiType):
    """`path`."""

    type = "path"
    Spec = PathKpi
    summary = "The path the vehicle drove: length, straight line, efficiency, speeds."
    metrics: ClassVar[dict[str, str]] = {
        "path_m": "the path length (m)",
        "straight_m": "the straight-line distance (m)",
        "efficiency": "straight line over path (0 to 1)",
        "max_speed_mps": "the highest ground-truth speed (m/s)",
        "mean_speed_mps": "the path over the window's duration (m/s)",
    }
    collector = PathCollector

    def plan(self, spec: PathKpi, context: PlanContext) -> PlannedAssertion:
        """The vehicle's model, the action, the target."""
        vehicle = context.vehicle(spec.vehicle)
        if spec.action is not None:
            context.action(spec.action)
        target = context.place(spec.target) if spec.target is not None else None
        return PlannedAssertion(spec, model=vehicle.name, target=target)


class MissionTimeKpiType(KpiType):
    """`mission_time`."""

    type = "mission_time"
    Spec = MissionTimeKpi
    summary = "How long a span of the timeline took."
    metrics: ClassVar[dict[str, str]] = {"duration_s": "the span's duration (s)"}
    collector = MissionTimeCollector

    def plan(self, spec: MissionTimeKpi, context: PlanContext) -> PlannedAssertion:
        """The entries must be timeline entries."""
        for what in ("start", "end"):
            name = getattr(spec, what)
            if name is not None:
                context.action(name, what)
        return PlannedAssertion(spec)


class DistanceType(KpiType):
    """`distance`."""

    type = "distance"
    Spec = DistanceKpi
    summary = "The closest the vehicle came to another vehicle, a model or a point."
    metrics: ClassVar[dict[str, str]] = {
        "closest_m": "the closest approach (m)",
        "closest_at_s": "when it happened (s)",
        "final_m": "the distance at the end (m)",
    }
    collector = DistanceCollector

    def plan(self, spec: DistanceKpi, context: PlanContext) -> PlannedAssertion:
        """The vehicle's model and the subject."""
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


class RateType(KpiType):
    """`rate`."""

    type = "rate"
    Spec = RateKpi
    summary = "A topic's rate (in simulation time)."
    metrics: ClassVar[dict[str, str]] = {
        "mean_hz": "the mean of the windows' rates (Hz)",
        "lowest_hz": "the slowest window (Hz)",
        "highest_hz": "the fastest window (Hz)",
        "messages": "messages received",
    }
    collector = RateCollector

    def plan(self, spec: RateKpi, context: PlanContext) -> PlannedAssertion:
        """The topic under the vehicle's namespace."""
        topic = context.topic(spec.vehicle, spec.topic)
        watch = Watch(
            "rate", {"topic": topic, "window_s": spec.window_s, "type": spec.message_type}
        )
        return PlannedAssertion(spec, topic=topic, watch=(watch,))


class LatencyType(KpiType):
    """`latency`."""

    type = "latency"
    Spec = LatencyKpi
    summary = "A stamped topic's age (header stamp to receipt)."
    metrics: ClassVar[dict[str, str]] = {
        "p95_ms": "the worst window's 95th percentile (ms)",
        "max_ms": "the worst message (ms)",
        "mean_ms": "the mean over the windows (ms)",
        "messages": "messages received",
    }
    collector = LatencyCollector

    def plan(self, spec: LatencyKpi, context: PlanContext) -> PlannedAssertion:
        """The topic under the vehicle's namespace."""
        topic = context.topic(spec.vehicle, spec.topic)
        watch = Watch(
            "latency", {"topic": topic, "window_s": spec.window_s, "type": spec.message_type}
        )
        return PlannedAssertion(spec, topic=topic, watch=(watch,))


class ResourcesType(KpiType):
    """`resources`."""

    type = "resources"
    Spec = ResourcesKpi
    summary = "A container's CPU and memory."
    metrics: ClassVar[dict[str, str]] = {
        "peak_cpu_pct": "the highest CPU sample (% of one core)",
        "mean_cpu_pct": "the mean CPU (% of one core)",
        "peak_memory_mb": "the highest memory sample (MiB)",
        "mean_memory_mb": "the mean memory (MiB)",
        "samples": "samples",
    }
    collector = ResourcesCollector

    def plan(self, spec: ResourcesKpi, context: PlanContext) -> PlannedAssertion:
        """The container's name."""
        vehicle = context.vehicle(spec.vehicle)
        name = vehicle.container_name if spec.container == "vehicle" else vehicle.bridge_name
        return PlannedAssertion(spec, resolved={"container": name})


class ValueType(KpiType):
    """`value`."""

    type = "value"
    Spec = ValueKpi
    summary = "The statistics of a field of a topic, and how often a condition held."
    metrics: ClassVar[dict[str, str]] = {
        "samples": "messages seen",
        "min": "the lowest value",
        "max": "the highest value",
        "mean": "the mean of the numeric values",
        "last": "the last value",
        "count": "how often `count_if` held",
    }
    collector = ValueCollector

    def plan(self, spec: ValueKpi, context: PlanContext) -> PlannedAssertion:
        """The topic under the vehicle's namespace."""
        topic = context.topic(spec.vehicle, spec.topic)
        watch = Watch("value", {"topic": topic, "field": spec.field, "type": spec.message_type})
        return PlannedAssertion(spec, topic=topic, watch=(watch,))


KPIS: dict[str, KpiType] = {
    t.type: t()
    for t in (
        PathType,
        MissionTimeKpiType,
        DistanceType,
        RateType,
        LatencyType,
        ResourcesType,
        ValueType,
    )
}


def plan_kpi(spec: KpiBase, context: PlanContext) -> PlannedAssertion:
    """Resolve a collector through its type."""
    kind = spec.type
    if kind not in KPIS:
        raise ScenarioError(f"{context.where}.type: {kind!r} is not a KPI collector")
    return KPIS[kind].plan(spec, context)


def collector_of(planned: PlannedAssertion) -> Collector:
    """The collector of a planned KPI."""
    return KPIS[planned.type].collector(planned)


def kpi_watches(planned: PlannedAssertion) -> list[Watch]:
    """What the observer must watch for a planned KPI."""
    return KPIS[planned.type].watches(planned)
