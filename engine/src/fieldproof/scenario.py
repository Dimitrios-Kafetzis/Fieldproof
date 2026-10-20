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

The timeline is a stub in v0: it knows ``navigate_to_pose`` and the link events ``link.set``,
``link.drop`` and ``link.restore`` (the event injection framework is task P2-T03). The
assertions are the five primitives of task P1-T06 (``reach_pose``, ``within_time``,
``topic_rate``, ``no_collision``, ``topic_value``), one model each, told apart by ``type``;
:mod:`fieldproof.assertions` evaluates them.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PositiveFloat,
    PrivateAttr,
    ValidationError,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from fieldproof.expression import ExpressionError, compile_condition
from fieldproof.manifest import Name, Resources, format_errors
from fieldproof.network import DROP, PRESETS, LinkProfile

API_VERSION = "fieldproof.dev/v0alpha1"
KIND = "Scenario"
SCHEMA_ID = "https://dimitrios-kafetzis.github.io/Fieldproof/schemas/scenario.schema.json"
# The assertion primitives of task P1-T06.
ASSERTION_TYPES = ("reach_pose", "within_time", "topic_rate", "no_collision", "topic_value")
DURATION = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s)?\s*$"
)


class ScenarioError(ValueError):
    """The scenario cannot be read, validated or resolved."""


def parse_duration(value: Any) -> float:
    """Seconds from a number or a ``1h2m3.5s`` string (any unit may be left out)."""
    if isinstance(value, bool):
        raise ValueError("a duration is a number of seconds or a string such as '90s' or '1m30s'")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        match = DURATION.match(value)
        if match and value.strip():
            hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
            return hours * 3600.0 + minutes * 60.0 + seconds
    raise ValueError(f"{value!r} is not a duration ('90s', '1m30s', '0.5s' or a number of seconds)")


DURATION_SCHEMA = {
    "anyOf": [
        {"type": "number", "minimum": 0},
        {
            "type": "string",
            "pattern": r"^\s*(\d+(\.\d+)?h)?\s*(\d+(\.\d+)?m)?\s*(\d+(\.\d+)?s)?\s*$",
        },
    ],
    "description": "Seconds, or a duration such as '90s', '1m30s' or '2h'.",
}
Duration = Annotated[
    float, BeforeValidator(parse_duration), Field(ge=0), WithJsonSchema(DURATION_SCHEMA)
]


class Strict(BaseModel):
    """Base of every section: unknown keys are errors, so typos never pass silently."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


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


class Placement(Strict):
    """A pose in the world: a named frame of the world file, or coordinates.

    `frame` and `x`/`y` are exclusive. `z` is optional with coordinates (the model's default
    spawn height, or the ground for a goal); `yaw` is in radians.
    """

    frame: str | None = Field(default=None, description="A `<frame name>` of the world file.")
    x: float | None = None
    y: float | None = None
    z: float | None = None
    yaw: float | None = None

    @model_validator(mode="after")
    def _frame_or_coordinates(self) -> Placement:
        if self.frame is not None:
            if any(v is not None for v in (self.x, self.y, self.z, self.yaw)):
                raise ValueError("give either frame or x, y, z, yaw")
        elif self.x is None or self.y is None:
            raise ValueError("give either frame or at least x and y")
        return self


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
    spawn: Placement
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
        description="What a link whose measured latency is not within 10 % of its profile does: "
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


class Time(Strict):
    """The clock of the run (ADR-0006)."""

    mode: Literal["sim", "realtime"] = Field(
        default="sim",
        description="`sim`: simulated time on /clock at the target real-time factor; "
        "`realtime`: real-time-locked mode (task P2-T04).",
    )
    rtf: PositiveFloat | None = Field(
        default=None, description="Target real-time factor (absent: the world's own setting)."
    )
    tolerance: PositiveFloat = Field(
        default=0.1, description="Allowed deviation of the real-time factor from the target."
    )
    timeout_s: Duration = Field(
        default=600.0, description="Wall-clock budget of the run once every vehicle is ready."
    )


class TimelineBase(Strict):
    """What every timeline entry has: when it starts and its label."""

    at: Duration = Field(
        default=0.0, description="When, after the vehicles are ready (`30s`, `1m`)."
    )
    name: str | None = Field(
        default=None, description="Label in the run record (default: the action)."
    )


class NavigateToPose(TimelineBase):
    """Send a Nav2 `NavigateToPose` goal to a vehicle and wait for its result."""

    vehicle: Name = Field(description="The vehicle the goal goes to.")
    action: Literal["navigate_to_pose"]
    goal: Placement = Field(description="The goal, in the world (frame or coordinates).")
    timeout_s: Duration = Field(default=300.0, description="Give up on the result after this long.")


class LinkSet(TimelineBase):
    """Put another profile on a vehicle's radio link (`network.profiles` or a preset)."""

    vehicle: Name = Field(description="The vehicle whose link changes.")
    action: Literal["link.set"]
    profile: str = Field(description="The profile from now on.")


