"""A fake container runtime and clock for the runner's tests (tasks P1-T05, P1-T06, P2-T02)."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fieldproof.backend import (
    BackendError,
    ContainerSpec,
    ContainerState,
    ExecResult,
    HostInfo,
    LineSink,
    ResourceUsage,
)

WORLD_LIST = "/world/depot/control\n/world/depot/create\n/world/depot/set_physics\n"
TOPIC_LIST = "/clock\n/odom\n/scan\n/tf\n"
# What a bridge's `ros2 topic list` shows for a namespaced vehicle, for any namespace.
NAMESPACED_TOPICS = "/clock\n/{ns}/odom\n/{ns}/scan\n/{ns}/tf\n"
CONTRACT_OK = "ok graph /odom: publisher nav_msgs/msg/Odometry\n14 passed, 0 failed\n"
DISCOVERY_LOG = "### Server is running ###\n"
PROBE_OK = "waiting (3/9 ready): /map topic no publisher\nready: 9 conditions hold after 5.5 s\n"
GOAL_OK = (
    "Waiting for an action server to become available...\nGoal finished with status: SUCCEEDED\n"
)
LINK_OK = "eth1\nqdisc netem 8001: root refcnt 2 limit 1000 delay 60ms 20ms\n"
PARAM_OK = "Set parameter successful\n"
WORLD_OK = 'data: "applied"\n'
TRAFFIC_OK = (
    '{"out": [{"kind": "prio", "handle": "1:"}, {"kind": "netem", "handle": "20:", '
    '"bytes": 2263618, "packets": 6769, "drops": 12, "backlog": 0}], '
    '"in": [{"kind": "netem", "handle": "20:", "bytes": 917404, "packets": 3100, "drops": 3, '
    '"backlog": 0}]}'
)


def world_stats(sim_s: float, *, paused: bool) -> str:
    """A `gz.msgs.WorldStatistics` as `gz topic -e` prints it."""
    seconds = int(sim_s)
    return (
        f"sim_time {{\n  sec: {seconds}\n  nsec: {round((sim_s - seconds) * 1e9)}\n}}\n"
        f"real_time {{\n  sec: {seconds}\n  nsec: 0\n}}\n"
        + ("paused: true\n" if paused else "real_time_factor: 1.0\n")
        + f"iterations: {seconds * 1000}\nstep_size {{\n  nsec: 1000000\n}}\n"
    )


@dataclass
class FakeContainer:
    spec: ContainerSpec
    running: bool = True
    exit_code: int | None = None
    logs: str = ""
    paused: bool = False


class FakeClock:
    """Time that moves only when someone sleeps."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


FAKE_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2040
"""What the fake simulator's recorder "wrote": an ISO media file header and padding."""


