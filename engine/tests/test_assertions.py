"""Tests of the streaming assertion evaluator (task P1-T06) on synthetic and recorded streams."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fieldproof.assertions import (
    FAILED,
    PASSED,
    PENDING,
    SIDE_CHANNELS,
    SKIPPED,
    Evaluator,
    PlannedAssertion,
    Sample,
    observe_plan,
)
from fieldproof.scenario import (
    Pose,
    validate_data,
)

FIXTURES = Path(__file__).parent / "fixtures"
T0 = 1_700_000_000.0


def planned(data: dict[str, Any], **resolved: Any) -> PlannedAssertion:
    """A planned assertion from its YAML data (validated through a minimal scenario)."""
    scenario = validate_data(
        {
            "apiVersion": "fieldproof.dev/v0alpha1",
            "kind": "Scenario",
            "metadata": {"name": "t"},
            "world": {"file": "depot.sdf"},
            "vehicles": [{"name": "ugv1", "manifest": "r.yaml", "spawn": {"x": 0, "y": 0}}],
            "timeline": [
                {
                    "name": "drive",
                    "vehicle": "ugv1",
                    "action": "navigate_to_pose",
                    "goal": {"x": 1, "y": 0},
                }
            ],
            "assertions": [data],
        }
    )
    return PlannedAssertion(scenario.assertions[0], **resolved)


def bare(evidence: dict[str, Any]) -> dict[str, Any]:
    """The evidence without the excerpt of the stream (task P2-T05 adds it to every verdict)."""
    return {k: v for k, v in evidence.items() if k != "excerpt"}


def sample(kind: str, t: float, sim: float | None = None, **data: Any) -> Sample:
    return Sample(kind, T0 + t, sim, {"kind": kind, "wall": T0 + t, "sim": sim, **data})


def pose(t: float, x: float, y: float, yaw: float = 0.0, model: str = "ugv1") -> Sample:
    return sample("pose", t, t * 0.9, model=model, x=x, y=y, z=0.0, yaw=yaw)


def reach(**extra: Any) -> PlannedAssertion:
    return planned(
        {
            "name": "reached",
            "type": "reach_pose",
            "vehicle": "ugv1",
            "target": {"x": 10.0, "y": -1.0},
            "tolerance_m": 0.5,
            **extra,
        },
        target=Pose(10.0, -1.0, 0.0, -1.5708),
        model="ugv1",
    )


# ------------------------------------------------------------------------------------ samples
def test_sample_parsing() -> None:
    line = json.dumps({"kind": "pose", "wall": 1.5, "sim": 0.7, "model": "ugv1", "x": 1})
    parsed = Sample.from_line(line)
    assert parsed.kind == "pose"
    assert parsed.wall == 1.5
    assert parsed.sim == 0.7
    assert parsed.data["x"] == 1
    assert Sample.from_line('{"kind": "ready", "wall": 2}').sim is None
    for bad in ("not json", "[]", '{"kind": "x"}', '{"wall": 1}'):
        with pytest.raises(ValueError, match="not a sample"):
            Sample.from_line(bad)


def test_feed_lines_counts_bad_lines() -> None:
    evaluator = Evaluator([reach()])
    evaluator.start(T0)
    evaluator.feed_lines(["", "garbage", '{"kind": "pose", "wall": 1}', "\n"])
    assert evaluator.bad_lines == 1
    assert evaluator.samples == 1


# --------------------------------------------------------------------------------- reach_pose
def test_reach_pose_passes_at_the_first_sample_within_tolerance() -> None:
    evaluator = Evaluator([reach()])
    assert evaluator.feed(pose(-5.0, 10.0, 3.0)) == [], "before the timeline: tracked only"
    evaluator.start(T0)
    assert evaluator.feed(pose(1.0, 10.0, 2.0)) == []
    assert evaluator.feed(pose(2.0, 10.0, -0.3)) == []  # 0.7 m away
    (result,) = evaluator.feed(pose(3.0, 10.1, -0.8))  # 0.22 m away
    assert result.status == PASSED
    assert result.t_s == 3.0
    assert result.sim_s == pytest.approx(2.7)
    assert result.reason.startswith("0.22 m from (10, -1) at 3.0 s (tolerance 0.5 m)")
    assert result.evidence["reached"]["distance_m"] == pytest.approx(0.224, abs=1e-3)
    assert result.evidence["samples"] == 3
    assert evaluator.feed(pose(4.0, 10.0, -1.0)) == [], "a verdict sticks"
    assert evaluator.finish(T0 + 5.0) == []
    assert result.evidence["final"]["y"] == -1.0, "the end of the timeline adds the final pose"
    assert evaluator.sim_elapsed() == pytest.approx(2.7)


def test_reach_pose_fails_at_the_deadline_with_the_closest_sample() -> None:
    evaluator = Evaluator([reach(within_s=10)])
    evaluator.start(T0)
    evaluator.feed(pose(1.0, 10.0, 3.0))
    evaluator.feed(pose(5.0, 10.0, 1.0))  # closest: 2 m
    evaluator.feed(pose(8.0, 10.0, 1.5))
    assert evaluator.tick(T0 + 9.9) == []
    (result,) = evaluator.tick(T0 + 10.0)
    assert result.status == FAILED
    assert (
        result.reason == "not reached within 10 s: closest 2.00 m from (10, -1) (tolerance 0.5 m)"
    )
    assert result.evidence["closest"]["t_s"] == 5.0
    assert result.evidence["final"]["y"] == 1.5
    assert result.evidence["samples"] == 3


def test_reach_pose_fails_at_the_end_or_without_poses() -> None:
    evaluator = Evaluator([reach()])
    evaluator.start(T0)
    evaluator.feed(pose(1.0, 9.0, -1.0))
    evaluator.feed(pose(1.5, 9.0, -1.0, model="other"))
    (result,) = evaluator.finish(T0 + 20.0)
    assert result.status == FAILED
    assert "not reached by the end of the timeline: closest 1.00 m" in result.reason
    evaluator = Evaluator([reach()])
    evaluator.start(T0)
    (result,) = evaluator.finish(T0 + 1.0)
    assert result.status == FAILED
    assert "no ground-truth pose of ugv1 was observed" in result.reason


def test_reach_pose_checks_the_heading_when_asked() -> None:
    evaluator = Evaluator([reach(yaw_tolerance_rad=0.2)])
    evaluator.start(T0)
    assert evaluator.feed(pose(1.0, 10.0, -1.0, yaw=0.0)) == [], "position ok, heading off"
    (result,) = evaluator.feed(pose(2.0, 10.0, -1.0, yaw=-1.6))
    assert result.status == PASSED
    assert "heading off by 0.03 rad" in result.reason
    evaluator = Evaluator([reach(yaw_tolerance_rad=0.2, within_s=5)])
    evaluator.start(T0)
    evaluator.feed(pose(1.0, 10.0, -1.0, yaw=1.5))
    (result,) = evaluator.tick(T0 + 5.0)
    assert result.status == FAILED
    assert "heading off by 3.07 rad there" in result.reason


# -------------------------------------------------------------------------------- within_time
def within(max_s: float = 30.0) -> PlannedAssertion:
    return planned({"name": "fast", "type": "within_time", "action": "drive", "max_s": max_s})


def test_within_time_passes_when_the_action_completes_in_time() -> None:
    evaluator = Evaluator([within(30)])
    evaluator.start(T0)
    assert evaluator.action("other", "started", T0 + 1.0) == []
    assert evaluator.action("drive", "started", T0 + 2.0) == []
    assert evaluator.tick(T0 + 20.0) == []
    (result,) = evaluator.action("drive", "finished", T0 + 25.0, "ok")
    assert result.status == PASSED
    assert result.reason == "drive completed in 23.0 s (limit 30 s)"
    assert bare(result.evidence) == {"started_t_s": 2.0, "duration_s": 23.0, "action_status": "ok"}


def test_within_time_fails_as_soon_as_the_limit_passes() -> None:
    evaluator = Evaluator([within(1)])
    evaluator.start(T0)
    evaluator.action("drive", "started", T0)
    (result,) = evaluator.tick(T0 + 1.5)
    assert result.status == FAILED
    assert result.reason == "drive has not completed 1 s after its start"
    assert result.evidence["elapsed_s"] == 1.5
    assert evaluator.action("drive", "finished", T0 + 20.0, "ok") == []


def test_within_time_fails_on_a_failed_or_late_action_or_no_action() -> None:
    evaluator = Evaluator([within(30)])
    evaluator.start(T0)
    evaluator.action("drive", "started", T0)
    (result,) = evaluator.action("drive", "finished", T0 + 5.0, "failed")
    assert result.status == FAILED
    assert result.reason == "drive ended failed after 5.0 s"
    evaluator = Evaluator([within(30)])
    evaluator.start(T0)
    evaluator.action("drive", "started", T0)
    (result,) = evaluator.action("drive", "finished", T0 + 31.0, "ok")
    assert result.status == FAILED
    assert "over the limit of 30 s" in result.reason
    evaluator = Evaluator([within(30)])
    evaluator.start(T0)
    (result,) = evaluator.finish(T0 + 2.0)
    assert result.status == FAILED
    assert result.reason == "drive never started"
    evaluator = Evaluator([within(30)])
    evaluator.start(T0)
    evaluator.action("drive", "started", T0)
    (result,) = evaluator.finish(T0 + 2.0)
    assert result.reason == "drive did not complete"
    assert evaluator.action("drive", "finished", T0 + 3.0) == [], "finished without a status"


# --------------------------------------------------------------------------------- topic_rate
def rate_assertion(**extra: Any) -> PlannedAssertion:
    return planned(
        {
            "name": "lidar",
            "type": "topic_rate",
            "topic": "scan",
            "vehicle": "ugv1",
            "min_hz": 8.0,
            "window_s": 5,
            "after_s": 5,
            **extra,
        },
        topic="/scan",
    )


def rate(t: float, hz: float, topic: str = "/scan", total: int = 100) -> Sample:
    return sample("rate", t, t, topic=topic, hz=hz, window_s=5.0, total=total)


def test_topic_rate_passes_with_evidence_of_the_windows() -> None:
    evaluator = Evaluator([rate_assertion()])
    evaluator.start(T0)
    assert evaluator.feed(rate(1.0, 2.0)) == [], "in the grace period"
    assert evaluator.feed(rate(6.0, 9.8)) == []
    assert evaluator.feed(rate(7.0, 10.2, total=170)) == []
    assert evaluator.feed(rate(7.5, 1.0, topic="/odom")) == [], "another topic"
    (result,) = evaluator.finish(T0 + 8.0)
    assert result.status == PASSED
    assert result.reason == "/scan stayed between 9.8 and 10.2 Hz over 2 window(s) (minimum 8 Hz)"
    assert bare(result.evidence) == {
        "windows": 2,
        "lowest_hz": 9.8,
        "highest_hz": 10.2,
        "messages": 170,
    }


def test_topic_rate_fails_at_the_first_slow_or_fast_window() -> None:
    evaluator = Evaluator([rate_assertion()])
    evaluator.start(T0)
    evaluator.feed(rate(6.0, 9.0))
    (result,) = evaluator.feed(rate(7.0, 4.2))
    assert result.status == FAILED
    assert result.reason == "/scan at 4.2 Hz at 7.0 s, below 8 Hz (window 5 s)"
    assert result.evidence["window"] == {"t_s": 7.0, "sim_s": 7.0, "hz": 4.2}
    assert result.evidence["lowest_hz"] == 4.2
    evaluator = Evaluator([rate_assertion(max_hz=12)])
    evaluator.start(T0)
    (result,) = evaluator.feed(rate(6.0, 15.0))
    assert result.status == FAILED
    assert "above 12 Hz" in result.reason


def test_topic_rate_fails_without_measurements() -> None:
    evaluator = Evaluator([rate_assertion()])
    evaluator.start(T0)
    evaluator.feed(rate(1.0, 9.0, total=9))
    (result,) = evaluator.finish(T0 + 3.0)
    assert result.status == FAILED
    assert result.reason == "no rate of /scan was measured after 5 s (9 message(s) seen)"


# ------------------------------------------------------------------------------- no_collision
def collision(**extra: Any) -> PlannedAssertion:
    return planned(
        {"name": "safe", "type": "no_collision", "vehicle": "ugv1", **extra},
        topic="/contacts",
        model="ugv1",
    )


def contact(t: float, other: str, topic: str = "/contacts", count: int = 1) -> Sample:
    return sample(
        "contact",
        t,
        t,
        topic=topic,
        model="ugv1",
        collision1="ugv1::base_link::base_link_collision",
        collision2=other,
        count=count,
    )


def test_no_collision_fails_at_the_first_contact() -> None:
    evaluator = Evaluator([collision()])
    evaluator.start(T0)
    assert evaluator.feed(contact(-1.0, "depot_building::wall::collision")) == [], "before start"
    (result,) = evaluator.feed(contact(3.0, "depot_building::south_wall::collision", count=3))
    assert result.status == FAILED
    assert result.reason == "ugv1 touched depot_building::south_wall::collision at 3.0 s"
    assert result.evidence["contact"]["count"] == 3
    assert result.evidence["contact"]["collision1"].startswith("ugv1::")


def test_no_collision_ignores_named_entities_and_passes() -> None:
    evaluator = Evaluator([collision(ignore=["cardboard_box", "ground::link::collision"])])
    evaluator.start(T0)
    assert evaluator.feed(contact(1.0, "cardboard_box::link::collision")) == []
    assert evaluator.feed(contact(2.0, "ground::link::collision")) == []
    assert evaluator.feed(contact(2.5, "x::y", topic="/ugv2/contacts")) == []
    (result,) = evaluator.finish(T0 + 10.0)
    assert result.status == PASSED
    assert result.reason == "no contact of ugv1 in 10.0 s (2 ignored contact message(s))"
    assert bare(result.evidence) == {"ignored_contacts": 2, "messages": 2}


def test_no_collision_reads_the_other_side_whichever_it_is() -> None:
    evaluator = Evaluator([collision()])
    evaluator.start(T0)
    swapped = sample(
        "contact",
        1.0,
        None,
        topic="/contacts",
        model="ugv1",
        collision1="55gal_drum::link::collision",
        collision2="ugv1::base_link::base_link_collision",
        count=1,
    )
    (result,) = evaluator.feed(swapped)
    assert result.reason == "ugv1 touched 55gal_drum::link::collision at 1.0 s"


# -------------------------------------------------------------------------------- topic_value
def value_assertion(condition: str = "value <= 1.5", **extra: Any) -> PlannedAssertion:
    return planned(
        {
            "name": "speed",
            "type": "topic_value",
            "topic": "cmd_vel",
            "vehicle": "ugv1",
            "field": "linear.x",
            "condition": condition,
            **extra,
        },
        topic="/cmd_vel",
    )


def value(t: float, v: Any, field: str = "linear.x", topic: str = "/cmd_vel") -> Sample:
    return sample("value", t, t, topic=topic, field=field, value=v)


def test_topic_value_always_within_a_window() -> None:
    # "Stopped from 2 s to 5 s": messages before and after the window do not count.
    evaluator = Evaluator([value_assertion("abs(value) < 0.05", after_s=2, within_s=5)])
    evaluator.start(T0)
    assert evaluator.feed(value(1.0, 0.4)) == [], "before the window"
    assert evaluator.feed(value(2.5, 0.0)) == []
    assert evaluator.feed(value(4.5, 0.01)) == []
    assert evaluator.tick(T0 + 4.9) == []
    (result,) = evaluator.tick(T0 + 5.0)
    assert result.status == PASSED
    assert result.reason == (
        "/cmd_vel linear.x satisfied 'abs(value) < 0.05' in all 2 message(s) from 2 s to 5 s"
    )
    assert result.t_s == 5.0
    assert evaluator.feed(value(6.0, 0.4)) == [], "after the window: settled already"
    # A message past the window that arrives before the tick settles it the same way.
    late = Evaluator([value_assertion("abs(value) < 0.05", after_s=2, within_s=5)])
    late.start(T0)
    late.feed(value(3.0, 0.0))
    (result,) = late.feed(value(6.0, 0.4))
    assert result.status == PASSED
    assert result.evidence["samples"] == 1
    # A violation inside the window fails at once; no message in it fails at its end.
    violated = Evaluator([value_assertion("abs(value) < 0.05", after_s=2, within_s=5)])
    violated.start(T0)
    (result,) = violated.feed(value(3.0, 0.3))
    assert result.status == FAILED
    silent = Evaluator([value_assertion("abs(value) < 0.05", after_s=2, within_s=5)])
    silent.start(T0)
    (result,) = silent.tick(T0 + 5.0)
    assert result.status == FAILED
    assert result.reason == "no message of /cmd_vel from 2 s to 5 s"


def test_topic_value_always() -> None:
    evaluator = Evaluator([value_assertion(after_s=1)])
    evaluator.start(T0)
    assert evaluator.feed(value(0.5, 9.0)) == [], "grace period"
    assert evaluator.feed(value(1.0, 0.3)) == []
    assert evaluator.feed(value(2.0, 1.1)) == []
    assert evaluator.feed(value(2.5, 7.0, field="angular.z")) == [], "another field"
    (result,) = evaluator.finish(T0 + 3.0)
    assert result.status == PASSED
    assert result.reason == "/cmd_vel linear.x satisfied 'value <= 1.5' in all 2 message(s)"
    assert bare(result.evidence) == {"samples": 2, "lowest": 0.3, "highest": 1.1, "last": 1.1}
    evaluator = Evaluator([value_assertion()])
    evaluator.start(T0)
    evaluator.feed(value(1.0, 0.3))
    (result,) = evaluator.feed(value(2.0, 1.8))
    assert result.status == FAILED
    assert result.reason == "/cmd_vel linear.x = 1.8 at 2.0 s violates 'value <= 1.5'"
    assert result.evidence["violation"] == {"t_s": 2.0, "sim_s": 2.0, "value": 1.8}


def test_topic_value_eventually() -> None:
    evaluator = Evaluator([value_assertion("value == 'ACTIVE'", mode="eventually", within_s=10)])
    evaluator.start(T0)
    assert evaluator.feed(value(1.0, "INACTIVE")) == []
    (result,) = evaluator.feed(value(2.0, "ACTIVE"))
    assert result.status == PASSED
    assert result.reason == "/cmd_vel linear.x = 'ACTIVE' at 2.0 s satisfies \"value == 'ACTIVE'\""
    assert result.evidence["satisfied"]["value"] == "ACTIVE"
    assert result.evidence["lowest"] is None, "strings have no extremes"
    evaluator = Evaluator([value_assertion("value > 5", mode="eventually", within_s=10)])
    evaluator.start(T0)
    evaluator.feed(value(1.0, 1))
    (result,) = evaluator.tick(T0 + 10.0)
    assert result.status == FAILED
    assert (
        result.reason == "/cmd_vel linear.x never satisfied 'value > 5' within 10 s (1 message(s))"
    )
    evaluator = Evaluator([value_assertion("value > 5", mode="eventually")])
    evaluator.start(T0)
    evaluator.feed(value(1.0, 1))
    (result,) = evaluator.finish(T0 + 3.0)
    assert result.reason == "/cmd_vel linear.x never satisfied 'value > 5' (1 message(s))"


def test_topic_value_without_messages_or_with_errors() -> None:
    evaluator = Evaluator([value_assertion()])
    evaluator.start(T0)
    (result,) = evaluator.finish(T0 + 3.0)
    assert result.status == FAILED
    assert result.reason == "no message of /cmd_vel after 0 s"
    evaluator = Evaluator([value_assertion()])
    evaluator.start(T0)
    bad = sample("value", 1.0, None, topic="/cmd_vel", field="linear.x", error="no field 'x'")
    (result,) = evaluator.feed(bad)
    assert result.status == FAILED
    assert result.reason == "/cmd_vel: field 'linear.x': no field 'x'"
    evaluator = Evaluator([value_assertion("value + 1 > 0")])
    evaluator.start(T0)
    (result,) = evaluator.feed(value(1.0, None))
    assert result.status == FAILED
    assert "cannot be evaluated for value None" in result.reason


# ---------------------------------------------------------------------------------- together
def test_skip_pending_and_bookkeeping() -> None:
    evaluator = Evaluator([reach(), within(), collision()])
    assert [r.status for r in evaluator.results] == [PENDING] * 3
    assert evaluator.tick(T0) == [], "nothing before the timeline starts"
    assert evaluator.elapsed(T0) < 0
    evaluator.start(T0)
    evaluator.feed(pose(1.0, 10.0, -1.0))
    skipped = evaluator.skip_pending("the run ended failed: boom")
    assert [r.name for r in skipped] == ["fast", "safe"]
    assert all(r.status == SKIPPED and r.reason == "the run ended failed: boom" for r in skipped)
    assert [r.status for r in evaluator.results] == [PASSED, SKIPPED, SKIPPED]
    assert evaluator.failed == []
    assert evaluator.pending == []
    assert evaluator.skip_pending("again") == []
    data = evaluator.results[0].to_dict()
    assert set(data) == {
        "name",
        "type",
        "status",
        "reason",
        "t_s",
        "sim_s",
        "evidence",
        "metrics",
    }


def test_observe_plan() -> None:
    items = [
        reach(),
        within(),
        rate_assertion(),
        rate_assertion(),
        collision(),
        value_assertion(),
        value_assertion("value > 0"),
    ]
    plan = observe_plan(
        items,
        world="depot",
        models=["ugv1"],
        record=True,
        topics=["/odom", "/scan", "/fieldproof/events"],
        run_dir="/fieldproof/run",
    )
    assert plan["world"] == "depot"
    assert plan["models"] == ["ugv1"]
    assert plan["rates"] == [{"topic": "/scan", "window_s": 5.0, "type": None}]
    assert plan["values"] == [{"topic": "/cmd_vel", "field": "linear.x", "type": None}]
    assert plan["contacts"] == [{"topic": "/contacts", "model": "ugv1"}]
    assert plan["record"] == {
        "enabled": True,
        "topics": ["/odom", "/scan", *SIDE_CHANNELS.values()],
        "output": "/fieldproof/run/recording",
    }
    assert plan["samples_file"] == "/fieldproof/run/samples.jsonl"
    assert plan["events_file"] == "/fieldproof/run/engine.log"
    assert plan["side_channels"] == SIDE_CHANNELS
    everything = observe_plan([], world="w", models=[], record=True, topics=[], run_dir="/r")
    assert everything["record"]["topics"] == [], "an empty allow-list records every topic"


def test_planned_assertion_to_dict() -> None:
    data = reach().to_dict()
    assert data["name"] == "reached"
    assert data["tolerance_m"] == 0.5
    assert data["resolved"] == {
        "target_world": {"x": 10.0, "y": -1.0, "z": 0.0, "yaw": -1.5708},
        "model": "ugv1",
    }
    assert rate_assertion().to_dict()["resolved"] == {"topic": "/scan"}
    assert within().to_dict()["resolved"] == {}


# ------------------------------------------------------------------------- recorded fixture
@pytest.mark.skipif(
    not (FIXTURES / "samples_ugv_nav_to_goal.jsonl").is_file(), reason="fixture not recorded"
)
def test_recorded_run_of_the_example() -> None:
    """The stream of a real `fieldproof run` of the example: its five assertions pass."""
    meta = json.loads((FIXTURES / "samples_ugv_nav_to_goal.json").read_text())
    lines = (FIXTURES / "samples_ugv_nav_to_goal.jsonl").read_text().splitlines()
    example = validate_data(meta["scenario"])
    items = []
    for assertion in example.assertions:
        resolved = meta["resolved"][assertion.name]
        target = resolved.get("target")
        items.append(
            PlannedAssertion(
                assertion,
                topic=resolved.get("topic"),
                target=Pose(**target) if target else None,
                model=resolved.get("model"),
            )
        )
    # Replay: the samples before the timeline, then samples and action events in wall-clock
    # order, as the runner saw them.
    parsed = [Sample.from_line(line) for line in lines]
    events: list[tuple[str, Any]] = [("sample", s) for s in parsed if s.wall >= meta["start_wall"]]
    events += [("action", a) for a in meta["actions"]]
    events.sort(key=lambda e: e[1].wall if e[0] == "sample" else e[1][2])

    def replay(evaluator: Evaluator) -> None:
        for s in parsed:
            if s.wall < meta["start_wall"]:
                evaluator.feed(s)
        evaluator.start(meta["start_wall"])
        for kind, item in events:
            if kind == "sample":
                evaluator.feed(item)
                evaluator.tick(item.wall)
            else:
                evaluator.action(item[0], item[1], item[2], item[3])
        evaluator.finish(meta["finish_wall"])

    evaluator = Evaluator(items)
    replay(evaluator)
    verdicts = {r.name: r for r in evaluator.results}
    assert {n: r.status for n, r in verdicts.items()} == dict.fromkeys(verdicts, PASSED), {
        n: r.reason for n, r in verdicts.items()
    }
    assert verdicts["reached_goal"].evidence["reached"]["distance_m"] <= 0.5
    assert verdicts["lidar_streams"].evidence["lowest_hz"] >= 8.0
    assert verdicts["no_collision"].evidence["messages"] == 0
    assert verdicts["speed_limit"].evidence["highest"] <= 1.5
    # The same stream with a one-second limit on the drive: exactly that assertion fails.
    short = validate_data(
        {
            **meta["scenario"],
            "assertions": [
                {**a, "max_s": "1s"} if a["type"] == "within_time" else a
                for a in meta["scenario"]["assertions"]
            ],
        }
    )
    by_name = {a.name: a for a in short.assertions}
    evaluator = Evaluator(
        [
            PlannedAssertion(by_name[i.name], topic=i.topic, target=i.target, model=i.model)
            for i in items
        ]
    )
    replay(evaluator)
    assert [r.name for r in evaluator.failed] == ["arrived_in_time"]
    assert "has not completed 1 s after its start" in evaluator.failed[0].reason
