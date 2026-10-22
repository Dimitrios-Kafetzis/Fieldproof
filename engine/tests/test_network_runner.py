"""Tests of the radio links in the runner (task P2-T02): the topology of the plan, the `network`
phase, the link events of the timeline, the measurements checked against the profiles, and the
networks' teardown, on the fake backend."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes import FakeBackend, FakeClock, FakeContainer
from rich.console import Console
from test_runner import SyncExecutor

from fieldproof.backend import ExecResult
from fieldproof.config import Config, load_config
from fieldproof.network import GROUND_CONFIG, NETWORK_FILE
from fieldproof.runner import (
    BRIDGE_COMMAND,
    GROUND_IN_CONTAINER,
    GZ_IP_WRAPPER,
    LINK_KERNEL_HINT,
    LOG_FILE,
    RUN_LABEL,
    Plan,
    Run,
    RunLog,
    RunRecord,
    Status,
    plan,
)
from fieldproof.scenario import Scenario, ScenarioError, load_scenario, validate_data

REPO = Path(__file__).resolve().parents[2]
LINK_LOSS = REPO / "examples/scenarios/ugv_link_loss.yaml"
FLEET = REPO / "examples/scenarios/ugv_fleet.yaml"
T0 = 1_700_000_000.0
GOAL_S = 2.0


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> Config:
    fuel = tmp_path_factory.mktemp("fuel")
    for model in yaml.safe_load((REPO / "assets/fuel.lock").read_text())["models"]:
        (fuel / model["dir"]).mkdir()
    config = load_config(REPO / "fieldproof.toml", env={})
    config.stack.fuel = str(fuel)
    return config


@pytest.fixture(scope="module")
def link_loss() -> Scenario:
    return load_scenario(LINK_LOSS)


@pytest.fixture
def resolved(link_loss: Scenario, config: Config, tmp_path: Path) -> Plan:
    return plan(link_loss, config, run_id="link-run-1", runs_dir=tmp_path, render="software")


def variant(path: Path, mutate: Callable[[dict[str, Any]], None]) -> Scenario:
    text = path.read_text()
    data = yaml.safe_load(text)
    mutate(data)
    return validate_data(data, path.resolve(), text)


def link_window(start: float, rtt_ms: float | None, sent: int = 10) -> dict[str, Any]:
    """One second of the ground station's probes of ugv1."""
    received = 0 if rtt_ms is None else sent
    rtts = [rtt_ms or 0.0] * received
    return {
        "kind": "link",
        "vehicle": "ugv1",
        "start": start,
        "end": start + 1.0,
        "sent": sent,
        "received": received,
        "rtt_sum_ms": sum(rtts),
        "rtt_sq_sum_ms": sum(r * r for r in rtts),
        "rtt_min_ms": min(rtts) if rtts else None,
        "rtt_max_ms": max(rtts) if rtts else None,
    }


def stream(start: float) -> list[dict[str, Any]]:
    """What the vehicle did, `t` seconds into the timeline: drive, hold from 7.5 s, resume at 37 s
    (the 30 s drop lifted at 35 s, the heartbeat back), arrive at the goal."""
    samples: list[dict[str, Any]] = []

    def at(t: float, **sample: Any) -> None:
        samples.append({"wall": start + t, "sim": t, **sample})

    at(2.0, kind="value", topic="/link_state", field="data", value=True)
    at(7.5, kind="value", topic="/link_state", field="data", value=False)
    at(37.0, kind="value", topic="/link_state", field="data", value=True)
    for t in range(1, 56):
        moving = t < 8 or t >= 37
        speed = 0.4 if moving else 0.0
        at(t, kind="value", topic="/odom", field="twist.twist.linear.x", value=speed)
        at(t + 0.5, kind="value", topic="/cmd_vel", field="linear.x", value=speed)
    at(54.0, kind="pose", model="ugv1", x=-2.0, y=15.0, z=0.1, yaw=0.0)
    at(55.0, kind="clock", rtf=1.0)
    return samples


