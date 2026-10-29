"""The built-in `wait` event (task P2-T08): hold the timeline for a while.

A stack that runs on its own (a mission its launch starts, a status node) gives the timeline
nothing to send, and a timeline with nothing left to do ends the run as soon as the vehicles
are ready. `wait` keeps the run going for `duration_s`, so the sliding-window assertions and
the KPI collectors measure the stack, and a later entry's `after` can follow it. The hold
counts wall-clock seconds (simulation seconds in lockstep mode, like every timeline time) and
ends early, `cancelled` by the run, when the run is torn down.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field

from fieldproof.dsl import Duration, TimelineBase
from fieldproof.events import OK, TIMEOUT, ActionRecord, Event, EventContext, PlanContext


class Wait(TimelineBase):
    """Hold the timeline for a duration (a stack that runs on its own gets measured)."""

    action: Literal["wait"]
    duration_s: Duration = Field(
        description="How long the timeline holds (wall clock; simulation seconds in lockstep)."
    )


class WaitEvent(Event):
    """`wait`."""

    action: ClassVar[str] = "wait"
    Entry: ClassVar[type[TimelineBase]] = Wait
    summary: ClassVar[str] = (
        "Hold the timeline for a duration, so a stack that runs on its own is measured."
    )

    def plan(self, entry: Wait, context: PlanContext) -> dict[str, Any]:
        """A vehicle named by the entry must be one of the scenario's."""
        if entry.vehicle is not None:
            context.vehicle(entry.vehicle)
        return {}

    def apply(
        self,
        entry: Wait,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Hold the worker for the duration, or for what is left of the run's budget."""
        limit_s = float(resolved.get("limit_s", entry.duration_s))
        context.started(record)
        if not context.hold(min(entry.duration_s, limit_s)):
            return
        if entry.duration_s > limit_s:
            record.status = TIMEOUT
            record.detail = f"the budget ended the hold after {limit_s:.0f} s"
            return
        record.status, record.detail = OK, f"held for {entry.duration_s:g} s"


EVENTS = (WaitEvent,)
