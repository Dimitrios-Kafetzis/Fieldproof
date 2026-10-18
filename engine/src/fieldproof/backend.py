"""What the orchestrator needs from a container runtime (task P1-T05, ADR-0004).

:class:`Backend` is the small surface the runner drives containers through: images exist,
a network exists, a container starts detached or runs to completion, a command runs inside one,
its state and logs can be read, it stops and is removed, the containers of a run are found by
label, and the host's capacity is known. :mod:`fieldproof.docker_backend` implements it with the
Docker SDK; the runner's tests use a fake, so the state machine is tested without Docker. The
runner calls it from several threads at once (task P2-T01: the vehicles of a fleet start in
parallel), so an implementation must be safe to share between threads.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class BackendError(RuntimeError):
    """The runtime refused or failed an operation."""


@dataclass(frozen=True)
class Mount:
    """A bind mount."""

    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class ContainerSpec:
    """Everything needed to create one container."""

    name: str
    image: str
    role: str
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    network: str | None = None
    hostname: str | None = None
    mounts: tuple[Mount, ...] = ()
    devices: tuple[str, ...] = ()
    group_add: tuple[str, ...] = ()
    cpus: float | None = None
    """The CPU quota in cores (`--cpus`)."""
    cpuset: str | None = None
    """The cores the container may run on (`--cpuset-cpus`)."""
    memory: str | None = None
    pids: int | None = None
    user: str | None = None
    """`uid:gid` to run as instead of the image's user (the recorder writes as the host user)."""
    working_dir: str | None = None
    """Working directory in the container (the image's when None)."""


@dataclass(frozen=True)
class ExecResult:
    """Exit code and combined output of a command or a one-shot container."""

    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        """Exit code 0."""
        return self.exit_code == 0

    def tail(self, lines: int = 20) -> str:
        """The last lines of the output."""
        return "\n".join(self.output.rstrip().splitlines()[-lines:])


@dataclass(frozen=True)
class ContainerState:
    """What `docker inspect` says about a container."""

    exists: bool
    running: bool = False
    exit_code: int | None = None
    health: str | None = None


@dataclass(frozen=True)
class HostInfo:
    """What the runtime's host can give: cores and memory."""

    cpus: int
    memory_bytes: int


LineSink = Callable[[str], None]


class Backend(Protocol):
    """A container runtime, as the runner sees it."""

    def image_exists(self, image: str) -> bool:
        """Whether the image is present locally."""

    def ensure_network(self, name: str) -> None:
        """Create the bridge network unless it exists."""

    def start(self, spec: ContainerSpec) -> None:
        """Create and start a container in the background."""

    def run_to_completion(self, spec: ContainerSpec, timeout_s: float) -> ExecResult:
        """Create, start and wait for a one-shot container, remove it, return its result."""

    def exec(self, name: str, argv: Sequence[str], on_line: LineSink | None = None) -> ExecResult:
        """Run a command in a running container, streaming its lines to `on_line` if given."""

    def state(self, name: str) -> ContainerState:
        """The container's state (`exists` false when there is no such container)."""

    def logs(self, name: str) -> str:
        """The container's output so far."""

    def stop(self, name: str, timeout_s: float) -> None:
        """Stop the container, killing it after `timeout_s`."""

    def remove(self, name: str) -> None:
        """Remove the container (forced)."""

    def find(self, label: str, value: str) -> list[str]:
        """Names of every container carrying `label=value`, running or not."""

    def copy_from(self, name: str, path: str, destination: Path) -> int:
        """Copy a file out of a container to `destination`; return the bytes written."""

    def host(self) -> HostInfo:
        """The cores and memory of the host the containers run on."""
