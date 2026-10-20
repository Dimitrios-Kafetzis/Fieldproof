"""Tests of the Docker backend against a stub of the SDK's client (tasks P1-T05, P2-T02)."""

from __future__ import annotations

import io
import tarfile
import threading
from pathlib import Path
from typing import Any

import pytest
import requests
from docker.errors import APIError, ImageNotFound, NotFound

from fieldproof.backend import BackendError, ContainerSpec, HostInfo, Mount
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

    def get_archive(self, path: str) -> tuple[Any, dict[str, Any]]:
        self.calls.append(("get_archive", path))
        if path == "/scratch/missing.mp4":
            raise NotFound(path)
        if path == "/scratch/api-error.mp4":
            raise APIError("boom")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            if path == "/scratch/dir":
                info = tarfile.TarInfo("dir")
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100
                info = tarfile.TarInfo(path.rsplit("/", 1)[-1])
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        payload = buffer.getvalue()
        return iter([payload[:50], payload[50:]]), {"size": len(payload)}


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
    def __init__(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.name = name
        self.labels = labels or {}
        self.connected: list[tuple[str, list[str]]] = []
        self.removed = False

    def connect(self, container: StubContainer, aliases: list[str]) -> None:
        if self.name == "refuses":
            raise APIError("no room")
        self.connected.append((container.name, aliases))

    def remove(self) -> None:
        if self.name == "busy":
            raise APIError("has active endpoints")
        self.removed = True


class StubNetworks:
    def __init__(self) -> None:
        self.items = [StubNetwork("fieldproof")]
        self.created: list[str] = []
        self.labels: dict[str, dict[str, str]] = {}

    def list(
        self, names: list[str] | None = None, filters: dict[str, str] | None = None
    ) -> list[StubNetwork]:
        if filters is not None:
            key, value = filters["label"].split("=", 1)
            if key == "api-error":
                raise APIError("boom")
            return [n for n in self.items if n.labels.get(key) == value]
        return [n for n in self.items if names and names[0] in n.name]

    def get(self, name: str) -> StubNetwork:
        if name == "api-error":
            raise APIError("boom")
        found = next((n for n in self.items if n.name == name), None)
        if found is None:
            raise NotFound(name)
        return found

    def create(self, name: str, driver: str, labels: dict[str, str]) -> None:
        self.created.append(name)
        self.labels[name] = labels
        self.items.append(StubNetwork(name, labels))


class StubApi:
    def __init__(self, output: list[bytes], code: int | None = 0) -> None:
        self.output = output
        self.code = code
        self.calls: list[Any] = []

    def create_endpoint_config(self, aliases: list[str]) -> dict[str, Any]:
        return {"Aliases": aliases}

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
        self.info_error: Exception | None = None

    def info(self) -> dict[str, Any]:
        if self.info_error is not None:
            raise self.info_error
        return {"NCPU": 16, "MemTotal": 32 * 1024**3, "ServerVersion": "29.9.0"}


@pytest.fixture
def client() -> StubClient:
    return StubClient()


@pytest.fixture
def backend(client: StubClient) -> DockerBackend:
    return DockerBackend(client)  # type: ignore[arg-type]


def test_copy_from(backend: DockerBackend, client: StubClient, tmp_path: Path) -> None:
    container = StubContainer("c1", {"State": {"Running": True}})
    client.containers.items["c1"] = container
    destination = tmp_path / "video" / "spectator.mp4"
    size = backend.copy_from("c1", "/scratch/spectator.mp4", destination)
    assert size == 112
    assert destination.read_bytes()[4:8] == b"ftyp"
    assert destination.stat().st_size == 112
    assert ("get_archive", "/scratch/spectator.mp4") in container.calls
    with pytest.raises(BackendError, match=r"no such container or file /scratch/missing.mp4"):
        backend.copy_from("c1", "/scratch/missing.mp4", destination)
    with pytest.raises(BackendError, match=r"cannot copy /scratch/api-error.mp4: boom"):
        backend.copy_from("c1", "/scratch/api-error.mp4", destination)
    with pytest.raises(BackendError, match="/scratch/dir is not a file"):
        backend.copy_from("c1", "/scratch/dir", destination)
    with pytest.raises(BackendError, match="no such container"):
        backend.copy_from("gone", "/scratch/spectator.mp4", destination)


def test_images_and_network(backend: DockerBackend, client: StubClient) -> None:
    assert backend.image_exists("present:1")
    assert not backend.image_exists("absent:1")
    with pytest.raises(BackendError, match="boom"):
        backend.image_exists("api-error")
    backend.ensure_network("fieldproof")
    assert client.networks.created == []
    backend.ensure_network("lab")
    assert client.networks.created == ["lab"]
    assert client.networks.labels["lab"] == {}
    backend.ensure_network("radio", {"io.fieldproof.run": "abc"})
    assert client.networks.labels["radio"] == {"io.fieldproof.run": "abc"}


def test_networks_of_a_run(backend: DockerBackend, client: StubClient) -> None:
    backend.ensure_network("abc-radio", {"io.fieldproof.run": "abc"})
    backend.ensure_network("abc-onboard", {"io.fieldproof.run": "abc"})
    backend.ensure_network("other", {"io.fieldproof.run": "zzz"})
    assert backend.find_networks("io.fieldproof.run", "abc") == ["abc-radio", "abc-onboard"]
    with pytest.raises(BackendError, match="cannot list networks"):
        backend.find_networks("api-error", "x")
    backend.remove_network("abc-radio")
    assert client.networks.get("abc-radio").removed
    backend.remove_network("never-made")
    client.networks.items.append(StubNetwork("busy"))
    with pytest.raises(BackendError, match="network busy: cannot remove it"):
        backend.remove_network("busy")


def test_address(backend: DockerBackend, client: StubClient) -> None:
    client.containers.items["h"] = StubContainer(
        "h", {"NetworkSettings": {"Networks": {"radio": {"IPAddress": "172.20.0.2"}}}}
    )
    assert backend.address("h", "radio") == "172.20.0.2"
    with pytest.raises(BackendError, match="no address on network onboard"):
        backend.address("h", "onboard")
    with pytest.raises(BackendError, match="no such container"):
        backend.address("gone", "radio")
    with pytest.raises(BackendError, match="cannot inspect"):
        backend.address("api-error", "radio")


def test_start_behind_a_link(backend: DockerBackend, client: StubClient) -> None:
    client.networks.items += [StubNetwork("onboard"), StubNetwork("radio")]
    backend.start(
        ContainerSpec(
            name="holder",
            image="present:1",
            role="link",
            network="onboard",
            networks=("radio",),
            aliases=("ugv1",),
            hostname="ugv1",
            cap_add=("NET_ADMIN",),
        )
    )
    created = client.containers.created[-1]
    assert created["network"] == "onboard"
    assert created["hostname"] == "ugv1"
    assert created["cap_add"] == ["NET_ADMIN"]
    assert created["networking_config"] == {"onboard": {"Aliases": ["ugv1"]}}
    assert client.networks.get("radio").connected == [("holder", ["ugv1"])]
    backend.start(
        ContainerSpec(
            name="vehicle",
            image="present:1",
            role="vehicle",
            network="ignored",
            hostname="ignored",
            network_mode="container:holder",
        )
    )
    created = client.containers.created[-1]
    assert created["network_mode"] == "container:holder"
    assert "network" not in created
    assert "hostname" not in created
    assert created["cap_add"] is None
    client.networks.items.append(StubNetwork("refuses"))
    with pytest.raises(BackendError, match="cannot join network refuses: no room"):
        backend.start(
            ContainerSpec(name="c9", image="present:1", role="link", networks=("refuses",))
        )
    assert ("remove", {"force": True, "v": True}) in client.containers.items["c9"].calls


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
        user="1001:1001",
        working_dir="/fieldproof/run",
    )
    backend.start(spec)
    (created,) = client.containers.created
    assert created["user"] == "1001:1001"
    assert created["working_dir"] == "/fieldproof/run"
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
    assert client.containers.created[-1]["user"] is None
    assert client.containers.created[-1]["working_dir"] is None


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


