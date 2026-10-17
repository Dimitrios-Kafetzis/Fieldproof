"""Tests of the readiness probe (task P1-T04): condition evaluation, waiting, the command line."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from fieldproof import vehicle_ready
from fieldproof.vehicle_ready import (
    UNAVAILABLE,
    UNRESPONSIVE,
    Status,
    describe,
    evaluate,
    kind_of,
    load_conditions,
    main,
    pending,
    summary,
    wait,
)

REPO = Path(__file__).resolve().parents[2]
NAV2_MANIFEST = REPO / "examples/vehicles/ugv_nav2/robot.yaml"
NAV2_TYPE = "nav2_msgs/action/NavigateToPose"


class FakeGraph:
    """A graph whose contents the tests set; counts the queries."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.topics: dict[str, list[str]] = {}
        self.service_types: dict[str, list[str]] = {}
        self.node_names: list[str] = []
        self.namespace = "/"
        self.queries = 0
        self.closed = False

    def lifecycle_states(self, nodes: Sequence[str]) -> dict[str, str]:
        self.queries += 1
        return {n: self.states.get(n, UNAVAILABLE) for n in nodes}

    def publishers(self, topic: str) -> list[str]:
        return list(self.topics.get(topic, []))

    def services(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self.service_types.items()}

    def nodes(self) -> list[str]:
        return list(self.node_names)

    def resolve(self, name: str) -> str:
        return name if name.startswith("/") else f"{self.namespace.rstrip('/')}/{name}"

    def close(self) -> None:
        self.closed = True

    def offer_action(self, name: str, action_type: str) -> None:
        self.service_types[f"{name}/_action/send_goal"] = [f"{action_type}_SendGoal"]
        self.service_types[f"{name}/_action/get_result"] = [f"{action_type}_GetResult"]


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


def test_load_conditions_of_the_example() -> None:
    conditions, timeout = load_conditions(NAV2_MANIFEST)
    assert timeout == 180.0
    kinds = [kind_of(c) for c in conditions]
    assert kinds.count("lifecycle") == 7
    assert {"topic": "/map", "type": "nav_msgs/msg/OccupancyGrid"} in conditions
    assert {"action": "/navigate_to_pose", "type": NAV2_TYPE} in conditions


def test_load_conditions_without_ready(tmp_path: Path) -> None:
    manifest = tmp_path / "robot.yaml"
    manifest.write_text("runtime:\n  image: x\n")
    assert load_conditions(manifest) == ([], vehicle_ready.DEFAULT_TIMEOUT_S)
    manifest.write_text("runtime:\n  ready:\n    conditions: [{typo: 1}]\n")
    with pytest.raises(ValueError, match="unknown ready condition"):
        load_conditions(manifest)


def test_describe() -> None:
    assert describe({"lifecycle": "/bt_navigator"}) == "/bt_navigator lifecycle"
    assert describe({"topic": "/map", "type": "x"}) == "/map topic"
    assert describe({"action": "/navigate_to_pose"}) == "/navigate_to_pose action"
    assert describe({"service": "/s"}) == "/s service"
    assert describe({"node": "/n"}) == "/n node"


def test_lifecycle_conditions(graph: FakeGraph) -> None:
    graph.states = {"/slam_toolbox": "active", "/bt_navigator": "inactive", "/c": UNRESPONSIVE}
    conditions = [
        {"lifecycle": "/slam_toolbox"},
        {"lifecycle": "/bt_navigator"},
        {"lifecycle": "/bt_navigator", "state": "inactive"},
        {"lifecycle": "/c"},
        {"lifecycle": "/missing"},
    ]
    statuses = evaluate(conditions, graph)
    assert [s.ok for s in statuses] == [True, False, True, False, False]
    assert [s.detail for s in statuses] == [
        "active",
        "inactive",
        "inactive",
        UNRESPONSIVE,
        UNAVAILABLE,
    ]
    assert graph.queries == 1, "every lifecycle node is asked in one query"
    assert summary(statuses) == (
        "/bt_navigator lifecycle inactive, /c lifecycle unresponsive, "
        "/missing lifecycle unavailable"
    )


