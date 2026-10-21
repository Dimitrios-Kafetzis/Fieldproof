"""The scenario runner (tasks P1-T05, P1-T06 and P2-T01; ADR-0004, ADR-0005, ADR-0006).

:func:`plan` turns a validated scenario and the workspace configuration into a :class:`Plan`:
the world file and its named frames, every vehicle's manifest, spawn pose, namespace, budget and
the three containers it needs (the one-shot spawn, its bridge, the vehicle itself), the
simulator container, the discovery server when asked for, the recorder container, the resolved
timeline and assertions, the run's ROS domain, the run id and the run directory. Nothing in it
touches Docker, so ``fieldproof validate`` and ``fieldproof run --dry-run`` stop here.

:class:`Run` executes a plan against a :class:`~fieldproof.backend.Backend` as a state machine
of phases (01-architecture.md, "Data flow for one run"): preparing (images, assets, network, the
budget against the host), sim (the discovery server if any, then the simulator, started until
the world's services answer), spawn (one one-shot container per vehicle, all at once), bridges
(started at once, each until the vehicle's odometry topic appears, each checked against its
contract when asked), recorder (the observer of :mod:`fieldproof.observer` started on the run
directory, recording to MCAP when asked, its ``ready`` sample seen), vehicles (started in
parallel, a vehicle once the ones it ``depends_on`` are ready, each until the image's readiness
probe reports every ``runtime.ready`` condition), running (the timeline within the scenario's
budget, the :class:`~fieldproof.schedule.Schedule` starting every entry's occurrences at their
time, after another entry or when a condition of the stream holds, each performed by its
registered :class:`~fieldproof.events.Event` on a worker, while the
:class:`~fieldproof.assertions.Evaluator` consumes the observer's stream), teardown (vehicles,
recorder, bridges, simulator, discovery in reverse order, logs kept, then a sweep by label),
finished (the report written). A failure in any phase, a failed assertion, the scenario's
budget, a Ctrl-C or an error of the runtime all end in the teardown: a run never leaves
containers behind unless asked to with ``keep``. Everything the run did is in
``runs/<run_id>/``: the scenario as run, ``run.json`` (the record,
rewritten at every phase), ``engine.log`` (one JSON object per line), ``samples.jsonl`` and
``assertions.jsonl`` (the stream and the verdicts), ``recording/`` (the MCAP), ``report.json``
and ``report.md``, and the containers' logs.

Isolation between runs and between vehicles (task P2-T01): every container of a run carries the
run's ``ROS_DOMAIN_ID`` (derived from the run id unless configured, so two runs on one host never
share a domain) and ``GZ_PARTITION``; with ``discovery: server`` the ROS containers find each
other through a Fast DDS discovery server of the run instead of multicast; every vehicle's
topics, nodes and TF tree live under its namespace, its own DDS profile is mounted in, and its
container runs under the cgroup limits of its budget.

Radio links (task P2-T02): a scenario with a ``network`` section gets the topology of
:mod:`fieldproof.network`. The plan adds a radio network and an onboard network per vehicle, a
link holder per vehicle (the vehicle container shares its network namespace), and the ground
station; the bridge and the recorder join the onboard networks, the bridge publishing ``/clock``
all of them. A ``network`` phase between ``recorder`` and ``vehicles`` starts the holders, puts
each link's profile on it and starts the ground station; the timeline's link events change the
profiles; the ground station's measurements are checked against them at the end of the run.

Events (task P2-T03): the timeline's entries are dispatched to the events of
:mod:`fieldproof.events` by ``action``; :func:`plan` runs each event's ``plan`` hook (a goal
resolved in the map frame, a vehicle checked) and :class:`Run` runs its ``apply`` hook through
a :class:`RunContext` (commands in the containers, the backend for pauses and kills, the links,
the log). Every occurrence is in ``run.json`` ``timeline[]`` with its trigger and what it did,
and in the engine log as ``action.start`` and ``action.end`` lines carrying the same record,
which the observer relays onto ``/fieldproof/events``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

import yaml
from rich.console import Console

from fieldproof import __version__, ground, observer
from fieldproof.assertions import (
    ASSERTIONS_FILE,
    DEFAULT_CONTACTS_TOPIC,
    FAILED,
    OBSERVE_FILE,
    SAMPLES_FILE,
    Evaluator,
    PlannedAssertion,
    Result,
    Sample,
    observe_plan,
)
from fieldproof.backend import Backend, BackendError, ContainerSpec, ExecResult, LineSink, Mount
from fieldproof.config import AUTO, Config
from fieldproof.dsl import TimelineBase
from fieldproof.events import (
    CANCELLED,
    OK,
    RUNNING,
    SKIPPED,
    TIMEOUT,
    ActionRecord,
    PlanContext,
    VehicleHandle,
    registry,
    resolved_data,
)
from fieldproof.events import (
    FAILED as ACTION_FAILED,
)
from fieldproof.events.navigation import NAV2_ACTION, NAV2_ACTION_TYPE, goal_status
from fieldproof.expression import compile_condition
from fieldproof.manifest import DDS_PROFILE_IN_CONTAINER, Resources, RobotManifest, memory_bytes
from fieldproof.network import (
    DROP,
    DROP_PROFILE,
    GROUND_CONFIG,
    NETWORK_FILE,
    PROBE_INTERVAL_S,
    PROBE_PAYLOAD_BYTES,
    PROBE_TIMEOUT_S,
    PROBE_WINDOW_S,
    LinkMonitor,
    LinkProfile,
    link_script,
    traffic,
    traffic_script,
)
from fieldproof.network import (
    FAILED as LINK_FAILED,
)
from fieldproof.network import (
    PASSED as LINK_PASSED,
)
from fieldproof.observer import FileTail
from fieldproof.report import REPORT_JSON, REPORT_JUNIT, REPORT_MD, write_reports
from fieldproof.scenario import (
    Assertion,
    NoCollision,
    Pose,
    ReachPose,
    Scenario,
    ScenarioError,
    TopicRate,
    TopicValue,
    Vehicle,
    VideoOutput,
    WorldInfo,
    entry_label,
    place,
    read_world,
)
from fieldproof.schedule import Occurrence, PlannedWhen, Schedule
from fieldproof.vehicle import ENTRYPOINT_IN_IMAGE, READY_IN_IMAGE, load_vehicle
from fieldproof_bridge.manifest import ManifestError

RUN_LABEL = "io.fieldproof.run"
ROLE_LABEL = "io.fieldproof.role"
VEHICLE_LABEL = "io.fieldproof.vehicle"
DOMAIN_LABEL = "io.fieldproof.domain"
# The domains an automatic run id maps to: 1 to 100, inside the range ROS 2 documents as safe on
# Linux, and away from 0, the compose stack's default, so a run never disturbs `make up`.
DOMAIN_RANGE = (1, 100)
# The Fast DDS discovery server of a run (`discovery: server`): its container, the port it
# listens on, and the line its tool prints once it does.
DISCOVERY_PORT = 11811
DISCOVERY_READY = "Server is running"
# Where a vehicle's bridge writes the bundle it runs from, so a contract check can read it.
BUNDLE_DIR_IN_BRIDGE = "/tmp/fieldproof-bridge"  # noqa: S108  (inside the bridge container)
CONTRACT_CHECK = ("ros2", "run", "fieldproof_bridge", "contract_check")
CONTRACT_SUMMARY = re.compile(r"^(\d+) passed, (\d+) failed$")
# Where the bridge image sees the workspace (manifests, descriptions, launch files).
WORKSPACE = "/workspace"
# The simulator's resource path: the compose stack's four read-only mounts (containers.md).
SIM_MOUNTS = {
    "worlds": "/fieldproof/worlds",
    "models": "/fieldproof/models",
    "fuel": "/fieldproof/fuel",
    "robots": "/fieldproof/robots",
}
GZ_PARTITION = "fieldproof"
BRIDGE_ENTRYPOINT = "/usr/local/bin/fieldproof-bridge-entrypoint"
BRIDGE_COMMAND = "fieldproof-bridge"
# The package of the reference vehicles' spawn launch files, built into the bridge image.
ROBOTS_PACKAGE = "fieldproof_robots"
# The spawn launch's default `z`: the reference UGV's base_link height above the ground.
SPAWN_HEIGHT_M = 0.12
__all__ = ["NAV2_ACTION", "NAV2_ACTION_TYPE", "goal_status"]  # re-exported for the tests
RENDER_DIR = Path("/dev/dri")
POLL_S = 2.0
# How often the running phase reads the observer's stream and ticks the assertions.
EVAL_POLL_S = 0.5
MIN_POLL_S = 0.05
# The recorder container: the observer mounted from the engine, the run directory read-write.
OBSERVER_FILE = Path(observer.__file__).resolve()
OBSERVER_IN_CONTAINER = "/opt/fieldproof/observer.py"
RUN_DIR_IN_CONTAINER = "/fieldproof/run"
# The ground station of a run with radio links: the script mounted from the engine like the
# observer, on the radio network; and what a link holder runs (it only holds the namespace).
GROUND_FILE = Path(ground.__file__).resolve()
GROUND_IN_CONTAINER = "/opt/fieldproof/ground.py"
GROUND_HOSTNAME = "ground"
HOLDER_COMMAND = ("sleep", "infinity")
# A bridge behind a link sits on two networks, and gz-transport advertises the address of
# whichever interface comes first, which Docker does not keep stable: when it is the onboard one,
# the simulator cannot reach the bridge's publishers (`cmd_vel` never arrives). The bridge
# advertises its address on the route to the simulator instead (GZ_IP).
GZ_IP_WRAPPER = (
    'sim="$(getent ahostsv4 "$FIELDPROOF_SIM_HOST" | awk \'{print $1; exit}\')"; '
    'if [ -n "$sim" ]; then '
    'GZ_IP="$(ip -o -4 route get "$sim" | sed -n \'s/.* src \\([0-9.]*\\).*/\\1/p\')"; '
    "export GZ_IP; fi; "
    'exec "$@"'
)
LINK_KERNEL_HINT = (
    "the host kernel must provide the sch_netem and ifb modules (`sudo modprobe sch_netem ifb` "
    "if they are not loaded on demand)"
)
# In-container limit of a polling command, so a hung `gz service -l` cannot hang the runner.
PROBE_LIMIT_S = 20
RECORD_FILE = "run.json"
LOG_FILE = "engine.log"
VIDEO_DIR = "video"
VIDEO_DIR_IN_SIM = "/tmp"  # noqa: S108  (inside the simulator container, writable by its user)
VIDEO_SETTLE_S = 15.0
"""How long the encoder may take to close the file after the recording is stopped."""
SCENARIO_COPY = "scenario.yaml"
LOGS_DIR = "logs"


class Phase(StrEnum):
    """The states of a run, in order."""

    CREATED = "created"
    PREPARING = "preparing"
    SIM = "sim"
    SPAWN = "spawn"
    BRIDGES = "bridges"
    RECORDER = "recorder"
    NETWORK = "network"
    VEHICLES = "vehicles"
    RUNNING = "running"
    TEARDOWN = "teardown"
    FINISHED = "finished"


class Status(StrEnum):
    """How a run ended."""

    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"
    INTERRUPTED = "interrupted"


EXIT_CODES = {
    Status.PASSED: 0,
    Status.FAILED: 1,
    Status.TIMEOUT: 1,
    Status.ERROR: 1,
    Status.INTERRUPTED: 130,
}


class RunError(RuntimeError):
    """The run cannot proceed (images, assets, the simulator, the bridge): status `error`."""


class RunFailedError(RuntimeError):
    """The scenario failed: an assertion, a vehicle not ready, an action, a container died."""


class RunTimeoutError(RunFailedError):
    """The scenario's wall-clock budget passed."""


