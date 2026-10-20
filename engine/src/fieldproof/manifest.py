"""The vehicle manifest schema v0 (``robot.yaml``), tasks P1-T04 and P2-T01.

A manifest describes one vehicle for the whole stack: the **model** sections (``description``,
``frames``, ``dimensions``, ``drive``, ``sensors``) that task P1-T01 wrote for the reference UGV
and the bridge generator reads (:mod:`fieldproof_bridge.manifest`), and the **runtime** section
that says what runs on it: the container image or how to build it from the vehicle's repository,
the entrypoint launch (and which of its arguments take the vehicle's namespace and frame prefix
in a fleet), the parameter files, the driver-swap map, the readiness conditions, the DDS profile
and the resource budget (ADR-0004; ADR-0007 for the fidelity tier).

A manifest may ``extends`` another one: the vehicle manifest of a stack inherits the model
sections of the reference vehicle it runs on and adds ``runtime``. :func:`load_manifest` resolves
the chain (through the bridge's reader, so both read the same merged document) and validates the
result with :class:`RobotManifest`. :func:`json_schema` exports the schema for editors and the
``check-jsonschema`` hook.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    PrivateAttr,
    ValidationError,
    model_validator,
)

from fieldproof_bridge.manifest import ManifestError, read_manifest_data

API_VERSION = "fieldproof.dev/v0alpha1"
KIND = "Robot"
SCHEMA_ID = "https://dimitrios-kafetzis.github.io/Fieldproof/schemas/robot.schema.json"
# Topics the bridge provides beyond the manifest's interfaces (robot_state_publisher, clock);
# the TF and description topics follow the namespace like the drive's `tf` interface, so a
# driver may name them relative or absolute.
BRIDGE_TOPICS = frozenset(
    {"tf_static", "/tf_static", "robot_description", "/robot_description", "/clock"}
)
# The base image vehicle images build on by default (docs/setup/containers.md).
DEFAULT_BASE_IMAGE = "ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:local"
DEFAULT_ROS_DISTRO = "jazzy"
# ros/rosdistro commit the generated Dockerfile resolves rosdep keys against (2026-10-03, the last
# commit before the ROS apt snapshot of docker/base-ros2/Dockerfile).
DEFAULT_ROSDISTRO_COMMIT = "8e9a99d200fd312f106418b2b497b0cc5146e6a7"

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", min_length=1, max_length=63)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Scalar = bool | int | float | str
MEMORY_PATTERN = r"^[1-9][0-9]*(\.[0-9]+)?[kKmMgG]?[bB]?$"
# `--cpuset-cpus`: a list of cores or ranges (`0-1`, `0,2`, `0-1,4`).
CPUSET_PATTERN = r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$"
# A launch argument name (`namespace`, `frame_prefix`).
LaunchArgName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
# Where the vehicle's DDS profile is mounted in the container, and the variable each RMW reads it
# from (task P2-T01; the environment of the container names the file for the RMW it runs).
DDS_PROFILE_IN_CONTAINER = "/opt/fieldproof/dds/profile.xml"
DDS_PROFILE_ENV = {
    "rmw_fastrtps_cpp": "FASTRTPS_DEFAULT_PROFILES_FILE",
    "rmw_fastrtps_dynamic_cpp": "FASTRTPS_DEFAULT_PROFILES_FILE",
    "rmw_cyclonedds_cpp": "CYCLONEDDS_URI",
    "rmw_connextdds": "NDDS_QOS_PROFILES",
}
DEFAULT_RMW = "rmw_fastrtps_cpp"


class Strict(BaseModel):
    """Base of every section: unknown keys are errors, so typos never pass silently."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------------------- model sections
class Metadata(Strict):
    """Who the vehicle is."""

    name: Name = Field(description="Vehicle name: lower case, digits and underscores.")
    description: str = Field("", description="One line about the vehicle.")
    domain: Literal["ground", "aerial", "surface"] | None = Field(
        None, description="Operating domain."
    )
    licence: str | None = Field(None, description="SPDX licence identifier of the description.")


