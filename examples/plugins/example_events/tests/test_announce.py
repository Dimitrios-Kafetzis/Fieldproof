"""Tests of the example plugin: registered, planned and applied through a stub context."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest
from fieldproof_example_events import MESSAGE_TYPE, Announce, AnnounceEvent

from fieldproof.backend import ExecResult, LineSink
from fieldproof.dsl import ScenarioError
from fieldproof.events import ActionRecord, EventContext, PlanContext, VehicleHandle, registry
from fieldproof.world import Pose, WorldInfo


class Vehicle:
    """A vehicle of the plan, as the plugin needs it."""

    name = "ugv1"
    namespace = "ugv1"
    container_name = "fieldproof-x-ugv1"
    spawn = Pose(0.0, 0.0, 0.0, 0.0)

    def topic(self, name: str) -> str:
        return name if name.startswith("/") else f"/{self.namespace}/{name}"


class Context:
    """The event context, scripted: the `ros2` calls and their answer."""

    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[str, ...], float]] = []
        self.lines: list[str] = []

    def vehicle(self, name: str | None) -> Vehicle:
        assert name == "ugv1"
        return Vehicle()

    def ros2(
        self,
        vehicle: VehicleHandle,
        argv: Sequence[str],
        *,
        timeout_s: float,
        on_line: LineSink | None = None,
    ) -> ExecResult:
        self.calls.append((vehicle.container_name, tuple(argv), timeout_s))
        if on_line is not None:
            for line in self.result.output.splitlines():
                on_line(line)
        return self.result

    def log(self, event: str, message: str = "", *, level: str = "info", **fields: Any) -> None:
        self.lines.append(f"{level} {event} {message}")


def entry(**fields: Any) -> Announce:
    return Announce.model_validate(
        {"vehicle": "ugv1", "action": "example.announce", "text": "hello", **fields}
    )


def test_registered() -> None:
    registered = registry()["example.announce"]
    assert isinstance(registered.event, AnnounceEvent)
    assert registered.source.startswith("fieldproof-example-events 0.1.0")
    assert registered.entry_type is Announce


def test_plan_resolves_the_topic() -> None:
    world = WorldInfo(file=None, name="depot", frames={})  # type: ignore[arg-type]
    context = PlanContext(world, {"ugv1": Vehicle()}, where="timeline[0]")
    assert AnnounceEvent().plan(entry(), context) == {"topic": "/ugv1/announce"}
    assert AnnounceEvent().plan(entry(topic="/ground/announce"), context) == {
        "topic": "/ground/announce"
    }
    with pytest.raises(ScenarioError, match=r"timeline\[0\].vehicle: 'ugv9' is not a vehicle"):
        AnnounceEvent().plan(entry(vehicle="ugv9"), context)


@pytest.mark.parametrize(
    ("result", "status", "detail"),
    [
        (ExecResult(0, "publisher: beginning loop\npublishing #1\n"), "ok", "published 'hello'"),
        (ExecResult(124, ""), "timeout", "no 1 subscriber(s) of /ugv1/announce within 30 s"),
        (ExecResult(1, "boom\n"), "failed", "ros2 topic pub failed (exit 1): boom"),
    ],
)
def test_apply(result: ExecResult, status: str, detail: str) -> None:
    context = Context(result)
    record = ActionRecord("say_hello", "example.announce", "ugv1", 0.0)
    AnnounceEvent().apply(
        entry(subscribers=1),
        {"topic": "/ugv1/announce", "limit_s": 60.0},
        cast(EventContext, context),
        record,
    )
    assert record.status == status
    assert record.detail.startswith(detail)
    (call,) = context.calls
    assert call[0] == "fieldproof-x-ugv1"
    assert call[1][:5] == ("topic", "pub", "--once", "-w", "1")
    assert call[1][5:7] == ("/ugv1/announce", MESSAGE_TYPE)
    assert call[1][7] == '{"data": "hello"}'
    assert call[2] == 30.0
    assert len(context.lines) == len(result.output.splitlines())
