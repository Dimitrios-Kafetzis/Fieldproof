"""The timeline scheduler (task P2-T03): when each entry's occurrences start.

:class:`Schedule` takes the planned entries and answers the runner's questions as the timeline
goes, in seconds after the timeline started on the wall clock: which occurrences are due now
(:meth:`Schedule.due`), how long to wait for the next (:meth:`Schedule.wait`), and whether
anything can still happen (:meth:`Schedule.idle`). The runner tells it what it observed
(:meth:`Schedule.feed`, the observer's samples, which may trigger a ``when``) and how each
occurrence ended (:meth:`Schedule.finished`, which triggers the ``after`` entries and schedules
the next occurrence of an ``every``). It touches no container and no clock: the runner's tests
drive it with a fake clock, its own with numbers.

The rules, from :mod:`fieldproof.dsl`: an entry's trigger is the timeline start, the
successful end of its ``after`` entry (of every one, with a list), or the first sample that
satisfies its ``when``; its first occurrence is due ``at`` seconds after the trigger;
``every`` makes the next one due a period after the previous was due (an occurrence that
overran starts the next as soon as it ends), until ``count`` occurrences ran or ``until_s``
passed. The timeline is idle, and the
runner ends it, when nothing runs and nothing is *live*: an untriggered ``when`` is live only
until its ``within_s``, an ``after`` entry only while a predecessor is, and a repeated entry
without ``count`` or ``until_s`` is never live by itself. What is still waiting or repeating
then ends ``skipped`` (a ``when`` whose condition never held) or ``ok`` (a repetition that ran
its course), which is what the assertions see.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from fieldproof.assertions import Sample
from fieldproof.dsl import TimelineBase, When
from fieldproof.events import FINAL, OK, PENDING, RUNNING, SKIPPED, WAITING
from fieldproof.expression import Condition, ExpressionError, compile_condition
from fieldproof.world import Pose


@dataclass(frozen=True)
class PlannedWhen:
    """A `when` resolved against the plan: an absolute topic, a model, a target pose."""

    when: When
    topic: str | None = None
    """The ROS topic, absolute, for a topic condition."""
    model: str | None = None
    """The Gazebo model whose ground truth a vehicle metric reads."""
    target: Pose | None = None
    """Where `distance_to` measures to."""
    condition: Condition = field(default_factory=lambda: compile_condition("value"))

    def value_of(self, sample: Sample) -> Any:
        """What the condition applies to in this sample, or None if the sample is not its."""
        when, data = self.when, sample.data
        if when.topic is not None:
            ours = (
                sample.kind == "value"
                and data.get("topic") == self.topic
                and data.get("field") == when.field
            )
            return data.get("value") if ours else None
        if when.metric in ("sim_time", "rtf"):
            if sample.kind != "clock":
                return None
            return sample.sim if when.metric == "sim_time" else data.get("rtf")
        if sample.kind != "pose" or data.get("model") != self.model:
            return None
        if when.metric == "distance_to" and self.target is not None:
            pose = Pose(float(data["x"]), float(data["y"]), float(data.get("z", 0.0)), 0.0)
            return pose.distance_to(self.target)
        return data.get(str(when.metric))

    def holds(self, sample: Sample) -> bool:
        """Whether the sample satisfies the condition (False when it is not the sample's)."""
        value = self.value_of(sample)
        if value is None:
            return False
        try:
            return self.condition(value)
        except ExpressionError:
            return False

    def to_dict(self) -> dict[str, Any]:
        """As data."""
        data: dict[str, Any] = self.when.model_dump(exclude_none=True)
        if self.topic is not None:
            data["topic"] = self.topic
        if self.model is not None:
            data["model"] = self.model
        if self.target is not None:
            data["target_world"] = {"x": self.target.x, "y": self.target.y}
        return data


class Planned(Protocol):
    """A planned timeline entry, as the schedule needs it."""

    @property
    def name(self) -> str:
        """The entry's label."""

    @property
    def entry(self) -> TimelineBase:
        """The entry."""

    @property
    def watch(self) -> PlannedWhen | None:
        """Its `when`, resolved."""


@dataclass(frozen=True)
class Occurrence:
    """One start of an entry."""

    action: Any
    index: int
    due_s: float

    @property
    def name(self) -> str:
        """`<name>` for the first occurrence, `<name>#<index>` after."""
        base = str(self.action.name)
        return base if self.index == 1 else f"{base}#{self.index}"


@dataclass
class Scheduled:
    """The scheduling state of one entry."""

    action: Any
    status: str = PENDING
    """`pending` (triggered, an occurrence due at `next_due`), `waiting` (for its `after` entry
    or its `when`), `running`, `ok` (every occurrence ran), `skipped`."""
    triggered_s: float | None = None
    next_due: float | None = None
    occurrences: int = 0
    reason: str = ""

    @property
    def entry(self) -> TimelineBase:
        """The entry."""
        return self.action.entry  # type: ignore[no-any-return]

    @property
    def done(self) -> bool:
        """Whether nothing of the entry can still start."""
        return self.status in FINAL


class Schedule:
    """Decides when the occurrences of the planned entries start."""

    def __init__(self, actions: Sequence[Planned]) -> None:
        self.entries: dict[str, Scheduled] = {}
        self._ended_ok: set[str] = set()
        """The entries that ended `ok` (an `after` list waits for all of its names)."""
        for action in actions:
            state = Scheduled(action)
            entry = action.entry
            if entry.after is not None or entry.when is not None:
                state.status = WAITING
            else:
                self._trigger(state, 0.0)
            self.entries[action.name] = state

    # ------------------------------------------------------------------------------ inputs
    def _trigger(self, state: Scheduled, t: float) -> None:
        state.triggered_s = t
        state.next_due = t + state.entry.at
        state.status = PENDING

    def feed(self, sample: Sample, t: float) -> list[str]:
        """A sample of the stream at `t`; the names of the `when` entries it triggered."""
        triggered = []
        for name, state in self.entries.items():
            watch = state.action.watch
            if state.status != WAITING or watch is None:
                continue
            if state.entry.within_s is not None and t > state.entry.within_s:
                continue
            if watch.holds(sample):
                self._trigger(state, t)
                state.reason = f"condition held at {t:.1f} s"
                triggered.append(name)
        return triggered

    def expire(self, t: float) -> list[tuple[str, str, str]]:
        """Skip the `when` entries whose `within_s` has passed: `(name, status, reason)` each."""
        expired = []
        for name, state in self.entries.items():
            entry = state.entry
            within = entry.within_s
            waiting = state.status == WAITING and entry.when is not None
            if waiting and within is not None and t > within:
                state.status = SKIPPED
                state.reason = f"condition never held within {within:g} s"
                expired.append((name, state.status, state.reason))
        return expired

    def due(self, t: float) -> list[Occurrence]:
        """The occurrences to start now, earliest due first; each entry is marked running."""
        started = []
        pending = [
            s for s in self.entries.values() if s.status == PENDING and s.next_due is not None
        ]
        for state in sorted(pending, key=lambda s: s.next_due or 0.0):
            if state.next_due is None or state.next_due > t:
                continue
            entry = state.entry
            if entry.until_s is not None and state.next_due > entry.until_s:
                state.status = OK
                state.reason = f"until_s {entry.until_s:g} s passed"
                continue
            state.occurrences += 1
            state.status = RUNNING
            started.append(Occurrence(state.action, state.occurrences, state.next_due))
        return started

    def finished(self, name: str, status: str, t: float) -> list[str]:
        """An occurrence of `name` ended with `status` at `t`; the entries it triggered."""
        state = self.entries[name]
        entry = state.entry
        if entry.every is not None and status == OK and state.next_due is not None:
            if entry.count is not None and state.occurrences >= entry.count:
                state.status = OK
                state.reason = f"{entry.count} occurrence(s) ran"
            else:
                state.next_due += entry.every
                state.status = PENDING
        else:
            state.status = OK if status == OK else status
            state.reason = status
        if status != OK:
            return []
        self._ended_ok.add(name)
        triggered = []
        for other_name, other in self.entries.items():
            names = other.entry.after_names
            if other.status == WAITING and name in names and self._ended_ok >= set(names):
                self._trigger(other, t)
                other.reason = f"after {', '.join(names)} at {t:.1f} s"
                triggered.append(other_name)
        return triggered

    # ----------------------------------------------------------------------------- queries
    def _live(self, state: Scheduled, t: float, chain: tuple[str, ...] = ()) -> bool:
        """Whether the entry keeps the timeline going: it runs, is due, or may still be."""
        entry = state.entry
        if state.status == RUNNING:
            return True
        if state.status == PENDING:
            bounded = entry.count is not None or entry.until_s is not None
            return entry.every is None or state.occurrences == 0 or bounded
        if state.status != WAITING:
            return False
        if entry.when is not None:
            return entry.within_s is not None and t <= entry.within_s
        return any(
            self._live(self.entries[name], t, (*chain, name))
            for name in entry.after_names
            if name in self.entries and name not in chain
        )

    def running(self) -> list[str]:
        """The entries with an occurrence running."""
        return [name for name, state in self.entries.items() if state.status == RUNNING]

    def idle(self, t: float) -> bool:
        """Whether nothing runs and nothing can still start."""
        return not any(self._live(state, t) for state in self.entries.values())

    def wait(self, t: float) -> float:
        """Seconds until something may become due (inf when nothing is scheduled)."""
        waits = []
        for state in self.entries.values():
            entry = state.entry
            if state.status == PENDING and state.next_due is not None:
                waits.append(state.next_due - t)
            elif state.status == WAITING and entry.within_s is not None and t <= entry.within_s:
                waits.append(entry.within_s - t)
        return max(min(waits, default=math.inf), 0.0)

    def settle(self, reason: str) -> list[tuple[str, str, str]]:
        """End what never ran or repeats no more: `(name, status, reason)` per entry settled."""
        settled = []
        for name, state in self.entries.items():
            if state.done or state.status == RUNNING:
                continue
            if state.occurrences:
                state.status, state.reason = OK, f"{state.occurrences} occurrence(s) ran; {reason}"
            else:
                why = "condition never held" if state.entry.when is not None else reason
                missing = [n for n in state.entry.after_names if n not in self._ended_ok]
                if state.entry.after is not None:
                    why = f"{', '.join(missing) or state.entry.after_names[0]} did not end ok"
                state.status, state.reason = SKIPPED, why
            settled.append((name, state.status, state.reason))
        return settled

    def to_dict(self) -> dict[str, dict[str, Any]]:
        """Per entry: status, trigger time, occurrences, reason."""
        return {
            name: {
                "status": state.status,
                "triggered_s": state.triggered_s,
                "occurrences": state.occurrences,
                "reason": state.reason,
            }
            for name, state in self.entries.items()
        }