class LinkDrop(TimelineBase):
    """Drop a vehicle's radio link: nothing gets through either way.

    With `duration_s` the entry restores the link's configured profile after that long and ends
    then; without it the link stays down until a `link.restore` or `link.set`.
    """

    vehicle: Name = Field(description="The vehicle whose link drops.")
    action: Literal["link.drop"]
    duration_s: Duration | None = Field(
        default=None, description="Restore the link after this long (absent: stay down)."
    )


class LinkRestore(TimelineBase):
    """Bring back the profile the scenario configures for a vehicle's link."""

    vehicle: Name = Field(description="The vehicle whose link is restored.")
    action: Literal["link.restore"]


TimelineEntry = Annotated[
    NavigateToPose | LinkSet | LinkDrop | LinkRestore, Field(discriminator="action")
]
LINK_ACTIONS = ("link.set", "link.drop", "link.restore")


def entry_label(entry: TimelineEntry) -> str:
    """The label of a timeline entry in the record (`name`, or `<action>@<at>s`)."""
    return entry.name or f"{entry.action}@{entry.at:g}s"


# --------------------------------------------------------------------------------- assertions
class AssertionBase(Strict):
    """What every assertion has: a unique name and the type that selects its parameters."""

    name: Name = Field(description="Assertion name: lower case, digits and underscores.")


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
        try:
            compile_condition(self.condition)
        except ExpressionError as error:
            raise ValueError(f"condition: {error}") from error
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        return self


Assertion = Annotated[
    ReachPose | WithinTime | TopicRate | NoCollision | TopicValue, Field(discriminator="type")
]


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
    world: World
    vehicles: list[Vehicle] = Field(min_length=1)
    network: Network | None = Field(
        default=None,
        description="Radio links between the vehicles and the ground (absent: one shared "
        "network, no shaping).",
    )
    time: Time = Time()
    timeline: list[TimelineEntry] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    outputs: Outputs = Outputs()

    _path: Path | None = PrivateAttr(default=None)
    _digest: str = PrivateAttr(default="")

    @property
    def path(self) -> Path | None:
        """Where the scenario was loaded from."""
        return self._path

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
        """The timeline's vehicles exist; link events have links and known profiles."""
        if self.network is not None:
            for linked in self.network.links:
                if linked not in names:
                    raise ValueError(f"network.links: {linked!r} is not a vehicle")
        for index, entry in enumerate(self.timeline):
            if entry.vehicle not in names:
                raise ValueError(f"timeline[{index}].vehicle: {entry.vehicle!r} is not a vehicle")
            if entry.action in LINK_ACTIONS and self.network is None:
                raise ValueError(
                    f"timeline[{index}]: {entry.action} needs a network section (radio links)"
                )
            if isinstance(entry, LinkSet) and self.network is not None:
                profiles = self.network.known()
                if entry.profile not in profiles:
                    raise ValueError(
                        f"timeline[{index}].profile: unknown profile {entry.profile!r} "
                        f"(known: {', '.join(sorted(profiles))})"
                    )

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        names = [v.name for v in self.vehicles]
        if len(set(names)) != len(names):
            raise ValueError("vehicles: names must be unique")
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
        labels = [a.name for a in self.assertions]
        if len(set(labels)) != len(labels):
            raise ValueError("assertions: names must be unique")
        actions = [entry_label(e) for e in self.timeline]
        for index, assertion in enumerate(self.assertions):
            where = f"assertions[{index}] ({assertion.name})"
            vehicle = getattr(assertion, "vehicle", None)
            if vehicle is not None and vehicle not in names:
                raise ValueError(f"{where}.vehicle: {vehicle!r} is not a vehicle")
            if isinstance(assertion, WithinTime) and assertion.action not in actions:
                known = ", ".join(actions) or "none"
                raise ValueError(
                    f"{where}.action: {assertion.action!r} is not a timeline entry ({known})"
                )
        return self


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


