"""Tests of the fleet orchestration in the runner (task P2-T01): the plan of three vehicles,
the discovery server, the budgets, the parallel start with dependencies, concurrent actions and
the contract checks, on the fake backend."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes import FakeBackend, FakeClock, v0_text
from rich.console import Console
from test_runner import SyncExecutor, scenario_variant

from fieldproof.backend import ContainerSpec, ExecResult, HostInfo, LineSink
from fieldproof.config import Config, load_config
from fieldproof.runner import (
    DOMAIN_LABEL,
    LOG_FILE,
    RECORD_FILE,
    Plan,
    Run,
    RunLog,
    Status,
    auto_domain_id,
    plan,
)
from fieldproof.scenario import Scenario, ScenarioError, load_scenario, validate_data

REPO = Path(__file__).resolve().parents[2]
FLEET = REPO / "examples/scenarios/ugv_fleet.yaml"
EXAMPLE = REPO / "examples/scenarios/ugv_nav_to_goal.yaml"
T0 = 1_700_000_000.0


def fleet_samples() -> list[dict[str, Any] | str]:
    """Every vehicle at its goal, commanding 0.4 m/s, nothing touched."""
    goals = {"ugv1": (10.0, -1.0), "ugv2": (2.0, 15.0), "ugv3": (-4.0, -5.0)}
    samples: list[dict[str, Any] | str] = []
    for name, (x, y) in goals.items():
        samples.append(
            {"kind": "pose", "sim": 9.0, "model": name, "x": x, "y": y, "z": 0.1, "yaw": 0}
        )
        samples.append(
            {
                "kind": "value",
                "sim": 6.0,
                "topic": f"/{name}/cmd_vel",
                "field": "linear.x",
                "value": 0.4,
            }
        )
    samples.append({"kind": "clock", "sim": 9.5, "rtf": 1.0})
    return samples


@pytest.fixture(scope="module")
def fleet() -> Scenario:
    return fleet_variant(lambda _data: None)


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> Config:
    fuel = tmp_path_factory.mktemp("fuel")
    for model in yaml.safe_load((REPO / "assets/fuel.lock").read_text())["models"]:
        (fuel / model["dir"]).mkdir()
    config = load_config(REPO / "fieldproof.toml", env={})
    config.stack.fuel = str(fuel)
    return config


@pytest.fixture
def resolved(fleet: Scenario, config: Config, tmp_path: Path) -> Plan:
    return plan(fleet, config, run_id="fleet-run-1", runs_dir=tmp_path, render="software")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def fleet_variant(mutate: Callable[[dict[str, Any]], None]) -> Scenario:
    text = v0_text(FLEET.read_text())
    data = yaml.safe_load(text)
    mutate(data)
    return validate_data(data, FLEET.resolve(), text)


def ready_backend(resolved: Plan) -> FakeBackend:
    backend = FakeBackend()
    backend.images = {s.image for s in resolved.containers()} | {
        v.spawn_job.image for v in resolved.vehicles
    }
    backend.samples_on_goal = fleet_samples()
    return backend


def execute(
    resolved: Plan,
    backend: FakeBackend,
    clock: FakeClock,
    *,
    check_contracts: bool = False,
    executor: Any = None,
) -> tuple[Any, RunLog]:
    resolved.run_dir.parent.mkdir(parents=True, exist_ok=True)
    log = RunLog(
        resolved.run_id,
        file=resolved.run_dir / LOG_FILE,
        console=Console(file=(resolved.run_dir.parent / "console.txt").open("a")),
        clock=clock.now,
    )
    backend.clock = clock
    backend.wall = lambda: T0 + (clock.now() - 1000.0)
    run = Run(
        resolved,
        backend,
        log,
        check_contracts=check_contracts,
        sleep=clock.sleep,
        clock=clock.now,
        wall=lambda: T0 + (clock.now() - 1000.0),
        executor=executor or SyncExecutor(),  # type: ignore[arg-type]
    )
    return run.execute(), log


# ------------------------------------------------------------------------------------ plan
def test_fleet_example_loads(fleet: Scenario) -> None:
    assert [v.name for v in fleet.vehicles] == ["ugv1", "ugv2", "ugv3"]
    assert [v.namespace for v in fleet.vehicles] == ["ugv1", "ugv2", "ugv3"]
    assert fleet.vehicles[1].resources is not None
    assert fleet.vehicles[1].resources.memory == "3g"
    assert fleet.vehicles[2].depends_on == ["ugv1"]
    assert [v.name for v in fleet.start_order()] == ["ugv1", "ugv2", "ugv3"]
    assert [e.at for e in fleet.timeline] == [0.0, 0.0, 0.0]
    assert len(fleet.assertions) == 12


def test_fleet_plan_isolates_the_vehicles(resolved: Plan, config: Config) -> None:
    assert resolved.domain_id == auto_domain_id("fleet-run-1")
    assert resolved.domain_source == "auto"
    assert [v.name for v in resolved.vehicles] == ["ugv1", "ugv2", "ugv3"]
    for vehicle in resolved.vehicles:
        name = vehicle.name
        assert vehicle.spawn_job.name == f"fieldproof-1-spawn-{name}"
        assert f"namespace:={name}" in vehicle.spawn_job.command
        assert f"name:={name}" in vehicle.spawn_job.command
        assert "frame_prefix:=" not in " ".join(vehicle.spawn_job.command)
        assert vehicle.bridge.command[:2] == ("fieldproof-bridge", f"namespace:={name}")
        assert vehicle.bridge.command[-1] == "bundle_out:=/tmp/fieldproof-bridge"
        container = vehicle.container
        assert container.hostname == name
        assert container.command[-1] == f"namespace:={name}"
        assert container.env["FIELDPROOF_NAMESPACE"] == name
        assert container.env["ROS_DOMAIN_ID"] == str(resolved.domain_id)
        assert container.labels[DOMAIN_LABEL] == str(resolved.domain_id)
        assert container.env["FASTRTPS_DEFAULT_PROFILES_FILE"] == "/opt/fieldproof/dds/profile.xml"
        assert vehicle.topic("navigate_to_pose") == f"/{name}/navigate_to_pose"
        assert vehicle.topic("tf") == f"/{name}/tf"
    # One bridge publishes /clock; the others leave it alone.
    assert [v.clock for v in resolved.vehicles] == [True, False, False]
    assert "clock:=false" not in resolved.vehicles[0].bridge.command
    assert "clock:=false" in resolved.vehicles[1].bridge.command
    assert "clock:=false" in resolved.vehicles[2].bridge.command
    # The budgets: the manifest's, with ugv2's memory overridden by the scenario.
    assert [v.resources.to_dict() for v in resolved.vehicles] == [
        {"cpus": 2.0, "memory": "2g"},
        {"cpus": 2.0, "memory": "3g"},
        {"cpus": 2.0, "memory": "2g"},
    ]
    assert resolved.vehicles[1].container.memory == "3g"
    assert resolved.vehicles[2].depends_on == ("ugv1",)
    assert [v.name for v in resolved.start_order()] == ["ugv1", "ugv2", "ugv3"]
    # The spawn poses: two frames of the depot and coordinates.
    assert (resolved.vehicles[0].spawn.x, resolved.vehicles[0].spawn.y) == (10.0, 3.0)
    assert (resolved.vehicles[1].spawn.x, resolved.vehicles[1].spawn.y) == (6.0, 15.0)
    assert (resolved.vehicles[2].spawn.x, resolved.vehicles[2].spawn.y) == (-8.0, -5.0)
    # The actions, the assertions and the observer, each under its vehicle's namespace.
    assert [a.name for a in resolved.actions] == [
        "ugv1_to_yard",
        "ugv2_along_hall",
        "ugv3_across_yard",
    ]
    goals = [a.goal_map for a in resolved.actions]
    assert all(goal is not None for goal in goals)
    assert [g.x for g in goals if g is not None][1:] == [pytest.approx(4.0), pytest.approx(4.0)]
    by_name = {a.name: a for a in resolved.assertions}
    assert by_name["ugv2_speed_limit"].topic == "/ugv2/cmd_vel"
    assert by_name["ugv3_no_collision"].topic == "/ugv3/contacts"
    assert by_name["ugv3_no_collision"].model == "ugv3"
    assert resolved.observe["models"] == ["ugv1", "ugv2", "ugv3"]
    assert {c["topic"] for c in resolved.observe["contacts"]} == {
        "/ugv1/contacts",
        "/ugv2/contacts",
        "/ugv3/contacts",
    }
    assert "/ugv3/odom" in resolved.observe["record"]["topics"]
    assert [s.role for s in resolved.containers()] == [
        "sim",
        "bridge",
        "bridge",
        "bridge",
        "recorder",
        "vehicle",
        "vehicle",
        "vehicle",
    ]
    data = resolved.to_dict()
    assert data["discovery"] == "multicast"
    assert data["vehicles"][1]["resources"] == {"cpus": 2.0, "memory": "3g"}
    assert data["vehicles"][2]["depends_on"] == ["ugv1"]
    assert data["containers"][5]["cpuset"] is None
    json.dumps(data)


def test_auto_domain_id_is_stable_and_in_range() -> None:
    assert auto_domain_id("a-run") == auto_domain_id("a-run")
    assert auto_domain_id("a-run") != auto_domain_id("another-run")
    assert all(1 <= auto_domain_id(f"run-{i}") <= 100 for i in range(500))


def test_explicit_domain_from_the_configuration(
    fleet: Scenario, config: Config, tmp_path: Path
) -> None:
    config.docker.ros_domain_id = 42
    try:
        resolved = plan(fleet, config, run_id="r", runs_dir=tmp_path, render="software")
    finally:
        config.docker.ros_domain_id = "auto"
    assert resolved.domain_id == 42
    assert resolved.domain_source == "explicit"


def test_discovery_server_plan(fleet: Scenario, config: Config, tmp_path: Path) -> None:
    resolved = plan(
        fleet, config, run_id="srv-1", runs_dir=tmp_path, render="software", discovery="server"
    )
    server = resolved.discovery
    assert server is not None
    assert resolved.discovery_mode == "server"
    assert server.name == "fieldproof-1-discovery"
    assert server.role == "discovery"
    assert server.image == config.image("bridge")
    assert server.command[:2] == ("fastdds", "discovery")
    assert "11811" in server.command
    assert "ROS_DISCOVERY_SERVER" not in server.env
    assert [s.role for s in resolved.containers()][:2] == ["discovery", "sim"]
    assert "ROS_DISCOVERY_SERVER" not in resolved.sim.env, "the simulator speaks no DDS"
    expected = "fieldproof-1-discovery:11811"
    for spec in (
        resolved.recorder,
        *(v.bridge for v in resolved.vehicles),
        *(v.spawn_job for v in resolved.vehicles),
        *(v.container for v in resolved.vehicles),
    ):
        assert spec.env["ROS_DISCOVERY_SERVER"] == expected, spec.name
        assert spec.env["ROS_SUPER_CLIENT"] == "TRUE", spec.name
    assert resolved.to_dict()["discovery"] == "server"
    with pytest.raises(ScenarioError, match="discovery 'peer'"):
        plan(fleet, config, run_id="x", runs_dir=tmp_path, render="software", discovery="peer")


def derived_manifest(tmp_path: Path, name: str, mutate: Callable[[dict[str, Any]], None]) -> Path:
    """The example vehicle's manifest, changed, written into a workspace under `tmp_path`."""
    data = yaml.safe_load((REPO / "examples/vehicles/ugv_nav2/robot.yaml").read_text())
    data["extends"] = str(REPO / "robots/fieldproof_robots/models/ugv_ref/robot.yaml")
    data["runtime"]["params"] = []
    data["runtime"].pop("dds_profile")
    mutate(data)
    manifest = tmp_path / "examples" / "vehicles" / name / "robot.yaml"
    manifest.parent.mkdir(parents=True)
    (manifest.parent / "src").mkdir()
    manifest.write_text(yaml.safe_dump(data, sort_keys=False))
    return manifest


