"""The built-in container events (task P2-T03).

`container.pause`, `container.resume` and `container.kill` act on the vehicle's container
through the run's backend: a pause freezes every process of the stack under test (the
simulator and the bridge go on, so the vehicle's last command stands in the world until the
stack resumes); a kill ends the container with a signal, which the run then expects to be
gone (the teardown and the record note it; the assertions decide whether the fleet coped).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field

from fieldproof.dsl import Duration, TimelineBase
from fieldproof.events import OK, TIMEOUT, ActionRecord, Event, EventContext, PlanContext
from fieldproof.manifest import Name

Signal = Literal["SIGKILL", "SIGTERM", "SIGINT", "SIGHUP"]


class ContainerPause(TimelineBase):
    """Pause the vehicle's container (every process of the stack freezes).

    With `duration_s` the entry resumes the container after that long and ends then; without
    it the container stays paused until a `container.resume` (or the teardown).
    """

    vehicle: Name = Field(description="The vehicle whose container pauses.")
    action: Literal["container.pause"]
    duration_s: Duration | None = Field(
        default=None, description="Resume after this long (absent: stay paused)."
    )


class ContainerResume(TimelineBase):
    """Resume the vehicle's paused container."""

    vehicle: Name = Field(description="The vehicle whose container resumes.")
    action: Literal["container.resume"]


class ContainerKill(TimelineBase):
    """Kill the vehicle's container with a signal; the run expects it gone from then on."""

    vehicle: Name = Field(description="The vehicle whose container is killed.")
    action: Literal["container.kill"]
    signal: Signal = Field(default="SIGKILL", description="The signal sent to the container.")


class VehicleEvent(Event):
    """An event of a vehicle: the plan checks the vehicle exists."""

    def plan(self, entry: TimelineBase, context: PlanContext) -> dict[str, Any]:
        """The vehicle must be one of the scenario's."""
        context.vehicle(entry.vehicle)
        return {}


class ContainerPauseEvent(VehicleEvent):
    """`container.pause`."""

    action: ClassVar[str] = "container.pause"
    Entry: ClassVar[type[TimelineBase]] = ContainerPause
    summary: ClassVar[str] = (
        "Pause the vehicle's container, for a duration (then resume it) or until resumed."
    )

    def apply(
        self,
        entry: ContainerPause,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Pause; with a duration, hold the worker and resume."""
        container = context.vehicle(entry.vehicle).container_name
        context.backend.pause(container)
        context.mark_paused(container, True)
        context.log("container.paused", container, action=record.name, container=container)
        record.status, record.detail = OK, "paused"
        if entry.duration_s is None:
            return
        limit_s = float(resolved.get("limit_s", entry.duration_s))
        if not context.hold(min(entry.duration_s, limit_s)):
            return
        if entry.duration_s > limit_s:
            record.status = TIMEOUT
            record.detail = f"the budget ended the pause after {limit_s:.0f} s"
            return
        context.backend.unpause(container)
        context.mark_paused(container, False)
        context.log("container.resumed", container, action=record.name, container=container)
        record.detail = f"paused for {entry.duration_s:g} s, resumed"


class ContainerResumeEvent(VehicleEvent):
    """`container.resume`."""

    action: ClassVar[str] = "container.resume"
    Entry: ClassVar[type[TimelineBase]] = ContainerResume
    summary: ClassVar[str] = "Resume the vehicle's paused container."

    def apply(
        self,
        entry: ContainerResume,
        resolved: Mapping[str, Any],  # noqa: ARG002
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Unpause."""
        container = context.vehicle(entry.vehicle).container_name
        context.backend.unpause(container)
        context.mark_paused(container, False)
        context.log("container.resumed", container, action=record.name, container=container)
        record.status, record.detail = OK, "resumed"


class ContainerKillEvent(VehicleEvent):
    """`container.kill`."""

    action: ClassVar[str] = "container.kill"
    Entry: ClassVar[type[TimelineBase]] = ContainerKill
    summary: ClassVar[str] = "Kill the vehicle's container with a signal (SIGKILL by default)."

    def apply(
        self,
        entry: ContainerKill,
        resolved: Mapping[str, Any],  # noqa: ARG002
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Expect the exit, then send the signal."""
        container = context.vehicle(entry.vehicle).container_name
        context.expect_exit(container)
        context.backend.kill(container, entry.signal)
        context.log(
            "container.killed",
            f"{container}: {entry.signal}",
            action=record.name,
            container=container,
            signal=entry.signal,
        )
        record.status, record.detail = OK, f"{entry.signal} sent"


EVENTS = (ContainerPauseEvent, ContainerResumeEvent, ContainerKillEvent)
