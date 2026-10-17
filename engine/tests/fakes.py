"""A fake container runtime and clock for the runner's tests (task P1-T05)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from fieldproof.backend import BackendError, ContainerSpec, ContainerState, ExecResult, LineSink

WORLD_LIST = "/world/depot/control\n/world/depot/create\n/world/depot/set_physics\n"
TOPIC_LIST = "/clock\n/odom\n/scan\n/tf\n"
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
    fail_start: dict[str, BaseException] = field(default_factory=dict)
    fail_stop: set[str] = field(default_factory=set)
    exit_on_poll: dict[str, int] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)

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

    def _answer(self, container: FakeContainer, argv: tuple[str, ...]) -> ExecResult:
        command = " ".join(argv)
        role = container.spec.role
        if role == "sim" and "set_physics" in command:
            return self.rtf_result
        if role == "sim":
            ready = self.polls[container.spec.name] >= self.world_polls_needed
            return ExecResult(0, WORLD_LIST if ready else "/world/depot/control\n")
        if role == "bridge":
            ready = self.polls[container.spec.name] >= self.bridge_polls_needed
            return ExecResult(0, TOPIC_LIST if ready else "/clock\n")
        if "send_goal" in command:
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