class FakeLockstep:
    """The observer's clock manager, played against the run's clock file (task P2-T04).

    Wired into the run's `sleep`: every tick reads the engine's new commands and answers them
    the way the manager does, with `clock` samples: `lockstep` locks the world at `origin`
    (every vehicle acknowledging), `run_until` steps straight to the grid point the limit names
    (one sample per step, held there), or one second further per tick without a limit;
    `release` ends it. `samples_at` adds samples of the stream once a time is reached (what
    the vehicles "did"), and `refuse_from` makes a vehicle stop acknowledging at a time, which
    becomes a `lockstep.timeout` after `ack_timeout_s` ticks.
    """

    def __init__(
        self,
        backend: FakeBackend,
        *,
        step_s: float = 0.1,
        origin: float = 10.0,
        vehicles: Sequence[str] = ("ugv1",),
        ack_timeout_s: float = 30.0,
    ) -> None:
        self.backend = backend
        self.step_s = step_s
        self.origin = origin
        self.vehicles = list(vehicles)
        self.ack_timeout_s = ack_timeout_s
        self.offset = 0
        self.t: float | None = None
        self.hold: float | None = None
        self.locked = False
        self.released = False
        self.steps = 0
        self.holds = 0
        self.commands: list[dict[str, Any]] = []
        self.samples_at: dict[float, list[dict[str, Any]]] = {}
        self.refuse_from: dict[str, float] = {}
        self.waited = 0.0
        self.timed_out = False

    @property
    def control_file(self) -> Path | None:
        if self.backend.samples_file is None:
            return None
        return self.backend.samples_file.parent / "clock.jsonl"

    def state(self, **fields: Any) -> dict[str, Any]:
        t_s = None if self.t is None else round(self.t, 6)
        return {
            "kind": "clock",
            "sim": None if self.t is None else round(self.origin + self.t, 6),
            "rtf": None,
            "paused": self.locked,
            "mode": "lockstep",
            "target_rtf": 1.0,
            "lockstep": {
                "origin": self.origin if self.locked else None,
                "step": 0 if self.t is None else round(self.t / self.step_s),
                "step_s": self.step_s,
                "t_s": t_s,
                "hold_t": self.hold,
                "held": fields.pop("held", False),
                "steps": self.steps,
                "holds": self.holds,
                "max_wait_s": 0.0,
                "released": self.released,
            },
            "acks": dict.fromkeys(self.vehicles, self.origin + (self.t or 0.0)),
            **fields,
        }

    def tick(self) -> None:  # noqa: PLR0912
        """Read the new commands and move the fake world."""
        path = self.control_file
        if path is None or not path.is_file():
            return
        lines = path.read_text().splitlines()[self.offset :]
        self.offset += len(lines)
        for line in lines:
            command = json.loads(line)
            self.commands.append(command)
            if command["cmd"] == "lockstep" and not self.locked:
                self.locked, self.t, self.hold = True, 0.0, 0.0
                self.backend.write_samples([self.state(locked=True)])
                self.backend.write_samples([self.state(held=True)])
            elif command["cmd"] == "run_until":
                limit = command.get("t_s")
                self.hold = None if limit is None else limit
            elif command["cmd"] == "release":
                self.released = True
                self.backend.write_samples([self.state(released=True)])
        if not self.locked or self.released or self.t is None:
            return
        target = (
            self.t + 1.0
            if self.hold is None
            else math.ceil(self.hold / self.step_s - 1e-6) * self.step_s
        )
        while self.t < target - 1e-9:
            next_t = round(self.t + self.step_s, 6)
            refused = [v for v, since in self.refuse_from.items() if next_t > since + 1e-9]
            if refused:
                self.waited += 1.0
                if self.waited > self.ack_timeout_s and not self.timed_out:
                    self.timed_out = True
                    self.backend.write_samples(
                        [
                            {
                                "kind": "lockstep.timeout",
                                "sim": round(self.origin + next_t, 6),
                                "step": round(next_t / self.step_s),
                                "waited_s": self.waited,
                                "missing": refused,
                            }
                        ]
                    )
                else:
                    self.backend.write_samples([self.state(waiting=refused, waited_s=self.waited)])
                return
            self.t = next_t
            self.steps += 1
            self.backend.write_samples([self.state()])
            for at, samples in self.samples_at.items():
                if self.t - self.step_s < at <= self.t:
                    self.backend.write_samples(samples)
        if self.hold is not None:
            self.holds += 1
            self.backend.write_samples([self.state(held=True)])


