"""Tests of the event plugin API and registry (task P2-T03): what a well-formed event is, how
the built-ins and the entry points load, the built-in events through a scripted context, and
`fieldproof events`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest
from fakes import FakeBackend, FakeContainer
from pydantic import Field, TypeAdapter
from typer.testing import CliRunner

from fieldproof.backend import ContainerSpec, ExecResult, LineSink
from fieldproof.cli import app
from fieldproof.dsl import ScenarioError, TimelineBase
from fieldproof.events import (
    BUILTIN,
    ActionRecord,
    Event,
    PlanContext,
    Registry,
    VehicleHandle,
    check_event,
    entry_union,
    load_registry,
    parameters_of,
    registry,
    resolved_data,
)
from fieldproof.events.container import ContainerKill, ContainerPause, ContainerResume
from fieldproof.events.link import LinkDrop, LinkRestore, LinkSet
from fieldproof.events.navigation import NavigateToPose, goal_message, goal_status
from fieldproof.events.param import ParamSet, value_text
from fieldproof.manifest import Name
from fieldproof.world import Pose, WorldInfo
from fieldproof_bridge.generator import Bundle
from fieldproof_bridge.generator import plan as bridge_plan
from fieldproof_bridge.manifest import Manifest as BridgeManifest

REPO = Path(__file__).resolve().parents[2]
NAV2 = REPO / "examples/vehicles/ugv_nav2/robot.yaml"

BUILTINS = {
    "navigate_to_pose",
    "link.set",
    "link.drop",
    "link.restore",
    "container.pause",
    "container.resume",
    "container.kill",
    "param.set",
}


# ---------------------------------------------------------------------------------- fakes
class Vehicle:
    """A vehicle of the plan (the example vehicle's manifest for its bridge bundle)."""

    def __init__(self, name: str = "ugv1", namespace: str = "", manifest: Path = NAV2) -> None:
        self.name = name
        self.namespace = namespace
        self.container_name = f"fieldproof-run-{name}"
        self.bridge_name = f"fieldproof-run-bridge-{name}"
        self.spawn = Pose(10.0, 3.0, 0.0, -1.5708)
        self.manifest = manifest

    def topic(self, name: str) -> str:
        if name.startswith("/"):
            return name
        return f"/{self.namespace}/{name}" if self.namespace else f"/{name}"

    def bundle(self) -> Bundle:
        return bridge_plan(BridgeManifest.load(self.manifest), namespace=self.namespace)


class Context:
    """The event context on the fake backend, with a fake clock and scripted answers."""

    def __init__(self, backend: FakeBackend, vehicles: Sequence[Vehicle] = ()) -> None:
        self.backend = backend
        self.vehicles = {v.name: v for v in vehicles or [Vehicle()]}
        self.tearing_down = False
        self.t = 0.0
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.ros2_calls: list[tuple[str, tuple[str, ...], float]] = []
        self.ros2_result = ExecResult(0, "")
        self.ros2_results: list[ExecResult] = []
        """Answers in order before `ros2_result`."""
        self.bridge_calls: list[tuple[str, tuple[str, ...], float]] = []
        self.gz_calls: list[dict[str, Any]] = []
        self.gz_result = ExecResult(0, 'data: "applied"\n')
        self.gz_results: list[ExecResult] = []
        self.links: list[tuple[str, str, str]] = []
        self.exits: set[str] = set()
        self.paused: dict[str, bool] = {}
        self.held: list[float] = []
        self.stop_after_hold = False

    def vehicle(self, name: str | None) -> Vehicle:
        if name is None:
            raise RuntimeError("no vehicle")
        return self.vehicles[name]

    def exec(
        self, container: str, argv: Sequence[str], on_line: LineSink | None = None
    ) -> ExecResult:
        return self.backend.exec(container, argv, on_line)

    def ros2(
        self,
        vehicle: VehicleHandle,
        argv: Sequence[str],
        *,
        timeout_s: float,
        on_line: LineSink | None = None,
    ) -> ExecResult:
        self.ros2_calls.append((vehicle.container_name, tuple(argv), timeout_s))
        result = self.ros2_results.pop(0) if self.ros2_results else self.ros2_result
        if on_line is not None:
            for line in result.output.splitlines():
                on_line(line)
        return result

    def bridge_ros2(
        self,
        vehicle: VehicleHandle,
        argv: Sequence[str],
        *,
        timeout_s: float,
        on_line: LineSink | None = None,
    ) -> ExecResult:
        self.bridge_calls.append((vehicle.bridge_name, tuple(argv), timeout_s))
        result = self.ros2_results.pop(0) if self.ros2_results else self.ros2_result
        if on_line is not None:
            for line in result.output.splitlines():
                on_line(line)
        return result

    def gz_service(
        self,
        service: str,
        *,
        request_type: str,
        reply_type: str,
        request: str,
        timeout_s: float = 10.0,
    ) -> ExecResult:
        self.gz_calls.append(
            {
                "service": service,
                "request_type": request_type,
                "reply_type": reply_type,
                "request": request,
                "timeout_s": timeout_s,
            }
        )
        return self.gz_results.pop(0) if self.gz_results else self.gz_result

    def log(self, event: str, message: str = "", *, level: str = "info", **fields: Any) -> None:
        self.events.append((event, message, {"level": level, **fields}))

    def clock(self) -> float:
        return self.t

    def hold(self, seconds: float) -> bool:
        self.held.append(seconds)
        self.t += seconds
        if self.stop_after_hold:
            self.tearing_down = True
        return not self.tearing_down

    def expect_exit(self, container: str) -> None:
        self.exits.add(container)

    def mark_paused(self, container: str, paused: bool) -> None:
        self.paused[container] = paused

    def apply_link(self, vehicle: str, profile: str, cause: str) -> None:
        self.links.append((vehicle, profile, cause))

    def link_profile(self, vehicle: str) -> str:
        return f"configured-{vehicle}"


def backend_with(*names: str) -> FakeBackend:
    backend = FakeBackend()
    for name in names:
        spec = ContainerSpec(name=f"fieldproof-run-{name}", image="img", role="vehicle")
        backend.containers[spec.name] = FakeContainer(spec)
    return backend


def record(name: str = "e", action: str = "x") -> ActionRecord:
    return ActionRecord(name, action, "ugv1", 0.0)


# ---------------------------------------------------------------------------- the registry
def test_builtins_and_the_example_plugin_are_registered() -> None:
    known = registry()
    assert set(known.actions) >= BUILTINS
    assert "example.announce" in known
    assert known.problems == []
    for action in BUILTINS:
        assert known[action].source == BUILTIN
        assert known[action].action == action
        assert known[action].event.summary
    assert known["example.announce"].source.startswith("fieldproof-example-events")
    assert len(known.entry_types()) == len(known.actions)


def test_entry_union_tells_entries_apart() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(entry_union())
    entry = adapter.validate_python({"action": "container.kill", "vehicle": "ugv1"})
    assert isinstance(entry, ContainerKill)
    assert entry.signal == "SIGKILL"
    with pytest.raises(ValueError, match="'fly'"):
        adapter.validate_python({"action": "fly", "vehicle": "ugv1"})


class Good(TimelineBase):
    vehicle: Name = Field(description="v")
    action: Literal["test.good"]
    strength: int = Field(default=1, description="How hard.")


class GoodEvent(Event):
    action: ClassVar[str] = "test.good"
    Entry: ClassVar[type[TimelineBase]] = Good
    summary: ClassVar[str] = "A test event."

    def apply(self, entry: Any, resolved: Any, context: Any, record: ActionRecord) -> None:
        record.detail = f"strength {entry.strength}"


class WrongLiteral(TimelineBase):
    action: Literal["test.other"]


class BadEvent(Event):
    action: ClassVar[str] = "test.bad"
    Entry: ClassVar[type[TimelineBase]] = WrongLiteral


class NotAModel(Event):
    action: ClassVar[str] = "test.model"
    Entry = dict  # type: ignore[assignment]


class NoAction(Event):
    Entry: ClassVar[type[TimelineBase]] = Good


class ParamImpostor(Event):
    action: ClassVar[str] = "param.set"
    Entry: ClassVar[type[TimelineBase]] = ParamSet


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (object(), "not an Event subclass"),
        (NoAction, "`action` must be a non-empty string"),
        (NotAModel, "`Entry` must derive from TimelineBase"),
        (BadEvent, r"WrongLiteral.action must be Literal\['test.bad'\]"),
    ],
)
def test_check_event_refuses(candidate: Any, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        check_event(candidate)
    assert check_event(GoodEvent) is GoodEvent


def test_registry_add_and_collisions() -> None:
    reg = Registry()
    reg.add(GoodEvent, "here")
    reg.add(GoodEvent, "again")  # the same class twice is fine
    assert reg.actions == ["test.good"]

    class Impostor(GoodEvent):
        pass

    with pytest.raises(ValueError, match=r"'test\.good' is already registered by here"):
        reg.add(Impostor, "there")
    assert parameters_of(Good) == {"strength": "How hard."}


def test_load_registry_reports_broken_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    points = [
        EntryPoint("test.good", "test_events:GoodEvent", "fieldproof.events"),
        EntryPoint("test.missing", "no_such_module_xyz:Thing", "fieldproof.events"),
        EntryPoint("test.bad", "test_events:BadEvent", "fieldproof.events"),
        EntryPoint("param.set", "test_events:ParamImpostor", "fieldproof.events"),
    ]
    reg = load_registry(points)
    assert "test.good" in reg
    assert reg["test.good"].source == "an entry point"
    assert set(reg.actions) >= BUILTINS
    assert len(reg.problems) == 3
    assert reg.problems[0].startswith("an entry point (test.missing = no_such_module_xyz:Thing)")
    assert "ModuleNotFoundError" in reg.problems[0]
    assert "Literal['test.bad']" in reg.problems[1]
    assert "action 'param.set' is already registered by built-in" in reg.problems[2]


def test_load_registry_without_builtins() -> None:
    reg = load_registry([], builtin_modules=())
    assert reg.actions == []


def test_resolved_data() -> None:
    assert resolved_data({"goal": Pose(1, 2, 3, 4), "n": 5}) == {
        "goal": {"x": 1, "y": 2, "z": 3, "yaw": 4},
        "n": 5,
    }


# ------------------------------------------------------------------------------ the events
def world() -> WorldInfo:
    return WorldInfo(file=None, name="depot", frames={"spawn_dock": Pose(10.0, 3.0, 0.0, -1.5708)})  # type: ignore[arg-type]


def plan_context(**kwargs: Any) -> PlanContext:
    return PlanContext(world(), {"ugv1": Vehicle()}, where="timeline[0]", **kwargs)


def test_navigate_plan_and_apply() -> None:
    event = registry()["navigate_to_pose"].event
    entry = NavigateToPose.model_validate(
        {"vehicle": "ugv1", "action": "navigate_to_pose", "goal": {"x": 10.0, "y": -1.0}}
    )
    resolved = event.plan(entry, plan_context())
    assert resolved["goal_world"] == Pose(10.0, -1.0, 0.0, 0.0)
    assert resolved["goal_map"].x == pytest.approx(4.0)
    with pytest.raises(ScenarioError, match=r"timeline\[0\].goal: world depot has no frame 'x'"):
        event.plan(
            entry.model_copy(
                update={"goal": entry.goal.model_copy(update={"frame": "x", "x": None, "y": None})}
            ),
            plan_context(),
        )
    context = Context(backend_with("ugv1"))
    context.ros2_result = ExecResult(0, "Goal finished with status: SUCCEEDED\n")
    rec = record("drive", "navigate_to_pose")
    event.apply(entry, {**resolved, "limit_s": 120.0}, context, rec)
    assert (rec.status, rec.detail) == ("ok", "goal SUCCEEDED")
    (call,) = context.ros2_calls
    assert call[1][:3] == ("action", "send_goal", "/navigate_to_pose")
    assert json.loads(call[1][4])["pose"]["pose"]["position"]["x"] == pytest.approx(4.0)
    assert call[2] == 120.0
    assert context.events[0][0] == "action.goal"
    assert context.events[-1][0] == "action.output"
    for output, status, detail in [
        (ExecResult(124, ""), "timeout", "no result within 120 s"),
        (ExecResult(0, "Goal finished with status: ABORTED\n"), "failed", "goal ABORTED"),
        (ExecResult(1, "boom\n"), "failed", "no goal result (exit 1): boom"),
    ]:
        context.ros2_result = output
        rec = record()
        event.apply(entry, {**resolved, "limit_s": 120.0}, context, rec)
        assert (rec.status, rec.detail) == (status, detail)
    assert goal_status(ExecResult(0, "")) is None
    assert json.loads(goal_message(Pose(1, 2, 0, 0)))["pose"]["header"]["frame_id"] == "map"


def test_link_events() -> None:
    known = registry()
    context = Context(backend_with("ugv1"))
    with pytest.raises(ScenarioError, match=r"timeline\[0\]: link.set needs a network section"):
        known["link.set"].event.plan(
            LinkSet(vehicle="ugv1", action="link.set", profile="p"), plan_context()
        )
    with pytest.raises(ScenarioError, match="'ugv9' is not a vehicle"):
        known["link.drop"].event.plan(
            LinkDrop(vehicle="ugv9", action="link.drop"), plan_context(links=True)
        )
    assert (
        known["link.restore"].event.plan(
            LinkRestore(vehicle="ugv1", action="link.restore"), plan_context(links=True)
        )
        == {}
    )
    rec = record("set")
    known["link.set"].event.apply(
        LinkSet(vehicle="ugv1", action="link.set", profile="p"), {}, context, rec
    )
    assert (rec.status, rec.detail) == ("ok", "p applied")
    rec = record("restore")
    known["link.restore"].event.apply(
        LinkRestore(vehicle="ugv1", action="link.restore"), {}, context, rec
    )
    assert (rec.status, rec.detail) == ("ok", "configured-ugv1 restored")
    rec = record("cut")
    known["link.drop"].event.apply(LinkDrop(vehicle="ugv1", action="link.drop"), {}, context, rec)
    assert (rec.status, rec.detail) == ("ok", "dropped")
    rec = record("outage")
    known["link.drop"].event.apply(
        LinkDrop(vehicle="ugv1", action="link.drop", duration_s=20),
        {"limit_s": 100.0},
        context,
        rec,
    )
    assert (rec.status, rec.detail) == ("ok", "dropped for 20 s, configured-ugv1 restored")
    assert context.held == [20.0]
    assert context.links == [
        ("ugv1", "p", "set"),
        ("ugv1", "configured-ugv1", "restore"),
        ("ugv1", "drop", "cut"),
        ("ugv1", "drop", "outage"),
        ("ugv1", "configured-ugv1", "outage (restore)"),
    ]
    rec = record("short")
    known["link.drop"].event.apply(
        LinkDrop(vehicle="ugv1", action="link.drop", duration_s=20), {"limit_s": 5.0}, context, rec
    )
    assert (rec.status, rec.detail) == ("timeout", "the budget ended the drop after 5 s")
    context.stop_after_hold = True
    rec = record("torn")
    known["link.drop"].event.apply(
        LinkDrop(vehicle="ugv1", action="link.drop", duration_s=20),
        {"limit_s": 100.0},
        context,
        rec,
    )
    assert (rec.status, rec.detail) == ("ok", "dropped")


def test_container_events() -> None:
    known = registry()
    backend = backend_with("ugv1")
    context = Context(backend)
    name = "fieldproof-run-ugv1"
    rec = record("freeze")
    known["container.pause"].event.apply(
        ContainerPause(vehicle="ugv1", action="container.pause", duration_s=3),
        {"limit_s": 60.0},
        context,
        rec,
    )
    assert (rec.status, rec.detail) == ("ok", "paused for 3 s, resumed")
    assert backend.paused == [(name, True), (name, False)]
    assert context.paused == {name: False}
    assert [e[0] for e in context.events] == ["container.paused", "container.resumed"]
    rec = record("hold")
    known["container.pause"].event.apply(
        ContainerPause(vehicle="ugv1", action="container.pause"), {"limit_s": 60.0}, context, rec
    )
    assert (rec.status, rec.detail) == ("ok", "paused")
    assert backend.containers[name].paused is True
    assert context.paused == {name: True}
    rec = record("resume")
    known["container.resume"].event.apply(
        ContainerResume(vehicle="ugv1", action="container.resume"), {}, context, rec
    )
    assert (rec.status, rec.detail) == ("ok", "resumed")
    assert backend.containers[name].paused is False
    rec = record("short")
    known["container.pause"].event.apply(
        ContainerPause(vehicle="ugv1", action="container.pause", duration_s=30),
        {"limit_s": 5.0},
        context,
        rec,
    )
    assert (rec.status, rec.detail) == ("timeout", "the budget ended the pause after 5 s")
    assert backend.containers[name].paused is True
    backend.unpause(name)
    rec = record("kill")
    known["container.kill"].event.apply(
        ContainerKill(vehicle="ugv1", action="container.kill", signal="SIGTERM"), {}, context, rec
    )
    assert (rec.status, rec.detail) == ("ok", "SIGTERM sent")
    assert backend.killed == [(name, "SIGTERM")]
    assert context.exits == {name}
    assert backend.containers[name].running is False
    with pytest.raises(ScenarioError, match="'ugv9' is not a vehicle"):
        known["container.kill"].event.plan(
            ContainerKill(vehicle="ugv9", action="container.kill"), plan_context()
        )


def test_param_set() -> None:
    event = registry()["param.set"].event
    entry = ParamSet.model_validate(
        {
            "vehicle": "ugv1",
            "action": "param.set",
            "node": "link_monitor",
            "parameter": "loss_timeout_s",
            "value": 5.0,
        }
    )
    context = Context(backend_with("ugv1"), [Vehicle(namespace="ugv1")])
    resolved = event.plan(entry, PlanContext(world(), context.vehicles, where="timeline[1]"))
    assert resolved == {"node": "/ugv1/link_monitor", "value": "5.0"}
    absolute = entry.model_copy(update={"node": "/other/node", "value": "5"})
    assert event.plan(absolute, PlanContext(world(), context.vehicles))["node"] == "/other/node"
    assert event.plan(absolute, PlanContext(world(), context.vehicles))["value"] == '"5"'
    context.ros2_result = ExecResult(0, "Set parameter successful\n")
    rec = record("tighten", "param.set")
    event.apply(entry, {**resolved, "limit_s": 100.0}, context, rec)
    assert (rec.status, rec.detail) == ("ok", "/ugv1/link_monitor loss_timeout_s = 5.0")
    (call,) = context.ros2_calls
    assert call[1] == ("param", "set", "/ugv1/link_monitor", "loss_timeout_s", "5.0")
    assert call[2] == 30.0
    for output, status, detail in [
        (ExecResult(124, ""), "timeout", "/ugv1/link_monitor did not answer within 30 s"),
        (
            ExecResult(1, "Setting parameter failed: x\n"),
            "failed",
            "/ugv1/link_monitor refused loss_timeout_s (exit 1): Setting parameter failed: x",
        ),
        (
            ExecResult(0, ""),
            "failed",
            "/ugv1/link_monitor refused loss_timeout_s (exit 0): no output",
        ),
    ]:
        context.ros2_result = output
        rec = record()
        event.apply(entry, {**resolved, "limit_s": 100.0}, context, rec)
        assert (rec.status, rec.detail) == (status, detail)
    # The CLI's graph lags behind the stack: `Node not found` is asked again once a second.
    context.ros2_results = [ExecResult(1, "Node not found\n"), ExecResult(1, "Node not found\n")]
    context.ros2_result = ExecResult(0, "Set parameter successful\n")
    context.ros2_calls.clear()
    context.held.clear()
    rec = record()
    event.apply(entry, {**resolved, "limit_s": 100.0}, context, rec)
    assert (rec.status, rec.detail) == ("ok", "/ugv1/link_monitor loss_timeout_s = 5.0")
    assert len(context.ros2_calls) == 3
    assert context.held == [1.0, 1.0]
    assert [round(c[2]) for c in context.ros2_calls] == [30, 29, 28]
    context.ros2_result = ExecResult(1, "Node not found\n")
    context.ros2_calls.clear()
    rec = record()
    event.apply(entry, {**resolved, "limit_s": 3.0}, context, rec)
    assert (rec.status, rec.detail) == ("failed", "/ugv1/link_monitor not found within 3 s")
    assert len(context.ros2_calls) == 3
    context.stop_after_hold = True
    rec = record()
    event.apply(entry, {**resolved, "limit_s": 100.0}, context, rec)
    assert rec.status == "failed"
    context.stop_after_hold = False
    assert value_text(True) == "true"
    assert value_text(3) == "3"
    assert value_text(0.5) == "0.5"
    assert value_text("on") == '"on"'


# --------------------------------------------------------------------------------- the CLI
def test_events_command() -> None:
    result = CliRunner().invoke(app, ["events"])
    assert result.exit_code == 0, result.output
    for action in (*BUILTINS, "example.announce"):
        assert action in result.output
    assert "built-in" in result.output
    result = CliRunner().invoke(app, ["events", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["problems"] == []
    by_action = {e["action"]: e for e in data["events"]}
    assert by_action["container.pause"]["parameters"] == {
        "duration_s": "Resume after this long (absent: stay paused)."
    }
    assert by_action["example.announce"]["source"].startswith("fieldproof-example-events")


def test_events_command_reports_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = Registry(problems=["dist 1.0 (x = y:Z): ImportError: no"])
    monkeypatch.setattr("fieldproof.scenario_cli.registry", lambda: broken)
    result = CliRunner().invoke(app, ["events"])
    assert result.exit_code == 2
    assert "ImportError: no" in result.output
    result = CliRunner().invoke(app, ["events", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["problems"] == broken.problems