class Description(Strict):
    """The vehicle description: the xacro and what is generated from it.

    Paths are relative to the manifest that declares them; an inherited description keeps
    pointing at the parent's files.
    """

    xacro: str = Field(description="xacro URDF of the vehicle.")
    urdf: str | None = Field(None, description="URDF generated from the xacro at build time.")
    sdf: str | None = Field(None, description="SDF generated from the URDF at build time.")
    model_uri: str | None = Field(None, description="`model://` URI Gazebo resolves the SDF by.")
    launch: str | None = Field(None, description="Spawn launch file of the model.")
    meshes: str | None = Field(None, description="Mesh directory (with its licence file).")
    xacro_args: dict[str, Scalar] = Field(
        default_factory=dict, description="xacro argument defaults the spawn and bridge use."
    )


class Frames(Strict):
    """REP 105 frames of the vehicle."""

    base: str = Field(description="Base frame (`base_link`).")
    footprint: str | None = Field(None, description="Footprint frame on the ground.")
    odom: str = Field(description="Odometry frame.")


class Dimensions(Strict):
    """Outer dimensions and mass."""

    length_m: PositiveFloat | None = None
    width_m: PositiveFloat | None = None
    height_m: PositiveFloat | None = None
    ground_clearance_m: PositiveFloat | None = None
    mass_kg: PositiveFloat | None = None


class Qos(Strict):
    """QoS profile of a ROS topic (defaults: what the ros_gz bridge publishes with)."""

    reliability: Literal["reliable", "best_effort"] = "reliable"
    durability: Literal["volatile", "transient_local"] = "volatile"
    depth: PositiveInt = 10


class Topic(Strict):
    """One bridged interface: the Gazebo and ROS names and types.

    ROS topics are relative to the vehicle namespace unless they start with a slash.
    """

    gz_topic: str = Field(description="Gazebo topic, relative to the model.")
    gz_type: str = Field(description="Gazebo message type (`gz.msgs.X`).")
    ros_topic: str = Field(description="ROS topic the real driver uses.")
    ros_type: str = Field(description="ROS message type (`pkg/msg/X`).")
    qos: Qos = Field(default_factory=Qos)


class Command(Topic):
    """The velocity command the stack publishes."""


class Odometry(Topic):
    """Wheel odometry."""

    rate_hz: PositiveFloat
    frame_id: str
    child_frame_id: str


class JointStates(Topic):
    """Joint states; `gz_rate` names a faster Gazebo rate the bridge throttles."""

    rate_hz: PositiveFloat | None = None
    gz_rate: Literal["physics"] | PositiveFloat | None = None

    @model_validator(mode="after")
    def _throttle_needs_a_rate(self) -> JointStates:
        if self.gz_rate is not None and self.rate_hz is None:
            raise ValueError("gz_rate needs a rate_hz to throttle to")
        return self


class Tf(Topic):
    """The odometry transform the drive system publishes (`/tf` is global)."""

    rate_hz: PositiveFloat | None = None


class DriveLimits(Strict):
    """Kinematic limits the drive system enforces."""

    max_linear_velocity_mps: PositiveFloat | None = None
    max_angular_velocity_radps: PositiveFloat | None = None
    max_linear_acceleration_mps2: PositiveFloat | None = None
    max_angular_acceleration_radps2: PositiveFloat | None = None


class Drive(Strict):
    """The drive train and its four interfaces."""

    type: str = Field(description="`differential`, `ackermann`, `skid_steer`, ...")
    wheel_radius_m: PositiveFloat | None = None
    wheel_separation_m: PositiveFloat | None = None
    joints: list[str] = Field(default_factory=list, description="Driven joints.")
    limits: DriveLimits | None = None
    command: Command
    odometry: Odometry
    joint_states: JointStates
    tf: Tf

    def interfaces(self) -> list[Topic]:
        """The four interfaces in the bridge's order."""
        return [self.command, self.odometry, self.joint_states, self.tf]


class SensorOutput(Topic):
    """One topic a sensor publishes."""

    ros_frame_id: str = Field(description="Frame the ROS messages carry (relabelled if needed).")
    gz_header_frame_id: bool = Field(
        True, description="False where Gazebo leaves the header frame empty."
    )


