"""The scenario runner (tasks P1-T05 and P1-T06; ADR-0004, ADR-0005, ADR-0006).

:func:`plan` turns a validated scenario and the workspace configuration into a :class:`Plan`:
the world file and its named frames, every vehicle's manifest, spawn pose and the three
containers it needs (the one-shot spawn, its bridge, the vehicle itself), the simulator
container, the recorder container, the resolved timeline and assertions, the run id and the
run directory. Nothing in it touches Docker, so ``fieldproof validate`` and ``fieldproof run
--dry-run`` stop here.

:class:`Run` executes a plan against a :class:`~fieldproof.backend.Backend` as a state machine
of phases (01-architecture.md, "Data flow for one run"): preparing (images, assets, network),
sim (started, the world's services answer), spawn (one one-shot container per vehicle), bridges
(started, the vehicle's odometry topic appears), recorder (the observer of
:mod:`fieldproof.observer` started on the run directory, recording to MCAP when asked, its
``ready`` sample seen), vehicles (started, the image's readiness probe reports every
``runtime.ready`` condition), running (the timeline within the scenario's budget while the
:class:`~fieldproof.assertions.Evaluator` consumes the observer's stream), teardown (vehicles,
recorder, bridges, simulator in reverse order, logs kept, then a sweep by label), finished (the
report written). A failure in any phase, a failed assertion, the scenario's budget, a Ctrl-C
or an error of the runtime all end in the teardown: a run never leaves containers behind unless
asked to with ``keep``. Everything the run did is in ``runs/<run_id>/``: the scenario as run,
``run.json`` (the record, rewritten at every phase), ``engine.log`` (one JSON object per line),
``samples.jsonl`` and ``assertions.jsonl`` (the stream and the verdicts), ``recording/`` (the
MCAP), ``report.json`` and ``report.md``, and the containers' logs.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

import yaml
from rich.console import Console

from fieldproof import __version__, observer
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
from fieldproof.backend import Backend, BackendError, ContainerSpec, ExecResult, Mount
from fieldproof.config import Config
from fieldproof.manifest import RobotManifest
from fieldproof.observer import FileTail
from fieldproof.report import REPORT_JSON, REPORT_MD, write_reports
from fieldproof.scenario import (
    Assertion,
    NavigateToPose,
    NoCollision,
    Pose,
    ReachPose,
    Scenario,
    ScenarioError,
    TopicRate,
    TopicValue,
    Vehicle,
    WorldInfo,
    entry_label,
    place,
    read_world,
)
from fieldproof.vehicle import ENTRYPOINT_IN_IMAGE, READY_IN_IMAGE, load_vehicle
from fieldproof_bridge.manifest import ManifestError

RUN_LABEL = "io.fieldproof.run"
ROLE_LABEL = "io.fieldproof.role"
VEHICLE_LABEL = "io.fieldproof.vehicle"
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
NAV2_ACTION = "navigate_to_pose"
NAV2_ACTION_TYPE = "nav2_msgs/action/NavigateToPose"
RENDER_DIR = Path("/dev/dri")
POLL_S = 2.0
# How often the running phase reads the observer's stream and ticks the assertions.
EVAL_POLL_S = 0.5
# The recorder container: the observer mounted from the engine, the run directory read-write.
OBSERVER_FILE = Path(observer.__file__).resolve()
OBSERVER_IN_CONTAINER = "/opt/fieldproof/observer.py"
RUN_DIR_IN_CONTAINER = "/fieldproof/run"
# In-container limit of a polling command, so a hung `gz service -l` cannot hang the runner.
PROBE_LIMIT_S = 20
RECORD_FILE = "run.json"
LOG_FILE = "engine.log"
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
    """A timeline entry with its goal resolved in the world and in the vehicle's map frame."""

    entry: NavigateToPose
    goal_world: Pose
    goal_map: Pose

    @property
    def name(self) -> str:
        """The entry's label."""
        return entry_label(self.entry)