# ------------------------------------------------------------------------------------- worlds
@dataclass(frozen=True)
class Pose:
    """A pose in the world: position and heading (roll and pitch are not used)."""

    x: float
    y: float
    z: float
    yaw: float

    def relative(self, other: Pose) -> Pose:
        """`other` expressed in this pose's frame (a goal in the map frame of a spawn)."""
        dx, dy = other.x - self.x, other.y - self.y
        cos, sin = math.cos(self.yaw), math.sin(self.yaw)
        return Pose(
            cos * dx + sin * dy,
            -sin * dx + cos * dy,
            other.z - self.z,
            math.remainder(other.yaw - self.yaw, math.tau),
        )


@dataclass(frozen=True)
class WorldInfo:
    """What a world file declares that scenarios refer to."""

    file: Path
    name: str
    frames: dict[str, Pose]
    cameras: dict[str, str] = field(default_factory=dict)
    """Camera sensors with a `CameraVideoRecorder` plugin: sensor name to its service."""


VIDEO_RECORDER_PLUGIN = "gz::sim::systems::CameraVideoRecorder"


def read_world(file: Path) -> WorldInfo:
    """The world's name, its named frames and the cameras a video can be recorded from.

    Frames are `<frame name="..."><pose>x y z r p y</pose>`; cameras are the `<sensor
    type="camera">` elements carrying a `CameraVideoRecorder` plugin (`outputs.video`).
    """
    try:
        root = ET.parse(file).getroot()  # noqa: S314  (a world file of the workspace)
    except (OSError, ET.ParseError) as error:
        raise ScenarioError(f"{file}: cannot read the world file: {error}") from error
    world = root.find("world")
    if world is None or not world.get("name"):
        raise ScenarioError(f"{file}: no <world name=...> element")
    frames: dict[str, Pose] = {}
    for frame in world.findall("frame"):
        name = frame.get("name")
        values = (frame.findtext("pose") or "0 0 0 0 0 0").split()
        if name and len(values) == 6:
            x, y, z, _roll, _pitch, yaw = (float(v) for v in values)
            frames[name] = Pose(x, y, z, yaw)
    cameras: dict[str, str] = {}
    for sensor in world.iter("sensor"):
        if sensor.get("type") != "camera" or not sensor.get("name"):
            continue
        for plugin in sensor.findall("plugin"):
            if plugin.get("name") == VIDEO_RECORDER_PLUGIN and plugin.findtext("service"):
                cameras[str(sensor.get("name"))] = str(plugin.findtext("service")).strip()
    return WorldInfo(file, str(world.get("name")), frames, cameras)


def place(placement: Placement, world: WorldInfo, what: str) -> Pose:
    """The world pose of a placement (`z` and `yaw` default to 0 with coordinates)."""
    if placement.frame is not None:
        if placement.frame not in world.frames:
            known = ", ".join(sorted(world.frames)) or "none"
            raise ScenarioError(
                f"{what}: world {world.name} has no frame {placement.frame!r} ({known})"
            )
        return world.frames[placement.frame]
    if placement.x is None or placement.y is None:  # the model validator rules this out
        raise ScenarioError(f"{what}: a placement needs a frame or x and y")
    return Pose(placement.x, placement.y, placement.z or 0.0, placement.yaw or 0.0)
