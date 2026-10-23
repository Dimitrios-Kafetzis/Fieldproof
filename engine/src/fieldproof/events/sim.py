"""The built-in simulator clock events (task P2-T04, ADR-0006).

`sim.pause`, `sim.resume` and `sim.step` act on the world's clock through its
`/world/<name>/control` service (`gz.msgs.WorldControl`), called with the `gz service` CLI
inside the simulator's container like the world events: a pause stops the physics and every
stack on simulation time with it (the clock keeps publishing the time it stopped at, so a
stack sees the time stand still, which a wall-clock watchdog notices and a sim-time timer does
not); a step advances a paused world by a number of physics steps and pauses again. The
engine's drift monitor leaves a paused world out of the drift. In lockstep mode the clock is
the engine's, so these entries are refused at validation time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveInt, model_validator

from fieldproof.dsl import Duration, PositiveDuration, ScenarioError, TimelineBase
from fieldproof.events import OK, TIMEOUT, ActionRecord, Event, EventContext, PlanContext
from fieldproof.world import parse_stats

WORLD_CONTROL_TYPE = "gz.msgs.WorldControl"
BOOLEAN_TYPE = "gz.msgs.Boolean"
STEP_POLL_S = 0.25
"""How often `sim.step` reads the world's statistics while the steps run."""


class SimPause(TimelineBase):
    """Pause the simulator's clock (every stack on simulation time stands still with it).

    With `duration_s` the entry resumes the world after that long of wall-clock time and ends
    then; without it the world stays paused until a `sim.resume` or a `sim.step`.
    """

    action: Literal["sim.pause"]
    duration_s: Duration | None = Field(
        default=None, description="Resume after this long on the wall clock (absent: stay paused)."
    )


class SimResume(TimelineBase):
    """Resume the simulator's clock."""

    action: Literal["sim.resume"]


class SimStep(TimelineBase):
    """Advance the simulator by a number of physics steps (or a duration) and pause it again."""

    action: Literal["sim.step"]
    steps: PositiveInt | None = Field(
        default=None, description="Physics steps to take (the world's `max_step_size` each)."
    )
    duration_s: PositiveDuration | None = Field(
        default=None, description="Simulation seconds to advance by (instead of `steps`)."
    )

    @model_validator(mode="after")
    def _one_of(self) -> SimStep:
        if (self.steps is None) == (self.duration_s is None):
            raise ValueError("sim.step takes either steps or duration_s")
        return self


class SimEvent(Event):
    """An event on the world's control service."""

    def plan(self, entry: Any, context: PlanContext) -> dict[str, Any]:  # noqa: ARG002
        """The service of the world; refused in lockstep mode."""
        if context.time_mode == "lockstep":
            raise ScenarioError(
                f"{context.where}: {self.action} does not combine with time.mode lockstep, "
                "where the engine steps the clock"
            )
        return {"service": f"/world/{context.world.name}/control", "world": context.world.name}

    def control(self, context: EventContext, resolved: Mapping[str, Any], request: str) -> bool:
        """One `WorldControl` request; whether the world accepted it."""
        result = context.gz_service(
            str(resolved["service"]),
            request_type=WORLD_CONTROL_TYPE,
            reply_type=BOOLEAN_TYPE,
            request=request,
            timeout_s=10.0,
        )
        return result.ok and "true" in result.output


class SimPauseEvent(SimEvent):
    """`sim.pause`."""

    action: ClassVar[str] = "sim.pause"
    Entry: ClassVar[type[TimelineBase]] = SimPause
    summary: ClassVar[str] = (
        "Pause the simulator's clock, for a duration (then resume it) or until resumed."
    )

    def apply(
        self,
        entry: SimPause,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Pause; with a duration, hold the worker and resume."""
        if not self.control(context, resolved, "pause: true"):
            record.status, record.detail = "failed", "the world refused to pause"
            return
        context.log("sim.paused", "the simulator's clock is paused", action=record.name)
        context.started(record)
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
        if not self.control(context, resolved, "pause: false"):
            record.status, record.detail = "failed", "the world refused to resume"
            return
        context.log("sim.resumed", "the simulator's clock runs again", action=record.name)
        record.detail = f"paused for {entry.duration_s:g} s, resumed"


class SimResumeEvent(SimEvent):
    """`sim.resume`."""

    action: ClassVar[str] = "sim.resume"
    Entry: ClassVar[type[TimelineBase]] = SimResume
    summary: ClassVar[str] = "Resume the simulator's paused clock."

    def apply(
        self,
        entry: SimResume,  # noqa: ARG002
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Unpause."""
        if not self.control(context, resolved, "pause: false"):
            record.status, record.detail = "failed", "the world refused to resume"
            return
        context.log("sim.resumed", "the simulator's clock runs again", action=record.name)
        record.status, record.detail = OK, "resumed"


class SimStepEvent(SimEvent):
    """`sim.step`."""

    action: ClassVar[str] = "sim.step"
    Entry: ClassVar[type[TimelineBase]] = SimStep
    summary: ClassVar[str] = (
        "Advance the simulator by physics steps (or a duration) and pause it again."
    )

    def plan(self, entry: SimStep, context: PlanContext) -> dict[str, Any]:
        """The service and the number of physics steps."""
        resolved = super().plan(entry, context)
        steps = entry.steps
        if steps is None and entry.duration_s is not None:
            steps = max(1, round(entry.duration_s / context.world.step_s))
        resolved["steps"] = steps
        resolved["step_s"] = context.world.step_s
        return resolved

    def apply(
        self,
        entry: SimStep,  # noqa: ARG002
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Step (pausing first if the world ran) and wait until the world has taken the steps.

        The world runs the steps at its real-time factor and pauses at the end, which would
        undo a `sim.resume` sent meanwhile, so the entry ends only once the statistics show
        the world paused past the target (or `limit_s` passed).
        """
        steps = int(resolved["steps"])
        advance = steps * float(resolved["step_s"])
        before = parse_stats(context.gz_topic(f"/world/{resolved['world']}/stats").output)
        target = before["sim_time_s"] + advance - 1e-6
        if not self.control(context, resolved, f"multi_step: {steps} pause: true"):
            record.status, record.detail = "failed", "the world refused to step"
            return
        context.started(record)
        deadline = context.clock() + min(float(resolved.get("limit_s", 60.0)), 60.0 + advance * 10)
        stats = before
        while context.clock() < deadline and not context.tearing_down:
            stats = parse_stats(context.gz_topic(f"/world/{resolved['world']}/stats").output)
            if stats["paused"] and stats["sim_time_s"] >= target:
                break
            if not context.hold(STEP_POLL_S):
                return
        else:
            record.status = TIMEOUT
            record.detail = (
                f"the world did not reach {target:.3f} s paused within the limit "
                f"(at {stats['sim_time_s']:.3f} s, {'paused' if stats['paused'] else 'running'})"
            )
            return
        context.log(
            "sim.stepped",
            f"{steps} physics step(s), {advance:g} s of simulation, paused at "
            f"{stats['sim_time_s']:.3f} s",
            action=record.name,
            steps=steps,
            sim_s=stats["sim_time_s"],
        )
        record.status, record.detail = OK, f"{steps} step(s), {advance:g} s, paused"


EVENTS = (SimPauseEvent, SimResumeEvent, SimStepEvent)