@dataclass
class FakeBackend:
    """A runtime whose answers the tests script."""

    images: set[str] = field(default_factory=set)
    networks: set[str] = field(default_factory=set)
    containers: dict[str, FakeContainer] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    jobs: list[ContainerSpec] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    execs: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    # The container events (task P2-T03): what was paused, resumed and killed, in order, and
    # what `ros2 param set` answers.
    paused: list[tuple[str, bool]] = field(default_factory=list)
    killed: list[tuple[str, str]] = field(default_factory=list)
    param_result: ExecResult = field(default_factory=lambda: ExecResult(0, PARAM_OK))
    world_result: ExecResult = field(default_factory=lambda: ExecResult(0, WORLD_OK))
    """What the simulator answers a world event's service (`gz service` on the sim container)."""
    control_result: ExecResult = field(default_factory=lambda: ExecResult(0, "data: true\n"))
    """What the world's control service answers (`sim.pause`, `sim.step`, `sim.resume`)."""
    stats_calls: int = 0
    """How often the world's statistics were read (`sim.step` polls them); each answer is one
    second of simulation further, paused."""
    controls: list[str] = field(default_factory=list)
    """The `WorldControl` requests made, in order."""
    world_polls_needed: int = 2
    bridge_polls_needed: int = 1
    spawn_result: ExecResult = field(
        default_factory=lambda: ExecResult(0, "[create-1] process has finished cleanly\n")
    )
    probe_result: ExecResult = field(default_factory=lambda: ExecResult(0, PROBE_OK))
    goal_result: ExecResult = field(default_factory=lambda: ExecResult(0, GOAL_OK))
    rtf_result: ExecResult = field(default_factory=lambda: ExecResult(0, "data: true\n"))
    contract_result: ExecResult = field(default_factory=lambda: ExecResult(0, CONTRACT_OK))
    host_info: HostInfo = field(default_factory=lambda: HostInfo(16, 32 * 1024**3))
    fail_start: dict[str, BaseException] = field(default_factory=dict)
    fail_stop: set[str] = field(default_factory=set)
    # The world's video recorder: the answers to the start and stop requests, whether the file
    # exists once stopped, what copying it does.
    video_start_result: ExecResult = field(default_factory=lambda: ExecResult(0, "data: true\n"))
    video_stop_result: ExecResult = field(default_factory=lambda: ExecResult(0, "data: true\n"))
    video_file_polls_needed: int = 1
    video_file_polls: int = 0
    fail_copy: bool = False
    copies: list[tuple[str, str, Path]] = field(default_factory=list)
    exit_on_poll: dict[str, int] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)
    # The observer of the recorder container, scripted: the samples written into the run
    # directory's samples.jsonl when the recorder starts (a `ready` line by default) and when
    # the goal is sent (what the vehicle "did"); `wall` stamps them unless a sample has one.
    wall: Callable[[], float] = field(default=lambda: 1000.0)
    clock: FakeClock | None = None
    """When set, sending a goal takes `goal_duration_s` of this clock before the samples."""
    goal_duration_s: float = 10.0
    samples_on_start: list[dict[str, Any] | str] | None = None
    samples_on_goal: list[dict[str, Any] | str] = field(default_factory=list)
    samples_file: Path | None = None
    # Radio links (task P2-T02): the networks' labels and removals, the scripts the link
    # holders ran and their answer, and the ground station's network.jsonl, scripted like the
    # observer's samples (a `ready` line by default when it starts, more when the goal is sent).
    network_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    removed_networks: list[str] = field(default_factory=list)
    fail_remove_network: set[str] = field(default_factory=set)
    link_scripts: list[tuple[str, str]] = field(default_factory=list)
    link_result: ExecResult = field(default_factory=lambda: ExecResult(0, LINK_OK))
    traffic_result: ExecResult = field(default_factory=lambda: ExecResult(0, TRAFFIC_OK))
    network_on_start: list[dict[str, Any] | str] | None = None
    network_on_goal: list[dict[str, Any] | str] = field(default_factory=list)
    network_file: Path | None = None
    # The resource monitor (task P2-T05): the usage samples each container's statistics
    # stream yields (by name, or `*` for every container), in order; none by default.
    usage_samples: dict[str, list[ResourceUsage]] = field(default_factory=dict)
    usage_requested: list[str] = field(default_factory=list)

    def image_exists(self, image: str) -> bool:
        return image in self.images

    def ensure_network(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.networks.add(name)
        self.network_labels.setdefault(name, dict(labels or {}))

    def remove_network(self, name: str) -> None:
        if name in self.fail_remove_network:
            raise BackendError(f"network {name}: cannot remove it: has active endpoints")
        self.removed_networks.append(name)
        self.networks.discard(name)

    def find_networks(self, label: str, value: str) -> list[str]:
        return sorted(
            n for n in self.networks if self.network_labels.get(n, {}).get(label) == value
        )

    def address(self, name: str, network: str) -> str:
        container = self.containers.get(name)
        if container is None:
            raise BackendError(f"{name}: no such container")
        names = sorted(self.containers)
        return f"10.{names.index(name) + 1}.0.2"

    def write_network(self, lines: Sequence[dict[str, Any] | str]) -> None:
        """Append lines to the run's network.jsonl as the ground station would."""
        if self.network_file is None:
            return
        with self.network_file.open("a") as file:
            for line in lines:
                if isinstance(line, str):
                    file.write(line + "\n")
                else:
                    file.write(json.dumps({"wall": self.wall(), **line}) + "\n")

    def start(self, spec: ContainerSpec) -> None:
        if spec.name in self.fail_start:
            raise self.fail_start[spec.name]
        if spec.name in self.containers:
            raise BackendError(f"{spec.name}: name in use")
        self.containers[spec.name] = FakeContainer(spec, logs=f"log of {spec.name}\n")
        self.started.append(spec.name)
        if spec.role == "discovery":
            self.containers[spec.name].logs += DISCOVERY_LOG
        if spec.role == "ground":
            run_dir = next(m.source for m in spec.mounts if not m.read_only)
            self.network_file = Path(run_dir) / "network.jsonl"
            lines = self.network_on_start
            if lines is None:
                lines = [{"kind": "ready", "addresses": {}, "heartbeats": [], "probing": True}]
            self.write_network(lines)
        if spec.role == "recorder":
            run_dir = next(m.source for m in spec.mounts if not m.read_only)
            self.samples_file = Path(run_dir) / "samples.jsonl"
            samples = self.samples_on_start
            if samples is None:
                samples = [{"kind": "ready", "recording": True, "subscribed": [], "waiting": []}]
            self.write_samples(samples)

    def write_samples(self, samples: Sequence[dict[str, Any] | str]) -> None:
        """Append samples to the run's samples.jsonl as the observer would (strings verbatim)."""
        if self.samples_file is None:
            return
        self.samples_file.parent.mkdir(parents=True, exist_ok=True)
        with self.samples_file.open("a") as file:
            for sample in samples:
                if isinstance(sample, str):
                    file.write(sample + "\n")
                    continue
                record = {"wall": self.wall(), "sim": None, **sample}
                # `wall_offset_s` stamps a sample before (or after) the moment it is written.
                offset = record.pop("wall_offset_s", None)
                if offset is not None:
                    record["wall"] = self.wall() + float(offset)
                file.write(json.dumps(record) + "\n")

    def run_to_completion(self, spec: ContainerSpec, timeout_s: float) -> ExecResult:
        self.jobs.append(spec)
        return self.spawn_result

    def exec(self, name: str, argv: Sequence[str], on_line: LineSink | None = None) -> ExecResult:
        self.execs.append((name, tuple(argv)))
        container = self.containers[name]
        if not container.running:
            raise BackendError(f"{name}: not running")
        if container.paused:
            raise BackendError(f"{name}: container is paused")
        self.polls[name] = self.polls.get(name, 0) + 1
        if name in self.exit_on_poll and self.polls[name] >= self.exit_on_poll[name]:
            container.running, container.exit_code = False, 137
        result = self._answer(container, tuple(argv))
        if on_line is not None:
            for line in result.output.splitlines():
                on_line(line)
        return result

    def _answer(  # noqa: PLR0911, PLR0912
        self, container: FakeContainer, argv: tuple[str, ...]
    ) -> ExecResult:
        command = " ".join(argv)
        role = container.spec.role
        if role == "sim" and "set_physics" in command:
            return self.rtf_result
        if role == "sim" and "/fieldproof/" in command:
            return self.world_result
        if role == "sim" and "gz topic -e" in command and "/stats" in command:
            self.stats_calls += 1
            return ExecResult(0, world_stats(10.0 + self.stats_calls, paused=True))
        if role == "sim" and "/control" in command:
            request = command.split("--req ", 1)[1]
            self.controls.append(request.strip("'"))
            return self.control_result
        if role == "sim" and "record_video" in command:
            return self.video_stop_result if "stop: true" in command else self.video_start_result
        if role == "sim" and command.startswith("bash -lc test -s "):
            self.video_file_polls += 1
            return ExecResult(0 if self.video_file_polls >= self.video_file_polls_needed else 1, "")
        if role == "sim":
            ready = self.polls[container.spec.name] >= self.world_polls_needed
            return ExecResult(0, WORLD_LIST if ready else "/world/depot/control\n")
        if role == "bridge" and "contract_check" in command:
            return self.contract_result
        if role == "bridge" and "param set" in command:
            return self.param_result
        if role == "bridge":
            ready = self.polls[container.spec.name] >= self.bridge_polls_needed
            namespace = next(
                (
                    a.split(":=", 1)[1]
                    for a in container.spec.command
                    if a.startswith("namespace:=")
                ),
                "",
            )
            topics = NAMESPACED_TOPICS.format(ns=namespace) if namespace else TOPIC_LIST
            return ExecResult(0, topics if ready else "/clock\n")
        if role == "link" and "tc -s -j" in argv[-1]:
            return self.traffic_result
        if role == "link":
            self.link_scripts.append((container.spec.name, argv[-1]))
            return self.link_result
        if "send_goal" in command:
            if self.clock is not None:
                self.clock.sleep(self.goal_duration_s)
            self.write_samples(self.samples_on_goal)
            self.write_network(self.network_on_goal)
            return self.goal_result
        if "param set" in command:
            return self.param_result
        return self.probe_result

    def state(self, name: str) -> ContainerState:
        container = self.containers.get(name)
        if container is None:
            return ContainerState(exists=False)
        return ContainerState(True, container.running, container.exit_code, paused=container.paused)

    def usage(self, name: str) -> Iterator[ResourceUsage]:
        self.usage_requested.append(name)
        if name not in self.containers:
            raise BackendError(f"{name}: no such container")
        yield from self.usage_samples.get(name, self.usage_samples.get("*", []))

    def logs(self, name: str) -> str:
        return self.containers[name].logs

    def stop(self, name: str, timeout_s: float) -> None:
        if name in self.fail_stop:
            raise BackendError(f"{name}: cannot stop")
        self.containers[name].running = False
        self.stopped.append(name)

    def pause(self, name: str) -> None:
        container = self.containers.get(name)
        if container is None or not container.running:
            raise BackendError(f"{name}: not running")
        container.paused = True
        self.paused.append((name, True))

    def unpause(self, name: str) -> None:
        container = self.containers.get(name)
        if container is None or not container.paused:
            raise BackendError(f"{name}: not paused")
        container.paused = False
        self.paused.append((name, False))

    def kill(self, name: str, signal: str) -> None:
        container = self.containers.get(name)
        if container is None or not container.running:
            raise BackendError(f"{name}: not running")
        container.running, container.exit_code, container.paused = False, 137, False
        self.killed.append((name, signal))

    def remove(self, name: str) -> None:
        self.containers.pop(name, None)
        self.removed.append(name)

    def find(self, label: str, value: str) -> list[str]:
        return [n for n, c in self.containers.items() if c.spec.labels.get(label) == value]

    def host(self) -> HostInfo:
        return self.host_info

    def copy_from(self, name: str, path: str, destination: Path) -> int:
        self.copies.append((name, path, destination))
        if self.fail_copy:
            raise BackendError(f"{name}: no such container or file {path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = FAKE_MP4
        destination.write_bytes(data)
        return len(data)
