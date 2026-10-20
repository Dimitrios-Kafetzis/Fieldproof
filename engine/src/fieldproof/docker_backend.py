"""The :class:`fieldproof.backend.Backend` on the Docker SDK (task P1-T05, ADR-0004).

Every operation maps to one or two Docker API calls; errors of the daemon surface as
:class:`BackendError` with the container's name, so the runner can record and continue its
teardown. Commands run inside containers through the low-level exec API so their output streams
line by line (the readiness probe prints what is still pending) and their exit code is read
afterwards. The runner drives a fleet from several threads at once (task P2-T01); the SDK's
client holds one HTTP session, which is not meant to be shared, so each thread gets its own
client to the same daemon unless one was injected.
"""

from __future__ import annotations

import io
import math
import tarfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import docker
import requests
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.types import Mount as DockerMount

from fieldproof.backend import (
    BackendError,
    ContainerSpec,
    ContainerState,
    ExecResult,
    HostInfo,
    LineSink,
)


class DockerBackend:
    """Containers through the Docker daemon the environment points at (``DOCKER_HOST``)."""

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._shared = client
        self._local = threading.local()
        if client is None:
            self._local.client = self._connect()

    @staticmethod
    def _connect() -> docker.DockerClient:
        try:
            return docker.from_env()
        except DockerException as error:
            raise BackendError(f"cannot reach the Docker daemon: {error}") from error

    @property
    def client(self) -> docker.DockerClient:
        """The injected client, or this thread's own connection to the daemon."""
        if self._shared is not None:
            return self._shared
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._connect()
            self._local.client = client
        return client

    def host(self) -> HostInfo:
        """The daemon's cores and memory (`docker info`)."""
        try:
            info = self.client.info()
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"cannot read the daemon's info: {error}") from error
        return HostInfo(int(info.get("NCPU") or 0), int(info.get("MemTotal") or 0))

    # ------------------------------------------------------------------------------- images
    def image_exists(self, image: str) -> bool:
        """Whether the image is present locally."""
        try:
            self.client.images.get(image)
        except ImageNotFound:
            return False
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{image}: {error}") from error
        return True

    def ensure_network(self, name: str, labels: dict[str, str] | None = None) -> None:
        """Create the bridge network unless it exists (with `labels` when it is created)."""
        try:
            if not any(n.name == name for n in self.client.networks.list(names=[name])):
                self.client.networks.create(name, driver="bridge", labels=dict(labels or {}))
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"network {name}: {error}") from error

    def remove_network(self, name: str) -> None:
        """Remove the network if it exists."""
        try:
            self.client.networks.get(name).remove()
        except NotFound:
            return
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"network {name}: cannot remove it: {error}") from error

    def find_networks(self, label: str, value: str) -> list[str]:
        """Names of every network carrying `label=value`."""
        try:
            networks = self.client.networks.list(filters={"label": f"{label}={value}"})
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"cannot list networks: {error}") from error
        return [str(n.name) for n in networks]

    def address(self, name: str, network: str) -> str:
        """The container's IPv4 address on a network it joined."""
        try:
            container = self.client.containers.get(name)
        except NotFound as error:
            raise BackendError(f"{name}: no such container") from error
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{name}: cannot inspect the container: {error}") from error
        networks = container.attrs.get("NetworkSettings", {}).get("Networks") or {}
        address = (networks.get(network) or {}).get("IPAddress")
        if not address:
            raise BackendError(f"{name}: no address on network {network}")
        return str(address)

    # --------------------------------------------------------------------------- containers
    def _create(self, spec: ContainerSpec) -> Container:
        mounts = [
            DockerMount(m.target, str(m.source), type="bind", read_only=m.read_only)
            for m in spec.mounts
        ]
        networking: dict[str, Any] = {}
        if spec.network_mode is not None:
            networking["network_mode"] = spec.network_mode
        else:
            networking["hostname"] = spec.hostname
            networking["network"] = spec.network
            if spec.network is not None and spec.aliases:
                networking["networking_config"] = {
                    spec.network: self.client.api.create_endpoint_config(aliases=list(spec.aliases))
                }
        try:
            container = self.client.containers.create(
                spec.image,
                command=list(spec.command) or None,
                name=spec.name,
                environment=dict(spec.env),
                labels=dict(spec.labels),
                mounts=mounts,
                devices=[f"{d}:{d}:rwm" for d in spec.devices],
                group_add=list(spec.group_add),
                nano_cpus=int(spec.cpus * 1e9) if spec.cpus is not None else None,
                cpuset_cpus=spec.cpuset,
                mem_limit=spec.memory,
                pids_limit=spec.pids,
                user=spec.user,
                working_dir=spec.working_dir,
                cap_add=list(spec.cap_add) or None,
                detach=True,
                **networking,
            )
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{spec.name}: cannot create the container: {error}") from error
        for network in spec.networks:
            try:
                self.client.networks.get(network).connect(container, aliases=list(spec.aliases))
            except (APIError, requests.RequestException) as error:
                self._remove(container)
                raise BackendError(
                    f"{spec.name}: cannot join network {network}: {error}"
                ) from error
        return container

    def start(self, spec: ContainerSpec) -> None:
        """Create and start a container in the background."""
        container = self._create(spec)
        try:
            container.start()
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{spec.name}: cannot start the container: {error}") from error

    def run_to_completion(self, spec: ContainerSpec, timeout_s: float) -> ExecResult:
        """Create, start and wait for a one-shot container, remove it, return its result."""
        container = self._create(spec)
        try:
            try:
                container.start()
                result = container.wait(timeout=timeout_s)
            except (APIError, requests.RequestException) as error:
                output = self._logs(container)
                raise BackendError(
                    f"{spec.name}: no exit after {timeout_s:.0f} s ({error}):\n"
                    + "\n".join(output.splitlines()[-20:])
                ) from error
            return ExecResult(int(result.get("StatusCode", -1)), self._logs(container))
        finally:
            self._remove(container)

    def exec(self, name: str, argv: Sequence[str], on_line: LineSink | None = None) -> ExecResult:
        """Run a command in a running container, streaming its lines to `on_line` if given."""
        api = self.client.api
        chunks: list[str] = []
        pending = ""
        try:
            exec_id = api.exec_create(name, list(argv))["Id"]
            for chunk in api.exec_start(exec_id, stream=True):
                text = chunk.decode("utf-8", errors="replace")
                chunks.append(text)
                if on_line is not None:
                    pending += text
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        on_line(line)
            if on_line is not None and pending:
                on_line(pending)
            code = api.exec_inspect(exec_id).get("ExitCode")
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{name}: exec failed: {error}") from error
        return ExecResult(int(code) if code is not None else -1, "".join(chunks))

    def state(self, name: str) -> ContainerState:
        """The container's state (`exists` false when there is no such container)."""
        try:
            container = self.client.containers.get(name)
        except NotFound:
            return ContainerState(exists=False)
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{name}: cannot inspect the container: {error}") from error
        state = container.attrs.get("State", {})
        running = bool(state.get("Running"))
        health = (state.get("Health") or {}).get("Status")
        return ContainerState(
            exists=True,
            running=running,
            exit_code=None if running else state.get("ExitCode"),
            health=str(health) if health else None,
        )

    def logs(self, name: str) -> str:
        """The container's output so far."""
        try:
            return self._logs(self.client.containers.get(name))
        except NotFound:
            return ""
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{name}: cannot read the logs: {error}") from error

    def stop(self, name: str, timeout_s: float) -> None:
        """Stop the container, killing it after `timeout_s`."""
        try:
            self.client.containers.get(name).stop(timeout=math.ceil(timeout_s))
        except NotFound:
            return
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{name}: cannot stop the container: {error}") from error

    def remove(self, name: str) -> None:
        """Remove the container (forced)."""
        try:
            self._remove(self.client.containers.get(name))
        except NotFound:
            return

    def find(self, label: str, value: str) -> list[str]:
        """Names of every container carrying `label=value`, running or not.

        Through the low-level list call, whose answer carries the names: the high-level one
        inspects every container it lists, which fails when one is removed in between (a
        spawn's worker removing its one-shot container during an interrupted run, #56).
        """
        try:
            containers = self.client.api.containers(all=True, filters={"label": f"{label}={value}"})
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"cannot list containers: {error}") from error
        return [str(c["Names"][0]).lstrip("/") for c in containers if c.get("Names")]

    def copy_from(self, name: str, path: str, destination: Path) -> int:
        """Copy a file out of a container to `destination`; return the bytes written."""
        try:
            stream, _stat = self.client.containers.get(name).get_archive(path)
            archive = io.BytesIO(b"".join(stream))
        except NotFound as error:
            raise BackendError(f"{name}: no such container or file {path}") from error
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{name}: cannot copy {path}: {error}") from error
        with tarfile.open(fileobj=archive) as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            if not members:
                raise BackendError(f"{name}: {path} is not a file")
            extracted = tar.extractfile(members[0])
            if extracted is None:
                raise BackendError(f"{name}: {path} cannot be read")
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = extracted.read()
        destination.write_bytes(data)
        return len(data)

    # ---------------------------------------------------------------------------------- helpers
    @staticmethod
    def _logs(container: Container) -> str:
        try:
            return container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        except (APIError, requests.RequestException):
            return ""

    @staticmethod
    def _remove(container: Container) -> None:
        try:
            container.remove(force=True, v=True)
        except NotFound:
            return
        except (APIError, requests.RequestException) as error:
            raise BackendError(f"{container.name}: cannot remove the container: {error}") from error