class LinkBackend(FakeBackend):
    """The fake runtime plus a ground station that measures what the holders were told.

    Every link script ends the period of the previous one: its seconds become windows, answered
    at `rtt_ms` under a profile, unanswered under a drop. The goal writes the vehicle's stream
    and the first seconds of the initial period.
    """

    rtt_ms: float = 120.0
    changes: list[tuple[float, bool]]

    def _answer(self, container: FakeContainer, argv: tuple[str, ...]) -> ExecResult:
        result = super()._answer(container, argv)
        if not hasattr(self, "changes"):
            self.changes = []
        now = self.wall()
        if container.spec.role == "link" and "tc -s -j" not in argv[-1]:
            if self.changes:
                since, dropped = self.changes[-1]
                self.write_network(
                    [
                        link_window(since + s, None if dropped else self.rtt_ms)
                        for s in range(int(now - since))
                    ]
                )
            self.changes.append((now, "loss 100%" in argv[-1]))
        if "send_goal" in " ".join(argv):
            start = now - GOAL_S
            self.write_samples(stream(start))
            # The bring-up windows before the timeline (slow, as discovery saturates the link)
            # and the first second of the initial period after it.
            self.write_network(
                [link_window(start - 3.0 + s * 0.1, self.rtt_ms * 4, 1) for s in range(20)]
                + [link_window(now + s * 0.04, self.rtt_ms, 1) for s in range(25)]
            )
        return result


def ready_backend(resolved: Plan, cls: type[FakeBackend] = LinkBackend) -> FakeBackend:
    backend = cls()
    backend.images = {s.image for s in resolved.containers()} | {
        v.spawn_job.image for v in resolved.vehicles
    }
    backend.goal_duration_s = GOAL_S
    return backend


def execute(
    resolved: Plan, backend: FakeBackend, *, keep: bool = False
) -> tuple[RunRecord, list[dict[str, Any]]]:
    clock = FakeClock()
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
        keep=keep,
        sleep=clock.sleep,
        clock=clock.now,
        wall=lambda: T0 + (clock.now() - 1000.0),
        executor=SyncExecutor(),  # type: ignore[arg-type]
    )
    record = run.execute()
    return record, log.events


