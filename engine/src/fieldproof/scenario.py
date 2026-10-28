"""The scenario schema v0 (task P1-T05, ADR-0005).

A scenario is a YAML document, ``apiVersion: fieldproof.dev/v0alpha1``, ``kind: Scenario``, that
says what a run is made of: the **world** the simulator loads, the **vehicles** (a manifest, a
spawn pose, a namespace and frame prefix, a budget, what they wait for; task P2-T01 made them a
fleet), the **network** between them and the ground (radio links with named profiles, task
P2-T02), the **time** settings of ADR-0006, a **timeline** of actions, the **assertions** that
turn the run into a test, and the **outputs** of the run. The Pydantic models
here are the schema: unknown keys are errors in every section, :func:`json_schema` exports the
JSON Schema for editors and the ``check-jsonschema`` hook, :func:`load_scenario` reads and
validates a file. What the engine does with a scenario is :mod:`fieldproof.runner`.

The timeline's entries are the registered events of :mod:`fieldproof.events` (task P2-T03):
each event brings its own entry model, the union of them is the ``timeline[]`` item, told
apart by ``action``, and every entry carries the triggers of the timeline DSL
(:mod:`fieldproof.dsl`: ``at``, ``after``, ``when``, ``every``). The assertions are the
registered assertion types of :mod:`fieldproof.assertions` (task P2-T05: the five primitives
of P1-T06 and the library of v1, plus the installed plugins), each bringing its spec model,
the union of them the ``assertions[]`` item, told apart by ``type``; the ``kpis[]`` are the
KPI collectors of :mod:`fieldproof.assertions.kpi`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import (
    Field,
    PositiveFloat,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from fieldproof.assertions import KpiBase, assertion_union
from fieldproof.assertions import registry as assertion_registry
from fieldproof.assertions.kpi import KPI_TYPES, Kpi
from fieldproof.assertions.primitives import (
    NoCollision,
    ReachPose,
    TopicRate,
    TopicValue,
    WithinTime,
)
from fieldproof.dsl import (
    DURATION_SCHEMA,
    AssertionBase,
    Duration,
    Placement,
    ScenarioError,
    Strict,
    TimelineBase,
    When,
    entry_label,
    parse_duration,
)
from fieldproof.events import entry_union
from fieldproof.events.link import LINK_ACTIONS, LinkDrop, LinkRestore, LinkSet
from fieldproof.events.navigation import NavigateToPose
from fieldproof.manifest import Name, Resources, format_errors
from fieldproof.network import DROP, PRESETS, LinkProfile
from fieldproof.replay import GroundTruth, ReplaySource, ReplayTf, ReplayVehicle
from fieldproof.world import VIDEO_RECORDER_PLUGIN, Pose, WorldInfo, place, read_world

__all__ = [
    "API_VERSION",
    "ASSERTION_TYPES",
    "DURATION_SCHEMA",
    "KIND",
    "KPI_TYPES",
    "LINK_ACTIONS",
    "SCHEMA_ID",
    "VIDEO_RECORDER_PLUGIN",
    "Assertion",
    "AssertionBase",
    "Duration",
    "GroundTruth",
    "Kpi",
    "KpiBase",
    "LinkDrop",
    "LinkRestore",
    "LinkSet",
    "Metadata",
    "NavigateToPose",
    "Network",
    "NoCollision",
    "Outputs",
    "Placement",
    "Pose",
    "ReachPose",
    "ReplaySource",
    "ReplayTf",
    "ReplayVehicle",
    "Scenario",
    "ScenarioError",
    "Strict",
    "Time",
    "TimelineBase",
    "TimelineEntry",
    "TopicRate",
    "TopicValue",
    "Vehicle",
    "VideoOutput",
    "When",
    "WithinTime",
    "World",
    "WorldInfo",
    "entry_label",
    "json_schema",
    "load_scenario",
    "parse_duration",
    "place",
    "read_world",
    "validate_data",
]

API_VERSION = "fieldproof.dev/v0alpha1"
KIND = "Scenario"
SCHEMA_ID = "https://dimitrios-kafetzis.github.io/Fieldproof/schemas/scenario.schema.json"
# The assertion primitives of task P1-T06.
ASSERTION_TYPES = tuple(assertion_registry().names)
"""The registered assertion types (the built-ins and the installed plugins)."""


class Metadata(Strict):
    """What the scenario is."""

    name: Name = Field(description="Scenario name: lower case, digits and underscores.")
    description: str = Field(default="", description="One line about the scenario.")
    tags: list[str] = Field(default_factory=list, description="Free labels (`smoke`, `nav`).")


class World(Strict):
    """The world the simulator loads."""

    file: str = Field(
        description="World file in the workspace's worlds directory (`depot.sdf`); its named "
        "frames are the poses vehicles spawn at and goals refer to."
    )


class Vehicle(Strict):
    """One vehicle of the scenario.

    In a fleet every vehicle has its own `namespace`: its Gazebo topics, its ROS topics (the TF
    topics included, Nav2's multi-robot convention) and its nodes live under it, so nothing of
    one vehicle reaches another. `frame_prefix` is for a stack that keeps one shared `/tf` tree
    instead and tells its frames apart by name (REP 105); it is passed to the spawn, the bridge
    and the vehicle's launch (`runtime.entrypoint.launch.frame_prefix_arg`).
    """

    name: Name = Field(description="Vehicle name: the model name in Gazebo and the container.")
    manifest: str = Field(description="Vehicle manifest (robot.yaml), relative to the scenario.")
    namespace: str = Field(
        default="",
        pattern=r"^([a-z][a-z0-9_]*)?$",
        description="ROS and Gazebo namespace of the vehicle (empty: the root).",
    )
    frame_prefix: str = Field(
        default="",
        pattern=r"^([a-z][a-z0-9_]*[/_]?)?$",
        description="Prefix of every frame of the vehicle (`ugv1/`, `ugv1_`; empty: none).",
    )
    spawn: Placement | None = Field(
        default=None,
        description="Where the vehicle spawns, in the world (a frame of the world file, or "
        "coordinates); its map frame starts there. Required with the simulator; optional in a "
        "replay (where it says where the vehicle was in the ground truth's frame when the "
        "log started, for goals in the world and `align: spawn`).",
    )
    image: str | None = Field(default=None, description="Image to run instead of the manifest's.")
    env: dict[str, str] = Field(
        default_factory=dict, description="Extra environment of the vehicle container."
    )
    resources: Resources | None = Field(
        default=None,
        description="Budget of the vehicle container; fields set here replace the manifest's "
        "`runtime.resources` (`cpus`, `cpuset`, `memory`, `pids`).",
    )
    depends_on: list[Name] = Field(
        default_factory=list,
        description="Vehicles that must be ready before this one's container starts "
        "(start order within the fleet; the others start in parallel).",
    )


class Network(Strict):
    """Radio links between the vehicles and the ground (task P2-T02).

    With this section every vehicle talks to the ground station and to the other vehicles over
    its own radio link, shaped by a profile (latency, jitter, loss, reordering, rate), while its
    bridge and the run's recorder reach it unimpaired; the timeline changes a link with
    `link.set`, `link.drop` and `link.restore`. Without it the vehicles share the run's network
    as they always did.
    """

    profile: str = Field(
        default="wifi_good",
        description="Profile of every vehicle's link: a preset (`wifi_good`, `lte_rural`, "
        "`rf_900mhz_5km`) or a name under `profiles`.",
    )
    links: dict[Name, str] = Field(
        default_factory=dict,
        description="Profile of a vehicle's link instead of `profile` (vehicle name to profile).",
    )
    profiles: dict[Name, LinkProfile] = Field(
        default_factory=dict, description="Profiles of this scenario, by name."
    )
    verify: Literal["off", "warn", "fail"] = Field(
        default="warn",
        description="What a link whose measured latency is not within 10 % of its profile (plus "
        "the host's own round trip) does: "
        "nothing, a warning, or fails the run.",
    )

    @model_validator(mode="after")
    def _known_profiles(self) -> Network:
        for name in self.profiles:
            if name in PRESETS or name == DROP:
                raise ValueError(f"profiles: {name!r} is a preset; give the profile another name")
        known = self.known()
        for where, name in [("profile", self.profile)] + [
            (f"links.{vehicle}", value) for vehicle, value in self.links.items()
        ]:
            if name not in known:
                raise ValueError(
                    f"{where}: unknown profile {name!r} (known: {', '.join(sorted(known))})"
                )
        return self

    def known(self) -> dict[str, LinkProfile]:
        """Every profile the scenario can name: the presets and its own."""
        return {**PRESETS, **self.profiles}

    def link_profile(self, vehicle: str) -> str:
        """The name of the profile a vehicle's link is configured with."""
        return self.links.get(vehicle, self.profile)


TimeMode = Literal["sim", "realtime", "lockstep"]
REALTIME_RTF = 1.0
"""The real-time factor the real-time-locked mode pins the simulator to."""
LOCKSTEP_RTF = 1.0
"""The default pace of a lockstep step: no faster than real time, so a vehicle's stack gets at
least the wall-clock time its work would have in the field."""


SEED_MAX = 2**31 - 1
"""The largest seed: `gz sim --seed` goes through Gazebo's Ruby launcher, which converts it to a
C `int` (a larger value is refused with "integer too big to convert")."""


class Time(Strict):
    """The clock of the run (ADR-0006, task P2-T04; docs/reference/time.md).

    `sim`: the simulator free-runs at the target real-time factor and the timeline is scheduled
    on the wall clock. `realtime`: the real-time factor is pinned to 1.0 and the drift between
    simulation time and the wall clock is monitored over the timeline, warned about and, with
    `on_drift: fail`, fails the run past `max_drift_s`. `lockstep`: the simulator advances in
    steps of `step_s` only once every vehicle has acknowledged the previous one (a clock agent
    in each vehicle's network namespace), the engine holds it at every timeline due time and
    while an event is being started, and the timeline and the assertions run in simulation time.
    """

    mode: TimeMode = Field(
        default="sim",
        description="`sim`: simulated time on /clock at the target real-time factor; "
        "`realtime`: real-time-locked (RTF 1.0, drift monitored); `lockstep`: stepped, the "
        "simulator waiting for every vehicle's acknowledgement.",
    )
    rtf: PositiveFloat | None = Field(
        default=None,
        description="Target real-time factor (absent: the world's own setting). In `realtime` "
        "mode it is 1.0; in `lockstep` mode it is the pace cap of a step (1.0 by default).",
    )
    tolerance: PositiveFloat = Field(
        default=0.1,
        description="Allowed deviation of the measured real-time factor from the target over a "
        "metrics window (counted in the record; a `clock.drift` warning in realtime mode).",
    )
    max_drift_s: PositiveFloat = Field(
        default=1.0,
        description="`realtime`: the drift between simulation time and the wall clock tolerated "
        "over the timeline (simulation seconds minus wall-clock seconds).",
    )
    on_drift: Literal["fail", "warn"] = Field(
        default="fail",
        description="`realtime`: what a drift past `max_drift_s` does: fail the run or warn.",
    )
    step_s: PositiveFloat = Field(
        default=0.1,
        description="`lockstep`: simulation seconds per step (a multiple of the world's "
        "physics step).",
    )
    ack_timeout_s: PositiveFloat = Field(
        default=30.0,
        description="`lockstep`: wall-clock seconds a vehicle may take to acknowledge a step "
        "before the run fails.",
    )
    timeout_s: Duration = Field(
        default=600.0, description="Wall-clock budget of the run once every vehicle is ready."
    )

    @model_validator(mode="after")
    def _check_mode(self) -> Time:
        if self.mode == "realtime" and self.rtf is not None and self.rtf != REALTIME_RTF:
            raise ValueError(
                f"rtf: the real-time-locked mode pins the real-time factor to {REALTIME_RTF:g} "
                "(leave rtf out or set it to 1.0)"
            )
        return self

    @property
    def target_rtf(self) -> float | None:
        """The real-time factor the run asks the simulator for (None: the world's own)."""
        if self.mode == "realtime":
            return REALTIME_RTF
        if self.mode == "lockstep":
            return self.rtf if self.rtf is not None else LOCKSTEP_RTF
        return self.rtf

    @property
    def sim_base(self) -> bool:
        """Whether the timeline and the assertions count time in simulation seconds."""
        return self.mode == "lockstep"


# The timeline entry: every registered event's entry model, told apart by `action` (built when
# the module loads, from the registry; the type checker sees the base every entry derives from).
if TYPE_CHECKING:
    TimelineEntry = TimelineBase
else:
    TimelineEntry = entry_union()


# --------------------------------------------------------------------------------- assertions
# The assertion: every registered type's spec model, told apart by `type` (built when the
# module loads, from the registry; the type checker sees the base every spec derives from).
if TYPE_CHECKING:
    Assertion = AssertionBase
else:
    Assertion = assertion_union()


class VideoOutput(Strict):
    """A video of the run from a camera of the world (issue #46).

    The camera is a `<sensor type="camera">` of the world file with Gazebo's
    `CameraVideoRecorder` system attached; the runner starts the recording when the timeline
    starts, stops it when the timeline ends and copies the file into the run directory.
    """

    camera: str = Field(
        default="spectator",
        min_length=1,
        description="Name of the world's camera sensor (its recorder service is "
        "`/<camera>/record_video`).",
    )
    format: Literal["mp4"] = Field(default="mp4", description="Container format of the file.")


class Outputs(Strict):
    """What the run writes."""

    directory: str | None = Field(
        default=None, description="Run directory root (default: the workspace's `runs`)."
    )
    record: bool = Field(
        default=False,
        description="Record the topics to MCAP with rosbag2 (`recording/` in the run directory).",
    )
    topics: list[str] = Field(
        default_factory=list,
        description="Topics to record (all if empty; the `/fieldproof/*` side channels always).",
    )
    logs: bool = Field(default=True, description="Keep the containers' logs in the run directory.")
    video: VideoOutput | bool | None = Field(
        default=None,
        description="Record a video of the run from a camera of the world (`video/<camera>.mp4` "
        "in the run directory); `true` uses the world's `spectator` camera.",
    )

    @field_validator("video", mode="after")
    @classmethod
    def _video_from_bool(cls, value: VideoOutput | bool | None) -> VideoOutput | None:
        if value is True:
            return VideoOutput()
        if value is False:
            return None
        return value


class Scenario(Strict):
    """A scenario file, `apiVersion: fieldproof.dev/v0alpha1`, `kind: Scenario`."""

    api_version: Literal["fieldproof.dev/v0alpha1"] = Field(alias="apiVersion")
    kind: Literal["Scenario"]
    metadata: Metadata
    world: World | None = Field(
        default=None,
        description="The world the simulator loads (required with the simulator; in a replay, "
        "optional: its named frames then serve the placements, nothing is simulated).",
    )
    source: ReplaySource | None = Field(
        default=None,
        description="Where the run's inputs come from: absent, the simulator; `kind: replay`, "
        "a field log played into the vehicles (replay.md).",
    )
    vehicles: list[Vehicle] = Field(min_length=1)
    network: Network | None = Field(
        default=None,
        description="Radio links between the vehicles and the ground (absent: one shared "
        "network, no shaping).",
    )
    time: Time = Time()
    seed: int | None = Field(
        default=None,
        ge=0,
        le=SEED_MAX,
        description="The seed of the run's random numbers (the simulator's noise, the events); "
        "absent: Gazebo's default seed, 0, so unseeded runs are alike; recorded either way.",
    )
    timeline: list[TimelineEntry] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    kpis: list[Kpi] = Field(
        default_factory=list,
        description="KPI collectors: what to measure without judging (assertions.md).",
    )
    outputs: Outputs = Outputs()

    _path: Path | None = PrivateAttr(default=None)
    _digest: str = PrivateAttr(default="")
    _text: str = PrivateAttr(default="")
    _overrides: dict[str, Any] = PrivateAttr(default_factory=dict)

    @property
    def path(self) -> Path | None:
        """Where the scenario was loaded from."""
        return self._path

    @property
    def text(self) -> str:
        """The document as validated: the file's text, or the overridden document as YAML."""
        return self._text

    @property
    def overrides(self) -> dict[str, Any]:
        """The overrides applied to the file (`--set`, a matrix case), by path."""
        return self._overrides

    @property
    def directory(self) -> Path:
        """The scenario's directory, which its relative paths resolve against."""
        if self._path is None:
            raise ScenarioError("the scenario was not loaded from a file")
        return self._path.parent

    @property
    def name(self) -> str:
        """`metadata.name`."""
        return self.metadata.name

    @property
    def digest(self) -> str:
        """Short SHA-256 of the file's text (the first part of a run id)."""
        return self._digest

    def vehicle(self, name: str) -> Vehicle:
        """The vehicle called `name`."""
        return next(v for v in self.vehicles if v.name == name)

    def start_order(self) -> list[Vehicle]:
        """The vehicles, every dependency before its dependents, the scenario's order otherwise."""
        by_name = {v.name: v for v in self.vehicles}
        ordered: list[Vehicle] = []
        visiting: set[str] = set()

        def visit(vehicle: Vehicle, chain: tuple[str, ...]) -> None:
            if vehicle.name in visiting:
                return
            if vehicle.name in chain:
                cycle = " -> ".join((*chain[chain.index(vehicle.name) :], vehicle.name))
                raise ValueError(f"vehicles: depends_on forms a cycle: {cycle}")
            for name in vehicle.depends_on:
                visit(by_name[name], (*chain, vehicle.name))
            visiting.add(vehicle.name)
            ordered.append(vehicle)

        for vehicle in self.vehicles:
            visit(vehicle, ())
        return ordered

    def _check_timeline(self, names: list[str]) -> None:
        """The timeline's vehicles exist, `after` names entries, link events have links."""
        if self.network is not None:
            for linked in self.network.links:
                if linked not in names:
                    raise ValueError(f"network.links: {linked!r} is not a vehicle")
        labels = [entry_label(e) for e in self.timeline]
        if len(set(labels)) != len(labels):
            seen = sorted({label for label in labels if labels.count(label) > 1})
            raise ValueError(f"timeline: names must be unique ({', '.join(seen)})")
        by_label = dict(zip(labels, self.timeline, strict=True))
        for index, entry in enumerate(self.timeline):
            where = f"timeline[{index}]"
            if entry.vehicle is not None and entry.vehicle not in names:
                raise ValueError(f"{where}.vehicle: {entry.vehicle!r} is not a vehicle")
            self._check_when(entry, where, names)
            self._check_after(entry, where, labels, by_label)
            self._check_link_entry(entry, where)
        self._check_after_cycles(by_label)

    @staticmethod
    def _check_when(entry: TimelineBase, where: str, names: list[str]) -> None:
        if entry.when is None:
            return
        if entry.when.vehicle is not None and entry.when.vehicle not in names:
            raise ValueError(f"{where}.when.vehicle: {entry.when.vehicle!r} is not a vehicle")
        if entry.when.needs_vehicle() and (entry.when.vehicle or entry.vehicle) is None:
            raise ValueError(
                f"{where}.when: {entry.when.summary()} needs a vehicle (the entry's or "
                "when.vehicle)"
            )

    @staticmethod
    def _check_after(
        entry: TimelineBase, where: str, labels: list[str], by_label: dict[str, TimelineBase]
    ) -> None:
        for name in entry.after_names:
            if name not in by_label:
                known = ", ".join(labels) or "none"
                raise ValueError(f"{where}.after: {name!r} is not a timeline entry ({known})")
            if by_label[name] is entry:
                raise ValueError(f"{where}.after: an entry cannot follow itself")
            if by_label[name].repeated:
                raise ValueError(f"{where}.after: {name!r} is a repeated entry (every)")

    def _check_link_entry(self, entry: TimelineBase, where: str) -> None:
        if entry.action_name in LINK_ACTIONS and self.network is None:
            raise ValueError(f"{where}: {entry.action_name} needs a network section (radio links)")
        if isinstance(entry, LinkSet) and self.network is not None:
            profiles = self.network.known()
            if entry.profile not in profiles:
                raise ValueError(
                    f"{where}.profile: unknown profile {entry.profile!r} "
                    f"(known: {', '.join(sorted(profiles))})"
                )

    @staticmethod
    def _check_after_cycles(by_label: dict[str, TimelineBase]) -> None:
        def visit(label: str, chain: list[str]) -> None:
            for name in by_label[label].after_names:
                if name in chain:
                    cycle = " -> ".join((*chain[chain.index(name) :], name))
                    raise ValueError(f"timeline: after forms a cycle: {cycle}")
                visit(name, [*chain, name])

        for label in by_label:
            visit(label, [label])

    @property
    def replay(self) -> ReplaySource | None:
        """The replay source, when the run replays a field log (task P2-T07)."""
        return self.source

    def _check_source(self, names: list[str]) -> None:
        """What a replay run can and cannot combine with (docs/reference/replay.md)."""
        source = self.source
        if source is None:
            if self.world is None:
                raise ValueError("world: required (the simulator loads it) unless source is replay")
            for vehicle in self.vehicles:
                if vehicle.spawn is None:
                    raise ValueError(
                        f"vehicles[{vehicle.name}].spawn: required (where the vehicle spawns in "
                        "the world) unless source is replay"
                    )
            return
        for name in source.vehicles:
            if name not in names:
                raise ValueError(f"source.vehicles: {name!r} is not a vehicle of the scenario")
        if self.network is not None:
            raise ValueError(
                "network: radio links shape the traffic between the bridges and the vehicles, "
                "which a replay run has not (source: replay); leave the section out"
            )
        if self.time.mode == "lockstep":
            raise ValueError(
                "time.mode: lockstep steps the simulator; a replay run (source: replay) plays "
                "a log at source.rate instead (sim or realtime)"
            )
        if self.time.rtf is not None:
            raise ValueError("time.rtf: a replay run is paced by source.rate; leave rtf out")
        if self.time.mode == "realtime" and source.rate != REALTIME_RTF:
            raise ValueError(
                f"source.rate: the real-time-locked mode plays the log at {REALTIME_RTF:g}"
            )
        if self.outputs.video is not None:
            raise ValueError(
                "outputs.video: a video is recorded from a camera of the simulator's world, "
                "which a replay run has not"
            )

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        names = [v.name for v in self.vehicles]
        if len(set(names)) != len(names):
            raise ValueError("vehicles: names must be unique")
        self._check_source(names)
        namespaces = [v.namespace for v in self.vehicles]
        if len(set(namespaces)) != len(namespaces):
            raise ValueError(
                "vehicles: namespaces must be unique (several vehicles at the root collide)"
            )
        for dependent in self.vehicles:
            for name in dependent.depends_on:
                if name == dependent.name:
                    raise ValueError(f"vehicles[{dependent.name}].depends_on: depends on itself")
                if name not in names:
                    raise ValueError(
                        f"vehicles[{dependent.name}].depends_on: {name!r} is not a vehicle"
                    )
        self.start_order()  # raises on a cycle
        self._check_timeline(names)
        actions = [entry_label(e) for e in self.timeline]
        for section, items in (("assertions", self.assertions), ("kpis", self.kpis)):
            labels = [a.name for a in items]
            if len(set(labels)) != len(labels):
                raise ValueError(f"{section}: names must be unique")
            for index, item in enumerate(items):
                self._check_references(f"{section}[{index}] ({item.name})", item, names, actions)
        return self

    @staticmethod
    def _check_references(
        where: str, item: AssertionBase | KpiBase, names: list[str], actions: list[str]
    ) -> None:
        """The vehicles and timeline entries an assertion or a KPI collector names exist."""
        for vehicle in item.vehicle_names():
            if vehicle not in names:
                raise ValueError(f"{where}.vehicle: {vehicle!r} is not a vehicle")
        for action in item.action_names():
            if action not in actions:
                known = ", ".join(actions) or "none"
                raise ValueError(f"{where}.action: {action!r} is not a timeline entry ({known})")


# ------------------------------------------------------------------------------------ loading
def validate_data(data: Any, path: Path | None = None, text: str = "") -> Scenario:
    """Validate a scenario document."""
    prefix = f"{path}: " if path else ""
    if not isinstance(data, dict):
        raise ScenarioError(f"{prefix}a scenario is a YAML mapping")
    try:
        scenario = Scenario.model_validate(data)
    except ValidationError as error:
        raise ScenarioError(prefix + format_errors(error)) from error
    scenario._path = path
    scenario._digest = hashlib.sha256(text.encode()).hexdigest()[:8] if text else ""
    scenario._text = text
    return scenario


def load_scenario(path: Path | str) -> Scenario:
    """Read and validate the scenario at `path`."""
    file = Path(path).resolve()
    try:
        text = file.read_text()
    except OSError as error:
        raise ScenarioError(f"{file}: {error.strerror}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ScenarioError(f"{file}: not valid YAML: {error}") from error
    return validate_data(data, file, text)


def json_schema() -> dict[str, Any]:
    """The JSON Schema of a scenario file."""
    schema = Scenario.model_json_schema(by_alias=True)
    schema["title"] = "Fieldproof scenario v0"
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": SCHEMA_ID, **schema}