class Sensor(Strict):
    """A simulated sensor with its outputs and noise figures."""

    name: Name
    type: str = Field(description="Gazebo sensor type (`gpu_lidar`, `imu`, `rgbd_camera`, ...).")
    frame_id: str
    rate_hz: PositiveFloat
    gz_topic: str | None = None
    optional: bool = Field(False, description="Removable with the xacro argument `<name>:=false`.")
    resolution: tuple[PositiveInt, PositiveInt] | None = None
    horizontal_fov_rad: PositiveFloat | None = None
    noise: dict[str, Annotated[float, Field(ge=0)]] = Field(
        default_factory=dict, description="Standard deviations in SI units, per figure."
    )
    outputs: list[SensorOutput] = Field(min_length=1)


# ----------------------------------------------------------------------------- runtime section
class Repo(Strict):
    """A repository `vcs import` clones into the workspace at a pinned commit."""

    name: str = Field(description="Directory under `src/`.", pattern=r"^[A-Za-z0-9_.-]+$")
    url: str = Field(description="Clone URL.")
    version: Sha = Field(description="Commit SHA (40 hex digits); branches and tags are not pins.")
    type: Literal["git"] = "git"


class Build(Strict):
    """How the image is built from the vehicle's sources (`fieldproof vehicle build`)."""

    base: str = Field(DEFAULT_BASE_IMAGE, description="Base image of both stages.")
    ros_distro: str = Field(DEFAULT_ROS_DISTRO, description="ROS 2 distribution of the base.")
    rosdistro_commit: Sha = Field(
        DEFAULT_ROSDISTRO_COMMIT, description="ros/rosdistro commit rosdep resolves keys against."
    )
    repos: list[Repo] = Field(default_factory=list, description="Repositories at pinned commits.")
    sources: list[str] = Field(
        default_factory=list,
        description="Local source directories, relative to the manifest, copied into `src/`.",
    )
    apt: list[str] = Field(
        default_factory=list, description="Runtime apt packages beyond what rosdep resolves."
    )
    rosdep_skip_keys: list[str] = Field(
        default_factory=list, description="rosdep keys to ignore (`--skip-keys`)."
    )
    colcon_args: list[str] = Field(
        default_factory=list, description="Extra `colcon build` arguments."
    )

    @model_validator(mode="after")
    def _has_sources(self) -> Build:
        if not self.repos and not self.sources:
            raise ValueError("build needs repos or sources")
        for source in self.sources:
            path = Path(source)
            if path.is_absolute() or ".." in path.parts or not source.strip():
                raise ValueError(
                    f"sources: {source!r} must be a relative path inside the manifest's directory"
                )
        names = [r.name for r in self.repos]
        if len(set(names)) != len(names):
            raise ValueError("repos: names must be unique")
        return self


class Launch(Strict):
    """`ros2 launch <package> <file> key:=value ...`.

    `namespace_arg` and `frame_prefix_arg` name the launch arguments that take the vehicle's
    namespace and frame prefix when a scenario gives it one (task P2-T01): the orchestrator
    appends `<namespace_arg>:=<namespace>` to the command line of a vehicle in a fleet, and
    leaves the argument out when the value is empty (`ros2 launch` rejects an empty value, and
    the field default then applies). A stack whose launch takes no such argument runs at the
    root namespace only.
    """

    package: str
    file: str
    args: dict[str, Scalar] = Field(
        default_factory=dict,
        description="Launch arguments the simulation adds (`use_sim_time`, driver swap).",
    )
    namespace_arg: LaunchArgName | None = Field(
        None, description="Launch argument that receives the vehicle's namespace (`namespace`)."
    )
    frame_prefix_arg: LaunchArgName | None = Field(
        None, description="Launch argument that receives the vehicle's frame prefix."
    )