def test_host(backend: DockerBackend, client: StubClient) -> None:
    assert backend.host() == HostInfo(16, 32 * 1024**3)
    client.info_error = APIError("down")
    with pytest.raises(BackendError, match="daemon's info"):
        backend.host()


def test_create_applies_the_budget(backend: DockerBackend, client: StubClient) -> None:
    backend.start(
        ContainerSpec(
            name="v1",
            image="present:1",
            role="vehicle",
            cpus=1.5,
            cpuset="0-1",
            memory="2g",
            pids=256,
            mounts=(Mount(Path("/host/dds.xml"), "/opt/fieldproof/dds/profile.xml"),),
        )
    )
    (created,) = client.containers.created
    assert created["nano_cpus"] == 1_500_000_000
    assert created["cpuset_cpus"] == "0-1"
    assert created["mem_limit"] == "2g"
    assert created["pids_limit"] == 256
    (mount,) = created["mounts"]
    assert mount["Target"] == "/opt/fieldproof/dds/profile.xml"
    assert mount["ReadOnly"] is True
    backend.start(ContainerSpec(name="v2", image="present:1", role="vehicle"))
    assert client.containers.created[1]["cpuset_cpus"] is None


def test_each_thread_gets_its_own_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[str] = []

    def connect() -> Any:
        made.append(threading.current_thread().name)
        return StubClient()

    monkeypatch.setattr(DockerBackend, "_connect", staticmethod(connect))
    backend = DockerBackend()
    assert backend.client is backend.client, "one connection per thread"
    seen: list[Any] = []

    def use() -> None:
        seen.append(backend.client)
        seen.append(backend.client)

    thread = threading.Thread(target=use, name="worker-1")
    thread.start()
    thread.join()
    assert seen[0] is seen[1]
    assert seen[0] is not backend.client
    assert made == [threading.main_thread().name, "worker-1"]
    # An injected client is shared by every thread.
    shared = StubClient()
    injected = DockerBackend(shared)  # type: ignore[arg-type]
    seen.clear()
    thread = threading.Thread(target=lambda: seen.append(injected.client))
    thread.start()
    thread.join()
    assert seen == [shared]
    assert made == [threading.main_thread().name, "worker-1"], "nothing new connected"
