"""The event plugin API and the registry of events (task P2-T03, ADR-0005).

An **event** is what a timeline entry does: send a goal, drop a link, pause a container, set a
parameter, spawn an obstacle. Each one is a class deriving from :class:`Event` that names its
``action`` (the discriminator of the timeline, ``container.pause``), its ``Entry`` (a Pydantic
model deriving from :class:`~fieldproof.dsl.TimelineBase` with the entry's parameters, which
is what the scenario schema validates and exports as JSON Schema), a :meth:`Event.plan` hook
that resolves the entry against the plan before any container starts (a goal into the
vehicle's map frame; a :class:`~fieldproof.dsl.ScenarioError` refuses it) and an
:meth:`Event.apply` hook that performs it during the run through an :class:`EventContext`
(the containers, commands inside them, the radio links, the log, the clock) and reports in
its :class:`ActionRecord`.

Events are found through Python entry points of the group ``fieldproof.events``: the engine
registers its built-ins (:mod:`fieldproof.events.navigation`, :mod:`fieldproof.events.link`,
:mod:`fieldproof.events.container`, :mod:`fieldproof.events.param`,
:mod:`fieldproof.events.world`, :mod:`fieldproof.events.faults`) and any distribution
installed next to the engine adds its own (``examples/plugins`` shows one). The built-ins are
also loaded directly, so an engine checkout works without its metadata. :func:`registry` is
what the scenario schema builds its timeline union from and what the runner dispatches with;
``fieldproof events`` lists it. What a plugin cannot do: name an action another event has, or
run arbitrary code at validation time (its ``Entry`` is data, its hooks run in the engine's
process on the engine's terms).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from functools import cache
from importlib.metadata import EntryPoint, entry_points
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    Protocol,
    Union,
    get_args,
    get_origin,
)

from pydantic import Field

from fieldproof.backend import Backend, ExecResult, LineSink
from fieldproof.dsl import ScenarioError, TimelineBase
from fieldproof.world import Pose, WorldInfo

if TYPE_CHECKING:
    from fieldproof_bridge.generator import Bundle

ENTRY_POINT_GROUP = "fieldproof.events"
BUILTIN_MODULES = (
    "fieldproof.events.navigation",
    "fieldproof.events.link",
    "fieldproof.events.container",
    "fieldproof.events.param",
    "fieldproof.events.world",
    "fieldproof.events.faults",
    "fieldproof.events.sim",
)
BUILTIN = "built-in"
# Statuses of an action record.
PENDING = "pending"
WAITING = "waiting"
RUNNING = "running"
OK = "ok"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
SKIPPED = "skipped"
FINAL = (OK, FAILED, TIMEOUT, CANCELLED, SKIPPED)


# ------------------------------------------------------------------------------- the record
@dataclass
class ActionRecord:
    """One occurrence of a timeline entry as it ran (`run.json` `timeline[]`)."""

    name: str
    action: str
    vehicle: str | None
    at_s: float
    status: str = PENDING
    started_at: str | None = None
    duration_s: float | None = None
    detail: str = ""
    trigger: dict[str, Any] = field(default_factory=dict)
    """The entry's trigger (`at_s`, `after`, `when`, `every_s`, ...)."""
    occurrence: int = 1
    """Which occurrence of a repeated entry this is (1 for the first)."""
    t_s: float | None = None
    """Seconds after the timeline started when the occurrence started."""
    sim_s: float | None = None
    """Simulation time when it started, when the stream had one."""
    params: dict[str, Any] = field(default_factory=dict)
    """The entry's own parameters, as data."""

    def to_dict(self) -> dict[str, Any]:
        """The record as data."""
        return asdict(self)


# ------------------------------------------------------------------------------ the contexts
class VehicleHandle(Protocol):
    """A vehicle of the plan, as an event sees it."""

    @property
    def name(self) -> str:
        """The vehicle's name (its Gazebo model)."""

    @property
    def namespace(self) -> str:
        """Its ROS and Gazebo namespace (empty at the root)."""

    @property
    def container_name(self) -> str:
        """The name of its container."""

    @property
    def bridge_name(self) -> str:
        """The name of its bridge container (the fault injectors run there)."""

    @property
    def spawn(self) -> Pose:
        """Where it spawned, in the world."""

    def topic(self, name: str) -> str:
        """An absolute topic, node or action name under the vehicle's namespace."""

    def bundle(self) -> Bundle:
        """The plan of its bridge bundle (:mod:`fieldproof_bridge.generator`).

        The nodes the bridge runs, among them the fault injectors a fault event sets the
        parameters of, and the contract topics they publish.
        """