def events(log: Sequence[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in log if e["event"] == name]


# ------------------------------------------------------------------------------------- plan
def test_link_loss_topology(resolved: Plan) -> None:  # noqa: PLR0915
    links = resolved.links
    assert links is not None
    short = "1"
    radio, onboard = f"fieldproof-{short}-radio", f"fieldproof-{short}-onboard-ugv1"
    assert links.radio == radio
    assert links.onboard == {"ugv1": onboard}
    assert links.networks() == [radio, onboard]
    assert links.labels == {RUN_LABEL: "link-run-1"}
    assert links.links == {"ugv1": "lte_rural"}
    assert links.verify == "fail"
    assert links.heartbeats == {"ugv1": {"topic": "/gcs/heartbeat", "rate_hz": 2.0}}
    roles = [s.role for s in resolved.containers()]
    assert roles == ["sim", "bridge", "recorder", "link", "ground", "vehicle"]
    holder = links.holders["ugv1"]
    assert holder.name == f"fieldproof-{short}-link-ugv1"
    assert (holder.network, holder.networks, holder.aliases) == (onboard, (radio,), ("ugv1",))
    assert holder.hostname == "ugv1"
    assert holder.user == "0:0"
    assert holder.cap_add == ("NET_ADMIN",)
    assert holder.command == ("sleep", "infinity")
    assert holder.image == resolved.config.image("bridge")
    vehicle = resolved.vehicles[0]
    assert vehicle.container.network_mode == f"container:{holder.name}"
    assert vehicle.container.network is None
    assert vehicle.container.hostname is None
    assert vehicle.container.cap_add == (), "the stack under test gets no privilege"
    assert vehicle.bridge.network == resolved.network
    assert vehicle.bridge.networks == (onboard,)
    # On two networks the bridge advertises its address towards the simulator to gz-transport.
    assert vehicle.bridge.command[:4] == ("bash", "-c", GZ_IP_WRAPPER, "fieldproof-bridge-gz-ip")
    assert vehicle.bridge.command[4] == BRIDGE_COMMAND
    assert "bundle_out:=/tmp/fieldproof-bridge" in vehicle.bridge.command
    assert vehicle.bridge.env["FIELDPROOF_SIM_HOST"] == resolved.sim.hostname
    assert 'ip -o -4 route get "$sim"' in GZ_IP_WRAPPER
    assert GZ_IP_WRAPPER.endswith('exec "$@"')
    assert resolved.recorder.networks == (onboard,)
    ground = links.ground
    assert ground.network == radio
    assert ground.networks == ()
    assert ground.hostname == "ground"
    assert ground.command[:2] == ("python3", GROUND_IN_CONTAINER)
    assert ground.mounts[0].source.name == "ground.py"
    assert not ground.mounts[1].read_only
    assert ground.env["ROS_DOMAIN_ID"] == vehicle.container.env["ROS_DOMAIN_ID"]
    config = links.ground_config("/fieldproof/run")
    assert config["output"] == "/fieldproof/run/network.jsonl"
    assert config["links"] == [
        {
            "vehicle": "ugv1",
            "host": "ugv1",
            "heartbeat": {"topic": "/gcs/heartbeat", "rate_hz": 2.0},
        }
    ]
    assert config["probe"] == {
        "interval_s": 0.1,
        "window_s": 1.0,
        "timeout_s": 1.0,
        "payload_bytes": 16,
    }
    data = resolved.to_dict()
    assert data["links"]["links"]["ugv1"]["latency_ms"] == 60.0
    assert data["links"]["links"]["ugv1"]["holder"] == holder.name
    drop = data["timeline"][1]
    assert drop == {
        "name": "link_outage",
        "at_s": 5.0,
        "vehicle": "ugv1",
        "action": "link.drop",
        "trigger": {"at_s": 5.0},
        "duration_s": 30.0,
    }
    holder_dict = next(c for c in data["containers"] if c["role"] == "link")
    assert holder_dict["networks"] == [radio]
    assert holder_dict["cap_add"] == ["NET_ADMIN"]
    vehicle_dict = next(c for c in data["containers"] if c["role"] == "vehicle")
    assert vehicle_dict["network_mode"] == f"container:{holder.name}"


def test_fleet_behind_links(config: Config, tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["network"] = {
            "profile": "wifi_good",
            "links": {"ugv2": "far_modem"},
            "profiles": {"far_modem": {"latency_ms": 80, "jitter_ms": 10, "rate_kbit": 64}},
        }

    fleet = variant(FLEET, mutate)
    resolved = plan(fleet, config, run_id="fleet-net-1", runs_dir=tmp_path, render="software")
    links = resolved.links
    assert links is not None
    assert links.links == {"ugv1": "wifi_good", "ugv2": "far_modem", "ugv3": "wifi_good"}
    assert links.profile("far_modem").rate_kbit == 64
    assert links.heartbeats["ugv2"] == {"topic": "/ugv2/gcs/heartbeat", "rate_hz": 2.0}
    onboard = list(links.onboard.values())
    # The first vehicle's bridge publishes /clock: it reaches every vehicle's onboard network.
    clock_bridge, *others = [v.bridge for v in resolved.vehicles]
    assert clock_bridge.networks == tuple(onboard)
    assert [b.networks for b in others] == [(onboard[1],), (onboard[2],)]
    assert resolved.recorder.networks == tuple(onboard)
    assert [s.role for s in resolved.containers()].count("link") == 3
    assert {h.aliases for h in links.holders.values()} == {("ugv1",), ("ugv2",), ("ugv3",)}


def test_links_refuse_a_discovery_server(link_loss: Scenario, config: Config) -> None:
    with pytest.raises(ScenarioError, match="radio links need multicast discovery"):
        plan(link_loss, config, run_id="x-1", discovery="server", render="software")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("network"), r"timeline\[1\]: link.drop needs a network section"),
        (
            lambda d: d["network"].update(profile="lte_urban"),
            "profile: unknown profile 'lte_urban'",
        ),
        (
            lambda d: d["network"].update(links={"ugv9": "lte_rural"}),
            "network.links: 'ugv9' is not a vehicle",
        ),
        (
            lambda d: d["network"].update(links={"ugv1": "dial_up"}),
            r"links.ugv1: unknown profile 'dial_up'",
        ),
        (
            lambda d: d["network"].update(profiles={"wifi_good": {"latency_ms": 1}}),
            "'wifi_good' is a preset",
        ),
        (
            lambda d: d["network"].update(profiles={"bad": {"reorder_pct": 5}}),
            "reorder_pct needs a latency_ms",
        ),
        (lambda d: d["network"].update(verify="maybe"), "verify"),
        (
            lambda d: d["timeline"].append(
                {"at": "1s", "vehicle": "ugv1", "action": "link.set", "profile": "x"}
            ),
            r"timeline\[2\].profile: unknown profile 'x'",
        ),
        (
            lambda d: d["timeline"].append(
                {"at": "1s", "vehicle": "ugv1", "action": "link.restore", "profile": "x"}
            ),
            "Extra inputs are not permitted",
        ),
        (
            lambda d: d["timeline"].append(
                {"at": "1s", "vehicle": "ugv7", "action": "link.restore"}
            ),
            r"timeline\[2\].vehicle: 'ugv7' is not a vehicle",
        ),
        (lambda d: d["timeline"][1].update(duration_s="long"), "duration"),
    ],
)
def test_network_schema_errors(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        variant(LINK_LOSS, mutate)


# -------------------------------------------------------------------------------------- run
def test_link_loss_run(resolved: Plan) -> None:  # noqa: PLR0915
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend)
    assert record.status == Status.PASSED, record.reason
    phases = [p["name"] for p in record.phases]
    assert phases.index("network") == phases.index("recorder") + 1
    assert phases.index("vehicles") == phases.index("network") + 1
    links = resolved.links
    assert links is not None
    # The networks of the links, labelled with the run and removed at the end.
    for name in links.networks():
        assert backend.network_labels[name] == {RUN_LABEL: "link-run-1"}
    assert sorted(backend.removed_networks) == sorted(links.networks())
    # The holder started before the ground station, both before the vehicle.
    order = backend.started
    assert (
        order.index(links.holders["ugv1"].name)
        < order.index(links.ground.name)
        < order.index(resolved.vehicles[0].container.name)
    )
    # Three profiles: the configured one, the drop, the configured one again.
    scripts = [script for _, script in backend.link_scripts]
    assert len(scripts) == 3
    assert "netem delay 60ms 20ms distribution normal loss 1% rate 4000kbit" in scripts[0]
    assert "netem loss 100%" in scripts[1]
    assert scripts[2] == scripts[0]
    assert all("address=10." in s for s in scripts)
    applied = events(log, "link.applied")
    assert [e["cause"] for e in applied] == ["initial", "link_outage", "link_outage (restore)"]
    assert applied[1]["message"].startswith("ugv1: drop (dropped (100 % loss)) on eth1 of ")
    assert events(log, "ground.ready")[0]["message"] == (
        "probing 1 link(s), heartbeat on /gcs/heartbeat"
    )
    ground_config = json.loads((resolved.run_dir / GROUND_CONFIG).read_text())
    assert ground_config["links"][0]["host"] == "ugv1"
    # The timeline: the drop ran its 30 s and restored the profile.
    outage = next(a for a in record.timeline if a.name == "link_outage")
    assert outage.status == "ok"
    assert outage.detail == "dropped for 30 s, lte_rural restored"
    # Every assertion of the example held on the scripted stream.
    assert {a["name"]: a["status"] for a in record.assertions} == {
        "reports_link_lost": "passed",
        "holds_while_lost": "passed",
        "commands_zero_while_lost": "passed",
        "reports_link_back": "passed",
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "no_collision": "passed",
    }
    # The links in the record: the measured periods.
    network = record.network
    assert network is not None
    assert network["radio"] == links.radio
    assert network["addresses"]["ugv1"].startswith("10.")
    assert f"address={network['addresses']['ugv1']}" in scripts[0]
    (link,) = network["periods"]
    assert link["vehicle"] == "ugv1"
    assert link["current"] == "lte_rural"
    initial, dropped, restored = link["periods"]
    assert initial["status"] == "passed"
    assert initial["measured"]["latency_ms"] == 60.0
    assert initial["expected"]["latency_ms"] == 60.0
    assert dropped["status"] == "passed"
    assert dropped["measured"]["replies"] == 0
    assert dropped["measured"]["probes"] >= 190
    assert dropped["to_s"] - dropped["from_s"] == pytest.approx(30.0, abs=1.0)
    assert restored["status"] == "insufficient"
    assert [e["event"] for e in log].count("link.passed") == 2
    run_json = json.loads((resolved.run_dir / "run.json").read_text())
    assert run_json["network"]["periods"][0]["periods"][1]["profile"] == "drop"
    report = (resolved.run_dir / "report.md").read_text()
    assert "## Radio links" in report
    assert "| `ugv1` | `lte_rural` (initial) |" in report
    assert "| 60.00 ms | 60.00 ms |" in report
    assert "| `ugv1` | 2.2 MB | 12 | 895.9 KB | 3 |" in report
    assert network["traffic"] == {
        "ugv1": {
            "out": {"bytes": 2263618, "packets": 6769, "drops": 12, "backlog": 0},
            "in": {"bytes": 917404, "packets": 3100, "drops": 3, "backlog": 0},
        }
    }
    assert events(log, "link.traffic")[0]["message"] == (
        "ugv1: 2.26 MB out (12 dropped), 0.92 MB in (3 dropped)"
    )
    assert "| `ugv1` | `drop` (link_outage) |" in report