# -------------------------------------------------------------------------------------- plan
@dataclass(frozen=True)
class PlannedAction:
    """A timeline entry resolved by its event's `plan` hook, its `when` resolved too."""

    entry: TimelineBase
    resolved: dict[str, Any] = field(default_factory=dict)
    """What the event's `plan` returned (a goal in the world and in the map frame)."""
    watch: PlannedWhen | None = None
    """The entry's `when`, resolved against the plan."""

    @property
    def name(self) -> str:
        """The entry's label."""
        return entry_label(self.entry)

    @property
    def timeout_s(self) -> float:
        """How long an occurrence may take (no `timeout_s`: as long as the budget lets it)."""
        timeout = getattr(self.entry, "timeout_s", None)
        return float(timeout) if timeout is not None else math.inf

    @property
    def goal_world(self) -> Pose | None:
        """A `navigate_to_pose` goal in the world."""
        goal = self.resolved.get("goal_world")
        return goal if isinstance(goal, Pose) else None

    @property
    def goal_map(self) -> Pose | None:
        """A `navigate_to_pose` goal in the vehicle's map frame."""
        goal = self.resolved.get("goal_map")
        return goal if isinstance(goal, Pose) else None

    def to_dict(self) -> dict[str, Any]:
        """The entry as the dry run shows it."""
        entry = self.entry
        return {
            "name": self.name,
            "at_s": entry.at,
            "vehicle": entry.vehicle,
            "action": entry.action_name,
            "trigger": entry.trigger(),
            **({"when": self.watch.to_dict()} if self.watch is not None else {}),
            **entry.parameters(),
            **resolved_data(self.resolved),
        }


@dataclass(frozen=True)
class PlannedVehicle:
    """A vehicle of the scenario with its manifest, spawn pose, budget and containers."""

    vehicle: Vehicle
    manifest: RobotManifest
    spawn: Pose
    spawn_job: ContainerSpec
    bridge: ContainerSpec
    container: ContainerSpec
    ready_timeout_s: float
    resources: Resources = field(default_factory=Resources)
    """The budget applied: the manifest's `runtime.resources` under the scenario's overrides."""
    depends_on: tuple[str, ...] = ()
    dds_profile: Path | None = None
    """The vehicle's DDS profile on the host, mounted into its container."""
    clock: bool = True
    """Whether this vehicle's bridge is the one bridging `/clock` (the first vehicle's is)."""
    bundle_dir: str = BUNDLE_DIR_IN_BRIDGE
    """Where the bridge container writes its bundle (`contract.yaml` for the contract check)."""

    @property
    def name(self) -> str:
        """The vehicle's name."""
        return self.vehicle.name

    @property
    def namespace(self) -> str:
        """The vehicle's namespace (empty at the root)."""
        return self.vehicle.namespace

    @property
    def container_name(self) -> str:
        """The name of the vehicle's container."""
        return self.container.name

    def topic(self, name: str) -> str:
        """`name` under the vehicle's namespace (an absolute name stays as written)."""
        if name.startswith("/"):
            return name
        namespace = self.vehicle.namespace
        return f"/{namespace}/{name}" if namespace else f"/{name}"

    def describe(self) -> dict[str, Any]:
        """The vehicle as the record and the reports carry it."""
        return {
            "name": self.name,
            "manifest": self.manifest.name,
            "namespace": self.namespace,
            "frame_prefix": self.vehicle.frame_prefix,
            "image": self.container.image,
            "resources": self.resources.to_dict(),
            "depends_on": list(self.depends_on),
            "dds_profile": str(self.dds_profile) if self.dds_profile else None,
        }


@dataclass(frozen=True)
class PlannedVideo:
    """A video to record from a camera of the world (`outputs.video`)."""

    camera: str
    service: str
    """The `CameraVideoRecorder` service of the camera, called in the simulator container."""
    in_sim: str
    """Where the simulator writes the file."""
    file: str
    """The file in the run directory, relative to it."""

    def to_dict(self) -> dict[str, Any]:
        """The video plan as data."""
        return asdict(self)


