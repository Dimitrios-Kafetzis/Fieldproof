"""Tests of the timeline scheduler (task P2-T03), driven with numbers: `at`, `after`, `when`
and `every` entries, what is due, what is live, what is settled."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from fieldproof.assertions import Sample
from fieldproof.dsl import TimelineBase, When
from fieldproof.events import OK, PENDING, RUNNING, SKIPPED, WAITING
from fieldproof.events.container import ContainerResume
from fieldproof.expression import compile_condition
from fieldproof.schedule import PlannedWhen, Schedule
from fieldproof.world import Pose


@dataclass(frozen=True)
class Planned:
    name: str
    entry: TimelineBase
    watch: PlannedWhen | None = None


def planned(name: str, **fields: Any) -> Planned:
    entry = ContainerResume.model_validate(
        {"name": name, "vehicle": "ugv1", "action": "container.resume", **fields}
    )
    watch = None
    if entry.when is not None:
        when = entry.when
        watch = PlannedWhen(
            when,
            topic=f"/ugv1/{when.topic}" if when.topic else None,
            model="ugv1" if when.needs_vehicle() and when.topic is None else None,
            target=Pose(when.target.x or 0.0, when.target.y or 0.0, 0.0, 0.0)
            if when.target
            else None,
            condition=compile_condition(when.condition),
        )
    return Planned(name, entry, watch)


def sample(kind: str, **data: Any) -> Sample:
    return Sample(kind, 1000.0, data.pop("sim", None), {"kind": kind, **data})


def names(occurrences: list[Any]) -> list[str]:
    return [o.name for o in occurrences]


# ------------------------------------------------------------------------------------- at
def test_at_entries_are_due_at_their_time() -> None:
    schedule = Schedule([planned("a", at=0), planned("b", at=5)])
    assert schedule.entries["a"].status == PENDING
    assert names(schedule.due(0.0)) == ["a"]
    assert schedule.entries["a"].status == RUNNING
    assert schedule.due(0.0) == []
    assert schedule.wait(1.0) == 4.0
    assert schedule.idle(1.0) is False
    assert schedule.finished("a", OK, 2.0) == []
    assert schedule.entries["a"].status == OK
    assert names(schedule.due(5.0)) == ["b"]
    schedule.finished("b", OK, 6.0)
    assert schedule.idle(6.0) is True
    assert schedule.wait(6.0) == float("inf")
    assert schedule.settle("ended") == []


def test_running_entries_keep_the_timeline_alive() -> None:
    schedule = Schedule([planned("a")])
    schedule.due(0.0)
    assert schedule.running() == ["a"]
    assert schedule.idle(100.0) is False


# ---------------------------------------------------------------------------------- after
def test_after_waits_for_the_entry_and_adds_at() -> None:
    schedule = Schedule([planned("drive"), planned("pause", after="drive", at=1)])
    assert schedule.entries["pause"].status == WAITING
    assert names(schedule.due(0.0)) == ["drive"]
    assert schedule.due(10.0) == []
    assert schedule.idle(10.0) is False  # drive runs
    assert schedule.finished("drive", OK, 10.0) == ["pause"]
    assert schedule.entries["pause"].status == PENDING
    assert schedule.entries["pause"].triggered_s == 10.0
    assert schedule.due(10.5) == []
    assert schedule.wait(10.5) == 0.5
    assert names(schedule.due(11.0)) == ["pause"]


def test_after_several_entries_waits_for_all() -> None:
    schedule = Schedule([planned("a"), planned("b", at=3), planned("c", after=["a", "b"], at=1)])
    assert names(schedule.due(0.0)) == ["a"]
    assert schedule.finished("a", OK, 1.0) == []
    assert schedule.entries["c"].status == WAITING
    assert schedule.idle(1.0) is False  # b is still due
    assert names(schedule.due(3.0)) == ["b"]
    assert schedule.finished("b", OK, 4.0) == ["c"]
    assert schedule.entries["c"].reason == "after a, b at 4.0 s"
    assert names(schedule.due(5.0)) == ["c"]
    schedule = Schedule([planned("a"), planned("b", at=3), planned("c", after=["a", "b"])])
    schedule.due(0.0)
    schedule.finished("a", OK, 1.0)
    schedule.due(3.0)
    schedule.finished("b", "failed", 4.0)
    assert schedule.settle("ended") == [("c", SKIPPED, "b did not end ok")]


def test_after_a_failed_entry_never_triggers() -> None:
    schedule = Schedule([planned("drive"), planned("pause", after="drive")])
    schedule.due(0.0)
    assert schedule.finished("drive", "failed", 3.0) == []
    assert schedule.entries["pause"].status == WAITING
    assert schedule.idle(3.0) is True
    assert schedule.settle("ended") == [("pause", SKIPPED, "drive did not end ok")]


def test_after_chain_is_live_while_its_head_is() -> None:
    schedule = Schedule(
        [planned("a", at=5), planned("b", after="a"), planned("c", after="b", at=2)]
    )
    assert schedule.idle(0.0) is False
    schedule.due(5.0)
    schedule.finished("a", OK, 6.0)
    schedule.due(6.0)
    assert schedule.finished("b", OK, 7.0) == ["c"]
    assert schedule.idle(7.0) is False
    assert names(schedule.due(9.0)) == ["c"]


def test_after_a_skipped_when_is_skipped() -> None:
    schedule = Schedule(
        [
            planned("cond", when={"metric": "rtf", "condition": "value < 0.1"}, within_s=5),
            planned("then", after="cond"),
        ]
    )
    assert schedule.idle(4.0) is False
    assert schedule.idle(6.0) is True
    settled = schedule.settle("ended")
    assert settled == [
        ("cond", SKIPPED, "condition never held"),
        ("then", SKIPPED, "cond did not end ok"),
    ]


# ----------------------------------------------------------------------------------- when
def test_when_triggers_on_the_first_sample_that_holds() -> None:
    schedule = Schedule(
        [
            planned(
                "slow",
                when={"topic": "odom", "field": "twist.twist.linear.x", "condition": "value > 0.3"},
                at=1,
            )
        ]
    )
    assert schedule.idle(0.0) is True  # passive without within_s
    odom = {"topic": "/ugv1/odom", "field": "twist.twist.linear.x"}
    assert schedule.feed(sample("value", **odom, value=0.1), 2.0) == []
    assert schedule.feed(sample("value", topic="/ugv1/scan", field="x", value=9.0), 2.5) == []
    assert schedule.feed(sample("value", **odom, error="no field"), 2.6) == []
    assert schedule.feed(sample("value", **odom, value=0.5), 3.0) == ["slow"]
    assert schedule.entries["slow"].reason == "condition held at 3.0 s"
    assert schedule.feed(sample("value", **odom, value=0.9), 3.5) == []
    assert schedule.due(3.5) == []
    assert names(schedule.due(4.0)) == ["slow"]


def test_when_metrics() -> None:
    schedule = Schedule(
        [
            planned("sim", when={"metric": "sim_time", "condition": "value >= 20"}),
            planned("rtf", when={"metric": "rtf", "condition": "value < 0.5"}),
            planned(
                "near",
                when={
                    "metric": "distance_to",
                    "target": {"x": 10, "y": 0},
                    "condition": "value < 2",
                },
            ),
            planned("turned", when={"metric": "yaw", "condition": "abs(value) > 1"}),
        ]
    )
    assert schedule.feed(sample("clock", sim=19.0, rtf=0.9), 1.0) == []
    assert schedule.feed(sample("clock", sim=21.0, rtf=0.4), 2.0) == ["sim", "rtf"]
    assert schedule.feed(sample("pose", model="ugv2", x=10.0, y=1.0, z=0.0, yaw=2.0), 3.0) == []
    assert schedule.feed(sample("pose", model="ugv1", x=5.0, y=0.0, z=0.0, yaw=0.0), 3.0) == []
    assert schedule.feed(sample("pose", model="ugv1", x=9.0, y=0.0, z=0.0, yaw=2.0), 4.0) == [
        "near",
        "turned",
    ]


def test_when_within_s_bounds_the_wait() -> None:
    schedule = Schedule(
        [planned("w", when={"metric": "rtf", "condition": "value < 0.5"}, within_s=10)]
    )
    assert schedule.idle(9.0) is False
    assert schedule.wait(9.0) == 1.0
    assert schedule.feed(sample("clock", sim=1.0, rtf=0.1), 11.0) == []
    assert schedule.idle(11.0) is True
    assert schedule.settle("ended") == [("w", SKIPPED, "condition never held")]


def test_when_condition_that_cannot_be_evaluated_does_not_trigger() -> None:
    schedule = Schedule([planned("w", when={"metric": "rtf", "condition": "value > 1"})])
    assert schedule.feed(sample("clock", sim=1.0, rtf="fast"), 1.0) == []


# ---------------------------------------------------------------------------------- every
def test_every_repeats_at_the_period_without_overlap() -> None:
    schedule = Schedule([planned("tick", at=2, every=5, count=3)])
    assert schedule.due(1.0) == []
    (first,) = schedule.due(2.0)
    assert (first.name, first.index, first.due_s) == ("tick", 1, 2.0)
    assert schedule.due(7.0) == []  # the first still runs
    assert schedule.idle(7.0) is False
    assert schedule.finished("tick", OK, 7.5) == []
    assert schedule.entries["tick"].status == PENDING
    (second,) = schedule.due(7.5)  # due at 7, overran: starts at once
    assert (second.name, second.index, second.due_s) == ("tick#2", 2, 7.0)
    schedule.finished("tick", OK, 8.0)
    assert schedule.wait(8.0) == 4.0
    (third,) = schedule.due(12.0)
    assert third.name == "tick#3"
    schedule.finished("tick", OK, 12.5)
    assert schedule.entries["tick"].status == OK
    assert schedule.entries["tick"].reason == "3 occurrence(s) ran"
    assert schedule.idle(12.5) is True
    assert schedule.settle("ended") == []


def test_every_until_s() -> None:
    schedule = Schedule([planned("tick", every=4, until_s=10)])
    schedule.due(0.0)
    schedule.finished("tick", OK, 1.0)
    schedule.due(4.0)
    schedule.finished("tick", OK, 5.0)
    assert schedule.idle(5.0) is False
    schedule.due(8.0)
    schedule.finished("tick", OK, 9.0)
    assert schedule.entries["tick"].next_due == 12.0
    assert schedule.due(12.0) == []
    assert schedule.entries["tick"].status == OK
    assert schedule.entries["tick"].reason == "until_s 10 s passed"


def test_unbounded_every_runs_while_the_rest_does() -> None:
    schedule = Schedule([planned("drive", at=0), planned("tick", every=1)])
    assert names(schedule.due(0.0)) == ["drive", "tick"]
    schedule.finished("tick", OK, 0.5)
    assert schedule.idle(0.5) is False  # drive runs
    schedule.finished("drive", OK, 3.0)
    assert schedule.entries["tick"].status == PENDING
    assert schedule.idle(3.0) is True  # a passive repetition
    assert schedule.settle("the timeline ended") == [
        ("tick", OK, "1 occurrence(s) ran; the timeline ended")
    ]


def test_failed_occurrence_ends_the_repetition() -> None:
    schedule = Schedule([planned("tick", every=1, count=5)])
    schedule.due(0.0)
    schedule.finished("tick", "failed", 0.5)
    assert schedule.entries["tick"].status == "failed"
    assert schedule.entries["tick"].done is True
    assert schedule.idle(0.5) is True


def test_to_dict() -> None:
    schedule = Schedule([planned("a"), planned("b", after="a")])
    schedule.due(0.0)
    schedule.finished("a", OK, 1.0)
    assert schedule.to_dict() == {
        "a": {"status": OK, "triggered_s": 0.0, "occurrences": 1, "reason": OK},
        "b": {
            "status": PENDING,
            "triggered_s": 1.0,
            "occurrences": 0,
            "reason": "after a at 1.0 s",
        },
    }


@pytest.mark.parametrize("kind", ["value", "pose"])
def test_watch_to_dict(kind: str) -> None:
    when = When(metric="distance_to", target={"x": 1.0, "y": 2.0}, condition="value < 1")  # type: ignore[arg-type]
    watch = PlannedWhen(when, model="ugv1", target=Pose(1.0, 2.0, 0.0, 0.0))
    data = watch.to_dict()
    assert data["metric"] == "distance_to"
    assert data["model"] == "ugv1"
    assert data["target_world"] == {"x": 1.0, "y": 2.0}
    assert watch.value_of(sample(kind, model="ugv1", x=1.0, y=5.0)) == (
        3.0 if kind == "pose" else None
    )