@dataclass(frozen=True)
class PlannedVehicle:
    """A vehicle of the scenario with its manifest, spawn pose and containers."""

    vehicle: Vehicle
    manifest: RobotManifest
    spawn: Pose
    spawn_job: ContainerSpec
    bridge: ContainerSpec
    container: ContainerSpec
    ready_timeout_s: float

    @property
    def name(self) -> str:
        """The vehicle's name."""
        return self.vehicle.name

    def topic(self, name: str) -> str:
        """`name` under the vehicle's namespace (an absolute name stays as written)."""
        if name.startswith("/"):
            return name
        namespace = self.vehicle.namespace
        return f"/{namespace}/{name}" if namespace else f"/{name}"


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

    def containers(self) -> list[ContainerSpec]:
        """The long-running containers in start order (the teardown reverses it)."""
        specs = [self.sim]
        specs += [v.bridge for v in self.vehicles]
        specs.append(self.recorder)
        specs += [v.container for v in self.vehicles]
        return specs

    def vehicle(self, name: str) -> PlannedVehicle:
        """The planned vehicle called `name`."""
        return next(v for v in self.vehicles if v.name == name)

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
                "cpus": s.cpus,
                "memory": s.memory,
                "user": s.user,
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
            "world": {"name": self.world.name, "file": str(self.world.file)},
            "time": self.scenario.time.model_dump(),
            "vehicles": [
                {
                    "name": v.name,
                    "manifest": str(v.manifest.path),
                    "namespace": v.vehicle.namespace,
                    "spawn": asdict(v.spawn),
                    "ready_timeout_s": v.ready_timeout_s,
                    "spawn_job": spec(v.spawn_job),
                }
                for v in self.vehicles
            ],
            "containers": [spec(s) for s in self.containers()],
            "timeline": [
                {
                    "name": a.name,
                    "at_s": a.entry.at,
                    "vehicle": a.entry.vehicle,
                    "action": a.entry.action,
                    "goal_world": asdict(a.goal_world),
                    "goal_map": asdict(a.goal_map),
                    "timeout_s": a.entry.timeout_s,
                }
                for a in self.actions
            ],
            "assertions": [a.to_dict() for a in self.assertions],
            "observe": self.observe,
        }