class Entrypoint(Strict):
    """What the container runs: a launch file or a plain command."""

    launch: Launch | None = None
    command: list[str] | None = Field(None, min_length=1)

    @model_validator(mode="after")
    def _one_of(self) -> Entrypoint:
        if (self.launch is None) == (self.command is None):
            raise ValueError("entrypoint needs exactly one of launch or command")
        return self

    def argv(self, *, namespace: str = "", frame_prefix: str = "") -> list[str]:
        """The command line the image runs by default, or with a namespace and frame prefix.

        A non-empty `namespace` or `frame_prefix` is appended as the launch argument the
        manifest names for it (`namespace_arg`, `frame_prefix_arg`); without one it is left to
        the environment (`FIELDPROOF_NAMESPACE`, `FIELDPROOF_FRAME_PREFIX`).
        """
        if self.command is not None:
            return list(self.command)
        if self.launch is None:  # pragma: no cover  (the validator rules it out)
            raise ManifestError("entrypoint has neither launch nor command")
        launch = self.launch
        args = [f"{k}:={launch_argument(v)}" for k, v in launch.args.items()]
        if namespace and launch.namespace_arg:
            args.append(f"{launch.namespace_arg}:={namespace}")
        if frame_prefix and launch.frame_prefix_arg:
            args.append(f"{launch.frame_prefix_arg}:={frame_prefix}")
        return ["ros2", "launch", launch.package, launch.file, *args]

    def takes_namespace(self) -> bool:
        """Whether the launch has an argument for the vehicle's namespace."""
        return self.launch is not None and self.launch.namespace_arg is not None


def launch_argument(value: Scalar) -> str:
    """Render a launch argument value (booleans as `true`/`false`)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Driver(Strict):
    """One hardware driver of the field launch and the bridge interfaces that replace it."""

    name: Name
    package: str = Field(description="Driver package the field launch starts.")
    executable: str | None = Field(None, description="Node or launch file of the driver.")
    provides: list[str] = Field(
        min_length=1,
        description="ROS topics of this manifest the bridge provides instead (`/tf_static`, "
        "`/robot_description` and `/clock` are the bridge's own).",
    )
    note: str = ""


class LifecycleCondition(Strict):
    """A lifecycle node reached a state."""

    lifecycle: str = Field(description="Node name (`/bt_navigator`).")
    state: Literal["unconfigured", "inactive", "active", "finalized"] = "active"


class TopicCondition(Strict):
    """A topic has a publisher."""

    topic: str
    type: str | None = None


class ServiceCondition(Strict):
    """A service is offered."""

    service: str
    type: str | None = None


class ActionCondition(Strict):
    """An action server is offered."""

    action: str
    type: str | None = None


class NodeCondition(Strict):
    """A node is in the graph."""

    node: str


class TfCondition(Strict):
    """A chain of transforms on `/tf` or `/tf_static` links two frames."""

    tf: str = Field(description="Parent (target) frame (`map`).")
    child: str = Field(description="Child (source) frame (`base_link`).")


ReadyCondition = Annotated[
    LifecycleCondition
    | TopicCondition
    | ServiceCondition
    | ActionCondition
    | NodeCondition
    | TfCondition,
    Field(union_mode="left_to_right"),
]


class Ready(Strict):
    """When the stack counts as up (the orchestrator starts the scenario clock after it)."""

    timeout_s: PositiveFloat = Field(120.0, description="Give up after this long.")
    conditions: list[ReadyCondition] = Field(min_length=1)


class Resources(Strict):
    """Container budget, in Docker's units (cgroup CPU quota, cpuset, memory, pids)."""

    cpus: PositiveFloat | None = Field(
        default=None, description="`--cpus`: the CPU quota, in cores."
    )
    cpuset: str | None = Field(
        default=None,
        pattern=CPUSET_PATTERN,
        description="`--cpuset-cpus`: the cores the container may run on (`0-1`, `0,2`).",
    )
    memory: str | None = Field(
        default=None, pattern=MEMORY_PATTERN, description="`--memory` (`2g`)."
    )
    pids: PositiveInt | None = Field(default=None, description="`--pids-limit`.")

    def merged(self, override: Resources | None) -> Resources:
        """This budget with the fields `override` sets replacing this one's."""
        if override is None:
            return self
        return Resources(**{**self.model_dump(), **override.model_dump(exclude_none=True)})

    def to_dict(self) -> dict[str, Any]:
        """The limits that are set."""
        return self.model_dump(exclude_none=True)


def memory_bytes(value: str) -> int:
    """`2g`, `512m`, `1.5G`, `1024` as bytes (Docker's units)."""
    text = value.strip().rstrip("bB")
    unit = text[-1].lower() if text and text[-1].isalpha() else ""
    number = float(text[:-1] if unit else text)
    return int(number * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit])