def test_a_link_that_does_not_match_its_profile_fails_the_run(resolved: Plan) -> None:
    backend = ready_backend(resolved)
    backend.rtt_ms = 240.0  # type: ignore[attr-defined]
    record, log = execute(resolved, backend)
    assert record.status == Status.FAILED
    assert record.reason is not None
    assert record.reason.startswith("link ugv1 does not match profile lte_rural: latency 120.00 ms")
    assert record.reason.endswith("profile 60.00 ms (± 6.00 ms)")
    assert events(log, "link.failed")[0]["level"] == "warning"


def test_verify_warn_only_warns(config: Config, tmp_path: Path) -> None:
    scenario = variant(LINK_LOSS, lambda d: d["network"].update(verify="warn"))
    resolved = plan(scenario, config, run_id="warn-run-1", runs_dir=tmp_path, render="software")
    backend = ready_backend(resolved)
    backend.rtt_ms = 240.0  # type: ignore[attr-defined]
    record, log = execute(resolved, backend)
    assert record.status == Status.PASSED, record.reason
    assert events(log, "link.failed")
    off = variant(LINK_LOSS, lambda d: d["network"].update(verify="off"))
    resolved = plan(off, config, run_id="off-run-1", runs_dir=tmp_path, render="software")
    backend = ready_backend(resolved)
    backend.rtt_ms = 240.0  # type: ignore[attr-defined]
    record, log = execute(resolved, backend)
    assert record.status == Status.PASSED
    assert not events(log, "link.failed")
    assert not events(log, "link.passed")


