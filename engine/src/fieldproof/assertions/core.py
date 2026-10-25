"""The assertion library's core (tasks P1-T06 and P2-T05).

The stream, the checkers, the plugin API, the registry and the streaming evaluator.

The runner resolves every assertion of a scenario into a :class:`PlannedAssertion` through its
type's :meth:`AssertionType.plan` hook (topics under the vehicle's namespace, targets as world
poses, Gazebo models, containers) and builds an :class:`Evaluator` from them. While the
timeline runs, the observer in the recorder container (:mod:`fieldproof.observer`) appends one
JSON line per observation to ``samples.jsonl`` in the run directory and the engine's resource
monitor (:mod:`fieldproof.resources`) one per container and second to ``resources.jsonl``;
the runner tails both, feeds every line as a :class:`Sample`, tells the evaluator when actions
start and end, and ticks it on every poll so deadlines fire. Each assertion's
:class:`Checker` resolves once, as soon as the stream decides it (a ``reach_pose`` passes at
the first pose within tolerance, a ``no_collision`` fails at the first contact); what is still
pending at the end of the timeline is settled by :meth:`Evaluator.finish`. Every verdict
carries evidence: the samples that decided it, counts and extremes, an excerpt of the stream
just before the decision, in simulation time where the stream has it. Every checker also
reports its **metrics** (the KPIs of the run: a path length, a duration, a peak), as do the
**KPI collectors** of the scenario's ``kpis`` section, which measure without judging.

**Assertion types** are plugins (ADR-0005): a class deriving from :class:`AssertionType` names
its ``type`` (the discriminator of the ``assertions[]`` union), its ``Spec`` (a Pydantic model
deriving from :class:`~fieldproof.dsl.AssertionBase` with the parameters, which the scenario
schema validates and exports as JSON Schema), a :meth:`AssertionType.plan` hook and a
:meth:`AssertionType.checker` factory. They are found through Python entry points of the
group ``fieldproof.assertions``: the engine registers its built-ins (the five primitives of
P1-T06 in :mod:`fieldproof.assertions.primitives`, the spatial, timing, system and temporal
assertions of P2-T05) and a distribution installed next to the engine adds its own
(``examples/plugins/example_assertions`` shows one). ``fieldproof assertions`` lists the
registry.

Times: ``t`` is seconds after the timeline started, on the wall clock the engine and the
containers share; deadlines (``within_s``, ``max_s``, ``after_s``) are in this time, as the
timeline's ``at`` is. In lockstep mode (task P2-T04) the evaluator counts simulation seconds
from the origin the clock was locked at instead (``sim_base``), so the deadlines and the
timeline share the stepped clock and a run's verdicts repeat from one run to the next.
"""

from __future__ import annotations

import importlib
import json
import math
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import EntryPoint, entry_points
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    Union,
    get_args,
    get_origin,
)

from pydantic import Field

from fieldproof.dsl import AssertionBase, Placement, ScenarioError, Signal, Strict
from fieldproof.expression import Condition, ExpressionError, compile_condition
from fieldproof.manifest import Name
from fieldproof.network import NETWORK_FILE
from fieldproof.world import Pose, WorldInfo, place

if TYPE_CHECKING:
    from fieldproof.events import VehicleHandle

PENDING = "pending"
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
# The statuses of a KPI collector: it measured, or it had nothing to measure.
MEASURED = "measured"
MISSING = "missing"
# The side-channel topics the observer publishes and the recorder always records.
SIDE_CHANNELS = {
    "events": "/fieldproof/events",
    "assertions": "/fieldproof/assertions",
    "metrics": "/fieldproof/metrics",
    "network": "/fieldproof/network",
    "resources": "/fieldproof/resources",
    "kpis": "/fieldproof/kpis",
}
# Files of the run directory the observer reads and writes.
SAMPLES_FILE = "samples.jsonl"
ASSERTIONS_FILE = "assertions.jsonl"
KPIS_FILE = "kpis.jsonl"
RESOURCES_FILE = "resources.jsonl"
OBSERVE_FILE = "observe.json"
RECORDING_DIR = "recording"
DEFAULT_CONTACTS_TOPIC = "contacts"
# How many of the samples a checker noted are kept as the excerpt of its verdict.
EXCERPT_SIZE = 6
ENTRY_POINT_GROUP = "fieldproof.assertions"
BUILTIN_MODULES = (
    "fieldproof.assertions.primitives",
    "fieldproof.assertions.spatial",
    "fieldproof.assertions.timing",
    "fieldproof.assertions.system",
    "fieldproof.assertions.temporal",
)
BUILTIN = "built-in"
# The kinds of observation an assertion asks the observer for (`observe.json`).
WATCH_KINDS = ("rate", "value", "contact", "latency", "lifecycle", "model")