def test_topic_conditions(graph: FakeGraph) -> None:
    graph.topics = {"/map": ["nav_msgs/msg/OccupancyGrid"], "/odd": ["a/msg/A", "b/msg/B"]}
    statuses = evaluate(
        [
            {"topic": "/map"},
            {"topic": "/map", "type": "nav_msgs/msg/OccupancyGrid"},
            {"topic": "/map", "type": "std_msgs/msg/String"},
            {"topic": "/scan"},
            {"topic": "/odd", "type": "b/msg/B"},
        ],
        graph,
    )
    assert [s.ok for s in statuses] == [True, True, False, False, True]
    assert (
        statuses[2].detail
        == "publisher of type nav_msgs/msg/OccupancyGrid, not std_msgs/msg/String"
    )
    assert statuses[3].detail == "no publisher"
    assert statuses[4].detail == "publisher of type a/msg/A, b/msg/B"


def test_service_action_and_node_conditions(graph: FakeGraph) -> None:
    graph.service_types = {"/slam_toolbox/save_map": ["slam_toolbox/srv/SaveMap"]}
    graph.offer_action("/navigate_to_pose", NAV2_TYPE)
    graph.node_names = ["/bt_navigator", "/ns/planner"]
    statuses = evaluate(
        [
            {"service": "/slam_toolbox/save_map"},
            {"service": "/slam_toolbox/save_map", "type": "slam_toolbox/srv/SaveMap"},
            {"service": "/slam_toolbox/save_map", "type": "other/srv/X"},
            {"service": "/nothing"},
            {"action": "/navigate_to_pose"},
            {"action": "/navigate_to_pose", "type": NAV2_TYPE},
            {"action": "/navigate_to_pose", "type": "other/action/X"},
            {"action": "/follow_path"},
            {"node": "/bt_navigator"},
            {"node": "/ns/planner"},
            {"node": "/planner"},
        ],
        graph,
    )
    assert [s.ok for s in statuses] == [
        True, True, False, False, True, True, False, False, True, True, False,
    ]  # fmt: skip
    assert statuses[5].detail == f"server of type {NAV2_TYPE}"
    assert statuses[7].detail == "no server"
    assert statuses[10].detail == "absent"


def test_relative_names_resolve_against_the_namespace(graph: FakeGraph) -> None:
    graph.namespace = "/ugv1"
    graph.states = {"/ugv1/bt_navigator": "active"}
    graph.topics = {"/ugv1/map": ["nav_msgs/msg/OccupancyGrid"]}
    graph.node_names = ["/ugv1/planner_server"]
    statuses = evaluate(
        [{"lifecycle": "bt_navigator"}, {"topic": "map"}, {"node": "planner_server"}], graph
    )
    assert all(s.ok for s in statuses)


def test_unknown_condition_is_an_error(graph: FakeGraph) -> None:
    with pytest.raises(ValueError, match="unknown ready condition"):
        evaluate([{"typo": "/x"}], graph)