def make_run_id(scenario: Scenario, now: datetime, token: str) -> str:
    """`<scenario-hash>-<timestamp>-<short-uuid>` (01-architecture.md)."""
    return f"{scenario.digest or 'nohash'}-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{token}"


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
    domain = config.docker.ros_domain_id if domain_id is None else domain_id
    net = network or config.docker.network
    identity = run_id or make_run_id(
        scenario, now or datetime.now(UTC), token or uuid.uuid4().hex[:6]
    )
    short = identity.rsplit("-", 1)[-1]
    if scenario.outputs.directory is not None:
        base = config.resolve(scenario.outputs.directory)
    elif runs_dir is not None:
        base = Path(runs_dir).resolve()
    else:
        base = config.resolve(config.runs.directory)
    labels = {RUN_LABEL: identity}
    env = {"ROS_DOMAIN_ID": str(domain), "GZ_PARTITION": GZ_PARTITION}
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
        _plan_vehicle(scenario, config, world, v, short, net, env, labels)
        for v in scenario.vehicles
    )
    by_name = {v.name: v for v in vehicles}
    actions = []
    for index, entry in enumerate(sorted(scenario.timeline, key=lambda e: e.at)):
        goal_world = place(entry.goal, world, f"timeline[{index}].goal")
        actions.append(
            PlannedAction(entry, goal_world, by_name[entry.vehicle].spawn.relative(goal_world))
        )
    assertions = tuple(
        _plan_assertion(world, by_name, a, index) for index, a in enumerate(scenario.assertions)
    )
    run_dir = base / identity
    observe = observe_plan(
        assertions,
        world=world.name,
        models=[v.name for v in vehicles],
        record=scenario.outputs.record,
        topics=scenario.outputs.topics,
        run_dir=RUN_DIR_IN_CONTAINER,
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
        # user who started the run; HOME and the ROS log directory point at a writable place.
        env={**env, "HOME": "/tmp", "ROS_LOG_DIR": "/tmp/ros-log"},  # noqa: S108  (in-container)
        labels={**labels, ROLE_LABEL: "recorder"},
        network=net,
        hostname=f"fieldproof-{short}-recorder",
        mounts=(
            Mount(OBSERVER_FILE, OBSERVER_IN_CONTAINER),
            Mount(run_dir, RUN_DIR_IN_CONTAINER, read_only=False),
        ),
        user=f"{os.getuid()}:{os.getgid()}",
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
    namespace = vehicle.namespace
    spawn_job = ContainerSpec(
        name=f"fieldproof-{short}-spawn-{vehicle.name}",
        image=config.image("bridge"),
        role="spawn",
        command=(
            "ros2",
            "launch",
            *launch,
            f"name:={vehicle.name}",
            # ros2 launch rejects an empty value, and the launch defaults to the root.
            *((f"namespace:={namespace}",) if namespace else ()),
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
        command=(BRIDGE_COMMAND, *((f"namespace:={namespace}",) if namespace else ())),
        env={**env, "FIELDPROOF_MANIFEST": f"{WORKSPACE}/{in_workspace}"},
        labels={**labels, ROLE_LABEL: "bridge", VEHICLE_LABEL: vehicle.name},
        network=network,
        hostname=f"fieldproof-{short}-bridge-{vehicle.name}",
        mounts=(Mount(config.root, WORKSPACE),),
    )
    resources = runtime.resources
    container = ContainerSpec(
        name=f"fieldproof-{short}-{vehicle.name}",
        image=vehicle.image or runtime.image,
        role="vehicle",
        env={**runtime.env, "ROS_DOMAIN_ID": env["ROS_DOMAIN_ID"], **vehicle.env},
        labels={**labels, ROLE_LABEL: "vehicle", VEHICLE_LABEL: vehicle.name},
        network=network,
        hostname=vehicle.name,
        cpus=resources.cpus if resources else None,
        memory=resources.memory if resources else None,
        pids=resources.pids if resources else None,
    )
    return PlannedVehicle(
        vehicle=vehicle,
        manifest=manifest,
        spawn=spawn,
        spawn_job=spawn_job,
        bridge=bridge,
        container=container,
        ready_timeout_s=runtime.ready.timeout_s if runtime.ready else 0.0,
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
class ActionRecord:
    """One timeline entry as it ran."""

    name: str
    action: str
    vehicle: str
    at_s: float
    status: str = "pending"
    started_at: str | None = None
    duration_s: float | None = None
    detail: str = ""


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
    containers: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[ActionRecord] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    observation: dict[str, Any] = field(default_factory=dict)
    """The observer's stream: samples, simulation time covered, problems."""
    time: dict[str, Any] = field(default_factory=dict)
    stack: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The record as data."""
        return asdict(self)

    def write(self, path: Path) -> None:
        """Write the record as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")


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
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        executor: Executor | None = None,
    ) -> None:
        self.plan = plan
        self.backend = backend
        self.log = log
        self.keep = keep
        self.sleep = sleep
        self.clock = clock
        self.wall = wall
        """The wall clock the observer's samples are stamped with (epoch seconds)."""
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="fieldproof-action"
        )
        """Runs an action's command while the main thread keeps observing the run."""
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
            vehicles=[
                {
                    "name": v.name,
                    "manifest": v.manifest.name,
                    "namespace": v.vehicle.namespace,
                    "image": v.container.image,
                }
                for v in plan.vehicles
            ],
            assertions=[r.to_dict() for r in self.evaluator.results],
            time=plan.scenario.time.model_dump(),
            stack={
                "network": plan.network,
                "render": plan.render,
                "ros_domain_id": plan.domain_id,
                "world": plan.world.name,
                "images": sorted({s.image for s in plan.containers()}),
            },
        )
        self._phase_started: float | None = None
        self._samples = FileTail(plan.run_dir / SAMPLES_FILE)
        self._observer_errors = 0
        self._last_alive_check = -math.inf

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
            self.start_sim()
            self._phase(Phase.SPAWN)
            for vehicle in self.plan.vehicles:
                self.spawn(vehicle)
            self._phase(Phase.BRIDGES)
            for vehicle in self.plan.vehicles:
                self.start_bridge(vehicle)
            self._phase(Phase.RECORDER)
            self.start_recorder()
            self._phase(Phase.VEHICLES)
            for vehicle in self.plan.vehicles:
                self.start_vehicle(vehicle)
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
        self._resolve(self.evaluator.skip_pending(f"the run ended {status}: {reason}"))
        self.record.observation = {
            "samples": self.evaluator.samples,
            "sim_elapsed_s": self.evaluator.sim_elapsed(),
            "unparsed_lines": self.evaluator.bad_lines,
            "observer_errors": self._observer_errors,
        }

    def _write(self) -> None:
        self.record.write(self.plan.run_dir / RECORD_FILE)

    def _started(self, spec: ContainerSpec) -> None:
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
        if state.running:
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
        """Refuse what cannot run yet; check images and assets; make sure the network exists."""
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
        self.backend.ensure_network(self.plan.network)
        self.log.event("network.ok", self.plan.network)

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

    def _observe(self) -> None:
        """One poll of the running phase: the stream, the deadlines, the containers."""
        self._poll_samples()
        self._resolve(self.evaluator.tick(self.wall()))
        if self.clock() - self._last_alive_check >= POLL_S:
            self._last_alive_check = self.clock()
            self.check_alive()

    def start_vehicle(self, vehicle: PlannedVehicle) -> None:
        """Start the vehicle container and run its readiness probe until `runtime.ready` holds."""
        container = vehicle.container
        self.backend.start(container)
        self._started(container)
        self._running(container)
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
        self.log.event(
            "timeline.start",
            f"{len(self.plan.actions)} action(s), {len(self.plan.assertions)} assertion(s), "
            f"budget {budget:g} s",
            budget_s=budget,
        )
        for action in self.plan.actions:
            record = ActionRecord(
                action.name, action.entry.action, action.entry.vehicle, action.entry.at
            )
            self.record.timeline.append(record)
            while self.clock() - started < action.entry.at:
                if self.clock() - started >= budget:
                    raise RunTimeoutError(f"budget of {budget:g} s passed before {action.name}")
                self._observe()
                self.sleep(min(EVAL_POLL_S, action.entry.at - (self.clock() - started)))
            remaining = budget - (self.clock() - started)
            if remaining <= 0:
                raise RunTimeoutError(f"budget of {budget:g} s passed before {action.name}")
            limit = min(action.entry.timeout_s, remaining)
            record.started_at = utc_now()
            began = self.clock()
            self._resolve(self.evaluator.action(action.name, "started", self.wall()))
            try:
                self.navigate_to_pose(action, limit, record)
            finally:
                record.duration_s = round(self.clock() - began, 3)
                self._resolve(
                    self.evaluator.action(action.name, "finished", self.wall(), record.status)
                )
                self._write()
            if record.status != "ok":
                if record.status == "timeout" and limit < action.entry.timeout_s:
                    raise RunTimeoutError(f"budget of {budget:g} s passed during {action.name}")
                raise RunFailedError(f"{action.name}: {record.detail}")
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

    def _exec_while_observing(
        self, name: str, argv: tuple[str, ...], on_line: Callable[[str], None]
    ) -> ExecResult:
        """Run a command in a container in a thread, observing the run until it returns."""
        future: Future[ExecResult] = self._executor.submit(self.backend.exec, name, argv, on_line)
        while True:
            self._observe()
            if future.done():
                return future.result()
            self.sleep(EVAL_POLL_S)

    def navigate_to_pose(self, action: PlannedAction, limit_s: float, record: ActionRecord) -> None:
        """Send the goal with `ros2 action send_goal` inside the vehicle and read the result."""
        vehicle = self.plan.vehicle(action.entry.vehicle)
        goal = action.goal_map
        message = json.dumps(
            {
                "pose": {
                    "header": {"frame_id": "map"},
                    "pose": {
                        "position": {"x": goal.x, "y": goal.y, "z": 0.0},
                        "orientation": {"z": math.sin(goal.yaw / 2), "w": math.cos(goal.yaw / 2)},
                    },
                }
            }
        )
        self.log.event(
            "action.start",
            f"{action.name}: {vehicle.name} to ({action.goal_world.x:g}, {action.goal_world.y:g}) "
            f"in the world, ({goal.x:.2f}, {goal.y:.2f}, yaw {goal.yaw:.2f}) in map, "
            f"limit {limit_s:.0f} s",
            action=action.name,
            vehicle=vehicle.name,
        )
        result = self._exec_while_observing(
            vehicle.container.name,
            (
                ENTRYPOINT_IN_IMAGE,
                "timeout",
                "-s",
                "INT",
                f"{max(limit_s, 1.0):.0f}",
                "ros2",
                "action",
                "send_goal",
                vehicle.topic(NAV2_ACTION),
                NAV2_ACTION_TYPE,
                message,
            ),
            lambda line: self.log.event(
                "action.output", line.rstrip(), level="debug", action=action.name
            ),
        )
        status = goal_status(result)
        if result.exit_code == 124:
            record.status, record.detail = "timeout", f"no result within {limit_s:.0f} s"
        elif status == "SUCCEEDED":
            record.status, record.detail = "ok", "goal SUCCEEDED"
        elif status is not None:
            record.status, record.detail = "failed", f"goal {status}"
        else:
            record.status = "failed"
            record.detail = f"no goal result (exit {result.exit_code}): {result.tail(3)}"
        level = "info" if record.status == "ok" else "error"
        self.log.event(
            "action.end",
            f"{action.name}: {record.detail}",
            level=level,
            action=action.name,
            status=record.status,
        )

    def check_alive(self) -> None:
        """Raise if any container of the run exited."""
        for spec in self.plan.containers():
            self._running(spec)

    def teardown(self) -> None:
        """Stop and remove the run's containers in reverse order, keeping their logs."""
        self.tearing_down = True
        if self.keep:
            names = [s.name for s in self.plan.containers() if self.backend.state(s.name).exists]
            self.log.event("teardown.kept", ", ".join(names) or "nothing running", level="warning")
            return
        logs_dir = self.plan.run_dir / LOGS_DIR
        keep_logs = self.plan.scenario.outputs.logs
        removed = 0
        for spec in reversed(self.plan.containers()):
            try:
                if not self.backend.state(spec.name).exists:
                    continue
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
        self.log.event("teardown.done", f"{removed} container(s) removed", removed=removed)

    def write_report(self) -> None:
        """`report.json` and `report.md` from the record and the run directory."""
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
        self.log.event("report.written", f"{REPORT_MD}, {REPORT_JSON}", verdict=report["verdict"])


def goal_status(result: ExecResult) -> str | None:
    """The status `ros2 action send_goal` printed (`Goal finished with status: SUCCEEDED`)."""
    for line in reversed(result.output.splitlines()):
        if "Goal finished with status:" in line:
            return line.rsplit(":", 1)[1].strip()
    return None


def leftovers(backend: Backend, run_id: str | None = None) -> list[str]:
    """Containers of Fieldproof runs still present: of one run, or of every run."""
    if run_id is not None:
        return backend.find(RUN_LABEL, run_id)
    names: list[str] = []
    for role in ("sim", "bridge", "recorder", "vehicle", "spawn"):
        names += backend.find(ROLE_LABEL, role)
    return names
