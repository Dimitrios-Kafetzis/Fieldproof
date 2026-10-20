"""Tests of the events in the runner (task P2-T03): the example scenario `ugv_events.yaml` on
the fake backend, with every trigger of the DSL (`at`, `after`, `when`, `every`) and the
engine-side events (`container.pause`, `container.kill`, `param.set`, the example plugin's
`example.announce`), what the record and the log carry, and what goes wrong when an event
fails, a condition never holds or the teardown finds a paused container."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes import FakeClock
from test_runner import GOOD_SAMPLES, T0, execute, ready_backend

from fieldproof.backend import ExecResult
from fieldproof.config import Config, load_config
from fieldproof.runner import LOG_FILE, Plan, Status, plan
from fieldproof.scenario import Scenario, load_scenario, validate_data

REPO = Path(__file__).resolve().parents[2]
EVENTS = REPO / "examples/scenarios/ugv_events.yaml"


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> Config:
    fuel = tmp_path_factory.mktemp("fuel")
    for model in yaml.safe_load((REPO / "assets/fuel.lock").read_text())["models"]:
        (fuel / model["dir"]).mkdir()
    config = load_config(REPO / "fieldproof.toml", env={})
    config.stack.fuel = str(fuel)
    return config


@pytest.fixture(scope="module")
def example() -> Scenario:
    return load_scenario(EVENTS)


def variant(mutate: Callable[[dict[str, Any]], None]) -> Scenario:
    text = EVENTS.read_text()
    data = yaml.safe_load(text)
    mutate(data)
    return validate_data(data, EVENTS.resolve(), text)


def entry(data: dict[str, Any], name: str) -> dict[str, Any]:
    """The timeline entry called `name`."""
    return next(e for e in data["timeline"] if e.get("name") == name)


def drop(data: dict[str, Any], *names: str) -> None:
    """Remove timeline entries and the assertions that name them."""
    data["timeline"] = [e for e in data["timeline"] if e.get("name") not in names]
    data["assertions"] = [a for a in data["assertions"] if a.get("action") not in names]


@pytest.fixture
def resolved(example: Scenario, config: Config, tmp_path: Path) -> Plan:
    return plan(example, config, run_id="events-run-1", runs_dir=tmp_path, render="software")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def resolve(scenario: Scenario, config: Config, tmp_path: Path, run_id: str) -> Plan:
    return plan(scenario, config, run_id=run_id, runs_dir=tmp_path, render="software")


def events_of(log_file: Path, kind: str) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in log_file.read_text().splitlines()]
    return [e for e in lines if e["event"] == kind]


def statuses(record: Any) -> dict[str, str]:
    return {a.name: a.status for a in record.timeline}


# ------------------------------------------------------------------------------------ plan
def test_plan_resolves_every_trigger(resolved: Plan) -> None:
    by_name = {a.name: a for a in resolved.actions}
    # In order of `at`; the triggers decide the real order.
    assert list(by_name) == [
        "drive_ahead",
        "tighten_failsafe",
        "stop_stack",
        "freeze_stack",
        "say_hello",
    ]
    hello = by_name["say_hello"].to_dict()
    assert hello["trigger"] == {"at_s": 2.0, "every_s": 5.0, "count": 3}
    assert hello["topic"] == "/announce"
    assert hello["text"] == "fieldproof says hello"
    tighten = by_name["tighten_failsafe"]
    assert tighten.watch is not None
    assert tighten.watch.model == "ugv1"
    assert tighten.watch.target is not None
    assert tighten.watch.target.x == 10.0
    data = tighten.to_dict()
    assert data["trigger"]["when"]["metric"] == "distance_to"
    assert data["trigger"]["within_s"] == 240.0
    assert data["when"]["target_world"] == {"x": 10.0, "y": 3.0}
    assert data["node"] == "/link_monitor"
    assert data["value"] == "5.0"
    assert by_name["freeze_stack"].to_dict()["trigger"] == {"at_s": 1.0, "after": "drive_ahead"}
    assert by_name["stop_stack"].to_dict() == {
        "name": "stop_stack",
        "at_s": 0.0,
        "vehicle": "ugv1",
        "action": "container.kill",
        "trigger": {"at_s": 0.0, "after": "freeze_stack"},
        "signal": "SIGKILL",
    }
    # The observer watches nothing more: the condition reads the ground truth, not a topic.
    assert resolved.observe["values"] == []
    json.dumps(resolved.to_dict())


def test_plan_watches_a_when_topic(config: Config, tmp_path: Path) -> None:
    scenario = variant(
        lambda d: entry(d, "tighten_failsafe").update(
            when={"topic": "odom", "field": "twist.twist.linear.x", "condition": "value > 0.1"}
        )
    )
    resolved = resolve(scenario, config, tmp_path, "events-run-2")
    assert resolved.observe["values"] == [
        {"topic": "/odom", "field": "twist.twist.linear.x", "type": None}
    ]


# ------------------------------------------------------------------------------------- run
def test_run_passes_through_every_trigger(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    record, log = execute(resolved, backend, clock)
    assert record.status == Status.PASSED, record.reason
    assert statuses(record) == {
        "drive_ahead": "ok",
        "say_hello": "ok",
        "tighten_failsafe": "ok",
        "freeze_stack": "ok",
        "stop_stack": "ok",
        "say_hello#2": "ok",
        "say_hello#3": "ok",
    }
    by_name = {a.name: a for a in record.timeline}
    assert by_name["say_hello"].occurrence == 1
    assert by_name["say_hello#3"].occurrence == 3
    assert by_name["say_hello#3"].trigger == {"at_s": 2.0, "every_s": 5.0, "count": 3}
    assert by_name["say_hello"].detail == "published 'fieldproof says hello' on /announce"
    assert by_name["tighten_failsafe"].detail == "/link_monitor loss_timeout_s = 5.0"
    assert by_name["tighten_failsafe"].params == {
        "node": "link_monitor",
        "parameter": "loss_timeout_s",
        "value": 5.0,
        "timeout_s": 30.0,
    }
    assert by_name["freeze_stack"].detail == "paused for 3 s, resumed"
    drive_s = by_name["drive_ahead"].duration_s
    freeze_t = by_name["freeze_stack"].t_s
    assert drive_s is not None
    assert freeze_t is not None
    assert freeze_t >= drive_s + 1.0
    assert by_name["stop_stack"].detail == "SIGKILL sent"
    vehicle = resolved.vehicles[0].container.name
    assert backend.paused == [(vehicle, True), (vehicle, False)]
    assert backend.killed == [(vehicle, "SIGKILL")]
    # The killed container did not end the run; the teardown removed everything anyway.
    assert backend.containers == {}
    assert {a["name"]: a["status"] for a in record.assertions} == {
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "said_hello": "passed",
        "failsafe_tightened": "passed",
        "stack_frozen": "passed",
        "stack_stopped": "passed",
        "no_collision": "passed",
    }
    # Every occurrence is on the log (and so on /fieldproof/events) with its record.
    starts = events_of(resolved.run_dir / LOG_FILE, "action.start")
    assert [e["action"] for e in starts] == [
        "drive_ahead",
        "say_hello",
        "tighten_failsafe",
        "say_hello#2",
        "freeze_stack",
        "say_hello#3",
        "stop_stack",
    ]
    assert starts[2]["event_action"] == "param.set"
    assert starts[2]["trigger"]["when"]["metric"] == "distance_to"
    assert starts[2]["params"]["parameter"] == "loss_timeout_s"
    ends = events_of(resolved.run_dir / LOG_FILE, "action.end")
    assert {e["action"]: e["status"] for e in ends} == dict.fromkeys(statuses(record), "ok")
    triggered = events_of(resolved.run_dir / LOG_FILE, "action.triggered")
    assert [e["action"] for e in triggered] == ["tighten_failsafe", "freeze_stack", "stop_stack"]
    assert triggered[0]["message"].startswith("tighten_failsafe: condition held at")
    assert triggered[1]["message"] == "freeze_stack: after drive_ahead at 10.0 s"
    assert [e["event"] for e in log.events if e["event"].startswith("container.")] == [
        "container.paused",
        "container.resumed",
        "container.killed",
    ]


def test_when_that_never_holds_is_skipped(config: Config, tmp_path: Path, clock: FakeClock) -> None:
    scenario = variant(
        lambda d: entry(d, "tighten_failsafe")["when"].update(condition="value > 1000")
    )
    resolved = resolve(scenario, config, tmp_path, "events-skip")
    backend = ready_backend(resolved)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason.startswith(
        "assertion failsafe_tightened failed: tighten_failsafe was skipped"
    )
    by_name = {a.name: a for a in record.timeline}
    assert by_name["tighten_failsafe"].status == "skipped"
    assert by_name["tighten_failsafe"].detail == "condition never held within 240 s"
    assert by_name["stop_stack"].status == "ok"
    skipped = events_of(resolved.run_dir / LOG_FILE, "action.skipped")
    assert [e["action"] for e in skipped] == ["tighten_failsafe"]
    # Nothing else was wrong: the drive and the other events ran.
    assert {a["name"]: a["status"] for a in record.assertions}["stack_stopped"] == "passed"


def test_within_s_bounds_a_when(config: Config, tmp_path: Path, clock: FakeClock) -> None:
    """The condition holds only after `within_s`: the entry waits that long, then is skipped."""

    def mutate(d: dict[str, Any]) -> None:
        entry(d, "tighten_failsafe").update(within_s="3s")
        d["assertions"] = [a for a in d["assertions"] if a["name"] != "failsafe_tightened"]

    resolved = resolve(variant(mutate), config, tmp_path, "events-within")
    record, _ = execute(resolved, ready_backend(resolved), clock)
    assert record.status == Status.PASSED, record.reason
    assert statuses(record)["tighten_failsafe"] == "skipped"


def test_after_a_skipped_entry_is_skipped(config: Config, tmp_path: Path, clock: FakeClock) -> None:
    def mutate(d: dict[str, Any]) -> None:
        entry(d, "tighten_failsafe")["when"].update(condition="value > 1000")
        entry(d, "freeze_stack").update(after="tighten_failsafe")
        d["assertions"] = [a for a in d["assertions"] if a["type"] != "within_time"]

    resolved = resolve(variant(mutate), config, tmp_path, "events-chain")
    record, _ = execute(resolved, ready_backend(resolved), clock)
    assert record.status == Status.PASSED, record.reason
    assert statuses(record) == {
        "drive_ahead": "ok",
        "say_hello": "ok",
        "tighten_failsafe": "skipped",
        "freeze_stack": "skipped",
        "stop_stack": "skipped",
        "say_hello#2": "ok",
        "say_hello#3": "ok",
    }
    by_name = {a.name: a for a in record.timeline}
    assert by_name["freeze_stack"].detail == "tighten_failsafe did not end ok"


def test_failed_event_fails_the_run(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    backend.param_result = ExecResult(1, "Setting parameter failed: parameter not declared\n")
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason == (
        "tighten_failsafe: /link_monitor refused loss_timeout_s (exit 1): Setting parameter "
        "failed: parameter not declared"
    )
    assert statuses(record)["tighten_failsafe"] == "failed"
    assert backend.containers == {}


def test_event_exception_fails_the_run(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)

    def refusing(name: str) -> None:
        raise RuntimeError(f"{name}: the runtime is busy")

    backend.pause = refusing  # type: ignore[method-assign]
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.FAILED
    assert record.reason.startswith("freeze_stack: RuntimeError:")
    assert statuses(record)["freeze_stack"] == "failed"


def test_teardown_resumes_a_paused_container(
    config: Config, tmp_path: Path, clock: FakeClock
) -> None:
    """A pause without a duration leaves the container paused; the teardown thaws it first."""

    def mutate(d: dict[str, Any]) -> None:
        del entry(d, "freeze_stack")["duration_s"]
        drop(d, "stop_stack", "say_hello")  # nothing may run in the paused container

    resolved = resolve(variant(mutate), config, tmp_path, "events-paused")
    backend = ready_backend(resolved)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.PASSED, record.reason
    vehicle = resolved.vehicles[0].container.name
    assert backend.paused == [(vehicle, True), (vehicle, False)]
    assert vehicle in backend.stopped
    assert backend.containers == {}


def test_killed_vehicle_is_expected_gone(config: Config, tmp_path: Path, clock: FakeClock) -> None:
    """After the kill, the run keeps going (another entry runs) and the exit is not an error."""

    def mutate(d: dict[str, Any]) -> None:
        entry(d, "stop_stack").update(after="drive_ahead", at=0)
        entry(d, "freeze_stack").update(after="stop_stack", at=5, action="container.resume")
        del entry(d, "freeze_stack")["duration_s"]
        drop(d, "say_hello")  # nothing may run in the killed container
        d["assertions"] = [a for a in d["assertions"] if a["name"] != "stack_frozen"]

    resolved = resolve(variant(mutate), config, tmp_path, "events-killed")
    backend = ready_backend(resolved)
    record, _ = execute(resolved, backend, clock)
    # Resuming a killed container fails that entry, not the alive check.
    assert record.status == Status.FAILED
    assert record.reason.startswith("freeze_stack: BackendError:")
    assert statuses(record)["stop_stack"] == "ok"
    assert statuses(record)["freeze_stack"] == "failed"


def test_sim_time_condition(config: Config, tmp_path: Path, clock: FakeClock) -> None:
    def mutate(d: dict[str, Any]) -> None:
        entry(d, "tighten_failsafe").update(when={"metric": "sim_time", "condition": "value >= 9"})

    resolved = resolve(variant(mutate), config, tmp_path, "events-sim")
    backend = ready_backend(resolved)
    backend.samples_on_goal = [*GOOD_SAMPLES, {"kind": "clock", "sim": 12.0, "rtf": 1.0}]
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.PASSED, record.reason
    by_name = {a.name: a for a in record.timeline}
    assert by_name["tighten_failsafe"].status == "ok"
    assert by_name["tighten_failsafe"].sim_s is not None


def test_budget_names_the_waiting_entry(config: Config, tmp_path: Path, clock: FakeClock) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["time"]["timeout_s"] = "8s"
        entry(d, "tighten_failsafe")["when"].update(condition="value > 1000")
        entry(d, "tighten_failsafe")["within_s"] = "20s"
        drop(d, "freeze_stack", "stop_stack")
        d["assertions"] = [a for a in d["assertions"] if a["type"] != "within_time"]

    resolved = resolve(variant(mutate), config, tmp_path, "events-budget")
    backend = ready_backend(resolved)
    backend.goal_duration_s = 1.0
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.TIMEOUT
    assert record.reason == "budget of 8 s passed before tighten_failsafe"


def test_wall_stamps(resolved: Plan, clock: FakeClock) -> None:
    backend = ready_backend(resolved)
    record, _ = execute(resolved, backend, clock)
    assert record.status == Status.PASSED
    for action in record.timeline:
        assert action.started_at is not None
        assert action.t_s is not None
        assert action.t_s >= 0.0
    assert backend.wall() > T0