def test_wait_reports_changes_and_returns_when_ready(graph: FakeGraph) -> None:
    conditions = [{"lifecycle": "/a"}, {"lifecycle": "/b"}, {"topic": "/map"}]
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds
        # The stack comes up as time passes.
        if now[0] >= 1:
            graph.states["/a"] = "active"
        if now[0] >= 2:
            graph.states["/b"] = "active"
        if now[0] >= 3:
            graph.topics["/map"] = ["nav_msgs/msg/OccupancyGrid"]

    lines: list[str] = []
    statuses = wait(
        conditions,
        graph,
        timeout_s=10,
        interval_s=1,
        report=lines.append,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert not pending(statuses)
    assert lines == [
        "waiting (0/3 ready): /a lifecycle unavailable, /b lifecycle unavailable, "
        "/map topic no publisher",
        "waiting (1/3 ready): /b lifecycle unavailable, /map topic no publisher",
        "waiting (2/3 ready): /map topic no publisher",
        "ready: 3 conditions hold after 3.0 s",
    ]
    assert sleeps == [1, 1, 1]


def test_wait_times_out(graph: FakeGraph) -> None:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    lines: list[str] = []
    statuses = wait(
        [{"node": "/never"}],
        graph,
        timeout_s=2.5,
        interval_s=1,
        report=lines.append,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert [s.ok for s in statuses] == [False]
    assert lines[0] == "waiting (0/1 ready): /never node absent"
    assert lines[-1] == "not ready after 2.5 s: 1 of 1 pending: /never node absent"
    assert len(lines) == 2, "an unchanged pending set is not repeated"


def test_status_as_dict() -> None:
    status = Status({"node": "/n"}, True, "present")
    assert status.label == "/n node"
    assert status.as_dict() == {"condition": {"node": "/n"}, "ok": True, "detail": "present"}


def test_main_ready_json(tmp_path: Path, graph: FakeGraph) -> None:
    manifest = tmp_path / "robot.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "ready": {
                        "timeout_s": 5,
                        "conditions": [
                            {"lifecycle": "/a"},
                            {"action": "/go", "type": "p/action/Go"},
                        ],
                    }
                }
            }
        )
    )
    graph.states = {"/a": "active"}
    graph.offer_action("/go", "p/action/Go")
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--manifest", str(manifest), "--json"], graph_factory=lambda: graph, out=out, err=err
    )
    assert code == 0
    assert graph.closed
    assert "ready: 2 conditions hold" in err.getvalue(), "progress goes to stderr with --json"
    result = json.loads(out.getvalue())
    assert [r["ok"] for r in result] == [True, True]
    assert result[1]["detail"] == "server of type p/action/Go"


def test_main_timeout_lists_the_pending_conditions(tmp_path: Path, graph: FakeGraph) -> None:
    manifest = tmp_path / "robot.yaml"
    manifest.write_text(
        "runtime:\n  ready:\n    conditions:\n      - lifecycle: /a\n      - topic: /m\n"
    )
    graph.states = {"/a": "active"}
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--manifest", str(manifest), "--timeout", "0", "--interval", "0"],
        graph_factory=lambda: graph,
        out=out,
        err=err,
    )
    assert code == 1
    assert out.getvalue().splitlines() == [
        "waiting (1/2 ready): /m topic no publisher",
        "not ready after 0.0 s: 1 of 2 pending: /m topic no publisher",
    ]
    assert err.getvalue() == ""
    quiet = io.StringIO()
    assert (
        main(
            ["--manifest", str(manifest), "--timeout", "0", "--quiet"],
            graph_factory=lambda: graph,
            out=quiet,
        )
        == 1
    )
    assert quiet.getvalue() == ""


def test_main_without_ready_conditions(tmp_path: Path) -> None:
    manifest = tmp_path / "robot.yaml"
    manifest.write_text("runtime:\n  image: x\n")
    out = io.StringIO()
    assert main(["--manifest", str(manifest), "--json"], out=out, err=io.StringIO()) == 0
    assert out.getvalue() == "[]\n"


def test_main_bad_manifest(tmp_path: Path) -> None:
    err = io.StringIO()
    assert main(["--manifest", str(tmp_path / "none.yaml")], err=err) == 2
    assert "none.yaml" in err.getvalue()


def test_the_probe_is_standalone() -> None:
    """The file is copied into the image as it is, where only ros-base's Python is available."""
    source = Path(vehicle_ready.__file__).read_text()
    assert source.startswith("#!/usr/bin/env python3\n")
    assert "from fieldproof" not in source
    assert "import fieldproof" not in source
    assert "import rclpy" in source
    assert "lifecycle_msgs" in source