@dataclass(frozen=True)
class PlannedLinks:
    """The radio links of a run (`network`): networks, holders, ground station, profiles."""

    radio: str
    """The radio network: the ground station and every link holder."""
    onboard: dict[str, str]
    """Per vehicle, its onboard network: the holder, the bridge and the recorder."""
    holders: dict[str, ContainerSpec]
    ground: ContainerSpec
    profiles: dict[str, LinkProfile]
    """Every profile the scenario can name (the presets and its own)."""
    links: dict[str, str]
    """Per vehicle, the profile its link is configured with."""
    heartbeats: dict[str, dict[str, Any] | None]
    """Per vehicle, the heartbeat the ground station sends it (absolute topic, rate)."""
    verify: str
    labels: dict[str, str] = field(default_factory=dict)

    def networks(self) -> list[str]:
        """The networks the run creates and removes."""
        return [self.radio, *self.onboard.values()]

    def profile(self, name: str) -> LinkProfile:
        """A profile by name (`drop` included)."""
        return DROP_PROFILE if name == DROP else self.profiles[name]

    def ground_config(self, run_dir: str) -> dict[str, Any]:
        """`ground.json`: the links to probe and the heartbeats to send."""
        return {
            "output": f"{run_dir}/{NETWORK_FILE}",
            "links": [
                {"vehicle": name, "host": name, "heartbeat": self.heartbeats.get(name)}
                for name in self.links
            ],
            "probe": {
                "interval_s": PROBE_INTERVAL_S,
                "window_s": PROBE_WINDOW_S,
                "timeout_s": PROBE_TIMEOUT_S,
                "payload_bytes": PROBE_PAYLOAD_BYTES,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """The links as the dry run and the record show them."""
        return {
            "radio": self.radio,
            "onboard": dict(self.onboard),
            "ground": self.ground.name,
            "verify": self.verify,
            "links": {
                name: {
                    "profile": profile,
                    **self.profile(profile).model_dump(),
                    "holder": self.holders[name].name,
                    "heartbeat": self.heartbeats.get(name),
                }
                for name, profile in self.links.items()
            },
        }


@dataclass(frozen=True)
class Plan:
    """A resolved scenario, ready to run."""

    scenario: Scenario
    config: Config
    world: WorldInfo
    run_id: str
    run_dir: Path
    network: str
    render: str
    domain_id: int
    sim: ContainerSpec
    recorder: ContainerSpec
    vehicles: tuple[PlannedVehicle, ...]
    actions: tuple[PlannedAction, ...]
    assertions: tuple[PlannedAssertion, ...]
    observe: dict[str, Any]
    """What the recorder's observer watches and records (`observe.json`)."""
    video: PlannedVideo | None = None
    domain_source: str = "explicit"
    """Where the domain id came from: `auto` (derived from the run id) or `explicit`."""
    discovery: ContainerSpec | None = None
    """The run's Fast DDS discovery server (`discovery: server`), or None for multicast."""
    links: PlannedLinks | None = None
    """The radio links (`network`), or None: one shared network, no shaping."""

    @property
    def discovery_mode(self) -> str:
        """`server` or `multicast`."""
        return "server" if self.discovery is not None else "multicast"

    def containers(self) -> list[ContainerSpec]:
        """The long-running containers in start order (the teardown reverses it)."""
        specs = [self.discovery] if self.discovery is not None else []
        specs.append(self.sim)
        specs += [v.bridge for v in self.vehicles]
        specs.append(self.recorder)
        if self.links is not None:
            specs += [self.links.holders[v.name] for v in self.vehicles]
            specs.append(self.links.ground)
        specs += [v.container for v in self.vehicles]
        return specs

    def vehicle(self, name: str) -> PlannedVehicle:
        """The planned vehicle called `name`."""
        return next(v for v in self.vehicles if v.name == name)

    def start_order(self) -> list[PlannedVehicle]:
        """The vehicles with every dependency before its dependents."""
        return [self.vehicle(v.name) for v in self.scenario.start_order()]

    def to_dict(self) -> dict[str, Any]:
        """The plan as data (`fieldproof run --dry-run`)."""

        def spec(s: ContainerSpec) -> dict[str, Any]:
            return {
                "role": s.role,
                "name": s.name,
                "image": s.image,
                "command": shlex.join(s.command),
                "hostname": s.hostname,
                "env": dict(s.env),
                "mounts": [
                    f"{m.source}:{m.target}{':ro' if m.read_only else ''}" for m in s.mounts
                ],
                "devices": list(s.devices),
                "networks": list(s.networks),
                "aliases": list(s.aliases),
                "network_mode": s.network_mode,
                "cap_add": list(s.cap_add),
                "cpus": s.cpus,
                "cpuset": s.cpuset,
                "memory": s.memory,
                "pids": s.pids,
                "user": s.user,
                "working_dir": s.working_dir,
            }

        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "scenario": {
                "name": self.scenario.name,
                "path": str(self.scenario.path),
                "digest": self.scenario.digest,
            },
            "workspace": str(self.config.root),
            "network": self.network,
            "render": self.render,
            "ros_domain_id": self.domain_id,
            "domain_source": self.domain_source,
            "discovery": self.discovery_mode,
            "world": {"name": self.world.name, "file": str(self.world.file)},
            "time": self.scenario.time.model_dump(),
            "vehicles": [
                {
                    **v.describe(),
                    "manifest": str(v.manifest.path),
                    "spawn": asdict(v.spawn),
                    "ready_timeout_s": v.ready_timeout_s,
                    "clock": v.clock,
                    "spawn_job": spec(v.spawn_job),
                }
                for v in self.vehicles
            ],
            "containers": [spec(s) for s in self.containers()],
            "links": self.links.to_dict() if self.links else None,
            "timeline": [a.to_dict() for a in self.actions],
            "assertions": [a.to_dict() for a in self.assertions],
            "observe": self.observe,
            "video": self.video.to_dict() if self.video else None,
        }


def make_run_id(scenario: Scenario, now: datetime, token: str) -> str:
    """`<scenario-hash>-<timestamp>-<short-uuid>` (01-architecture.md)."""
    return f"{scenario.digest or 'nohash'}-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{token}"


def auto_domain_id(run_id: str) -> int:
    """The ROS domain a run id maps to: a hash of the id into :data:`DOMAIN_RANGE`.

    Derived rather than allocated so a dry run, the record and the containers agree without
    asking the daemon; two runs started at once collide with the odds of one in a hundred, and
    `--domain-id` settles it when that matters.
    """
    low, high = DOMAIN_RANGE
    digest = int(hashlib.sha256(run_id.encode()).hexdigest(), 16)
    return low + digest % (high - low + 1)


def detect_render(preference: str, render_dir: Path = RENDER_DIR) -> tuple[str, tuple[str, ...]]:
    """`gpu` with the render node's group, or `software` (the host decides under `auto`)."""
    nodes = sorted(render_dir.glob("renderD*")) if render_dir.is_dir() else []
    if preference == "software" or (preference == "auto" and not nodes):
        return "software", ()
    if not nodes:
        raise ScenarioError(f"render gpu: no render node under {render_dir}")
    return "gpu", (str(nodes[0].stat().st_gid),)


def plan(
    scenario: Scenario,
    config: Config,
    *,
    run_id: str | None = None,
    runs_dir: Path | str | None = None,
    render: str | None = None,
    domain_id: int | None = None,
    network: str | None = None,
    discovery: str | None = None,
    now: datetime | None = None,
    token: str | None = None,
) -> Plan:
    """Resolve the scenario against the workspace; raise :class:`ScenarioError` if it cannot run."""
    worlds_dir = config.resolve(config.stack.worlds)
    world_file = worlds_dir / scenario.world.file
    if not world_file.is_file():
        raise ScenarioError(f"world.file: {scenario.world.file!r} is not a file in {worlds_dir}")
    world = read_world(world_file)
    render_mode, groups = detect_render(render or config.docker.render)
    net = network or config.docker.network
    identity = run_id or make_run_id(
        scenario, now or datetime.now(UTC), token or uuid.uuid4().hex[:6]
    )
    short = identity.rsplit("-", 1)[-1]
    if domain_id is not None:
        domain, domain_source = domain_id, "explicit"
    elif config.docker.ros_domain_id == AUTO:
        domain, domain_source = auto_domain_id(identity), "auto"
    else:
        domain, domain_source = int(config.docker.ros_domain_id), "explicit"
    discovery_mode = discovery or config.docker.discovery
    if discovery_mode not in ("multicast", "server"):
        raise ScenarioError(f"discovery {discovery_mode!r}: expected multicast or server")
    if scenario.outputs.directory is not None:
        base = config.resolve(scenario.outputs.directory)
    elif runs_dir is not None:
        base = Path(runs_dir).resolve()
    else:
        base = config.resolve(config.runs.directory)
    labels = {RUN_LABEL: identity, DOMAIN_LABEL: str(domain)}
    env = {"ROS_DOMAIN_ID": str(domain), "GZ_PARTITION": GZ_PARTITION}
    # What every ROS container of the run gets on top: with a discovery server, where it is and
    # super-client mode, so graph queries (the readiness probe, the observer, `ros2 topic list`)
    # see the whole graph as they do with multicast.
    server: ContainerSpec | None = None
    ros_env = dict(env)
    if discovery_mode == "server":
        server = _plan_discovery(config, short, net, env, labels)
        ros_env.update(
            {
                "ROS_DISCOVERY_SERVER": f"{server.hostname}:{DISCOVERY_PORT}",
                "ROS_SUPER_CLIENT": "TRUE",
            }
        )
    sim = ContainerSpec(
        name=f"fieldproof-{short}-sim",
        image=config.image("sim"),
        role="sim",
        command=("gz", "sim", "-s", "-r", "--headless-rendering", world_file.name),
        env={
            **env,
            "LIBGL_ALWAYS_SOFTWARE": "0" if render_mode == "gpu" else "1",
            "GZ_SIM_RESOURCE_PATH": ":".join(SIM_MOUNTS.values()),
        },
        labels={**labels, ROLE_LABEL: "sim"},
        network=net,
        hostname=f"fieldproof-{short}-sim",
        mounts=tuple(
            Mount(config.resolve(getattr(config.stack, key)), target)
            for key, target in SIM_MOUNTS.items()
        ),
        devices=("/dev/dri",) if render_mode == "gpu" else (),
        group_add=groups,
    )
    vehicles = tuple(
        _plan_vehicle(scenario, config, world, v, short, net, ros_env, labels, clock=index == 0)
        for index, v in enumerate(scenario.vehicles)
    )
    by_name = {v.name: v for v in vehicles}
    actions = _plan_actions(scenario, world, by_name)
    assertions = tuple(
        _plan_assertion(world, by_name, a, index) for index, a in enumerate(scenario.assertions)
    )
    run_dir = base / identity
    video = _plan_video(scenario, world, short)
    links = None
    if scenario.network is not None:
        if server is not None:
            raise ScenarioError(
                "network: radio links need multicast discovery (the discovery server would sit "
                "on one side of every link); run with discovery multicast"
            )
        links = _plan_links(scenario, config, vehicles, short, run_dir, ros_env, labels)
        vehicles = tuple(_behind_link(v, links, str(sim.hostname)) for v in vehicles)
    observe = observe_plan(
        assertions,
        world=world.name,
        models=[v.name for v in vehicles],
        record=scenario.outputs.record,
        topics=scenario.outputs.topics,
        run_dir=RUN_DIR_IN_CONTAINER,
        conditions=[a.watch for a in actions if a.watch is not None],
    )
    recorder = ContainerSpec(
        name=f"fieldproof-{short}-recorder",
        image=config.image("bridge"),
        role="recorder",
        command=(
            "python3",
            OBSERVER_IN_CONTAINER,
            "--config",
            f"{RUN_DIR_IN_CONTAINER}/{OBSERVE_FILE}",
        ),
        # Runs as the engine's user so what it writes into the run directory belongs to the
        # user who started the run; HOME, the ROS log directory and the working directory point
        # at places that user can reach (the image's home is not, when the uids differ: rosbag2
        # then fails to open its file with "Permission denied").
        env={**ros_env, "HOME": "/tmp", "ROS_LOG_DIR": "/tmp/ros-log"},  # noqa: S108  (in-container)
        working_dir=RUN_DIR_IN_CONTAINER,
        labels={**labels, ROLE_LABEL: "recorder"},
        network=net,
        hostname=f"fieldproof-{short}-recorder",
        mounts=(
            Mount(OBSERVER_FILE, OBSERVER_IN_CONTAINER),
            Mount(run_dir, RUN_DIR_IN_CONTAINER, read_only=False),
        ),
        user=f"{os.getuid()}:{os.getgid()}",
        # With radio links the recorder sees every vehicle on its onboard network, unimpaired.
        networks=tuple(links.onboard.values()) if links else (),
    )
    return Plan(
        scenario=scenario,
        config=config,
        world=world,
        run_id=identity,
        run_dir=run_dir,
        network=net,
        render=render_mode,
        domain_id=domain,
        sim=sim,
        recorder=recorder,
        vehicles=vehicles,
        actions=tuple(actions),
        assertions=assertions,
        observe=observe,
        video=video,
        domain_source=domain_source,
        discovery=server,
        links=links,
    )


def _plan_actions(
    scenario: Scenario, world: WorldInfo, by_name: dict[str, PlannedVehicle]
) -> list[PlannedAction]:
    """The timeline in order of `at`, each entry resolved by its event, its `when` resolved."""
    events = registry()
    actions = []
    for index, entry in enumerate(sorted(scenario.timeline, key=lambda e: e.at)):
        context = PlanContext(
            world, by_name, links=scenario.network is not None, where=f"timeline[{index}]"
        )
        resolved = dict(events[entry.action_name].event.plan(entry, context))
        watch = _plan_when(entry, f"timeline[{index}].when", world, by_name)
        actions.append(PlannedAction(entry, resolved, watch))
    return actions


def _plan_when(
    entry: TimelineBase, what: str, world: WorldInfo, by_name: dict[str, PlannedVehicle]
) -> PlannedWhen | None:
    """The entry's `when` with its topic absolute, its model and its target in the world."""
    when = entry.when
    if when is None:
        return None
    vehicle = when.vehicle or entry.vehicle
    topic = model = target = None
    if when.topic is not None:
        if vehicle is not None:
            topic = by_name[vehicle].topic(when.topic)
        elif when.topic.startswith("/"):
            topic = when.topic
        else:
            raise ScenarioError(f"{what}.topic: {when.topic!r} needs a vehicle or a leading /")
    if when.metric in ("x", "y", "z", "yaw", "distance_to"):
        model = vehicle
    if when.target is not None:
        target = place(when.target, world, f"{what}.target")
    return PlannedWhen(when, topic, model, target, compile_condition(when.condition))


def _plan_video(scenario: Scenario, world: WorldInfo, short: str) -> PlannedVideo | None:
    """The video of the run from a camera of the world (`outputs.video`), if asked for."""
    wanted = scenario.outputs.video
    if not isinstance(wanted, VideoOutput):
        return None
    if wanted.camera not in world.cameras:
        known = ", ".join(sorted(world.cameras)) or "none"
        raise ScenarioError(
            f"outputs.video: world {world.name} has no camera sensor {wanted.camera!r} with "
            f"a CameraVideoRecorder plugin (cameras with one: {known})"
        )
    return PlannedVideo(
        camera=wanted.camera,
        service=world.cameras[wanted.camera],
        in_sim=f"{VIDEO_DIR_IN_SIM}/fieldproof-{short}-{wanted.camera}.{wanted.format}",
        file=f"{VIDEO_DIR}/{wanted.camera}.{wanted.format}",
    )


def _plan_discovery(
    config: Config, short: str, network: str, env: dict[str, str], labels: dict[str, str]
) -> ContainerSpec:
    """The run's Fast DDS discovery server: a container of the bridge image."""
    return ContainerSpec(
        name=f"fieldproof-{short}-discovery",
        image=config.image("bridge"),
        role="discovery",
        command=(
            "fastdds",
            "discovery",
            "--server-id",
            "0",
            "--udp-address",
            "0.0.0.0",  # noqa: S104  (every interface of the container)
            "--udp-port",
            str(DISCOVERY_PORT),
        ),
        env=dict(env),
        labels={**labels, ROLE_LABEL: "discovery"},
        network=network,
        hostname=f"fieldproof-{short}-discovery",
    )


def _plan_links(  # noqa: PLR0917
    scenario: Scenario,
    config: Config,
    vehicles: tuple[PlannedVehicle, ...],
    short: str,
    run_dir: Path,
    env: dict[str, str],
    labels: dict[str, str],
) -> PlannedLinks:
    """The radio network, the onboard networks, the link holders and the ground station."""
    network = scenario.network
    if network is None:  # the caller checks
        raise ScenarioError("network: no radio links in this scenario")
    radio = f"fieldproof-{short}-radio"
    network_labels = {RUN_LABEL: labels[RUN_LABEL]}
    onboard = {v.name: f"fieldproof-{short}-onboard-{v.name}" for v in vehicles}
    holders = {
        v.name: ContainerSpec(
            name=f"fieldproof-{short}-link-{v.name}",
            image=config.image("bridge"),
            role="link",
            command=HOLDER_COMMAND,
            env=dict(env),
            labels={**labels, ROLE_LABEL: "link", VEHICLE_LABEL: v.name},
            network=onboard[v.name],
            networks=(radio,),
            aliases=(v.name,),
            hostname=v.name,
            user="0:0",
            cap_add=("NET_ADMIN",),
        )
        for v in vehicles
    }
    heartbeats: dict[str, dict[str, Any] | None] = {}
    for v in vehicles:
        link = v.manifest.runtime.link if v.manifest.runtime else None
        beat = link.heartbeat if link else None
        heartbeats[v.name] = (
            {"topic": v.topic(beat.topic), "rate_hz": beat.rate_hz} if beat else None
        )
    ground_spec = ContainerSpec(
        name=f"fieldproof-{short}-ground",
        image=config.image("bridge"),
        role="ground",
        command=(
            "python3",
            GROUND_IN_CONTAINER,
            "--config",
            f"{RUN_DIR_IN_CONTAINER}/{GROUND_CONFIG}",
        ),
        # As the recorder: the engine's user, so what it writes belongs to whoever ran the run.
        env={**env, "HOME": "/tmp", "ROS_LOG_DIR": "/tmp/ros-log"},  # noqa: S108  (in-container)
        working_dir=RUN_DIR_IN_CONTAINER,
        labels={**labels, ROLE_LABEL: "ground"},
        network=radio,
        hostname=GROUND_HOSTNAME,
        mounts=(
            Mount(GROUND_FILE, GROUND_IN_CONTAINER),
            Mount(run_dir, RUN_DIR_IN_CONTAINER, read_only=False),
        ),
        user=f"{os.getuid()}:{os.getgid()}",
    )
    return PlannedLinks(
        radio=radio,
        onboard=onboard,
        holders=holders,
        ground=ground_spec,
        profiles=network.known(),
        links={v.name: network.link_profile(v.name) for v in vehicles},
        heartbeats=heartbeats,
        verify=network.verify,
        labels=network_labels,
    )


def _behind_link(vehicle: PlannedVehicle, links: PlannedLinks, sim_host: str) -> PlannedVehicle:
    """The vehicle in its link holder's network namespace, its bridge on its onboard network.

    The bridge that publishes `/clock` joins every onboard network, so each vehicle has exactly
    one clock publisher within reach. On two networks the bridge must tell gz-transport which
    address to advertise (:data:`GZ_IP_WRAPPER`).
    """
    onboard = links.onboard[vehicle.name]
    bridge_networks = (
        (onboard, *[n for n in links.onboard.values() if n != onboard])
        if vehicle.clock
        else (onboard,)
    )
    bridge = vehicle.bridge
    return replace(
        vehicle,
        bridge=replace(
            bridge,
            networks=bridge_networks,
            command=("bash", "-c", GZ_IP_WRAPPER, "fieldproof-bridge-gz-ip", *bridge.command),
            env={**bridge.env, "FIELDPROOF_SIM_HOST": sim_host},
        ),
        container=replace(
            vehicle.container,
            network=None,
            hostname=None,
            network_mode=f"container:{links.holders[vehicle.name].name}",
        ),
    )


def _plan_assertion(
    world: WorldInfo, by_name: dict[str, PlannedVehicle], assertion: Assertion, index: int
) -> PlannedAssertion:
    """Resolve an assertion's target, topic and model against the plan."""
    where = f"assertions[{index}] ({assertion.name})"
    if isinstance(assertion, ReachPose):
        vehicle = by_name[assertion.vehicle]
        target = place(assertion.target, world, f"{where}.target")
        return PlannedAssertion(assertion, target=target, model=vehicle.name)
    if isinstance(assertion, NoCollision):
        vehicle = by_name[assertion.vehicle]
        topic = assertion.topic or DEFAULT_CONTACTS_TOPIC
        return PlannedAssertion(assertion, topic=vehicle.topic(topic), model=vehicle.name)
    if isinstance(assertion, TopicRate | TopicValue):
        if assertion.vehicle is not None:
            topic = by_name[assertion.vehicle].topic(assertion.topic)
        else:
            topic = assertion.topic if assertion.topic.startswith("/") else f"/{assertion.topic}"
        return PlannedAssertion(assertion, topic=topic)
    return PlannedAssertion(assertion)


def _plan_vehicle(  # noqa: PLR0917
    scenario: Scenario,
    config: Config,
    world: WorldInfo,
    vehicle: Vehicle,
    short: str,
    network: str,
    env: dict[str, str],
    labels: dict[str, str],
    *,
    clock: bool = True,
) -> PlannedVehicle:
    where = f"vehicles[{vehicle.name}]"
    manifest_path = (scenario.directory / vehicle.manifest).resolve()
    try:
        manifest = load_vehicle(manifest_path)
    except ManifestError as error:
        raise ScenarioError(f"{where}.manifest: {error}") from error
    try:
        in_workspace = manifest_path.relative_to(config.root)
    except ValueError as error:
        raise ScenarioError(
            f"{where}.manifest: {manifest_path} is outside the workspace {config.root}, which "
            f"the bridge container mounts at {WORKSPACE}"
        ) from error
    runtime = manifest.runtime
    description = manifest.description
    if runtime is None or description is None:
        raise ScenarioError(f"{where}.manifest: {manifest_path} has no runtime or description")
    if description.launch is None:
        raise ScenarioError(f"{where}.manifest: description.launch names no spawn launch file")
    spawn = place(vehicle.spawn, world, f"{where}.spawn")
    launch_file = manifest.directory / description.launch
    launch: tuple[str, ...]
    if launch_file.is_file():
        launch = (f"{WORKSPACE}/{launch_file.resolve().relative_to(config.root)}",)
    else:
        launch = (ROBOTS_PACKAGE, description.launch)
    namespace, prefix = vehicle.namespace, vehicle.frame_prefix
    entrypoint = runtime.entrypoint
    # A launch that cannot be told its namespace runs at the root while its bridge would not:
    # refuse now rather than watch the readiness gate time out.
    if entrypoint.launch is not None:
        for value, arg, key in (
            (namespace, entrypoint.launch.namespace_arg, "namespace"),
            (prefix, entrypoint.launch.frame_prefix_arg, "frame_prefix"),
        ):
            if value and arg is None:
                raise ScenarioError(
                    f"{where}.{key}: {manifest.name}'s entrypoint launch names no {key}_arg, so "
                    f"its stack cannot be told its {key} (runtime.entrypoint.launch.{key}_arg)"
                )
    # ros2 launch rejects an empty value, and the launch files default to the root.
    namespace_args = (f"namespace:={namespace}",) if namespace else ()
    prefix_args = (f"frame_prefix:={prefix}",) if prefix else ()
    spawn_job = ContainerSpec(
        name=f"fieldproof-{short}-spawn-{vehicle.name}",
        image=config.image("bridge"),
        role="spawn",
        command=(
            "ros2",
            "launch",
            *launch,
            f"name:={vehicle.name}",
            *namespace_args,
            *prefix_args,
            f"world:={world.name}",
            f"x:={spawn.x:g}",
            f"y:={spawn.y:g}",
            f"z:={spawn.z + SPAWN_HEIGHT_M:g}",
            f"yaw:={spawn.yaw:g}",
            "use_sim_time:=true",
            "robot_state_publisher:=false",
        ),
        env=dict(env),
        labels={**labels, ROLE_LABEL: "spawn", VEHICLE_LABEL: vehicle.name},
        network=network,
        mounts=(Mount(config.root, WORKSPACE),),
    )
    bridge = ContainerSpec(
        name=f"fieldproof-{short}-bridge-{vehicle.name}",
        image=config.image("bridge"),
        role="bridge",
        command=(
            BRIDGE_COMMAND,
            *namespace_args,
            *prefix_args,
            # One bridge of the run publishes the simulation clock; the others leave it alone.
            *(() if clock else ("clock:=false",)),
            f"bundle_out:={BUNDLE_DIR_IN_BRIDGE}",
        ),
        env={**env, "FIELDPROOF_MANIFEST": f"{WORKSPACE}/{in_workspace}"},
        labels={**labels, ROLE_LABEL: "bridge", VEHICLE_LABEL: vehicle.name},
        network=network,
        hostname=f"fieldproof-{short}-bridge-{vehicle.name}",
        mounts=(Mount(config.root, WORKSPACE),),
    )
    resources = (runtime.resources or Resources()).merged(vehicle.resources)
    mounts: tuple[Mount, ...] = ()
    dds_profile: Path | None = None
    dds_env: dict[str, str] = {}
    if runtime.dds_profile is not None:
        dds_profile = (manifest.directory / runtime.dds_profile).resolve()
        if not dds_profile.is_file():
            raise ScenarioError(
                f"{where}.manifest: runtime.dds_profile: {dds_profile} is not a file"
            )
        mounts = (Mount(dds_profile, DDS_PROFILE_IN_CONTAINER),)
        try:
            dds_env = runtime.dds_profile_env()
        except ManifestError as error:
            raise ScenarioError(f"{where}.manifest: runtime.{error}") from error
    # The vehicle's command: the image's own (the manifest's entrypoint) unless the vehicle has
    # a namespace or frame prefix to pass to its launch.
    command = (
        tuple(entrypoint.argv(namespace=namespace, frame_prefix=prefix))
        if (namespace or prefix) and entrypoint.launch is not None
        else ()
    )
    container = ContainerSpec(
        name=f"fieldproof-{short}-{vehicle.name}",
        image=vehicle.image or runtime.image,
        role="vehicle",
        command=command,
        env={
            **runtime.env,
            **{k: v for k, v in env.items() if k != "GZ_PARTITION"},
            "FIELDPROOF_NAMESPACE": namespace,
            "FIELDPROOF_FRAME_PREFIX": prefix,
            **dds_env,
            **vehicle.env,
        },
        labels={**labels, ROLE_LABEL: "vehicle", VEHICLE_LABEL: vehicle.name},
        network=network,
        hostname=vehicle.name,
        mounts=mounts,
        cpus=resources.cpus,
        cpuset=resources.cpuset,
        memory=resources.memory,
        pids=resources.pids,
    )
    return PlannedVehicle(
        vehicle=vehicle,
        manifest=manifest,
        spawn=spawn,
        spawn_job=spawn_job,
        bridge=bridge,
        container=container,
        ready_timeout_s=runtime.ready.timeout_s if runtime.ready else 0.0,
        resources=resources,
        depends_on=tuple(vehicle.depends_on),
        dds_profile=dds_profile,
        clock=clock,
    )


# ---------------------------------------------------------------------------------- the log
def utc_now() -> str:
    """The current time as ISO 8601 with milliseconds, UTC."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class RunLog:
    """Structured events: one JSON object per line in the run directory, a line on the console.

    Levels: `debug` (the output of commands run in the containers, dim on the console),
    `info`, `warning`, `error`.
    """

    def __init__(
        self,
        run_id: str,
        *,
        file: Path | None = None,
        console: Console | None = None,
        clock: Callable[[], float] = time.monotonic,
        verbose: bool = True,
    ) -> None:
        self.run_id = run_id
        self.console = console
        self.clock = clock
        self.verbose = verbose
        self.started = clock()
        self.events: list[dict[str, Any]] = []
        self._file: IO[str] | None = None
        # Events arrive from the thread that streams an action's output as well.
        self._lock = threading.Lock()
        if file is not None:
            file.parent.mkdir(parents=True, exist_ok=True)
            self._file = file.open("a")

    def event(self, event: str, message: str = "", *, level: str = "info", **fields: Any) -> None:
        """Record one event."""
        record = {
            "ts": utc_now(),
            "t": round(self.clock() - self.started, 3),
            "run_id": self.run_id,
            "level": level,
            "event": event,
            "message": message,
            **fields,
        }
        with self._lock:
            self.events.append(record)
            if self._file is not None:
                self._file.write(json.dumps(record, sort_keys=False) + "\n")
                self._file.flush()
            if self.console is not None and (level != "debug" or self.verbose):
                style = {"debug": "dim", "warning": "yellow", "error": "bold red"}.get(level, "")
                text = message if message else " ".join(f"{k}={v}" for k, v in fields.items())
                line = f"{record['t']:8.1f}s  {event:<18} {text}"
                self.console.print(line, style=style, highlight=False, soft_wrap=True, markup=False)

    def close(self) -> None:
        """Close the file."""
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


# ------------------------------------------------------------------------------- the record
@dataclass
class RunRecord:
    """`run.json`: what the run was and how it went."""

    run_id: str
    run_dir: str
    scenario: dict[str, Any]
    engine_version: str
    started_at: str
    phase: str = Phase.CREATED
    status: str | None = None
    reason: str | None = None
    finished_at: str | None = None
    phases: list[dict[str, Any]] = field(default_factory=list)
    vehicles: list[dict[str, Any]] = field(default_factory=list)
    """Per vehicle: name, manifest, namespace, frame prefix, image, the budget applied, what it
    depends on, its DDS profile, and the contract check's counts when one ran."""
    containers: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[ActionRecord] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    observation: dict[str, Any] = field(default_factory=dict)
    """The observer's stream: samples, simulation time covered, problems."""
    video: dict[str, Any] | None = None
    """The video of the run (`outputs.video`): camera, file, bytes, status."""
    network: dict[str, Any] | None = None
    """The radio links (`network`): networks, holders, the ground station, and per link the
    periods between profile changes with what the ground station measured in each."""
    time: dict[str, Any] = field(default_factory=dict)
    stack: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The record as data."""
        return asdict(self)

    def write(self, path: Path) -> None:
        """Write the record as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")


WAITING = "waiting"
NOT_STARTED = ("pending", WAITING)


class RunContext:
    """What the events act through during a run (:class:`fieldproof.events.EventContext`)."""

    def __init__(self, run: Run) -> None:
        self._run = run

    @property
    def backend(self) -> Backend:
        return self._run.backend

    @property
    def tearing_down(self) -> bool:
        return self._run.tearing_down

    def vehicle(self, name: str | None) -> PlannedVehicle:
        if name is None:
            raise RunError("the entry names no vehicle")
        return self._run.plan.vehicle(name)

    def exec(
        self, container: str, argv: Sequence[str], on_line: LineSink | None = None
    ) -> ExecResult:
        return self._run.backend.exec(container, argv, on_line)

    def ros2(
        self,
        vehicle: VehicleHandle,
        argv: Sequence[str],
        *,
        timeout_s: float,
        on_line: LineSink | None = None,
    ) -> ExecResult:
        command = (ENTRYPOINT_IN_IMAGE, "timeout", "-s", "INT", f"{max(timeout_s, 1.0):.0f}")
        return self._run.backend.exec(vehicle.container_name, (*command, "ros2", *argv), on_line)

    def log(self, event: str, message: str = "", *, level: str = "info", **fields: Any) -> None:
        self._run.log.event(event, message, level=level, **fields)

    def clock(self) -> float:
        return self._run.clock()

    def hold(self, seconds: float) -> bool:
        run = self._run
        deadline = run.clock() + seconds
        while not run.tearing_down and run.clock() < deadline:
            run.sleep(min(EVAL_POLL_S, max(deadline - run.clock(), 0.0)))
        return not run.tearing_down

    def expect_exit(self, container: str) -> None:
        self._run.expect_exit(container)

    def mark_paused(self, container: str, paused: bool) -> None:
        self._run.mark_paused(container, paused)

    def apply_link(self, vehicle: str, profile: str, cause: str) -> None:
        self._run.apply_link(vehicle, profile, cause)

    def link_profile(self, vehicle: str) -> str:
        return self._run.configured_profile(vehicle)


# ---------------------------------------------------------------------------------- the run
class Run:
    """One execution of a plan against a backend."""

    def __init__(
        self,
        plan: Plan,
        backend: Backend,
        log: RunLog,
        *,
        keep: bool = False,
        check_contracts: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        executor: Executor | None = None,
    ) -> None:
        self.plan = plan
        self.backend = backend
        self.log = log
        self.keep = keep
        self.check_contracts = check_contracts
        """Run each bridge's contract check before the recorder starts."""
        self.sleep = sleep
        self.clock = clock
        self.wall = wall
        """The wall clock the observer's samples are stamped with (epoch seconds)."""
        # One worker per vehicle, so the fleet's spawns, bridges and vehicles proceed at once (a
        # vehicle waiting for the ones it depends on holds its worker), and one per timeline
        # entry, so actions run concurrently while the main thread observes the run.
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max(1, len(plan.vehicles), len(plan.actions)),
            thread_name_prefix="fieldproof-worker",
        )
        self._lock = threading.Lock()
        """Guards the record, which the workers add to while the main thread writes it."""
        self.tearing_down = False
        self.evaluator = Evaluator(plan.assertions)
        self.record = RunRecord(
            run_id=plan.run_id,
            run_dir=str(plan.run_dir),
            scenario={
                "name": plan.scenario.name,
                "path": str(plan.scenario.path),
                "digest": plan.scenario.digest,
            },
            engine_version=__version__,
            started_at=utc_now(),
            vehicles=[{**v.describe(), "contract": None} for v in plan.vehicles],
            assertions=[r.to_dict() for r in self.evaluator.results],
            time=plan.scenario.time.model_dump(),
            stack={
                "network": plan.network,
                "render": plan.render,
                "ros_domain_id": plan.domain_id,
                "domain_source": plan.domain_source,
                "discovery": plan.discovery_mode,
                "world": plan.world.name,
                "images": sorted({s.image for s in plan.containers()}),
            },
        )
        self._phase_started: float | None = None
        self.monitor = LinkMonitor(plan.links.links) if plan.links is not None else None
        """The links' profiles and measurements (radio links only)."""
        self._addresses: dict[str, str] = {}
        """Per vehicle, its link holder's address on the radio network."""
        self._network = FileTail(plan.run_dir / NETWORK_FILE)
        self._ground_ready = False
        if plan.links is not None:
            self.record.network = plan.links.to_dict()
        self._samples = FileTail(plan.run_dir / SAMPLES_FILE)
        self._observer_errors = 0
        self._last_alive_check = -math.inf
        self.schedule = Schedule(plan.actions)
        """When each timeline entry's occurrences start."""
        self.context = RunContext(self)
        """What the events act through."""
        self._expected_exits: set[str] = set()
        """Containers an event killed: their exit does not end the run."""
        self._paused: set[str] = set()
        """Containers an event paused: the teardown resumes them before stopping them."""

    # ------------------------------------------------------------------------- the machine
    def execute(self) -> RunRecord:
        """Run every phase; tear down whatever happens; return the record."""
        scenario = self.plan.scenario
        self.plan.run_dir.mkdir(parents=True, exist_ok=True)
        if scenario.path is not None:
            (self.plan.run_dir / SCENARIO_COPY).write_text(scenario.path.read_text())
        self._write()
        self.log.event(
            "run.start",
            f"{scenario.name} ({self.plan.run_id}) in {self.plan.run_dir}",
            scenario=str(scenario.path),
        )
        try:
            self._phase(Phase.PREPARING)
            self.prepare()
            self._phase(Phase.SIM)
            self.start_discovery()
            self.start_sim()
            self._phase(Phase.SPAWN)
            self._parallel("spawn", [(v.name, v, self.spawn) for v in self.plan.vehicles])
            self._phase(Phase.BRIDGES)
            self._parallel("bridge", [(v.name, v, self.start_bridge) for v in self.plan.vehicles])
            self._phase(Phase.RECORDER)
            self.start_recorder()
            self.start_links()
            self._phase(Phase.VEHICLES)
            self.start_vehicles()
            self._phase(Phase.RUNNING)
            self.run_timeline()
            self.check_alive()
            self._finish(Status.PASSED, None)
        except KeyboardInterrupt:
            self._finish(Status.INTERRUPTED, "interrupted (Ctrl-C or SIGTERM)")
        except RunTimeoutError as error:
            self._finish(Status.TIMEOUT, str(error))
        except RunFailedError as error:
            self._finish(Status.FAILED, str(error))
        except (RunError, BackendError, ScenarioError, ManifestError) as error:
            self._finish(Status.ERROR, str(error))
        except Exception as error:
            self._finish(Status.ERROR, f"{type(error).__name__}: {error}")
            raise
        finally:
            self._phase(Phase.TEARDOWN)
            self.teardown()
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._phase(Phase.FINISHED)
            self.record.finished_at = utc_now()
            self._write()
            self.write_report()
            self.log.event(
                "run.end",
                f"{self.record.status}: {self.record.reason or scenario.name}",
                status=self.record.status,
                run_dir=str(self.plan.run_dir),
            )
            self.log.close()
        return self.record

    def _phase(self, phase: Phase) -> None:
        now = self.clock()
        if self.record.phases and self.record.phases[-1].get("finished_at") is None:
            last = self.record.phases[-1]
            last["finished_at"] = utc_now()
            last["duration_s"] = round(now - (self._phase_started or now), 3)
        self.record.phase = phase
        self.record.phases.append(
            {"name": str(phase), "started_at": utc_now(), "finished_at": None}
        )
        self._phase_started = now
        self.log.event("phase", str(phase), phase=str(phase))
        self._write()

    def _finish(self, status: Status, reason: str | None) -> None:
        self.record.status = status
        self.record.reason = reason
        level = "info" if status is Status.PASSED else "error"
        self.log.event("run.status", reason or str(status), level=level, status=str(status))
        for action in self.record.timeline:
            if action.status == "running":
                action.status, action.detail = "cancelled", f"the run ended {status}: {reason}"
        self._resolve(self.evaluator.skip_pending(f"the run ended {status}: {reason}"))
        self.record.observation = {
            "samples": self.evaluator.samples,
            "sim_elapsed_s": self.evaluator.sim_elapsed(),
            "unparsed_lines": self.evaluator.bad_lines,
            "observer_errors": self._observer_errors,
        }
        self.collect_traffic()
        self._record_links()

    def _write(self) -> None:
        with self._lock:
            self.record.write(self.plan.run_dir / RECORD_FILE)

    def _started(self, spec: ContainerSpec) -> None:
        with self._lock:
            self.record.containers.append(
                {
                    "role": spec.role,
                    "name": spec.name,
                    "image": spec.image,
                    "vehicle": spec.labels.get(VEHICLE_LABEL),
                    "started_at": utc_now(),
                }
            )
        self.log.event(f"{spec.role}.start", f"{spec.name} ({spec.image})", container=spec.name)

    def _vehicle_record(self, name: str) -> dict[str, Any]:
        return next(v for v in self.record.vehicles if v["name"] == name)

    def _parallel(
        self,
        label: str,
        tasks: Sequence[tuple[str, PlannedVehicle, Callable[[PlannedVehicle], None]]],
    ) -> None:
        """Run one step for every vehicle at once on the workers and wait for all of them.

        Every task runs to its end (or its own failure) before the first failure, in the
        vehicles' order, is raised; the others are logged.
        """
        futures = [(name, self._executor.submit(fn, vehicle)) for name, vehicle, fn in tasks]
        failures: list[tuple[str, BaseException]] = []
        for name, future in futures:
            try:
                future.result()
            except Exception as error:
                failures.append((name, error))
        if not failures:
            return
        for name, failure in failures[1:]:
            self.log.event(f"{label}.failed", f"{name}: {failure}", level="error", vehicle=name)
        raise failures[0][1]

    def wait_for(self, predicate: Callable[[], bool], timeout_s: float, what: str) -> None:
        """Poll `predicate` until it holds or `timeout_s` passes (a :class:`RunError`)."""
        deadline = self.clock() + timeout_s
        while not predicate():
            if self.clock() >= deadline:
                raise RunError(f"timed out after {timeout_s:.0f} s waiting for {what}")
            self.sleep(POLL_S)

    def _running(self, spec: ContainerSpec) -> None:
        """Raise unless the container is still running (with the tail of its log)."""
        state = self.backend.state(spec.name)
        if state.running or spec.name in self._expected_exits:
            return
        tail = "\n".join(self.backend.logs(spec.name).rstrip().splitlines()[-20:])
        message = f"{spec.role} container {spec.name} exited ({state.exit_code})"
        if tail:
            message += f":\n{tail}"
        if spec.role == "vehicle":
            raise RunFailedError(message)
        raise RunError(message)

    # ---------------------------------------------------------------------------- phases
    def prepare(self) -> None:
        """Refuse what cannot run yet; check images, assets and the budget; make the network."""
        scenario = self.plan.scenario
        if scenario.time.mode == "realtime":
            raise RunError("time.mode realtime: the real-time-locked mode arrives with task P2-T04")
        if not OBSERVER_FILE.is_file():
            raise RunError(f"the observer {OBSERVER_FILE} is missing from the engine")
        images = sorted(
            {s.image for s in self.plan.containers()}
            | {v.spawn_job.image for v in self.plan.vehicles}
        )
        missing = [image for image in images if not self.backend.image_exists(image)]
        if missing:
            raise RunError(
                "image(s) not built: " + ", ".join(missing) + " (make images, make vehicle-image)"
            )
        self.log.event("images.ok", ", ".join(images))
        self.check_assets()
        self.check_budget()
        self.backend.ensure_network(self.plan.network)
        links = self.plan.links
        if links is not None:
            if not GROUND_FILE.is_file():
                raise RunError(f"the ground station {GROUND_FILE} is missing from the engine")
            for name in links.networks():
                self.backend.ensure_network(name, links.labels)
            self.log.event(
                "network.links",
                f"radio network {links.radio}, {len(links.onboard)} onboard network(s); "
                + "; ".join(f"{name}: {profile}" for name, profile in links.links.items()),
                radio=links.radio,
                links=dict(links.links),
            )
        self.log.event(
            "network.ok",
            f"{self.plan.network}, ROS_DOMAIN_ID {self.plan.domain_id} "
            f"({self.plan.domain_source}), discovery by {self.plan.discovery_mode}",
            network=self.plan.network,
            ros_domain_id=self.plan.domain_id,
            discovery=self.plan.discovery_mode,
        )

    def check_budget(self) -> None:
        """Log every vehicle's budget and warn when the fleet asks for more than the host has."""
        host = self.backend.host()
        cpus = sum(v.resources.cpus or 0.0 for v in self.plan.vehicles)
        memory = sum(
            memory_bytes(v.resources.memory) for v in self.plan.vehicles if v.resources.memory
        )
        budgets = "; ".join(
            f"{v.name}: "
            + (", ".join(f"{k} {val}" for k, val in v.resources.to_dict().items()) or "unlimited")
            for v in self.plan.vehicles
        )
        self.log.event(
            "budget",
            f"{len(self.plan.vehicles)} vehicle(s) ask for {cpus:g} CPU(s) and "
            f"{memory / 1024**3:.1f} GiB of the host's {host.cpus} CPU(s) and "
            f"{host.memory_bytes / 1024**3:.1f} GiB ({budgets})",
            cpus=cpus,
            memory_bytes=memory,
            host_cpus=host.cpus,
            host_memory_bytes=host.memory_bytes,
        )
        if host.cpus and cpus > host.cpus:
            self.log.event(
                "budget.oversubscribed",
                f"the vehicles' CPU quotas ({cpus:g}) exceed the host's {host.cpus} CPU(s): the "
                "simulator and the bridges share what is left, expect a low real-time factor",
                level="warning",
            )
        if host.memory_bytes and memory > host.memory_bytes:
            self.log.event(
                "budget.oversubscribed",
                f"the vehicles' memory limits ({memory / 1024**3:.1f} GiB) exceed the host's "
                f"{host.memory_bytes / 1024**3:.1f} GiB",
                level="warning",
            )

    def start_discovery(self) -> None:
        """Start the run's discovery server (`discovery: server`) and wait until it listens."""
        server = self.plan.discovery
        if server is None:
            return
        self.backend.start(server)
        self._started(server)

        def listening() -> bool:
            self._running(server)
            return DISCOVERY_READY in self.backend.logs(server.name)

        self.wait_for(listening, self.plan.config.timeouts.discovery_s, "the discovery server")
        self.log.event(
            "discovery.ready",
            f"Fast DDS discovery server at {server.hostname}:{DISCOVERY_PORT}",
            server=f"{server.hostname}:{DISCOVERY_PORT}",
        )

    def check_assets(self) -> None:
        """Every Fuel model the world uses is fetched (`make assets`), if there is a lock."""
        config = self.plan.config
        lock_file = config.resolve(config.stack.fuel_lock)
        if lock_file.is_file():
            lock = yaml.safe_load(lock_file.read_text()) or {}
            fuel_dir = config.resolve(config.stack.fuel)
            missing_models = [
                str(model.get("dir"))
                for model in lock.get("models", [])
                if (not model.get("used_by") or self.plan.world.name in model["used_by"])
                and not (fuel_dir / str(model.get("dir"))).is_dir()
            ]
            if missing_models:
                raise RunError(
                    f"Fuel model(s) not fetched under {fuel_dir}: "
                    + ", ".join(missing_models)
                    + " (make assets)"
                )

    def start_sim(self) -> None:
        """Start the simulator and wait until the world's services answer."""
        sim, world, timeouts = self.plan.sim, self.plan.world, self.plan.config.timeouts
        self.backend.start(sim)
        self._started(sim)

        def world_ready() -> bool:
            self._running(sim)
            result = self.backend.exec(
                sim.name, ("bash", "-lc", f"timeout {PROBE_LIMIT_S} gz service -l")
            )
            return f"/world/{world.name}/create" in result.output.split()

        self.wait_for(world_ready, timeouts.world_s, f"the services of world {world.name}")
        self.log.event("sim.ready", f"world {world.name} is up", world=world.name)
        rtf = self.plan.scenario.time.rtf
        if rtf is not None:
            result = self.backend.exec(
                sim.name,
                (
                    "bash",
                    "-lc",
                    f"timeout {PROBE_LIMIT_S} gz service -s /world/{world.name}/set_physics "
                    "--reqtype gz.msgs.Physics --reptype gz.msgs.Boolean --timeout 5000 "
                    f"--req 'real_time_factor: {rtf:g}'",
                ),
            )
            if not result.ok or "true" not in result.output:
                raise RunError(f"set_physics (real_time_factor {rtf:g}) failed:\n{result.tail()}")
            self.log.event("sim.rtf", f"target real-time factor {rtf:g}", rtf=rtf)

    def spawn(self, vehicle: PlannedVehicle) -> None:
        """Spawn the vehicle's model with a one-shot container of the bridge image."""
        job = vehicle.spawn_job
        self.log.event(
            "spawn.start",
            f"{vehicle.name} at ({vehicle.spawn.x:g}, {vehicle.spawn.y:g}, "
            f"yaw {vehicle.spawn.yaw:g})",
            vehicle=vehicle.name,
            command=shlex.join(job.command),
        )
        result = self.backend.run_to_completion(job, self.plan.config.timeouts.spawn_s)
        if not result.ok:
            raise RunError(f"{vehicle.name}: the spawn exited {result.exit_code}:\n{result.tail()}")
        self.log.event("spawn.done", f"{vehicle.name} spawned", vehicle=vehicle.name)

    def start_bridge(self, vehicle: PlannedVehicle) -> None:
        """Start the vehicle's bridge and wait until it publishes the odometry topic."""
        bridge = vehicle.bridge
        self.backend.start(bridge)
        self._started(bridge)
        drive = vehicle.manifest.drive
        odom = vehicle.topic(drive.odometry.ros_topic) if drive is not None else None

        def bridge_ready() -> bool:
            self._running(bridge)
            if odom is None:
                return True
            result = self.backend.exec(
                bridge.name,
                (BRIDGE_ENTRYPOINT, "timeout", str(PROBE_LIMIT_S), "ros2", "topic", "list"),
            )
            return odom in result.output.split()

        self.wait_for(
            bridge_ready,
            self.plan.config.timeouts.bridge_s,
            f"the bridge of {vehicle.name} ({odom})",
        )
        self.log.event("bridge.ready", f"{vehicle.name}: {odom} is published", vehicle=vehicle.name)
        if self.check_contracts:
            self.check_contract(vehicle)

    def check_contract(self, vehicle: PlannedVehicle) -> None:
        """Run the bridge's contract check inside the bridge container (`--check-contracts`).

        `contract_check` subscribes to every topic of the bundle's `contract.yaml` and reports
        the graph, QoS, frames, rates and stray topics; a failure is a :class:`RunError`, since
        the bridge is Fieldproof's own promise, not the stack under test.
        """
        bridge = vehicle.bridge
        timeout_s = self.plan.config.timeouts.contract_s
        self.log.event(
            "contract.start", f"{vehicle.name}: checking the bridge contract", vehicle=vehicle.name
        )
        result = self.backend.exec(
            bridge.name,
            (
                BRIDGE_ENTRYPOINT,
                "timeout",
                f"{timeout_s + 30:.0f}",
                *CONTRACT_CHECK,
                vehicle.bundle_dir,
                "--timeout",
                f"{timeout_s:g}",
            ),
            lambda line: self.log.event(
                "contract.output", line.rstrip(), level="debug", vehicle=vehicle.name
            ),
        )
        lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        summary = next(
            (
                CONTRACT_SUMMARY.match(line)
                for line in reversed(lines)
                if CONTRACT_SUMMARY.match(line)
            ),
            None,
        )
        counts = {
            "passed": int(summary.group(1)) if summary else None,
            "failed": int(summary.group(2)) if summary else None,
            "exit_code": result.exit_code,
            "findings": [line for line in lines if line.startswith("FAIL")],
        }
        with self._lock:
            self._vehicle_record(vehicle.name)["contract"] = counts
        self._write()
        if not result.ok:
            raise RunError(
                f"{vehicle.name}: the bridge contract check exited {result.exit_code}:\n"
                + result.tail()
            )
        self.log.event(
            "contract.ok",
            f"{vehicle.name}: {counts['passed']} check(s) passed",
            vehicle=vehicle.name,
            passed=counts["passed"],
        )

    def start_recorder(self) -> None:
        """Start the recorder container on the run directory and wait for the observer's `ready`."""
        recorder = self.plan.recorder
        run_dir = self.plan.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / OBSERVE_FILE).write_text(json.dumps(self.plan.observe, indent=2) + "\n")
        # The files exist, owned by the engine's user, before the container mounts the directory.
        (run_dir / SAMPLES_FILE).touch()
        (run_dir / ASSERTIONS_FILE).touch()
        self.backend.start(recorder)
        self._started(recorder)
        record = self.plan.observe["record"]
        self.log.event(
            "observer.start",
            f"recording {len(record['topics']) or 'all'} topic(s) to MCAP"
            if record["enabled"]
            else "observing only (outputs.record is off)",
            recording=record["enabled"],
        )
        seen: dict[str, Any] = {}

        def observer_ready() -> bool:
            self._running(recorder)
            for sample in self._poll_samples():
                if sample.kind == "ready":
                    seen.update(sample.data)
            return bool(seen)

        self.wait_for(
            observer_ready, self.plan.config.timeouts.recorder_s, "the run's observer to be ready"
        )
        waiting = seen.get("waiting") or []
        self.log.event(
            "recorder.ready",
            "observer ready"
            + (", recording" if seen.get("recording") else "")
            + (f"; waiting for topic(s) {', '.join(waiting)}" if waiting else ""),
            subscribed=seen.get("subscribed"),
            waiting=waiting,
        )

    def _poll_samples(self) -> list[Sample]:
        """Read the observer's new lines, feed the evaluator, report its errors."""
        lines = self._samples.lines()
        samples: list[Sample] = []
        for line in lines:
            try:
                sample = Sample.from_line(line)
            except ValueError:
                self.evaluator.bad_lines += 1
                continue
            samples.append(sample)
            if sample.kind == "error":
                self._observer_errors += 1
                self.log.event(
                    "observer.error", str(sample.data.get("message", "")), level="warning"
                )
            self._resolve(self.evaluator.feed(sample))
        return samples

    def _resolve(self, results: list[Result]) -> None:
        """Record the assertions that just resolved."""
        if not results:
            return
        by_name = {a["name"]: index for index, a in enumerate(self.record.assertions)}
        with (self.plan.run_dir / ASSERTIONS_FILE).open("a") as verdicts:
            for result in results:
                data = result.to_dict()
                if result.name in by_name:
                    self.record.assertions[by_name[result.name]] = data
                else:
                    self.record.assertions.append(data)
                verdicts.write(json.dumps({"run_id": self.plan.run_id, **data}) + "\n")
                level = {FAILED: "error", "passed": "info"}.get(result.status, "warning")
                self.log.event(
                    f"assertion.{result.status}",
                    f"{result.name}: {result.reason}",
                    level=level,
                    assertion=result.name,
                    type=result.type,
                    t_s=result.t_s,
                )
        self._write()

    def _observe(self) -> list[Sample]:
        """One poll of the running phase: the stream, the deadlines, the containers."""
        samples = self._poll_samples()
        self._poll_network()
        self._resolve(self.evaluator.tick(self.wall()))
        if self.clock() - self._last_alive_check >= POLL_S:
            self._last_alive_check = self.clock()
            self.check_alive()
        return samples

    # ---------------------------------------------------------------------------- radio links
    def start_links(self) -> None:
        """The `network` phase of radio links: every link holder with its profile, the ground."""
        if self.plan.links is None:
            return
        self._phase(Phase.NETWORK)
        self._parallel("link", [(v.name, v, self.start_link) for v in self.plan.vehicles])
        self.start_ground()

    def _links(self) -> PlannedLinks:
        if self.plan.links is None:
            raise RunError("this run has no radio links (no network section)")
        return self.plan.links

    def start_link(self, vehicle: PlannedVehicle) -> None:
        """Start the vehicle's link holder and put the link's configured profile on it."""
        links = self._links()
        holder = links.holders[vehicle.name]
        self.backend.start(holder)
        self._started(holder)
        self._running(holder)
        address = self.backend.address(holder.name, links.radio)
        with self._lock:
            self._addresses[vehicle.name] = address
        self.apply_link(vehicle.name, links.links[vehicle.name], "initial")

    def apply_link(self, vehicle: str, profile_name: str, cause: str) -> None:
        """Put a profile (`drop` included) on the vehicle's link, in its holder."""
        links = self._links()
        profile = links.profile(profile_name)
        holder = links.holders[vehicle]
        # The profile changes somewhere during the exec, which can take a while on a loaded host:
        # the previous period ends when the exec starts, the new one begins when it returns.
        since = self.wall()
        result = self.backend.exec(
            holder.name, ("bash", "-c", link_script(self._addresses[vehicle], profile))
        )
        if not result.ok:
            raise RunError(
                f"{vehicle}: cannot put profile {profile_name} on the link (exit "
                f"{result.exit_code}; {LINK_KERNEL_HINT}):\n{result.tail()}"
            )
        device = result.output.strip().splitlines()[0] if result.output.strip() else "?"
        with self._lock:
            if self.monitor is not None:
                self.monitor.change(vehicle, profile_name, profile, self.wall(), cause, since=since)
        self.log.event(
            "link.applied",
            f"{vehicle}: {profile_name} ({profile.summary()}) on {device} of {holder.name}",
            vehicle=vehicle,
            profile=profile_name,
            cause=cause,
        )
        self._record_links()

    def start_ground(self) -> None:
        """Start the ground station and wait for its `ready` line."""
        links = self._links()
        run_dir = self.plan.run_dir
        (run_dir / GROUND_CONFIG).write_text(
            json.dumps(links.ground_config(RUN_DIR_IN_CONTAINER), indent=2) + "\n"
        )
        (run_dir / NETWORK_FILE).touch()
        self.backend.start(links.ground)
        self._started(links.ground)

        def ready() -> bool:
            self._running(links.ground)
            self._poll_network()
            return self._ground_ready

        self.wait_for(ready, self.plan.config.timeouts.ground_s, "the ground station to be ready")
        beats = [b["topic"] for b in links.heartbeats.values() if b]
        self.log.event(
            "ground.ready",
            f"probing {len(links.links)} link(s)"
            + (f", heartbeat on {', '.join(beats)}" if beats else ", no heartbeat"),
            heartbeats=beats,
        )

    def _poll_network(self) -> None:
        """Read the ground station's new lines: its ready line, its errors, the link windows."""
        for line in self._network.lines():
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get("kind")
            if kind == "ready":
                self._ground_ready = True
                if not data.get("probing", True):
                    self.log.event(
                        "ground.warning",
                        "the ground station cannot probe the links",
                        level="warning",
                    )
            elif kind == "error":
                self.log.event("ground.error", str(data.get("message", "")), level="warning")
            elif self.monitor is not None:
                with self._lock:
                    self.monitor.feed(data)

    def _record_links(self) -> None:
        """The links' periods into the record."""
        if self.monitor is None or self.record.network is None:
            return
        with self._lock:
            self.record.network["addresses"] = dict(self._addresses)
            self.record.network["periods"] = self.monitor.to_dict()

    def collect_traffic(self) -> None:
        """What every link carried and dropped (its queue counters), into the record."""
        links = self.plan.links
        if links is None or self.record.network is None:
            return
        counted: dict[str, Any] = {}
        for vehicle, address in sorted(self._addresses.items()):
            holder = links.holders[vehicle]
            try:
                result = self.backend.exec(holder.name, ("bash", "-c", traffic_script(address)))
                counters = traffic(json.loads(result.output)) if result.ok else {}
            except (BackendError, ValueError) as error:
                self.log.event("link.traffic", f"{vehicle}: no counters ({error})", level="warning")
                continue
            counted[vehicle] = counters
            out, inbound = counters.get("out", {}), counters.get("in", {})
            self.log.event(
                "link.traffic",
                f"{vehicle}: {out.get('bytes', 0) / 1e6:.2f} MB out ({out.get('drops', 0)} "
                f"dropped), {inbound.get('bytes', 0) / 1e6:.2f} MB in "
                f"({inbound.get('drops', 0)} dropped)",
                vehicle=vehicle,
                traffic=counters,
            )
        with self._lock:
            self.record.network["traffic"] = counted

    def check_links(self) -> None:
        """Compare every link period's measurement with its profile (`network.verify`)."""
        links = self.plan.links
        if self.monitor is None or links is None:
            return
        self._poll_network()
        self._record_links()
        if links.verify == "off":
            return
        for link in self.monitor.links.values():
            for period in link.periods:
                status, reason = period.verdict()
                level = {LINK_PASSED: "info", LINK_FAILED: "warning"}.get(status, "debug")
                self.log.event(
                    f"link.{status}",
                    f"{link.vehicle}: {period.profile_name} ({period.cause}): {reason}",
                    level=level,
                    vehicle=link.vehicle,
                    profile=period.profile_name,
                )
        failures = self.monitor.failures()
        if failures and links.verify == "fail":
            vehicle, period, reason = failures[0]
            more = f" (and {len(failures) - 1} more)" if len(failures) > 1 else ""
            raise RunFailedError(
                f"link {vehicle} does not match profile {period.profile_name}: {reason}{more}"
            )

    def perform(self, action: PlannedAction, limit_s: float, record: ActionRecord) -> None:
        """Run one occurrence of a timeline entry on a worker: its event's `apply` hook.

        The event only touches its own record, the context and the log (which is thread-safe);
        a record left `running` ends `ok`, an exception ends it `failed`.
        """
        registered = registry()[action.entry.action_name]
        resolved = {**action.resolved, "limit_s": limit_s}
        try:
            registered.event.apply(action.entry, resolved, self.context, record)
        except Exception as error:
            record.status = ACTION_FAILED
            record.detail = f"{type(error).__name__}: {error}"
            raise
        if record.status == RUNNING:
            record.status = OK

    def start_vehicles(self) -> None:
        """Start every vehicle on the workers, each once the ones it depends on are ready."""
        futures: dict[str, Future[None]] = {}

        def task(vehicle: PlannedVehicle) -> None:
            for name in vehicle.depends_on:
                try:
                    futures[name].result()
                except Exception as error:
                    raise RunFailedError(
                        f"{vehicle.name}: not started, it depends on {name}, which failed ({error})"
                    ) from error
                self.log.event(
                    "vehicle.dependency", f"{vehicle.name}: {name} is ready", vehicle=vehicle.name
                )
            self.start_vehicle(vehicle)

        # Dependencies are submitted before their dependents, so a synchronous executor (the
        # tests') runs them in a valid order and a thread pool never waits on a task it has
        # not been given.
        ordered = self.plan.start_order()
        for vehicle in ordered:
            futures[vehicle.name] = self._executor.submit(task, vehicle)
        failures: list[tuple[str, BaseException]] = []
        for vehicle in ordered:
            try:
                futures[vehicle.name].result()
            except Exception as error:
                failures.append((vehicle.name, error))
        if not failures:
            return
        for name, failure in failures[1:]:
            self.log.event("vehicle.failed", f"{name}: {failure}", level="error", vehicle=name)
        raise failures[0][1]

    def start_vehicle(self, vehicle: PlannedVehicle) -> None:
        """Start the vehicle container and run its readiness probe until `runtime.ready` holds."""
        container = vehicle.container
        self.backend.start(container)
        self._started(container)
        self._running(container)
        self.log.event(
            "vehicle.budget",
            f"{vehicle.name}: "
            + (", ".join(f"{k} {v}" for k, v in vehicle.resources.to_dict().items()) or "unlimited")
            + (f"; namespace /{vehicle.namespace}" if vehicle.namespace else "; the root namespace")
            + (f"; DDS profile {vehicle.dds_profile.name}" if vehicle.dds_profile else ""),
            vehicle=vehicle.name,
            resources=vehicle.resources.to_dict(),
        )
        if vehicle.ready_timeout_s <= 0:
            self.log.event(
                "vehicle.ready", f"{vehicle.name}: no ready conditions", vehicle=vehicle.name
            )
            return
        last = ""

        def on_line(line: str) -> None:
            nonlocal last
            if line.strip():
                last = line.strip()
                self.log.event("vehicle.probe", last, level="debug", vehicle=vehicle.name)

        result = self.backend.exec(
            container.name,
            (ENTRYPOINT_IN_IMAGE, READY_IN_IMAGE, "--timeout", f"{vehicle.ready_timeout_s:g}"),
            on_line,
        )
        if result.exit_code == 1:
            # The probe's last line already reads `not ready after N s: <pending conditions>`.
            detail = last or f"not ready after {vehicle.ready_timeout_s:.0f} s"
            raise RunFailedError(f"{vehicle.name}: {detail}")
        if not result.ok:
            self._running(container)
            raise RunError(f"{vehicle.name}: the readiness probe exited {result.exit_code}: {last}")
        self.log.event("vehicle.ready", f"{vehicle.name}: {last}", vehicle=vehicle.name)

    def run_timeline(self) -> None:
        """Run the timeline in order within the budget while the assertions evaluate."""
        scenario = self.plan.scenario
        budget = scenario.time.timeout_s
        started = self.clock()
        self._poll_samples()
        self.evaluator.start(self.wall())
        if self.monitor is not None:
            self.monitor.origin = self.evaluator.start_wall
        self.log.event(
            "timeline.start",
            f"{len(self.plan.actions)} action(s), {len(self.plan.assertions)} assertion(s), "
            f"budget {budget:g} s",
            budget_s=budget,
        )
        self.start_video()
        try:
            self._run_actions(started, budget)
        finally:
            self.stop_video()
        self.log.event("timeline.done", f"after {self.clock() - started:.1f} s")
        self._observe()
        self._resolve(self.evaluator.finish(self.wall()))
        failed = self.evaluator.failed
        if failed:
            first = failed[0]
            more = f" (and {len(failed) - 1} more)" if len(failed) > 1 else ""
            raise RunFailedError(f"assertion {first.name} failed: {first.reason}{more}")
        if self.plan.assertions:
            self.log.event(
                "assertions.passed", f"all {len(self.plan.assertions)} assertion(s) passed"
            )
        self.check_links()

    def _record_for(self, occurrence: Occurrence, t: float) -> ActionRecord:
        """The record of an occurrence (the first of an entry reuses the record planned for it)."""
        action: PlannedAction = occurrence.action
        entry = action.entry
        with self._lock:
            for existing in self.record.timeline:
                if existing.name == occurrence.name and existing.status in NOT_STARTED:
                    record = existing
                    break
            else:
                record = ActionRecord(occurrence.name, entry.action_name, entry.vehicle, entry.at)
                self.record.timeline.append(record)
            record.trigger = entry.trigger()
            record.occurrence = occurrence.index
            record.params = entry.parameters()
            record.t_s = round(t, 3)
            record.sim_s = self.evaluator.sim_elapsed()
            record.started_at = utc_now()
            record.status = RUNNING
        return record

    def _log_action(self, event: str, record: ActionRecord, message: str, level: str) -> None:
        """An `action.*` line carrying the occurrence's record (relayed to the side channel)."""
        self.log.event(
            event,
            message,
            level=level,
            action=record.name,
            event_action=record.action,
            vehicle=record.vehicle,
            occurrence=record.occurrence,
            status=record.status,
            t_s=record.t_s,
            sim_s=record.sim_s,
            trigger=record.trigger,
            params=record.params,
        )

    def _run_actions(self, started: float, budget: float) -> None:  # noqa: PLR0912, PLR0915
        """Start every occurrence when the schedule says so and settle it when it returns.

        Occurrences run concurrently on workers (three vehicles can drive at once); the main
        thread keeps observing the run between polls and feeds the stream to the schedule,
        whose `when` conditions it may trigger. The first occurrence that does not end `ok`
        fails the run at once; the budget ends it whatever is still running; the timeline ends
        when the schedule is idle, and what never ran is settled then.
        """
        schedule = self.schedule
        for action in self.plan.actions:  # the record lists every entry from the start
            entry = action.entry
            record = ActionRecord(action.name, entry.action_name, entry.vehicle, entry.at)
            record.trigger = entry.trigger()
            record.params = entry.parameters()
            if entry.after is not None or entry.when is not None:
                record.status = WAITING
            self.record.timeline.append(record)
        running: dict[str, tuple[PlannedAction, Future[None], float, float, ActionRecord]] = {}
        while True:
            elapsed = self.clock() - started
            if elapsed >= budget:
                pending = [n for n, st in schedule.entries.items() if not st.done]
                what = f"during {', '.join(running)}" if running else f"before {pending[0]}"
                raise RunTimeoutError(f"budget of {budget:g} s passed {what}")
            for name, status, reason in schedule.expire(elapsed):
                self._settle_one(name, status, reason)
            for occurrence in schedule.due(elapsed):
                action = occurrence.action
                limit = min(action.timeout_s, budget - elapsed)
                record = self._record_for(occurrence, elapsed)
                self._log_action(
                    "action.start",
                    record,
                    f"{record.name}: {record.action}"
                    + (f" on {record.vehicle}" if record.vehicle else "")
                    + (f" (occurrence {record.occurrence})" if record.occurrence > 1 else ""),
                    "info",
                )
                self._resolve(self.evaluator.action(record.name, "started", self.wall()))
                future = self._executor.submit(self.perform, action, limit, record)
                running[record.name] = (action, future, self.clock(), limit, record)
            # The stream first, so what the vehicles did during an action is judged before the
            # action's own result ends the run.
            for sample in self._observe():
                for name in schedule.feed(sample, self.evaluator.elapsed(sample.wall)):
                    self._triggered(name)
            failed: tuple[str, PlannedAction, float, ActionRecord] | None = None
            now = self.clock() - started
            for name in [n for n, (_, f, _, _, _) in running.items() if f.done()]:
                action, future, began, limit, record = running.pop(name)
                record.duration_s = round(self.clock() - began, 3)
                error = future.exception()
                if error is not None and record.status != ACTION_FAILED:
                    record.status = ACTION_FAILED
                    record.detail = f"{type(error).__name__}: {error}"
                level = "info" if record.status == OK else "error"
                self._log_action("action.end", record, f"{name}: {record.detail}", level)
                self._resolve(self.evaluator.action(name, "finished", self.wall(), record.status))
                for triggered in schedule.finished(action.name, record.status, now):
                    self._triggered(triggered)
                if record.status != OK and failed is None:
                    failed = (name, action, limit, record)
            if failed is not None:
                name, action, limit, record = failed
                for other in running.values():
                    other[4].status = CANCELLED
                    other[4].detail = f"cancelled: {name} {record.status}"
                self._write()
                if record.status == TIMEOUT and limit < action.timeout_s:
                    raise RunTimeoutError(f"budget of {budget:g} s passed during {name}")
                raise RunFailedError(f"{name}: {record.detail}")
            if not running and schedule.idle(self.clock() - started):
                self._settle()
                self._write()
                break
            self.sleep(min(EVAL_POLL_S, max(schedule.wait(self.clock() - started), MIN_POLL_S)))

    def _triggered(self, name: str) -> None:
        self.log.event(
            "action.triggered", f"{name}: {self.schedule.entries[name].reason}", action=name
        )

    def _settle(self) -> None:
        """End the entries that never ran or repeat no more, in the record and the evaluator."""
        for name, status, reason in self.schedule.settle("the timeline ended"):
            self._settle_one(name, status, reason)

    def _settle_one(self, name: str, status: str, reason: str) -> None:
        record = next((r for r in self.record.timeline if r.name == name), None)
        if record is None or record.status not in NOT_STARTED:
            return  # a repeated entry: its occurrences carry their own records
        record.status, record.detail = status, reason
        self.log.event(
            "action.skipped" if status == SKIPPED else "action.settled",
            f"{name}: {reason}",
            level="warning" if status == SKIPPED else "info",
            action=name,
            status=status,
        )
        self._resolve(self.evaluator.action(name, "finished", self.wall(), status))

    # ------------------------------------------------------------------------------ the video
    def _video_service(self, video: PlannedVideo, request: str) -> ExecResult:
        return self.backend.exec(
            self.plan.sim.name,
            (
                "bash",
                "-lc",
                f"timeout {PROBE_LIMIT_S} gz service -s {video.service} "
                "--reqtype gz.msgs.VideoRecord --reptype gz.msgs.Boolean --timeout 5000 "
                f"--req {shlex.quote(request)}",
            ),
        )

    def start_video(self) -> None:
        """Ask the world's camera to record (`outputs.video`); a failure is a warning."""
        video = self.plan.video
        if video is None:
            return
        self.record.video = {
            "camera": video.camera,
            "file": video.file,
            "bytes": None,
            "status": "starting",
        }
        try:
            result = self._video_service(
                video,
                f'start: true, format: "{video.file.rsplit(".", 1)[-1]}", '
                f'save_filename: "{video.in_sim}"',
            )
        except BackendError as error:
            self._video_failed(f"cannot start the recording: {error}")
            return
        if not result.ok or "true" not in result.output:
            self._video_failed(f"the recorder refused to start:\n{result.tail()}")
            return
        self.record.video["status"] = "recording"
        self.log.event(
            "video.start",
            f"camera {video.camera} recording through {video.service}",
            camera=video.camera,
        )

    def stop_video(self) -> None:
        """Stop the recording and copy the file into the run directory.

        Waits for the encoder to close the file first; a failure is a warning.
        """
        video = self.plan.video
        if video is None or not self.record.video or self.record.video["status"] != "recording":
            return
        try:
            result = self._video_service(video, "stop: true")
            if not result.ok or "true" not in result.output:
                self._video_failed(f"the recorder refused to stop:\n{result.tail()}")
                return
            self.record.video["status"] = "encoding"
            deadline = self.clock() + VIDEO_SETTLE_S
            check = ("bash", "-lc", f"test -s {shlex.quote(video.in_sim)}")
            while not self.backend.exec(self.plan.sim.name, check).ok:
                if self.clock() >= deadline:
                    self._video_failed(
                        f"{video.in_sim} not written {VIDEO_SETTLE_S:g} s after the stop"
                    )
                    return
                self.sleep(EVAL_POLL_S)
            destination = self.plan.run_dir / video.file
            size = self.backend.copy_from(self.plan.sim.name, video.in_sim, destination)
        except BackendError as error:
            self._video_failed(f"cannot save the recording: {error}")
            return
        self.record.video.update({"bytes": size, "status": "saved"})
        self.log.event(
            "video.saved", f"{video.file} ({size} bytes)", camera=video.camera, bytes=size
        )
        self._write()

    def _video_failed(self, reason: str) -> None:
        video = self.record.video or {}
        video.update({"status": "error", "reason": reason})
        self.record.video = video
        self.log.event("video.error", reason, level="warning", camera=video.get("camera"))
        self._write()

    def check_alive(self) -> None:
        """Raise if any container of the run exited (unless an event killed it)."""
        for spec in self.plan.containers():
            self._running(spec)

    def expect_exit(self, container: str) -> None:
        """An event is about to kill this container: its exit does not end the run."""
        self._expected_exits.add(container)

    def mark_paused(self, container: str, paused: bool) -> None:
        """An event paused (or resumed) this container."""
        if paused:
            self._paused.add(container)
        else:
            self._paused.discard(container)

    def configured_profile(self, vehicle: str) -> str:
        """The profile the scenario configures for a vehicle's link."""
        return self._links().links[vehicle]

    def teardown(self) -> None:
        """Stop and remove the run's containers in reverse order, keeping their logs."""
        self.tearing_down = True
        if self.keep:
            names = [s.name for s in self.plan.containers() if self.backend.state(s.name).exists]
            if self.plan.links is not None:
                names += self.plan.links.networks()
            self.log.event("teardown.kept", ", ".join(names) or "nothing running", level="warning")
            return
        logs_dir = self.plan.run_dir / LOGS_DIR
        keep_logs = self.plan.scenario.outputs.logs
        removed = 0
        for spec in reversed(self.plan.containers()):
            try:
                if not self.backend.state(spec.name).exists:
                    continue
                if spec.name in self._paused:
                    self.backend.unpause(spec.name)
                    self._paused.discard(spec.name)
                self.backend.stop(spec.name, self.plan.config.timeouts.teardown_s)
                if keep_logs:
                    logs_dir.mkdir(parents=True, exist_ok=True)
                    suffix = (
                        f"-{spec.labels[VEHICLE_LABEL]}" if VEHICLE_LABEL in spec.labels else ""
                    )
                    (logs_dir / f"{spec.role}{suffix}.log").write_text(self.backend.logs(spec.name))
                self.backend.remove(spec.name)
                removed += 1
                self.log.event(f"{spec.role}.stopped", spec.name, container=spec.name)
            except BackendError as error:
                self.log.event("teardown.error", str(error), level="warning", container=spec.name)
        try:
            for name in self.backend.find(RUN_LABEL, self.plan.run_id):
                self.backend.remove(name)
                removed += 1
                self.log.event("teardown.swept", name, level="warning", container=name)
        except BackendError as error:
            self.log.event("teardown.error", str(error), level="warning")
        networks = self.remove_networks()
        self.log.event(
            "teardown.done",
            f"{removed} container(s)"
            + (f" and {networks} network(s)" if networks else "")
            + " removed",
            removed=removed,
        )

    def remove_networks(self) -> int:
        """Remove the networks of the radio links, then any other network of the run."""
        if self.plan.links is None:
            return 0
        removed = 0
        names = list(self.plan.links.networks())
        try:
            names += [
                n for n in self.backend.find_networks(RUN_LABEL, self.plan.run_id) if n not in names
            ]
        except BackendError as error:
            self.log.event("teardown.error", str(error), level="warning")
        for name in names:
            try:
                self.backend.remove_network(name)
                removed += 1
            except BackendError as error:
                self.log.event("teardown.error", str(error), level="warning", network=name)
        return removed

    def write_report(self) -> None:
        """`report.json`, `report.md` and `junit.xml` from the record and the run directory."""
        try:
            report = write_reports(self.record.to_dict(), self.plan.run_dir)
        except OSError as error:
            self.log.event("report.error", str(error), level="warning")
            return
        recording = report.get("recording")
        if self.plan.scenario.outputs.record and not (recording and recording["mcap"]):
            self.log.event(
                "recording.missing",
                "outputs.record was on but no MCAP file is in the run directory",
                level="warning",
            )
        elif recording:
            self.log.event(
                "recording.saved",
                ", ".join(recording["mcap"]) + f" ({recording['bytes']} bytes)",
                files=recording["mcap"],
            )
        self.log.event(
            "report.written",
            f"{REPORT_MD}, {REPORT_JSON}, {REPORT_JUNIT}",
            verdict=report["verdict"],
        )


def leftovers(backend: Backend, run_id: str | None = None) -> list[str]:
    """Containers of Fieldproof runs still present: of one run, or of every run."""
    if run_id is not None:
        return backend.find(RUN_LABEL, run_id)
    names: list[str] = []
    for role in ("sim", "bridge", "recorder", "vehicle", "spawn"):
        names += backend.find(ROLE_LABEL, role)
    return names
