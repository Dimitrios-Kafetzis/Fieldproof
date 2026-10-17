"""The scenario runner v0 (task P1-T05, ADR-0004, ADR-0005, ADR-0006).

:func:`plan` turns a validated scenario and the workspace configuration into a :class:`Plan`:
the world file and its named frames, every vehicle's manifest, spawn pose and the three
containers it needs (the one-shot spawn, its bridge, the vehicle itself), the simulator
container, the resolved timeline, the run id and the run directory. Nothing in it touches
Docker, so ``fieldproof validate`` and ``fieldproof run --dry-run`` stop here.

:class:`Run` executes a plan against a :class:`~fieldproof.backend.Backend` as a state machine
of phases (01-architecture.md, "Data flow for one run"): preparing (images, assets, network),
sim (started, the world's services answer), spawn (one one-shot container per vehicle), bridges
(started, the vehicle's odometry topic appears), vehicles (started, the image's readiness probe
reports every ``runtime.ready`` condition), running (the timeline, within the scenario's
budget), teardown (vehicles, bridges, simulator in reverse order, logs kept, then a sweep by
label), finished. A failure in any phase, the scenario's budget, a Ctrl-C or an error of the
runtime all end in the teardown: a run never leaves containers behind unless asked to with
``keep``. Everything the run did is in ``runs/<run_id>/``: the scenario as run, ``run.json``
(the record, rewritten at every phase), ``engine.log`` (one JSON object per line) and the
containers' logs.
"""

from __future__ import annotations

import json
import math
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

import yaml
from rich.console import Console

from fieldproof import __version__
from fieldproof.backend import Backend, BackendError, ContainerSpec, ExecResult, Mount
from fieldproof.config import Config
from fieldproof.manifest import RobotManifest
from fieldproof.scenario import (
    NavigateToPose,
    Pose,
    Scenario,
    ScenarioError,
    Vehicle,
    WorldInfo,
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
    """The scenario failed (a vehicle not ready, an action failed, a container died)."""


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
        return self.entry.name or f"{self.entry.action}@{self.entry.at:g}s"


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
    vehicles: tuple[PlannedVehicle, ...]
    actions: tuple[PlannedAction, ...]

    def containers(self) -> list[ContainerSpec]:
        """The long-running containers in start order (the teardown reverses it)."""
        specs = [self.sim]
        specs += [v.bridge for v in self.vehicles]
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
            "assertions": [a.model_dump() for a in self.scenario.assertions],
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
    return Plan(
        scenario=scenario,
        config=config,
        world=world,
        run_id=identity,
        run_dir=base / identity,
        network=net,
        render=render_mode,
        domain_id=domain,
        sim=sim,
        vehicles=vehicles,
        actions=tuple(actions),
    )


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
    containers: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[ActionRecord] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
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
    ) -> None:
        self.plan = plan
        self.backend = backend
        self.log = log
        self.keep = keep
        self.sleep = sleep
        self.clock = clock
        self.tearing_down = False
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
            self._phase(Phase.FINISHED)
            self.record.finished_at = utc_now()
            self._write()
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
        if scenario.outputs.record:
            self.log.event(
                "outputs.record",
                "recording to MCAP arrives with task P1-T06; nothing is recorded",
                level="warning",
            )
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
        """Run the timeline in order within the budget; record the assertions as skipped."""
        scenario = self.plan.scenario
        budget = scenario.time.timeout_s
        started = self.clock()
        for assertion in scenario.assertions:
            self.record.assertions.append(
                {
                    "name": assertion.name,
                    "type": assertion.type,
                    "status": "skipped",
                    "reason": "assertions are evaluated from task P1-T06",
                }
            )
        if scenario.assertions:
            self.log.event(
                "assertions.skipped",
                f"{len(scenario.assertions)} assertion(s) declared; evaluation arrives with P1-T06",
                level="warning",
            )
        self.log.event(
            "timeline.start",
            f"{len(self.plan.actions)} action(s), budget {budget:g} s",
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
                self.check_alive()
                self.sleep(min(POLL_S, action.entry.at - (self.clock() - started)))
            remaining = budget - (self.clock() - started)
            if remaining <= 0:
                raise RunTimeoutError(f"budget of {budget:g} s passed before {action.name}")
            limit = min(action.entry.timeout_s, remaining)
            record.started_at = utc_now()
            began = self.clock()
            try:
                self.navigate_to_pose(action, limit, record)
            finally:
                record.duration_s = round(self.clock() - began, 3)
                self._write()
            if record.status != "ok":
                if record.status == "timeout" and limit < action.entry.timeout_s:
                    raise RunTimeoutError(f"budget of {budget:g} s passed during {action.name}")
                raise RunFailedError(f"{action.name}: {record.detail}")
        self.log.event("timeline.done", f"after {self.clock() - started:.1f} s")

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
        result = self.backend.exec(
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
    for role in ("sim", "bridge", "vehicle", "spawn"):
        names += backend.find(ROLE_LABEL, role)
    return names
