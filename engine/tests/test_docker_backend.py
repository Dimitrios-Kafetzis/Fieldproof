"""Tests of the Docker backend against a stub of the SDK's client (task P1-T05)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests
from docker.errors import APIError, ImageNotFound, NotFound

from fieldproof.backend import BackendError, ContainerSpec, Mount
from fieldproof.docker_backend import DockerBackend


class StubContainer:
    def __init__(
        self, name: str, attrs: dict[str, Any], logs: bytes = b"", wait_code: int = 0
    ) -> None:
        self.name = name
        self.attrs = attrs
        self._logs = logs
        self._wait_code = wait_code
        self.calls: list[tuple[str, Any]] = []

    def start(self) -> None:
        self.calls.append(("start", None))

    def wait(self, timeout: float) -> dict[str, Any]:
        self.calls.append(("wait", timeout))
        if self._wait_code < 0:
            raise requests.exceptions.ReadTimeout("slow")
        return {"StatusCode": self._wait_code}

    def logs(self, **kwargs: Any) -> bytes:
        return self._logs

    def stop(self, timeout: int) -> None:
        self.calls.append(("stop", timeout))

    def remove(self, **kwargs: Any) -> None:
        self.calls.append(("remove", kwargs))


class StubCollection:
    def __init__(self, items: dict[str, Any]) -> None:
        self.items = items
        self.created: list[dict[str, Any]] = []

    def get(self, name: str) -> Any:
        if name == "api-error":
            raise APIError("boom")
        if name not in self.items:
            raise ImageNotFound(name) if "/" in name or ":" in name else NotFound(name)
        return self.items[name]

    def list(self, **kwargs: Any) -> list[Any]:
        if "names" in kwargs:
            return [n for n in self.items.values() if kwargs["names"][0] in n.name]
        wanted = kwargs["filters"]["label"]
        return [c for c in self.items.values() if wanted in c.attrs.get("labels", [])]

    def create(self, image: str, **kwargs: Any) -> StubContainer:
        if image == "broken:1":
            raise APIError("no such image")
        self.created.append({"image": image, **kwargs})
        container = StubContainer(kwargs["name"], {"State": {"Running": True}}, b"out\n", 0)
        self.items[kwargs["name"]] = container
        return container


class StubNetwork:
    def __init__(self, name: str) -> None:
        self.name = name


class StubNetworks:
    def __init__(self) -> None:
        self.items = [StubNetwork("fieldproof")]
        self.created: list[str] = []

    def list(self, names: list[str]) -> list[StubNetwork]:
        return [n for n in self.items if names[0] in n.name]

    def create(self, name: str, driver: str) -> None:
        self.created.append(name)


class StubApi:
    def __init__(self, output: list[bytes], code: int | None = 0) -> None:
        self.output = output
        self.code = code
        self.calls: list[Any] = []

    def exec_create(self, name: str, cmd: list[str]) -> dict[str, str]:
        if name == "gone":
            raise NotFound("gone")
        self.calls.append(("create", name, cmd))
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, stream: bool) -> Any:
        return iter(self.output)

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        return {"ExitCode": self.code}


class StubClient:
    def __init__(self) -> None:
        self.images = StubCollection({"present:1": object()})
        self.networks = StubNetworks()
        self.containers = StubCollection({})
        self.api = StubApi([b"line 1\nli", b"ne 2\n", b"tail"])


@pytest.fixture
def client() -> StubClient:
    return StubClient()


@pytest.fixture
def backend(client: StubClient) -> DockerBackend:
    return DockerBackend(client)  # type: ignore[arg-type]


def test_images_and_network(backend: DockerBackend, client: StubClient) -> None:
    assert backend.image_exists("present:1")
    assert not backend.image_exists("absent:1")
    with pytest.raises(BackendError, match="boom"):
        backend.image_exists("api-error")
    backend.ensure_network("fieldproof")
    assert client.networks.created == []
    backend.ensure_network("lab")
    assert client.networks.created == ["lab"]


def test_start_maps_the_spec(backend: DockerBackend, client: StubClient) -> None:
    spec = ContainerSpec(
        name="c1",
        image="present:1",
        role="sim",
        command=("gz", "sim"),
        env={"A": "1"},
        labels={"l": "v"},
        network="fieldproof",
        hostname="sim",
        mounts=(Mount(Path("/host/worlds"), "/fieldproof/worlds"),),
        devices=("/dev/dri",),
        group_add=("992",),
        cpus=2.0,
        memory="2g",
        pids=100,
    )
    backend.start(spec)
    (created,) = client.containers.created
    assert created["image"] == "present:1"
    assert created["command"] == ["gz", "sim"]
    assert created["name"] == "c1"
    assert created["hostname"] == "sim"
    assert created["environment"] == {"A": "1"}
    assert created["labels"] == {"l": "v"}
    assert created["network"] == "fieldproof"
    assert created["mounts"][0]["Target"] == "/fieldproof/worlds"
    assert created["mounts"][0]["Source"] == "/host/worlds"
    assert created["mounts"][0]["ReadOnly"] is True
    assert created["devices"] == ["/dev/dri:/dev/dri:rwm"]
    assert created["group_add"] == ["992"]
    assert created["nano_cpus"] == 2_000_000_000
    assert created["mem_limit"] == "2g"
    assert created["pids_limit"] == 100
    assert client.containers.items["c1"].calls == [("start", None)]
    with pytest.raises(BackendError, match="cannot create"):
        backend.start(ContainerSpec(name="c2", image="broken:1", role="sim"))
    minimal = ContainerSpec(name="c3", image="present:1", role="vehicle")
    backend.start(minimal)
    assert client.containers.created[-1]["command"] is None
    assert client.containers.created[-1]["nano_cpus"] is None


def test_run_to_completion(backend: DockerBackend, client: StubClient) -> None:
    result = backend.run_to_completion(
        ContainerSpec(name="job", image="present:1", role="spawn"), 30.0
    )
    assert result.exit_code == 0
    assert result.output == "out\n"
    assert result.ok
    assert ("remove", {"force": True, "v": True}) in client.containers.items["job"].calls
    original = client.containers.create

    def slow(image: str, **kwargs: Any) -> StubContainer:
        container = original(image, **kwargs)
        container._wait_code = -1
        container._logs = b"still spawning\n"
        return container

    client.containers.create = slow  # type: ignore[method-assign]
    with pytest.raises(BackendError, match="no exit after 30 s") as excinfo:
        backend.run_to_completion(ContainerSpec(name="job2", image="present:1", role="spawn"), 30.0)
    assert "still spawning" in str(excinfo.value)
    assert ("remove", {"force": True, "v": True}) in client.containers.items["job2"].calls


def test_exec_streams_lines(backend: DockerBackend, client: StubClient) -> None:
    seen: list[str] = []
    result = backend.exec("c1", ["echo"], seen.append)
    assert result.exit_code == 0
    assert result.output == "line 1\nline 2\ntail"
    assert seen == ["line 1", "line 2", "tail"]
    assert result.tail(1) == "tail"
    assert backend.exec("c1", ["echo"]).output == "line 1\nline 2\ntail"
    client.api.code = None
    assert backend.exec("c1", ["x"]).exit_code == -1
    with pytest.raises(BackendError, match="exec failed"):
        backend.exec("gone", ["x"])


def test_state_logs_stop_remove_find(backend: DockerBackend, client: StubClient) -> None:
    running = StubContainer(
        "r",
        {
            "State": {"Running": True, "ExitCode": 0, "Health": {"Status": "healthy"}},
            "labels": ["io.fieldproof.run=abc"],
        },
        b"hello",
    )
    exited = StubContainer(
        "e", {"State": {"Running": False, "ExitCode": 137}, "labels": ["io.fieldproof.run=abc"]}
    )
    client.containers.items.update({"r": running, "e": exited})
    assert backend.state("r").running
    assert backend.state("r").health == "healthy"
    assert backend.state("r").exit_code is None
    assert backend.state("e").exit_code == 137
    assert backend.state("e").health is None
    assert not backend.state("nope").exists
    with pytest.raises(BackendError, match="inspect"):
        backend.state("api-error")
    assert backend.logs("r") == "hello"
    assert backend.logs("nope") == ""
    backend.stop("r", 2.5)
    assert ("stop", 3) in running.calls
    backend.stop("nope", 1.0)
    backend.remove("e")
    assert ("remove", {"force": True, "v": True}) in exited.calls
    backend.remove("nope")
    assert sorted(backend.find("io.fieldproof.run", "abc")) == ["e", "r"]
    assert backend.find("io.fieldproof.run", "zzz") == []


def test_from_env_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import docker  # noqa: PLC0415

    def refuse() -> Any:
        raise docker.errors.DockerException("no socket")

    monkeypatch.setattr(docker, "from_env", refuse)
    with pytest.raises(BackendError, match="cannot reach the Docker daemon"):
        DockerBackend()