def derived_workspace(tmp_path: Path, config: Config) -> Config:
    """A workspace at `tmp_path` that borrows the repository's worlds and assets."""
    (tmp_path / "fieldproof.toml").write_text("")
    nested = load_config(tmp_path / "fieldproof.toml", env={})
    nested.stack.worlds = str(REPO / "sim/fieldproof_worlds/worlds")
    nested.stack.fuel = config.stack.fuel
    nested.stack.fuel_lock = str(REPO / "assets/fuel.lock")
    return nested


def derived_scenario(tmp_path: Path, manifest: str, vehicle_lines: str) -> Scenario:
    file = tmp_path / "examples" / "scenarios" / "one.yaml"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        v0_text(EXAMPLE.read_text())
        .replace("manifest: ../vehicles/ugv_nav2/robot.yaml", f"manifest: {manifest}")
        .replace(
            "    spawn:\n      frame: spawn_dock",
            vehicle_lines + "    spawn:\n      frame: spawn_dock",
        )
    )
    return load_scenario(file)


def test_frame_prefix_reaches_the_spawn_and_the_bridge(config: Config, tmp_path: Path) -> None:
    """A vehicle with a frame prefix: the spawn and the bridge take it; a launch without a
    frame_prefix_arg cannot (the example's), so the plan refuses it."""
    with pytest.raises(ScenarioError, match=r"vehicles\[ugv1\].frame_prefix: .*frame_prefix_arg"):
        plan(
            fleet_variant(lambda d: d["vehicles"][0].update(frame_prefix="ugv1/")),
            config,
            runs_dir=tmp_path,
            render="software",
        )
    derived_manifest(
        tmp_path,
        "prefixed",
        lambda d: d["runtime"]["entrypoint"]["launch"].update(frame_prefix_arg="frame_prefix"),
    )
    nested = derived_workspace(tmp_path, config)
    scenario = derived_scenario(
        tmp_path,
        "../vehicles/prefixed/robot.yaml",
        "    namespace: ugv1\n    frame_prefix: ugv1/\n",
    )
    resolved = plan(scenario, nested, runs_dir=tmp_path, render="software")
    (vehicle,) = resolved.vehicles
    assert "frame_prefix:=ugv1/" in vehicle.spawn_job.command
    assert "frame_prefix:=ugv1/" in vehicle.bridge.command
    assert vehicle.container.command[-2:] == ("namespace:=ugv1", "frame_prefix:=ugv1/")
    assert vehicle.container.env["FIELDPROOF_FRAME_PREFIX"] == "ugv1/"
    assert vehicle.container.mounts == (), "no DDS profile in this manifest"
    assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in vehicle.container.env
    assert resolved.to_dict()["vehicles"][0]["frame_prefix"] == "ugv1/"