def test_link_set_restore_and_a_drop_that_stays(config: Config, tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["assertions"] = []
        data["network"]["verify"] = "off"  # the fake ground station measures 60 ms throughout
        data["timeline"] = [
            {"at": "1s", "name": "slow", "vehicle": "ugv1", "action": "link.set",
             "profile": "rf_900mhz_5km"},
            {"at": "3s", "name": "back", "vehicle": "ugv1", "action": "link.restore"},
            {"at": "5s", "name": "cut", "vehicle": "ugv1", "action": "link.drop"},
        ]  # fmt: skip

    scenario = variant(LINK_LOSS, mutate)
    resolved = plan(scenario, config, run_id="set-run-1", runs_dir=tmp_path, render="software")
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend)
    assert record.status == Status.PASSED, record.reason
    scripts = [script for _, script in backend.link_scripts]
    assert len(scripts) == 4
    assert "rate 200kbit" in scripts[1]
    assert "rate 4000kbit" in scripts[2]
    assert "loss 100%" in scripts[3]
    assert [(a.name, a.status, a.detail) for a in record.timeline] == [
        ("slow", "ok", "rf_900mhz_5km applied"),
        ("back", "ok", "lte_rural restored"),
        ("cut", "ok", "dropped"),
    ]
    assert record.network is not None
    assert record.network["periods"][0]["current"] == "drop"
    assert [e["message"] for e in events(log, "action.end")][-1] == "cut: dropped"