class Heartbeat(Strict):
    """The ground station's heartbeat the vehicle listens for (`std_msgs/msg/Header`)."""

    topic: str = Field(
        description="Topic of the heartbeat (relative: under the vehicle's namespace).",
        min_length=1,
    )
    rate_hz: PositiveFloat = Field(2.0, description="How often the ground station sends it.")


class Link(Strict):
    """The vehicle's radio link to its ground station (task P2-T02).

    In a scenario with radio links (`network`) the ground station sends the heartbeat over the
    link, so a dropped link stops it, and the scenario's assertions check the behaviour
    `loss_behaviour` documents.
    """

    heartbeat: Heartbeat | None = Field(
        None, description="The heartbeat the ground station sends (absent: none)."
    )
    loss_behaviour: str = Field(
        "", description="What the vehicle does when the link is lost, and when it comes back."
    )


class Runtime(Strict):
    """What runs on the vehicle and how."""

    tier: Literal["T1", "T2", "T3"] = Field(
        "T1", description="Fidelity tier (ADR-0007); only T1 (SIL) is implemented before Phase 6."
    )
    image: str = Field(description="Image reference to build or run.")
    build: Build | None = Field(None, description="Absent: `image` is prebuilt.")
    entrypoint: Entrypoint
    params: list[str] = Field(
        default_factory=list,
        description="Parameter files the launch reads unchanged from the field, relative to the "
        "manifest.",
    )
    drivers: list[Driver] = Field(default_factory=list, description="The driver-swap map.")
    ready: Ready | None = None
    resources: Resources | None = None
    dds_profile: str | None = Field(
        None,
        description="The vehicle's own DDS XML profile (relative to the manifest), mounted into "
        "the container and named to its RMW (`FASTRTPS_DEFAULT_PROFILES_FILE`, `CYCLONEDDS_URI`).",
    )
    env: dict[str, str] = Field(
        default_factory=dict, description="Extra environment of the container."
    )
    link: Link | None = Field(None, description="The radio link to the ground station.")

    @model_validator(mode="after")
    def _unique_drivers(self) -> Runtime:
        names = [d.name for d in self.drivers]
        if len(set(names)) != len(names):
            raise ValueError("drivers: names must be unique")
        if self.dds_profile is not None:
            path = Path(self.dds_profile)
            if path.is_absolute() or ".." in path.parts or not self.dds_profile.strip():
                raise ValueError(
                    f"dds_profile: {self.dds_profile!r} must be a relative path inside the "
                    "manifest's directory"
                )
        return self

    @property
    def rmw(self) -> str:
        """The RMW the container runs (`RMW_IMPLEMENTATION` of `env`, Fast DDS by default)."""
        return self.env.get("RMW_IMPLEMENTATION", DEFAULT_RMW)

    def dds_profile_env(self, path: str = DDS_PROFILE_IN_CONTAINER) -> dict[str, str]:
        """The variable that names the mounted DDS profile to the container's RMW."""
        if self.dds_profile is None:
            return {}
        variable = DDS_PROFILE_ENV.get(self.rmw)
        if variable is None:
            raise ManifestError(
                f"dds_profile: no known profile variable for RMW {self.rmw!r} "
                f"(known: {', '.join(sorted(DDS_PROFILE_ENV))})"
            )
        value = f"file://{path}" if variable == "CYCLONEDDS_URI" else path
        return {variable: value}