@dataclass(frozen=True)
class PlanContext:
    """What :meth:`Event.plan` resolves an entry against: the world and the vehicles."""

    world: WorldInfo
    vehicles: Mapping[str, VehicleHandle]
    links: bool = False
    """Whether the scenario has radio links (a `network` section)."""
    where: str = "timeline[]"
    """The entry being planned, for error messages (`timeline[2]`)."""
    time_mode: str = "sim"
    """The scenario's `time.mode` (`sim`, `realtime`, `lockstep`; task P2-T04)."""

    def vehicle(self, name: str | None, what: str = "vehicle") -> VehicleHandle:
        """The vehicle called `name`, or a :class:`ScenarioError` naming `where.what`."""
        if name is None or name not in self.vehicles:
            raise ScenarioError(f"{self.where}.{what}: {name!r} is not a vehicle of the scenario")
        return self.vehicles[name]


class EventContext(Protocol):
    """What :meth:`Event.apply` acts through during a run.

    Everything here is safe to call from the worker thread an event runs on.
    """

    @property
    def backend(self) -> Backend:
        """The container runtime of the run."""

    @property
    def tearing_down(self) -> bool:
        """Whether the run is ending (an event holding for a duration should return)."""

    def vehicle(self, name: str | None) -> VehicleHandle:
        """The vehicle called `name` (a :class:`RuntimeError` if the entry names none)."""

    def exec(
        self, container: str, argv: Sequence[str], on_line: LineSink | None = None
    ) -> ExecResult:
        """Run a command inside a container of the run, streaming its lines to `on_line`."""

    def ros2(
        self,
        vehicle: VehicleHandle,
        argv: Sequence[str],
        *,
        timeout_s: float,
        on_line: LineSink | None = None,
    ) -> ExecResult:
        """`ros2 <argv>` inside the vehicle's container.

        Through the image's entry point (the stack's environment sourced), interrupted after
        `timeout_s` (exit code 124).
        """

    def bridge_ros2(
        self,
        vehicle: VehicleHandle,
        argv: Sequence[str],
        *,
        timeout_s: float,
        on_line: LineSink | None = None,
    ) -> ExecResult:
        """`ros2 <argv>` inside the vehicle's bridge container (the fault injectors' side).

        Through the bridge image's entry point, interrupted after `timeout_s` (exit code 124);
        the bridge runs on whether the vehicle's container is paused, killed or behind a link.
        """

    def gz_service(
        self,
        service: str,
        *,
        request_type: str,
        reply_type: str,
        request: str,
        timeout_s: float = 10.0,
    ) -> ExecResult:
        """`gz service -s <service>` inside the simulator's container (the world events).

        `request` is the message in protobuf text format; the reply is printed the same way
        (`data: "..."` for a `gz.msgs.StringMsg`). The call is interrupted after `timeout_s`.
        """

    def log(self, event: str, message: str = "", *, level: str = "info", **fields: Any) -> None:
        """Record an event in the run's log (relayed to `/fieldproof/events`)."""

    def clock(self) -> float:
        """A monotonic clock, in seconds."""

    def hold(self, seconds: float) -> bool:
        """Wait this long (or until the run ends; then False), in slices."""

    def started(self, record: ActionRecord) -> None:
        """Tell the run the event's effect is in place (a goal accepted, a fault set).

        In lockstep mode (task P2-T04) the simulator is held at the occurrence's start time
        until every occurrence starting then has said so, or returned, or held it for ten
        seconds; an event that does not call it holds the clock until `apply` returns.
        """

    def expect_exit(self, container: str) -> None:
        """Tell the run that this container may exit from now on (a kill)."""

    def mark_paused(self, container: str, paused: bool) -> None:
        """Tell the run that this container is paused (the teardown resumes it first)."""

    def apply_link(self, vehicle: str, profile: str, cause: str) -> None:
        """Put a profile on the vehicle's radio link (`drop` included)."""

    def link_profile(self, vehicle: str) -> str:
        """The name of the profile the scenario configures for the vehicle's link."""