Metric = tuple[Any, str]
"""A measured value and its unit (`(4.2, "m")`, `(3, "")` for a count)."""
AssertionSpec = type[AssertionBase]
"""The class of an assertion type's spec model."""


# ------------------------------------------------------------------------------- the stream
@dataclass(frozen=True)
class Sample:
    """One line of the observer's stream (or of the resource monitor's)."""

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
    """The verdict of one assertion (or the measurement of one KPI collector), with evidence."""

    name: str
    type: str
    status: str = PENDING
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    t_s: float | None = None
    """Seconds after the timeline started when the assertion resolved."""
    sim_s: float | None = None
    """Simulation time of the sample that resolved it, when the stream had one."""
    metrics: dict[str, Any] = field(default_factory=dict)
    """What the checker measured (`{"path_m": {"value": 4.2, "unit": "m"}}`), the run's KPIs."""

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
            "metrics": self.metrics,
        }


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def rounded(value: Any, digits: int = 3) -> Any:
    """A float rounded for the record; anything else as it is."""
    if isinstance(value, float):
        return round(value, digits)
    return value


# ----------------------------------------------------------------------------- the planned
@dataclass(frozen=True)
class Watch:
    """What the observer must watch for an assertion: one entry of `observe.json`.

    `kind` is `rate` (`topic`, `window_s`, `type`), `value` (`topic`, `field`, `type`),
    `contact` (`topic`, `model`), `latency` (`topic`, `window_s`, `type`), `lifecycle`
    (`node`) or `model` (a Gazebo `model` whose ground-truth pose is streamed).
    """

    kind: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in WATCH_KINDS:
            raise ValueError(f"{self.kind!r} is not a kind of observation ({WATCH_KINDS})")


@dataclass(frozen=True)
class PlannedSignal:
    """A :class:`~fieldproof.dsl.Signal` resolved against the plan.

    An absolute `topic` for a topic field, the Gazebo `model` and the `target` pose for a
    vehicle metric. :meth:`value_of` reads it from a sample.
    """

    signal: Signal
    topic: str | None = None
    model: str | None = None
    target: Pose | None = None

    def value_of(self, sample: Sample) -> Any:
        """What the signal is in this sample, or None if the sample is not its."""
        signal, data = self.signal, sample.data
        if signal.topic is not None:
            ours = (
                sample.kind == "value"
                and data.get("topic") == self.topic
                and data.get("field") == signal.field
                and "error" not in data
            )
            return data.get("value") if ours else None
        if signal.metric in ("sim_time", "rtf"):
            if sample.kind != "clock":
                return None
            return sample.sim if signal.metric == "sim_time" else data.get("rtf")
        if sample.kind != "pose" or data.get("model") != self.model:
            return None
        if signal.metric == "distance_to" and self.target is not None:
            pose = Pose(float(data["x"]), float(data["y"]), float(data.get("z", 0.0)), 0.0)
            return pose.distance_to(self.target)
        return data.get(str(signal.metric))

    def error_of(self, sample: Sample) -> str | None:
        """The observer's error for the signal's field, if this sample reports one."""
        signal, data = self.signal, sample.data
        if (
            signal.topic is not None
            and sample.kind == "value"
            and data.get("topic") == self.topic
            and data.get("field") == signal.field
            and "error" in data
        ):
            return str(data["error"])
        return None

    def watches(self) -> list[Watch]:
        """What the observer must watch for the signal."""
        signal = self.signal
        if signal.topic is not None:
            return [
                Watch(
                    "value",
                    {"topic": self.topic, "field": signal.field, "type": signal.message_type},
                )
            ]
        if self.model is not None:
            return [Watch("model", {"model": self.model})]
        return []

    def summary(self) -> str:
        """`/odom.twist.twist.linear.x`, `ugv1 distance_to (10, -1)`."""
        signal = self.signal
        if signal.topic is not None:
            return f"{self.topic}.{signal.field}"
        if self.model is not None and self.target is not None:
            return f"{self.model} distance_to ({self.target.x:g}, {self.target.y:g})"
        if self.model is not None:
            return f"{self.model} {signal.metric}"
        return str(signal.metric)

    def to_dict(self) -> dict[str, Any]:
        """As data."""
        data: dict[str, Any] = self.signal.model_dump(exclude_none=True)
        if self.topic is not None:
            data["topic"] = self.topic
        if self.model is not None:
            data["model"] = self.model
        if self.target is not None:
            data["target_world"] = {"x": self.target.x, "y": self.target.y}
        return data