def test_a_drop_longer_than_the_budget(config: Config, tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["assertions"] = []
        data["time"]["timeout_s"] = "30s"
        data["timeline"] = [
            {"at": "5s", "name": "long_cut", "vehicle": "ugv1", "action": "link.drop",
             "duration_s": "1m"},
        ]  # fmt: skip

    scenario = variant(LINK_LOSS, mutate)
    resolved = plan(scenario, config, run_id="long-run-1", runs_dir=tmp_path, render="software")
    record, _ = execute(resolved, ready_backend(resolved))
    assert record.status == Status.TIMEOUT
    (cut,) = record.timeline
    assert cut.status == "timeout"
    assert cut.detail == "the budget ended the drop after 25 s"


def test_a_link_that_cannot_be_shaped(resolved: Plan) -> None:
    backend = ready_backend(resolved)
    backend.link_result = ExecResult(2, "Error: Specified qdisc kind is unknown.\n")
    record, _ = execute(resolved, backend)
    assert record.status == Status.ERROR
    assert record.reason is not None
    assert "ugv1: cannot put profile lte_rural on the link (exit 2" in record.reason
    assert LINK_KERNEL_HINT in record.reason
    assert "Specified qdisc kind is unknown" in record.reason
    assert resolved.vehicles[0].container.name not in backend.started
    assert resolved.links is not None
    assert resolved.links.ground.name not in backend.started
    assert sorted(backend.removed_networks) == sorted(resolved.links.networks())


def test_a_ground_station_that_never_gets_ready(resolved: Plan) -> None:
    backend = ready_backend(resolved)
    backend.network_on_start = [
        {"kind": "error", "message": "cannot resolve the link holder of ugv1"},
        "not json",
        "[1, 2]",
    ]
    record, log = execute(resolved, backend)
    assert record.status == Status.ERROR
    assert record.reason == "timed out after 60 s waiting for the ground station to be ready"
    assert events(log, "ground.error")[0]["message"] == "cannot resolve the link holder of ugv1"


def test_a_ground_station_that_cannot_probe(resolved: Plan) -> None:
    backend = ready_backend(resolved)
    backend.network_on_start = [{"kind": "ready", "probing": False}]
    record, log = execute(resolved, backend)
    assert (
        events(log, "ground.warning")[0]["message"] == "the ground station cannot probe the links"
    )
    assert record.status == Status.PASSED, record.reason


def test_keep_leaves_the_networks(resolved: Plan) -> None:
    backend = ready_backend(resolved)
    _, log = execute(resolved, backend, keep=True)
    assert backend.removed_networks == []
    kept = events(log, "teardown.kept")[0]["message"]
    assert resolved.links is not None
    assert all(name in kept for name in resolved.links.networks())


def test_teardown_sweeps_networks_and_reports_failures(resolved: Plan) -> None:
    backend = ready_backend(resolved)
    backend.ensure_network("fieldproof-1-stray", {RUN_LABEL: "link-run-1"})
    links = resolved.links
    assert links is not None
    backend.fail_remove_network = {links.radio}
    _, log = execute(resolved, backend)
    assert "fieldproof-1-stray" in backend.removed_networks
    assert links.onboard["ugv1"] in backend.removed_networks
    errors = [e for e in events(log, "teardown.error") if e.get("network") == links.radio]
    assert "has active endpoints" in errors[0]["message"]
    assert events(log, "teardown.done")[0]["message"].endswith("and 2 network(s) removed")


def test_traffic_counters_that_cannot_be_read(resolved: Plan) -> None:
    backend = ready_backend(resolved)
    backend.traffic_result = ExecResult(0, "not json")
    record, log = execute(resolved, backend)
    assert record.status == Status.PASSED, record.reason
    assert record.network is not None
    assert record.network["traffic"] == {}
    assert events(log, "link.traffic")[0]["level"] == "warning"
    backend = ready_backend(resolved)
    backend.traffic_result = ExecResult(1, "Cannot find device")
    record, _ = execute(resolved, backend)
    assert record.network is not None
    assert record.network["traffic"] == {"ugv1": {}}


def test_network_file_relayed_by_the_observer(resolved: Plan) -> None:
    assert resolved.observe["side_channels"]["network"] == "/fieldproof/network"
    assert resolved.observe["network_file"] == f"/fieldproof/run/{NETWORK_FILE}"