# --------------------------------------------------------------------------------- the event
class Event:
    """One kind of timeline action; subclasses set the class attributes and the hooks."""

    action: ClassVar[str]
    """The `action` of the timeline entries this event performs (`container.pause`)."""
    Entry: ClassVar[type[TimelineBase]]
    """The schema of those entries: `action` as a literal, the parameters as fields."""
    summary: ClassVar[str] = ""
    """One line for `fieldproof events`."""

    def plan(self, entry: Any, context: PlanContext) -> dict[str, Any]:  # noqa: ARG002
        """Resolve the entry against the plan; the dry run shows the result.

        Raise a :class:`~fieldproof.dsl.ScenarioError` to refuse the entry. Values may be
        dataclasses (a :class:`~fieldproof.world.Pose`); the dry run turns them into data.
        """
        return {}

    def apply(
        self, entry: Any, resolved: Mapping[str, Any], context: EventContext, record: ActionRecord
    ) -> None:
        """Perform the entry during the run (on a worker thread).

        Set `record.status` (`ok`, `failed` or `timeout`) and `record.detail`; a record left
        `running` ends `ok`, an exception ends `failed`. `resolved` is what :meth:`plan`
        returned.
        """
        raise NotImplementedError


def resolved_data(resolved: Mapping[str, Any]) -> dict[str, Any]:
    """`plan`'s result as JSON-friendly data (dataclasses as dicts)."""
    return {
        key: asdict(value) if is_dataclass(value) and not isinstance(value, type) else value
        for key, value in resolved.items()
    }


def parameters_of(entry_type: type[TimelineBase]) -> dict[str, str]:
    """The entry type's own fields with their descriptions (`fieldproof events`)."""
    common = {*TimelineBase.model_fields, "action"}
    return {
        name: info.description or ""
        for name, info in entry_type.model_fields.items()
        if name not in common
    }


def check_event(candidate: Any) -> type[Event]:
    """The event class if `candidate` is a well-formed one, or a :class:`TypeError` saying why."""
    if not isinstance(candidate, type) or not issubclass(candidate, Event):
        raise TypeError(f"{candidate!r} is not an Event subclass")
    action = getattr(candidate, "action", None)
    if not isinstance(action, str) or not action:
        raise TypeError(f"{candidate.__name__}: `action` must be a non-empty string")
    entry = getattr(candidate, "Entry", None)
    if not isinstance(entry, type) or not issubclass(entry, TimelineBase):
        raise TypeError(f"{candidate.__name__}: `Entry` must derive from TimelineBase")
    info = entry.model_fields.get("action")
    annotation = info.annotation if info is not None else None
    if get_origin(annotation) is not Literal or get_args(annotation) != (action,):
        raise TypeError(
            f"{candidate.__name__}: {entry.__name__}.action must be Literal[{action!r}]"
        )
    return candidate


# ------------------------------------------------------------------------------ the registry
@dataclass(frozen=True)
class Registered:
    """An event and where it came from."""

    event: Event
    source: str
    """`built-in`, or the distribution (name and version) whose entry point registered it."""

    @property
    def action(self) -> str:
        """The event's action."""
        return self.event.action

    @property
    def entry_type(self) -> type[TimelineBase]:
        """The event's entry schema."""
        return self.event.Entry


@dataclass
class Registry:
    """The events the engine knows, by action."""

    events: dict[str, Registered] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    """Entry points that could not be loaded or were refused, one line each."""

    def __contains__(self, action: str) -> bool:
        return action in self.events

    def __getitem__(self, action: str) -> Registered:
        return self.events[action]

    @property
    def actions(self) -> list[str]:
        """Every action, sorted."""
        return sorted(self.events)

    def entry_types(self) -> list[type[TimelineBase]]:
        """The entry schemas, in registration order."""
        return [r.entry_type for r in self.events.values()]

    def add(self, candidate: Any, source: str) -> None:
        """Register an event class.

        A :class:`TypeError` for a malformed one, a :class:`ValueError` for an action already
        taken by another class.
        """
        cls = check_event(candidate)
        existing = self.events.get(cls.action)
        if existing is not None:
            if type(existing.event) is cls:
                return  # the same class seen twice (a built-in and its entry point)
            raise ValueError(
                f"{source}: action {cls.action!r} is already registered by {existing.source}"
            )
        self.events[cls.action] = Registered(cls(), source)


def load_registry(
    points: Iterable[EntryPoint] | None = None,
    builtin_modules: Sequence[str] = BUILTIN_MODULES,
) -> Registry:
    """The built-ins, then the entry points of the group (`points` overrides discovery).

    A plugin that fails to import or is malformed does not stop the engine: it is listed in
    `problems` and its action stays unknown to the schema.
    """
    registry = Registry()
    for module_name in builtin_modules:
        module = importlib.import_module(module_name)
        for cls in module.EVENTS:
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


def entry_union(events: Registry | None = None) -> Any:
    """The timeline entry type: the registered entries, told apart by `action`."""
    types = (events or registry()).entry_types()
    union = Union[tuple(types)] if len(types) > 1 else types[0]  # noqa: UP007  (dynamic)
    return Annotated[union, Field(discriminator="action")]