class KpiBase(Strict):
    """What every KPI collector has: a unique name and the type that selects its parameters.

    The collectors (:mod:`fieldproof.assertions.kpi`) measure what the assertions judge, without
    a verdict: a path length, a duration, a closest approach, a rate, a latency, a container's
    footprint, the statistics of a field.
    """

    name: Name = Field(description="KPI name: lower case, digits and underscores.")
    type: str = Field(description="The collector type.")

    def vehicle_names(self) -> list[str]:
        """The vehicles the collector refers to (its `vehicle` field by default)."""
        vehicle = getattr(self, "vehicle", None)
        return [vehicle] if isinstance(vehicle, str) else []

    def action_names(self) -> list[str]:
        """The timeline entries the collector refers to (none by default)."""
        return []


@dataclass(frozen=True)
class PlannedAssertion:
    """An assertion (or a KPI collector) with its references resolved against the plan."""

    assertion: AssertionBase | KpiBase
    topic: str | None = None
    """The ROS topic (`topic_rate`, `topic_value`) or Gazebo topic (`no_collision`), absolute."""
    target: Pose | None = None
    """The `reach_pose` target in the world."""
    model: str | None = None
    """The Gazebo model name of the vehicle (`reach_pose`, `no_collision`)."""
    resolved: dict[str, Any] = field(default_factory=dict)
    """What the type's `plan` resolved besides (a container, a node, another model, signals)."""
    watch: tuple[Watch, ...] = ()
    """What the observer must watch for it (the type may derive more in `watches`)."""

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
        for key, value in self.resolved.items():
            data["resolved"][key] = (
                value.to_dict() if isinstance(value, PlannedSignal) else _as_data(value)
            )
        return data


