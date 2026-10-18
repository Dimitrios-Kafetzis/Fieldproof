"""A fake container runtime and clock for the runner's tests (tasks P1-T05, P1-T06)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
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


@dataclass
class FakeContainer:
    spec: ContainerSpec
    running: bool = True
    exit_code: int | None = None
    logs: str = ""


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

    def image_exists(self, image: str) -> bool:
        return image in self.images

    def ensure_network(self, name: str) -> None:
        self.networks.add(name)

    def start(self, spec: ContainerSpec) -> None:
        if spec.name in self.fail_start:
            raise self.fail_start[spec.name]
        if spec.name in self.containers:
            raise BackendError(f"{spec.name}: name in use")
        self.containers[spec.name] = FakeContainer(spec, logs=f"log of {spec.name}\n")
        self.started.append(spec.name)
        if spec.role == "discovery":
            self.containers[spec.name].logs += DISCOVERY_LOG
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
                file.write(json.dumps(record) + "\n")

    def run_to_completion(self, spec: ContainerSpec, timeout_s: float) -> ExecResult:
        self.jobs.append(spec)
        return self.spawn_result

    def exec(self, name: str, argv: Sequence[str], on_line: LineSink | None = None) -> ExecResult:
        self.execs.append((name, tuple(argv)))
        container = self.containers[name]
        if not container.running:
            raise BackendError(f"{name}: not running")
        self.polls[name] = self.polls.get(name, 0) + 1
        if name in self.exit_on_poll and self.polls[name] >= self.exit_on_poll[name]:
            container.running, container.exit_code = False, 137
        result = self._answer(container, tuple(argv))
        if on_line is not None:
            for line in result.output.splitlines():
                on_line(line)
        return result

    def _answer(self, container: FakeContainer, argv: tuple[str, ...]) -> ExecResult:  # noqa: PLR0911
        command = " ".join(argv)
        role = container.spec.role
        if role == "sim" and "set_physics" in command:
            return self.rtf_result
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
        if "send_goal" in command:
            if self.clock is not None:
                self.clock.sleep(self.goal_duration_s)
            self.write_samples(self.samples_on_goal)
            return self.goal_result
        return self.probe_result

    def state(self, name: str) -> ContainerState:
        container = self.containers.get(name)
        if container is None:
            return ContainerState(exists=False)
        return ContainerState(True, container.running, container.exit_code)

    def logs(self, name: str) -> str:
        return self.containers[name].logs

    def stop(self, name: str, timeout_s: float) -> None:
        if name in self.fail_stop:
            raise BackendError(f"{name}: cannot stop")
        self.containers[name].running = False
        self.stopped.append(name)

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