def test_plan_refuses_a_namespace_the_launch_cannot_take(config: Config, tmp_path: Path) -> None:
    derived_manifest(
        tmp_path, "plain", lambda d: d["runtime"]["entrypoint"]["launch"].pop("namespace_arg")
    )
    nested = derived_workspace(tmp_path, config)
    scenario = derived_scenario(tmp_path, "../vehicles/plain/robot.yaml", "    namespace: ugv1\n")
    with pytest.raises(
        ScenarioError, match=r"vehicles\[ugv1\].namespace: .*names no namespace_arg"
    ):
        plan(scenario, nested, runs_dir=tmp_path, render="software")
    # A command entrypoint takes the namespace through the environment, so it is accepted.
    derived_manifest(
        tmp_path,
        "command",
        lambda d: d["runtime"].update(entrypoint={"command": ["ros2", "launch", "x", "y.py"]}),
    )
    scenario = derived_scenario(tmp_path, "../vehicles/command/robot.yaml", "    namespace: ugv1\n")
    resolved = plan(scenario, nested, runs_dir=tmp_path, render="software")
    assert resolved.vehicles[0].container.command == ()
    assert resolved.vehicles[0].container.env["FIELDPROOF_NAMESPACE"] == "ugv1"


def test_missing_dds_profile_is_refused(config: Config, tmp_path: Path) -> None:
    text = (REPO / "examples/vehicles/ugv_nav2/robot.yaml").read_text()
    copy = REPO / "examples" / "vehicles" / "ugv_nav2" / "robot.missing-profile.yaml"
    copy.write_text(text.replace("src/ugv_nav2_bringup/config/fastdds.xml", "config/none.xml"))
    try:
        scenario = scenario_variant(
            lambda d: d["vehicles"][0].update(
                manifest="../vehicles/ugv_nav2/robot.missing-profile.yaml"
            )
        )
        with pytest.raises(ScenarioError, match=r"runtime\.dds_profile: .*none\.xml is not a file"):
            plan(scenario, config, runs_dir=tmp_path, render="software")
    finally:
        copy.unlink()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: d["vehicles"][2].update(depends_on=["ugv9"]),
            "depends_on: 'ugv9' is not a vehicle",
        ),
        (lambda d: d["vehicles"][2].update(depends_on=["ugv3"]), "depends on itself"),
        (
            lambda d: d["vehicles"][0].update(depends_on=["ugv3"]),
            "depends_on forms a cycle: ugv1 -> ugv3 -> ugv1",
        ),
        (lambda d: d["vehicles"][0].update(frame_prefix="Ugv-1/"), "frame_prefix"),
        (lambda d: d["vehicles"][1]["resources"].update(cpuset="a-b"), "resources.cpuset"),
        (lambda d: d["vehicles"][1]["resources"].update(memory="lots"), "resources.memory"),
        (lambda d: d["vehicles"][1]["resources"].update(gpus=1), "resources.gpus"),
    ],
)
def test_fleet_schema_errors(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        fleet_variant(mutate)


def test_start_order_follows_the_dependencies() -> None:
    def reverse(data: dict[str, Any]) -> None:
        data["vehicles"][0]["depends_on"] = ["ugv3"]
        data["vehicles"][2].pop("depends_on")

    scenario = fleet_variant(reverse)
    assert [v.name for v in scenario.start_order()] == ["ugv3", "ugv1", "ugv2"]


# ------------------------------------------------------------------------------------- runs
def test_fleet_run_passes(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED, record.reason
    assert backend.started == [
        "fieldproof-1-sim",
        "fieldproof-1-bridge-ugv1",
        "fieldproof-1-bridge-ugv2",
        "fieldproof-1-bridge-ugv3",
        "fieldproof-1-recorder",
        "fieldproof-1-ugv1",
        "fieldproof-1-ugv2",
        "fieldproof-1-ugv3",
    ]
    assert [j.name for j in backend.jobs] == [
        "fieldproof-1-spawn-ugv1",
        "fieldproof-1-spawn-ugv2",
        "fieldproof-1-spawn-ugv3",
    ]
    # Each bridge was polled for its own vehicle's odometry, each vehicle probed, each goal
    # sent under its namespace.
    assert [a["detail"] for a in record.to_dict()["timeline"]] == ["goal SUCCEEDED"] * 3
    goals = [a for n, a in backend.execs if "send_goal" in a]
    assert [g[8] for g in goals] == [
        "/ugv1/navigate_to_pose",
        "/ugv2/navigate_to_pose",
        "/ugv3/navigate_to_pose",
    ]
    probes = [n for n, a in backend.execs if "fieldproof-vehicle-ready" in a[1]]
    assert probes == ["fieldproof-1-ugv1", "fieldproof-1-ugv2", "fieldproof-1-ugv3"]
    assert {a["status"] for a in record.assertions} == {"passed"}
    assert len(record.assertions) == 12
    assert backend.containers == {}
    assert backend.stopped[0] == "fieldproof-1-ugv3", "torn down in reverse"
    events = [e["event"] for e in log.events]
    assert events.count("vehicle.ready") == 3
    assert events.count("vehicle.budget") == 3
    assert "vehicle.dependency" in events
    dependency = next(e for e in log.events if e["event"] == "vehicle.dependency")
    assert dependency["message"] == "ugv3: ugv1 is ready"
    budget = next(e for e in log.events if e["event"] == "budget")
    assert budget["cpus"] == 6.0
    assert budget["memory_bytes"] == 7 * 1024**3
    assert "budget.oversubscribed" not in events
    assert sorted(p.name for p in (resolved.run_dir / "logs").iterdir()) == [
        "bridge-ugv1.log",
        "bridge-ugv2.log",
        "bridge-ugv3.log",
        "recorder.log",
        "sim.log",
        "vehicle-ugv1.log",
        "vehicle-ugv2.log",
        "vehicle-ugv3.log",
    ]
    written = json.loads((resolved.run_dir / RECORD_FILE).read_text())
    assert [v["namespace"] for v in written["vehicles"]] == ["ugv1", "ugv2", "ugv3"]
    assert written["vehicles"][1]["resources"] == {"cpus": 2.0, "memory": "3g"}
    assert written["stack"]["ros_domain_id"] == resolved.domain_id
    report = (resolved.run_dir / "report.md").read_text()
    assert "`ugv3`" in report


def test_budget_oversubscription_is_a_warning(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.host_info = HostInfo(4, 4 * 1024**3)
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    warnings = [e for e in log.events if e["event"] == "budget.oversubscribed"]
    assert len(warnings) == 2
    assert "CPU quotas (6) exceed the host's 4 CPU(s)" in warnings[0]["message"]
    assert "memory limits (7.0 GiB) exceed" in warnings[1]["message"]


def test_dependent_vehicle_waits_for_a_failed_dependency(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    original = backend.exec

    def failing_probe(
        name: str, argv: Sequence[str], on_line: LineSink | None = None
    ) -> ExecResult:
        if name == "fieldproof-1-ugv1" and "fieldproof-vehicle-ready" in argv[1]:
            line = "not ready after 180.0 s: map topic no publisher"
            if on_line is not None:
                on_line(line)
            return ExecResult(1, line + "\n")
        return original(name, argv, on_line)

    backend.exec = failing_probe  # type: ignore[method-assign]
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason == "ugv1: not ready after 180.0 s: map topic no publisher"
    assert "fieldproof-1-ugv2" in backend.started, "independent of ugv1, started"
    assert "fieldproof-1-ugv3" not in backend.started, "depends on ugv1, never started"
    failed = [e for e in log.events if e["event"] == "vehicle.failed"]
    assert any("ugv3: ugv3: not started, it depends on ugv1" in e["message"] for e in failed)
    assert backend.containers == {}


def test_discovery_server_run(
    fleet: Scenario, config: Config, tmp_path: Path, clock: FakeClock
) -> None:
    resolved = plan(
        fleet, config, run_id="srv-2", runs_dir=tmp_path, render="software", discovery="server"
    )
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED, record.reason
    assert backend.started[:2] == ["fieldproof-2-discovery", "fieldproof-2-sim"]
    assert backend.stopped[-1] == "fieldproof-2-discovery", "torn down last"
    ready = next(e for e in log.events if e["event"] == "discovery.ready")
    assert ready["server"] == "fieldproof-2-discovery:11811"
    assert record.stack["discovery"] == "server"
    assert (resolved.run_dir / "logs" / "discovery.log").is_file()
    # A server that never listens ends the run before the simulator starts.
    resolved = plan(
        fleet, config, run_id="srv-3", runs_dir=tmp_path, render="software", discovery="server"
    )
    backend = ready_backend(resolved)
    original = backend.start

    def silent(spec: ContainerSpec) -> None:
        original(spec)
        if spec.role == "discovery":
            backend.containers[spec.name].logs = "starting\n"

    backend.start = silent  # type: ignore[method-assign]
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.ERROR
    assert "timed out after 60 s waiting for the discovery server" in str(record.reason)
    assert backend.started == ["fieldproof-3-discovery"]
    assert backend.containers == {}


def test_contract_checks(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend, clock, check_contracts=True)
    assert record.status == Status.PASSED, record.reason
    checks = [(n, a) for n, a in backend.execs if "contract_check" in a]
    assert [n for n, _ in checks] == [
        "fieldproof-1-bridge-ugv1",
        "fieldproof-1-bridge-ugv2",
        "fieldproof-1-bridge-ugv3",
    ]
    assert checks[0][1][3:9] == (
        "ros2",
        "run",
        "fieldproof_bridge",
        "contract_check",
        "/tmp/fieldproof-bridge",  # noqa: S108
        "--timeout",
    )
    expected = {"passed": 14, "failed": 0, "exit_code": 0, "findings": []}
    assert all(v["contract"] == expected for v in record.vehicles)
    events = [e["event"] for e in log.events]
    assert events.count("contract.ok") == 3
    assert events.index("contract.ok") < events.index("recorder.start")
    # A failed check is an error of the stack, not of the vehicle under test.
    backend = ready_backend(resolved)
    backend.contract_result = ExecResult(
        1,
        "ok graph /ugv1/odom\n"
        "FAIL rate /ugv1/scan: 4.0 Hz, expected 10.0 Hz\n"
        "13 passed, 1 failed\n",
    )
    record, _ = execute(resolved, backend, clock, check_contracts=True)
    assert record.status == Status.ERROR
    assert record.reason is not None
    assert record.reason.startswith("ugv1: the bridge contract check exited 1")
    assert "FAIL rate /ugv1/scan" in record.reason
    assert record.vehicles[0]["contract"]["failed"] == 1
    assert record.vehicles[0]["contract"]["findings"] == [
        "FAIL rate /ugv1/scan: 4.0 Hz, expected 10.0 Hz"
    ]
    assert backend.containers == {}


def test_actions_run_concurrently(resolved: Plan) -> None:
    """The three goals are in flight at once: each blocks until all three arrived."""
    backend = ready_backend(resolved)
    barrier = threading.Barrier(3, timeout=10.0)
    original = backend.exec

    def meeting(name: str, argv: Sequence[str], on_line: LineSink | None = None) -> ExecResult:
        if "send_goal" in argv:
            barrier.wait()
        return original(name, argv, on_line)

    backend.exec = meeting  # type: ignore[method-assign]
    # The real clock runs this one: stamp the samples a little ahead of the timeline's start.
    backend.wall = lambda: time.time() + 10.0
    resolved.run_dir.parent.mkdir(parents=True, exist_ok=True)
    log = RunLog(resolved.run_id, file=resolved.run_dir / LOG_FILE)
    with ThreadPoolExecutor(max_workers=3) as pool:
        run = Run(resolved, backend, log, executor=pool)
        record = run.execute()
    assert record.status == Status.PASSED, record.reason
    assert barrier.broken is False
    assert [a.status for a in record.timeline] == ["ok"] * 3
    assert all(a.duration_s is not None and a.duration_s < 5.0 for a in record.timeline)


def test_failed_action_cancels_the_others(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    original = backend.exec

    def aborting(name: str, argv: Sequence[str], on_line: LineSink | None = None) -> ExecResult:
        if "send_goal" in argv and "/ugv2/navigate_to_pose" in argv:
            backend.write_samples(backend.samples_on_goal)
            return ExecResult(0, "Goal finished with status: ABORTED\n")
        return original(name, argv, on_line)

    backend.exec = aborting  # type: ignore[method-assign]
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason == "ugv2_along_hall: goal ABORTED"
    statuses = {a.name: a.status for a in record.timeline}
    # With the synchronous executor every action had returned before the verdict; whatever
    # was still running would have been marked cancelled.
    assert statuses["ugv2_along_hall"] == "failed"
    assert statuses["ugv1_to_yard"] == "ok"
    assert statuses["ugv3_across_yard"] == "ok"
    assert backend.containers == {}


def test_budget_names_the_running_actions(config: Config, tmp_path: Path, clock: FakeClock) -> None:
    scenario = fleet_variant(lambda d: d["time"].update(timeout_s="30s"))
    resolved = plan(scenario, config, run_id="budget-1", runs_dir=tmp_path, render="software")
    backend = ready_backend(resolved)
    backend.goal_duration_s = 0.0
    original = backend.exec

    def never_returns(
        name: str, argv: Sequence[str], on_line: LineSink | None = None
    ) -> ExecResult:
        if "send_goal" in argv:
            clock.sleep(40.0)
            return ExecResult(124, "")
        return original(name, argv, on_line)

    backend.exec = never_returns  # type: ignore[method-assign]
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.TIMEOUT
    assert str(record.reason).startswith("budget of 30 s passed during ugv1_to_yard")
    assert {a.status for a in record.timeline} >= {"timeout"}


def test_run_without_dependencies_starts_everything_at_once(
    config: Config, tmp_path: Path, clock: FakeClock
) -> None:
    scenario = fleet_variant(lambda d: d["vehicles"][2].pop("depends_on"))
    resolved = plan(scenario, config, run_id="free-1", runs_dir=tmp_path, render="software")
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED, record.reason
    assert "vehicle.dependency" not in [e["event"] for e in log.events]
    assert backend.started[-3:] == ["fieldproof-1-ugv1", "fieldproof-1-ugv2", "fieldproof-1-ugv3"]