def _as_data(value: Any) -> Any:
    if isinstance(value, PlannedSignal):
        return value.to_dict()
    if isinstance(value, Pose):
        return {"x": value.x, "y": value.y, "z": value.z, "yaw": value.yaw}
    if isinstance(value, Mapping):
        return {k: _as_data(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_as_data(v) for v in value]
    return value


@dataclass(frozen=True)
class PlanContext:
    """What :meth:`AssertionType.plan` resolves a spec against.

    The world, the vehicles, the timeline's labels and the other assertions' names.
    """

    world: WorldInfo
    vehicles: Mapping[str, VehicleHandle]
    actions: tuple[str, ...] = ()
    """The labels of the timeline's entries."""
    assertions: tuple[str, ...] = ()
    """The names of the scenario's assertions."""
    where: str = "assertions[]"
    """The assertion being planned, for error messages (`assertions[2] (reached_goal)`)."""
    time_mode: str = "sim"
    """The scenario's `time.mode` (`sim`, `realtime`, `lockstep`; task P2-T04)."""

    def vehicle(self, name: str | None, what: str = "vehicle") -> VehicleHandle:
        """The vehicle called `name`, or a :class:`ScenarioError` naming `where.what`."""
        if name is None or name not in self.vehicles:
            raise ScenarioError(f"{self.where}.{what}: {name!r} is not a vehicle of the scenario")
        return self.vehicles[name]

    def topic(self, vehicle: str | None, topic: str, what: str = "topic") -> str:
        """An absolute topic: under the vehicle's namespace, or with its leading slash."""
        if vehicle is not None:
            return self.vehicle(vehicle, "vehicle").topic(topic)
        if topic.startswith("/"):
            return topic
        raise ScenarioError(f"{self.where}.{what}: {topic!r} needs a vehicle or a leading /")

    def place(self, placement: Placement, what: str = "target") -> Pose:
        """A placement in the world (a :class:`ScenarioError` for an unknown frame)."""
        return place(placement, self.world, f"{self.where}.{what}")

    def action(self, name: str, what: str = "action") -> str:
        """A timeline entry's label, or a :class:`ScenarioError`."""
        if name not in self.actions:
            known = ", ".join(self.actions) or "none"
            raise ScenarioError(f"{self.where}.{what}: {name!r} is not a timeline entry ({known})")
        return name

    def signal(
        self, signal: Signal, default_vehicle: str | None, what: str = "signal"
    ) -> PlannedSignal:
        """A signal resolved: its topic absolute, its model, its target in the world."""
        vehicle = signal.vehicle or default_vehicle
        topic = model = target = None
        if signal.topic is not None:
            topic = self.topic(vehicle, signal.topic, f"{what}.topic")
        if signal.metric in ("x", "y", "z", "yaw", "distance_to"):
            model = self.vehicle(vehicle, f"{what}.vehicle").name
        if signal.target is not None:
            target = self.place(signal.target, f"{what}.target")
        return PlannedSignal(signal, topic, model, target)


# ----------------------------------------------------------------------------- the checkers
class Checker:
    """One assertion's streaming logic; subclasses override the `on_*` hooks.

    A checker keeps an excerpt of the samples it noted (:meth:`note`), attached to its
    verdict as evidence, and reports what it measured through :meth:`metrics`.
    """

    def __init__(self, planned: PlannedAssertion) -> None:
        self.planned = planned
        self.result = Result(planned.name, planned.type)
        self._excerpt: deque[dict[str, Any]] = deque(maxlen=EXCERPT_SIZE)

    @property
    def done(self) -> bool:
        """Whether the assertion has a verdict."""
        return self.result.resolved

    def note(self, t: float, sample: Sample | None = None, **fields: Any) -> None:
        """Remember a sample for the excerpt of the verdict."""
        entry: dict[str, Any] = {"t_s": _round(t)}
        if sample is not None:
            entry["sim_s"] = _round(sample.sim)
        entry.update({k: rounded(v) for k, v in fields.items()})
        self._excerpt.append(entry)

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
        if self._excerpt:
            self.result.evidence["excerpt"] = list(self._excerpt)
        self.result.metrics = self.measured()

    def measured(self) -> dict[str, Any]:
        """:meth:`metrics` as data (`{"path_m": {"value": 4.2, "unit": "m"}}`)."""
        return {
            key: {"value": rounded(value), "unit": unit}
            for key, (value, unit) in self.metrics().items()
            if value is not None
        }

    def metrics(self) -> dict[str, Metric]:
        """What the checker measured so far (the run's KPIs), by name with a unit."""
        return {}

    def on_sample(self, sample: Sample, t: float) -> None:
        """A new observation at `t` seconds into the timeline (negative before it started)."""

    def on_tick(self, t: float) -> None:
        """Time passed."""

    def on_action(self, label: str, event: str, t: float, status: str | None = None) -> None:
        """A timeline action `started` or `finished` (with its status)."""

    def on_verdict(self, result: Result, t: float) -> None:
        """Another assertion resolved."""

    def depends_on(self) -> Sequence[str]:
        """The assertions whose verdicts this checker waits for (settled before it)."""
        return ()

    def on_finish(self, t: float) -> None:
        """The timeline completed; settle a pending verdict."""


class WindowedChecker(Checker):
    """A checker with an `after_s`/`within_s` window on its spec."""

    @property
    def after_s(self) -> float:
        """When the window opens."""
        return float(getattr(self.planned.assertion, "after_s", 0.0) or 0.0)

    @property
    def within_s(self) -> float | None:
        """When the window closes (None: the end of the timeline)."""
        value = getattr(self.planned.assertion, "within_s", None)
        return float(value) if value is not None else None

    def in_window(self, t: float) -> bool:
        """Whether `t` is inside the window."""
        if t < self.after_s:
            return False
        return self.within_s is None or t <= self.within_s

    def window_text(self) -> str:
        """`from 5 s to 20 s`, `after 5 s`, `during the timeline`."""
        if self.within_s is not None:
            return f"from {self.after_s:g} s to {self.within_s:g} s"
        if self.after_s:
            return f"after {self.after_s:g} s"
        return "during the timeline"

    def window_closed(self, t: float) -> bool:
        """Whether the window has closed by `t`."""
        return self.within_s is not None and t >= self.within_s


# ------------------------------------------------------------------------------- the types
class AssertionType:
    """One kind of assertion; subclasses set the class attributes and the hooks."""

    type: ClassVar[str]
    """The `type` of the assertions this class evaluates (`geofence`)."""
    Spec: ClassVar[AssertionSpec]
    """The schema of those assertions: `type` as a literal, the parameters as fields."""
    summary: ClassVar[str] = ""
    """One line for `fieldproof assertions`."""
    metrics: ClassVar[dict[str, str]] = {}
    """The metrics its checker reports, by name with a one-line meaning."""

    def plan(self, spec: Any, context: PlanContext) -> PlannedAssertion:  # noqa: ARG002
        """Resolve the spec against the plan; raise a :class:`ScenarioError` to refuse it."""
        return PlannedAssertion(spec)

    def checker(self, planned: PlannedAssertion) -> Checker:
        """The streaming checker of a planned assertion."""
        raise NotImplementedError

    def watches(self, planned: PlannedAssertion) -> Sequence[Watch]:
        """What the observer must watch for it (the planned assertion's `watch` by default)."""
        return planned.watch


def parameters_of(spec: type[Strict]) -> dict[str, str]:
    """The spec's own fields with their descriptions (`fieldproof assertions`)."""
    common = {*AssertionBase.model_fields, "type"}
    return {
        name: info.description or ""
        for name, info in spec.model_fields.items()
        if name not in common
    }


def check_assertion_type(candidate: Any) -> type[AssertionType]:
    """The class if `candidate` is a well-formed assertion type, or a :class:`TypeError`."""
    if not isinstance(candidate, type) or not issubclass(candidate, AssertionType):
        raise TypeError(f"{candidate!r} is not an AssertionType subclass")
    kind = getattr(candidate, "type", None)
    if not isinstance(kind, str) or not kind:
        raise TypeError(f"{candidate.__name__}: `type` must be a non-empty string")
    spec = getattr(candidate, "Spec", None)
    if not isinstance(spec, type) or not issubclass(spec, AssertionBase):
        raise TypeError(f"{candidate.__name__}: `Spec` must derive from AssertionBase")
    info = spec.model_fields.get("type")
    annotation = info.annotation if info is not None else None
    if get_origin(annotation) is not Literal or get_args(annotation) != (kind,):
        raise TypeError(f"{candidate.__name__}: {spec.__name__}.type must be Literal[{kind!r}]")
    return candidate


# ---------------------------------------------------------------------------- the registry
@dataclass(frozen=True)
class Registered:
    """An assertion type and where it came from."""

    assertion_type: AssertionType
    source: str
    """`built-in`, or the distribution (name and version) whose entry point registered it."""

    @property
    def type(self) -> str:
        """The type's name."""
        return self.assertion_type.type

    @property
    def spec(self) -> AssertionSpec:
        """The type's schema."""
        return self.assertion_type.Spec


@dataclass
class Registry:
    """The assertion types the engine knows, by type."""

    types: dict[str, Registered] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    """Entry points that could not be loaded or were refused, one line each."""

    def __contains__(self, kind: str) -> bool:
        return kind in self.types

    def __getitem__(self, kind: str) -> Registered:
        return self.types[kind]

    @property
    def names(self) -> list[str]:
        """Every type, sorted."""
        return sorted(self.types)

    def specs(self) -> list[AssertionSpec]:
        """The schemas, in registration order."""
        return [r.spec for r in self.types.values()]

    def add(self, candidate: Any, source: str) -> None:
        """Register an assertion type class.

        A :class:`TypeError` for a malformed one, a :class:`ValueError` for a type already
        taken by another class.
        """
        cls = check_assertion_type(candidate)
        existing = self.types.get(cls.type)
        if existing is not None:
            if type(existing.assertion_type) is cls:
                return  # the same class seen twice (a built-in and its entry point)
            raise ValueError(
                f"{source}: type {cls.type!r} is already registered by {existing.source}"
            )
        self.types[cls.type] = Registered(cls(), source)


def load_registry(
    points: Iterable[EntryPoint] | None = None,
    builtin_modules: Sequence[str] = BUILTIN_MODULES,
) -> Registry:
    """The built-ins, then the entry points of the group (`points` overrides discovery).

    A plugin that fails to import or is malformed does not stop the engine: it is listed in
    `problems` and its type stays unknown to the schema.
    """
    registry = Registry()
    for module_name in builtin_modules:
        module = importlib.import_module(module_name)
        for cls in module.ASSERTIONS:
            registry.add(cls, BUILTIN)
    if points is None:
        points = entry_points(group=ENTRY_POINT_GROUP)
    for point in points:
        dist = getattr(point, "dist", None)
        source = f"{dist.name} {dist.version}" if dist is not None else "an entry point"
        where = f"{source} ({point.name} = {point.value})"
        try:
            registry.add(point.load(), source)
        except (ImportError, AttributeError, TypeError, ValueError) as error:
            registry.problems.append(f"{where}: {type(error).__name__}: {error}")
    return registry


@cache
def registry() -> Registry:
    """The registry of this process, loaded once."""
    return load_registry()


def assertion_union(types: Registry | None = None) -> Any:
    """The assertion type of the scenario schema: the registered specs, told apart by `type`."""
    specs = (types or registry()).specs()
    union = Union[tuple(specs)] if len(specs) > 1 else specs[0]  # noqa: UP007  (dynamic)
    return Annotated[union, Field(discriminator="type")]


def plan_assertion(
    spec: AssertionBase, context: PlanContext, types: Registry | None = None
) -> PlannedAssertion:
    """Resolve an assertion through its type's `plan` hook."""
    kind = spec.type
    known = types or registry()
    if kind not in known:
        raise ScenarioError(f"{context.where}.type: {kind!r} is not a registered assertion type")
    return known[kind].assertion_type.plan(spec, context)


# ---------------------------------------------------------------------------- the evaluator
class Evaluator:
    """Feeds the stream and the timeline's events to every checker and KPI collector."""

    def __init__(
        self,
        planned: Iterable[PlannedAssertion],
        collectors: Iterable[Checker] = (),
        *,
        sim_base: bool = False,
        types: Registry | None = None,
    ) -> None:
        known = types or registry()
        self.checkers = [known[p.type].assertion_type.checker(p) for p in planned]
        self.collectors = list(collectors)
        """The KPI collectors of the scenario's `kpis` section (task P2-T05)."""
        self.start_wall: float | None = None
        self.sim_base = sim_base
        """Whether `t` counts simulation seconds from `origin_sim` (lockstep mode, task
        P2-T04) instead of wall-clock seconds from `start_wall`."""
        self.origin_sim: float | None = None
        self.samples = 0
        self.last_sim: float | None = None
        self.first_sim: float | None = None
        self.bad_lines = 0
        self._abort: Result | None = None

    @property
    def everything(self) -> list[Checker]:
        """The checkers and the collectors."""
        return [*self.checkers, *self.collectors]

    @property
    def results(self) -> list[Result]:
        """Every assertion's result, in the scenario's order."""
        return [c.result for c in self.checkers]

    @property
    def kpi_results(self) -> list[Result]:
        """Every KPI collector's result, in the scenario's order."""
        return [c.result for c in self.collectors]

    @property
    def failed(self) -> list[Result]:
        """The failed assertions."""
        return [r for r in self.results if r.status == FAILED]

    @property
    def pending(self) -> list[Result]:
        """The assertions without a verdict yet."""
        return [r for r in self.results if r.status == PENDING]

    @property
    def abort(self) -> Result | None:
        """The first failed assertion whose `on_fail` is `abort`, if any."""
        return self._abort

    def kpis(self) -> dict[str, dict[str, Any]]:
        """The run's KPIs: every collector's and every checker's metrics, by `source.metric`."""
        kpis: dict[str, dict[str, Any]] = {}
        for checker in self.collectors + self.checkers:
            for key, (value, unit) in checker.metrics().items():
                if value is None:
                    continue
                kpis[f"{checker.result.name}.{key}"] = {
                    "value": rounded(value),
                    "unit": unit,
                    "source": checker.result.name,
                    "type": checker.result.type,
                }
        return kpis

    def elapsed(self, wall: float) -> float:
        """Wall-clock seconds since the timeline started (-inf when it has not)."""
        return wall - self.start_wall if self.start_wall is not None else -math.inf

    def now(self, wall: float) -> float:
        """The timeline's time now: `elapsed(wall)`, or in simulation time the last sample's."""
        if not self.sim_base:
            return self.elapsed(wall)
        if self.origin_sim is None or self.last_sim is None:
            return -math.inf
        return round(self.last_sim - self.origin_sim, 6)

    def time_of(self, sample: Sample) -> float:
        """The timeline's time of a sample: its wall clock, or its simulation time."""
        if not self.sim_base:
            return self.elapsed(sample.wall)
        if self.origin_sim is None:
            return -math.inf
        sim = sample.sim if sample.sim is not None else self.last_sim
        return round(sim - self.origin_sim, 6) if sim is not None else -math.inf

    def start(self, wall: float, origin_sim: float | None = None) -> None:
        """The timeline starts now (at `origin_sim` on the simulation clock, in lockstep)."""
        self.start_wall = wall
        if self.sim_base:
            self.origin_sim = origin_sim
            self.first_sim = origin_sim
            self.last_sim = origin_sim

    def _changed(self, before: list[str]) -> list[Result]:
        return [
            c.result
            for c, status in zip(self.everything, before, strict=True)
            if c.result.status != status
        ]

    def _collect(self, before: list[str], t: float | None = None) -> list[Result]:
        """What resolved since `before`, the verdicts told to the pending checkers at `t`."""
        resolved = self._changed(before)
        if t is not None:
            announced: set[int] = set()
            while True:
                fresh = [r for r in resolved if id(r) not in announced and r in self.results]
                if not fresh:
                    break
                announced.update(id(r) for r in fresh)
                for checker in self.checkers:
                    if not checker.done:
                        for result in fresh:
                            checker.on_verdict(result, t)
                resolved = self._changed(before)
        for result in resolved:
            if (
                result in self.results
                and result.status == FAILED
                and self._abort is None
                and self._on_fail(result.name) == "abort"
            ):
                self._abort = result
        return resolved

    def _on_fail(self, name: str) -> str:
        checker = next(c for c in self.checkers if c.result.name == name)
        return str(getattr(checker.planned.assertion, "on_fail", "continue"))

    def _statuses(self) -> list[str]:
        return [c.result.status for c in self.everything]

    def feed(self, sample: Sample) -> list[Result]:
        """One observation; returns the assertions it resolved."""
        before = self._statuses()
        self.samples += 1
        if sample.sim is not None and (self.last_sim is None or sample.sim >= self.last_sim):
            self.last_sim = sample.sim
            if (
                self.first_sim is None
                and self.start_wall is not None
                and sample.wall >= self.start_wall
            ):
                self.first_sim = sample.sim
        t = self.time_of(sample)
        for checker in self.everything:
            checker.on_sample(sample, t)  # a resolved checker keeps what it tracks for evidence
        return self._collect(before, t if not math.isinf(t) else None)

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
        before = self._statuses()
        t = self.now(wall)
        if math.isinf(t):
            return []
        for checker in self.everything:
            if not checker.done:
                checker.on_tick(t)
        return self._collect(before, t)

    def action(
        self, label: str, event: str, wall: float, status: str | None = None
    ) -> list[Result]:
        """A timeline action `started` or `finished` with `status`."""
        before = self._statuses()
        t = self.now(wall)
        for checker in self.everything:
            if not checker.done:
                checker.on_action(label, event, t, status)
        return self._collect(before, t if not math.isinf(t) else None)

    def finish(self, wall: float) -> list[Result]:
        """The timeline completed: settle every pending assertion and every collector.

        A checker that waits on other assertions' verdicts (:meth:`Checker.depends_on`) is
        settled after them.
        """
        before = self._statuses()
        t = self.now(wall) if self.start_wall is not None else 0.0
        if math.isinf(t):
            t = 0.0
        for checker in self.everything:
            if not checker.done:
                checker.on_tick(t)
        remaining = list(self.checkers)
        while remaining:
            pending = {c.result.name for c in remaining if not c.done}
            ready = [c for c in remaining if not (set(c.depends_on()) & pending)]
            if not ready:
                ready = remaining  # a cycle of dependencies
            for checker in ready:
                checker.on_finish(t)  # a resolved checker may add evidence of the end
            remaining = [c for c in remaining if c not in ready]
            self._collect(before, t)
        for collector in self.collectors:
            collector.on_finish(t)
        return self._collect(before, t)

    def skip_pending(self, reason: str) -> list[Result]:
        """The run ended before the timeline completed: the pending assertions are skipped."""
        before = self._statuses()
        for checker in self.everything:
            if not checker.done:
                checker.resolve(SKIPPED, reason, None)
        return self._collect(before)

    def sim_elapsed(self) -> float | None:
        """Simulation seconds covered by the stream since the timeline started."""
        if self.first_sim is None or self.last_sim is None:
            return None
        return round(self.last_sim - self.first_sim, 3)


# ------------------------------------------------------------------------------- observation
def watches_of(planned: Sequence[PlannedAssertion], types: Registry | None = None) -> list[Watch]:
    """Every observation the planned assertions ask for, in order, without repeats."""
    known = types or registry()
    watches: list[Watch] = []
    for item in planned:
        for watch in known[item.type].assertion_type.watches(item):
            if watch not in watches:
                watches.append(watch)
    return watches


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
    watches: Sequence[Watch] = (),
    types: Registry | None = None,
) -> dict[str, Any]:
    """What the observer must watch and record (`observe.json`).

    From the planned assertions, the timeline's `when` conditions (task P2-T03: their topic
    fields are watched too) and any other `watches` (the KPI collectors'). `run_dir` is the
    run directory as the recorder container sees it.
    """
    rates: list[dict[str, Any]] = []
    values: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    latencies: list[dict[str, Any]] = []
    lifecycles: list[dict[str, Any]] = []
    tracked = list(models)
    for condition in conditions:
        when = condition.when
        if condition.topic is not None:
            entry = {"topic": condition.topic, "field": when.field, "type": when.message_type}
            if entry not in values:
                values.append(entry)
    lists = {
        "rate": rates,
        "value": values,
        "contact": contacts,
        "latency": latencies,
        "lifecycle": lifecycles,
    }
    for watch in [*watches_of(planned, types), *watches]:
        if watch.kind == "model":
            if watch.data["model"] not in tracked:
                tracked.append(str(watch.data["model"]))
            continue
        entry = dict(watch.data)
        if entry not in lists[watch.kind]:
            lists[watch.kind].append(entry)
    recorded = list(dict.fromkeys([*topics, *SIDE_CHANNELS.values()])) if topics else []
    return {
        "world": world,
        "models": tracked,
        "pose_hz": pose_hz,
        "contacts": contacts,
        "rates": rates,
        "values": values,
        "latencies": latencies,
        "lifecycles": lifecycles,
        "record": {
            "enabled": record,
            "topics": recorded,
            "output": f"{run_dir}/{RECORDING_DIR}",
        },
        "side_channels": dict(SIDE_CHANNELS),
        "events_file": f"{run_dir}/engine.log",
        "assertions_file": f"{run_dir}/{ASSERTIONS_FILE}",
        "network_file": f"{run_dir}/{NETWORK_FILE}",
        "resources_file": f"{run_dir}/{RESOURCES_FILE}",
        "kpis_file": f"{run_dir}/{KPIS_FILE}",
        "samples_file": f"{run_dir}/{SAMPLES_FILE}",
    }


def compile_or_refuse(text: str, names: Sequence[str] = ("value",)) -> Condition:
    """A condition compiled, or a :class:`ValueError` for the schema's validators."""
    try:
        return compile_condition(text, names)
    except ExpressionError as error:
        raise ValueError(f"condition: {error}") from error