# ----------------------------------------------------------------------------------- manifest
class RobotManifest(Strict):
    """A vehicle manifest, `apiVersion: fieldproof.dev/v0alpha1`, `kind: Robot`."""

    api_version: Literal["fieldproof.dev/v0alpha1"] = Field(alias="apiVersion")
    kind: Literal["Robot"]
    extends: str | None = Field(
        None,
        description="Manifest this one inherits from (relative path); resolved before validation.",
    )
    metadata: Metadata
    description: Description | None = None
    frames: Frames | None = None
    dimensions: Dimensions | None = None
    drive: Drive | None = None
    sensors: list[Sensor] = Field(default_factory=list)
    runtime: Runtime | None = None

    _path: Path | None = PrivateAttr(default=None)

    @property
    def path(self) -> Path | None:
        """Where the manifest was loaded from."""
        return self._path

    @property
    def directory(self) -> Path:
        """The manifest's directory, which its relative paths resolve against."""
        if self._path is None:
            raise ManifestError("the manifest was not loaded from a file")
        return self._path.parent

    @property
    def name(self) -> str:
        """`metadata.name`."""
        return self.metadata.name

    def interfaces(self) -> dict[str, Topic]:
        """Every ROS topic of the model, drive interfaces first, keyed by its topic name."""
        topics: dict[str, Topic] = {}
        if self.drive is not None:
            for topic in self.drive.interfaces():
                topics[topic.ros_topic] = topic
        for sensor in self.sensors:
            for output in sensor.outputs:
                topics[output.ros_topic] = output
        return topics

    @model_validator(mode="after")
    def _consistent(self) -> RobotManifest:
        names = [s.name for s in self.sensors]
        if len(set(names)) != len(names):
            raise ValueError("sensors: names must be unique")
        topics = [t.ros_topic for s in self.sensors for t in s.outputs]
        if self.drive is not None:
            topics += [t.ros_topic for t in self.drive.interfaces()]
        duplicates = sorted({t for t in topics if topics.count(t) > 1})
        if duplicates:
            raise ValueError(f"ROS topics declared more than once: {duplicates}")
        if self.runtime is not None and (self.drive is not None or self.sensors):
            known = set(topics) | BRIDGE_TOPICS
            provided: dict[str, str] = {}
            for driver in self.runtime.drivers:
                for topic in driver.provides:
                    if topic not in known:
                        raise ValueError(
                            f"runtime.drivers[{driver.name}].provides: {topic!r} is not an "
                            "interface of this manifest"
                        )
                    if topic in provided:
                        raise ValueError(
                            f"runtime.drivers: {topic!r} is provided for both "
                            f"{provided[topic]!r} and {driver.name!r}"
                        )
                    provided[topic] = driver.name
        return self

    def complete(self) -> RobotManifest:
        """Check that the model sections a stack needs are present (after `extends`)."""
        missing = [s for s in ("description", "frames", "drive") if getattr(self, s) is None]
        if missing:
            raise ManifestError(f"{self.metadata.name}: missing {', '.join(missing)}")
        return self

    def unswapped(self) -> list[str]:
        """Model interfaces no driver of the swap map provides (informational)."""
        if self.runtime is None:
            return sorted(self.interfaces())
        provided = {t for d in self.runtime.drivers for t in d.provides}
        return [t for t in self.interfaces() if t not in provided]


def format_errors(error: ValidationError) -> str:
    """One line per problem: `where: what`."""
    lines = []
    for problem in error.errors():
        where = ".".join(str(p) for p in problem["loc"]) or "manifest"
        message = re.sub(r"^(Value|Assertion) error, ", "", str(problem["msg"]))
        lines.append(f"{where}: {message}")
    return "\n".join(lines)


def validate_data(data: dict[str, Any], path: Path | None = None) -> RobotManifest:
    """Validate a merged manifest document."""
    try:
        manifest = RobotManifest.model_validate(data)
    except ValidationError as error:
        prefix = f"{path}: " if path else ""
        raise ManifestError(prefix + format_errors(error)) from error
    manifest._path = path
    return manifest


def load_manifest(path: Path | str, *, complete: bool = True) -> RobotManifest:
    """Read, resolve `extends` and validate the manifest at `path`.

    With `complete` (the default) the model sections must be present: a manifest meant only to
    be extended may skip them when validated with `complete=False`.
    """
    file = Path(path).resolve()
    data = read_manifest_data(file)
    manifest = validate_data(data, file)
    if complete:
        try:
            manifest.complete()
        except ManifestError as error:
            raise ManifestError(f"{file}: {error}") from error
    return manifest


def json_schema() -> dict[str, Any]:
    """The JSON Schema of a manifest file (before `extends` is resolved: sections are optional)."""
    schema = RobotManifest.model_json_schema(by_alias=True)
    schema["title"] = "Fieldproof vehicle manifest (robot.yaml) v0"
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": SCHEMA_ID, **schema}
