"""Tests of the run observer (task P1-T06): the pure parts and the loop on fake ROS and Gazebo."""

from __future__ import annotations

import json
import math
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fieldproof import observer
from fieldproof.observer import (
    FileTail,
    Observer,
    RateWindow,
    Recorder,
    Sink,
    extract_field,
    json_value,
    quaternion_yaw,
    recorder_command,
    stamp_seconds,
)


# ---------------------------------------------------------------------------------- pure parts
def test_sink_writes_json_lines(tmp_path: Path) -> None:
    clock = iter([1.0, 2.5])
    sink = Sink(tmp_path / "s" / "samples.jsonl", clock=lambda: next(clock))
    sink.emit("ready", recording=True)
    sink.emit("pose", 0.5, model="ugv1", x=1.0)
    sink.close()
    lines = [
        json.loads(line) for line in (tmp_path / "s" / "samples.jsonl").read_text().splitlines()
    ]
    assert lines == [
        {"kind": "ready", "wall": 1.0, "sim": None, "recording": True},
        {"kind": "pose", "wall": 2.5, "sim": 0.5, "model": "ugv1", "x": 1.0},
    ]
    assert sink.count == 2


def test_extract_field() -> None:
    message = SimpleNamespace(
        linear=SimpleNamespace(x=0.5),
        ranges=[1.0, 2.0],
        status=SimpleNamespace(status=4),
        data=True,
        grid=[[1, 2], [3, 4]],
        nested={"a": {"b": 7}},
    )
    assert extract_field(message, "linear.x") == 0.5
    assert extract_field(message, "ranges[1]") == 2.0
    assert extract_field(message, "status.status") == 4
    assert extract_field(message, "data") is True
    assert extract_field(message, "grid[1][0]") == 3
    assert extract_field(message, "nested.a.b") == 7
    assert extract_field({"x": {"y": 1}}, "x.y") == 1
    for path, message_text in (
        ("linear.z", "no field 'z'"),
        ("ranges[5]", "out of range"),
        ("nested.c", "no field 'c'"),
        ("linear..x", "is not a field"),
        ("1x", "is not a field"),
    ):
        with pytest.raises(ValueError, match=message_text):
            extract_field(message, path)


def test_json_value() -> None:
    assert json_value(1) == 1
    assert json_value(1.5) == 1.5
    assert json_value("s") == "s"
    assert json_value(None) is None
    assert json_value(True) is True
    assert json_value([1, (2, 3)]) == [1, [2, 3]]
    assert json_value(b"\x01\x02") == [1, 2]
    assert json_value(math.nan) == "nan"
    assert json_value(math.inf) == "inf"
    assert json_value(SimpleNamespace(tolist=lambda: [1, 2])) == [1, 2]
    assert json_value(SimpleNamespace(x=1)) == "namespace(x=1)"


def test_rate_window() -> None:
    window = RateWindow(2.0)
    assert not window.covered(0.0)
    assert window.rate(0.0) == 0.0
    for t in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5):
        window.add(t)
    assert window.total == 6
    assert window.covered(2.5)
    assert window.rate(2.5) == pytest.approx(2.0)  # 1.0, 1.5, 2.0, 2.5 within (0.5, 2.5]
    assert window.rate(10.0) == 0.0
    assert not RateWindow(5.0).covered(100.0)
    late = RateWindow(5.0)
    late.started = 0.0
    assert late.covered(5.0)
    assert not late.covered(4.9)


def test_quaternion_yaw_and_stamps() -> None:
    assert quaternion_yaw(0.0, 0.0, 0.0, 1.0) == 0.0
    half = math.pi / 2
    assert quaternion_yaw(0.0, 0.0, math.sin(half / 2), math.cos(half / 2)) == pytest.approx(half)
    assert stamp_seconds(None) is None
    assert stamp_seconds(SimpleNamespace()) is None
    assert stamp_seconds(SimpleNamespace(stamp=SimpleNamespace(sec=0, nsec=0))) is None
    assert stamp_seconds(SimpleNamespace(stamp=SimpleNamespace(sec=3, nsec=500_000_000))) == 3.5
    ros = SimpleNamespace(stamp=SimpleNamespace(sec=1, nanosec=250_000_000))
    assert stamp_seconds(ros) == 1.25


def test_file_tail(tmp_path: Path) -> None:
    path = tmp_path / "engine.log"
    tail = FileTail(path)
    assert tail.lines() == [], "no file yet"
    path.write_text('{"a": 1}\n{"b":')
    assert tail.lines() == ['{"a": 1}']
    with path.open("a") as file:
        file.write(' 2}\n\n{"c": 3}\n')
    assert tail.lines() == ['{"b": 2}', '{"c": 3}']
    assert tail.lines() == []


def test_recorder_command() -> None:
    assert recorder_command("/r/recording", ["/odom", "/tf"]) == [
        "ros2",
        "bag",
        "record",
        "-s",
        "mcap",
        "-o",
        "/r/recording",
        "--include-hidden-topics",
        "--topics",
        "/odom",
        "/tf",
    ]
    assert recorder_command("/r/recording", []) == [
        "ros2",
        "bag",
        "record",
        "-s",
        "mcap",
        "-o",
        "/r/recording",
        "-a",
    ]


def is_alive(recorder: Recorder) -> bool:
    """`recorder.alive` through a call, so the type checker does not narrow it across starts."""
    return recorder.alive


def test_recorder_process(tmp_path: Path) -> None:
    script = (
        "import signal, time, sys\n"
        "signal.signal(signal.SIGINT, lambda *a: sys.exit(3))\n"
        "print('rec', flush=True)\n"
        "time.sleep(30)\n"
    )
    log = (tmp_path / "rec.log").open("w")
    recorder = Recorder([sys.executable, "-c", script], log)
    assert not is_alive(recorder)
    assert recorder.process is None
    recorder.stop()  # nothing to stop yet
    recorder.start()
    assert is_alive(recorder)
    deadline = time.monotonic() + 20.0
    while (tmp_path / "rec.log").read_text() != "rec\n" and time.monotonic() < deadline:
        time.sleep(0.05)  # the handler is installed once the child prints
    recorder.stop(timeout_s=10.0)
    assert not recorder.alive
    assert recorder.exit_code == 3
    log.close()
    assert (tmp_path / "rec.log").read_text() == "rec\n"
    recorder.stop()  # idempotent


def test_recorder_stop_escalates(tmp_path: Path) -> None:
    script = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(30)\n"
    with (tmp_path / "ignored.log").open("w") as log:
        recorder = Recorder([sys.executable, "-c", script], log)
        recorder.start()
        recorder.stop(timeout_s=0.5)
    assert not recorder.alive
    assert recorder.exit_code is not None


# ------------------------------------------------------------------------------- the observer
class FakeRos:
    """ROS as the observer sees it, scripted."""

    def __init__(self) -> None:
        self.sim: float | None = None
        self.types: dict[str, str] = {}
        self.subscriptions: dict[str, tuple[str, Callable[[Any], None]]] = {}
        self.published: dict[str, list[str]] = {}
        self.timers: list[tuple[float, Callable[[], None]]] = []
        self.spins = 0
        self.shut_down = False
        self.refuse: set[str] = set()

    def now_sim(self) -> float | None:
        return self.sim

    def topic_types(self) -> dict[str, str]:
        return dict(self.types)

    def subscribe(self, topic: str, type_name: str, callback: Callable[[Any], None]) -> None:
        if topic in self.refuse:
            raise ValueError("no such type")
        self.subscriptions[topic] = (type_name, callback)

    def publisher(self, topic: str) -> Callable[[str], None]:
        self.published[topic] = []
        return self.published[topic].append

    def timer(self, period_s: float, callback: Callable[[], None]) -> None:
        self.timers.append((period_s, callback))

    def spin_once(self, timeout_s: float) -> None:
        self.spins += 1
        for _period, callback in self.timers:
            callback()

    def shutdown(self) -> None:
        self.shut_down = True

    def deliver(self, topic: str, message: Any) -> None:
        self.subscriptions[topic][1](message)


class FakeGz:
    def __init__(self, refuse: set[str] | None = None) -> None:
        self.poses: dict[str, Callable[[Any], None]] = {}
        self.contacts: dict[str, Callable[[Any], None]] = {}
        self.stats: dict[str, Callable[[Any], None]] = {}
        self.controls: list[tuple[str, bool | None, int | None, float | None]] = []
        self.refuse = refuse or set()

    def subscribe_poses(self, topic: str, callback: Callable[[Any], None]) -> bool:
        if topic in self.refuse:
            return False
        self.poses[topic] = callback
        return True

    def subscribe_contacts(self, topic: str, callback: Callable[[Any], None]) -> bool:
        if topic in self.refuse:
            return False
        self.contacts[topic] = callback
        return True

    def subscribe_stats(self, topic: str, callback: Callable[[Any], None]) -> bool:
        if topic in self.refuse:
            return False
        self.stats[topic] = callback
        return True

    def world_control(
        self,
        service: str,
        *,
        pause: bool | None = None,
        multi_step: int | None = None,
        run_to_sim_time: float | None = None,
    ) -> bool:
        self.controls.append((service, pause, multi_step, run_to_sim_time))
        return service not in self.refuse


class FakeClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def config(run_dir: Path, **overrides: Any) -> dict[str, Any]:
    base = {
        "world": "depot",
        "models": ["ugv1"],
        "pose_hz": 10.0,
        "contacts": [{"topic": "/contacts", "model": "ugv1"}],
        "rates": [{"topic": "/scan", "window_s": 2.0, "type": None}],
        "values": [
            {"topic": "/cmd_vel", "field": "linear.x", "type": "geometry_msgs/msg/Twist"},
            {"topic": "/cmd_vel", "field": "angular.q", "type": "geometry_msgs/msg/Twist"},
        ],
        "record": {"enabled": False, "topics": [], "output": str(run_dir / "recording")},
        "side_channels": {
            "events": "/fieldproof/events",
            "assertions": "/fieldproof/assertions",
            "metrics": "/fieldproof/metrics",
            "network": "/fieldproof/network",
        },
        "events_file": str(run_dir / "engine.log"),
        "assertions_file": str(run_dir / "assertions.jsonl"),
        "network_file": str(run_dir / "network.jsonl"),
        "samples_file": str(run_dir / "samples.jsonl"),
    }
    base.update(overrides)
    return base


def samples(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run_dir / "samples.jsonl").read_text().splitlines()]


def pose_v(stamp_s: float, **models: tuple[float, float, float]) -> Any:
    poses = []
    for name, (x, y, yaw) in models.items():
        poses.append(
            SimpleNamespace(
                name=name,
                position=SimpleNamespace(x=x, y=y, z=0.1),
                orientation=SimpleNamespace(x=0.0, y=0.0, z=math.sin(yaw / 2), w=math.cos(yaw / 2)),
            )
        )
    return SimpleNamespace(
        header=SimpleNamespace(
            stamp=SimpleNamespace(sec=int(stamp_s), nsec=round((stamp_s % 1) * 1e9))
        ),
        pose=poses,
    )


def contacts(*pairs: tuple[str, str]) -> Any:
    return SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=4, nsec=0)),
        contact=[
            SimpleNamespace(collision1=SimpleNamespace(name=a), collision2=SimpleNamespace(name=b))
            for a, b in pairs
        ],
    )


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    (tmp_path / "engine.log").write_text('{"event": "run.start"}\n')
    (tmp_path / "assertions.jsonl").write_text("")
    return tmp_path


Started = tuple[FakeRos, FakeGz, FakeClock, Observer]


def started(run_dir: Path) -> Started:
    """An observer on the test configuration, started, with the lidar not yet published."""
    ros, gz, clock = FakeRos(), FakeGz(), FakeClock()
    ros.types = {"/odom": "nav_msgs/msg/Odometry"}
    sink = Sink(run_dir / "samples.jsonl", clock=clock)
    watcher = Observer(config(run_dir), sink, ros, gz, clock=clock)
    watcher.start()
    return ros, gz, clock, watcher


def traffic(ros: FakeRos, gz: FakeGz, clock: FakeClock) -> None:
    """The lidar appears and publishes six scans, Nav2 two commands, Gazebo poses and contacts."""
    ros.types["/scan"] = "sensor_msgs/msg/LaserScan"
    ros.spin_once(0.1)
    for i in range(6):
        clock.t = 101.0 + i * 0.25
        ros.sim = 10.0 + i * 0.25
        ros.deliver("/scan", SimpleNamespace(ranges=[1.0]))
    for x in (0.4, 0.6):
        twist = SimpleNamespace(linear=SimpleNamespace(x=x), angular=SimpleNamespace(z=0.0))
        ros.deliver("/cmd_vel", twist)
    gz.poses["/world/depot/pose/info"](pose_v(12.5, ugv1=(1.0, 2.0, 0.5), other=(0, 0, 0)))
    gz.poses["/world/depot/pose/info"](pose_v(12.51, ugv1=(1.1, 2.0, 0.5)))
    clock.t += 0.2
    gz.poses["/world/depot/pose/info"](pose_v(12.7, ugv1=(1.2, 2.0, 0.5)))
    gz.contacts["/contacts"](contacts())
    gz.contacts["/contacts"](
        contacts(("ugv1::base_link::base_link_collision", "wall::c"), ("a", "b"))
    )
    gz.contacts["/contacts"](contacts(("x", "y")))


def test_observer_subscribes_and_streams_messages(run_dir: Path) -> None:
    ros, gz, clock, watcher = started(run_dir)
    # Publishers, Gazebo subscriptions, the typed topic subscribed at once, the other pending.
    assert set(ros.published) == {
        "/fieldproof/events",
        "/fieldproof/assertions",
        "/fieldproof/metrics",
        "/fieldproof/network",
    }
    assert set(gz.poses) == {"/world/depot/pose/info"}
    assert set(gz.contacts) == {"/contacts"}
    assert set(ros.subscriptions) == {"/cmd_vel"}
    assert watcher.ready, "no recorder: ready at once"
    kinds = [s["kind"] for s in samples(run_dir)]
    assert kinds == ["subscribed", "ready"]
    assert samples(run_dir)[-1]["waiting"] == ["/scan"]
    # The lidar appears and the discovery timer subscribes to it; then messages: values per
    # message with the simulation time, a field error reported once; ground truth: poses at
    # pose_hz, untracked models skipped; contacts rate-limited.
    traffic(ros, gz, clock)
    assert ros.subscriptions["/scan"][0] == "sensor_msgs/msg/LaserScan"
    values = [s for s in samples(run_dir) if s["kind"] == "value"]
    assert [v.get("value") for v in values if v["field"] == "linear.x"] == [0.4, 0.6]
    errors = [v for v in values if v["field"] == "angular.q"]
    assert len(errors) == 1
    assert "no field 'q'" in errors[0]["error"]
    assert values[0]["sim"] == 11.25
    poses = [s for s in samples(run_dir) if s["kind"] == "pose"]
    assert [(p["model"], p["x"], p["sim"]) for p in poses] == [
        ("ugv1", 1.0, 12.5),
        ("ugv1", 1.2, 12.7),
    ]
    assert poses[0]["yaw"] == pytest.approx(0.5, abs=1e-3)
    hits = [s for s in samples(run_dir) if s["kind"] == "contact"]
    assert len(hits) == 1
    assert hits[0]["collision2"] == "wall::c"
    assert hits[0]["count"] == 2
    assert hits[0]["sim"] == 4.0
    assert hits[0]["model"] == "ugv1"
    watcher.stop()
    assert watcher.run() == 0


def test_observer_timers_and_side_channels(run_dir: Path) -> None:
    ros, gz, clock, watcher = started(run_dir)
    traffic(ros, gz, clock)
    # Timers: a rate sample once the window is covered, metrics with the real-time factor, the
    # engine log relayed onto /fieldproof/events.
    clock.t = 103.5
    ros.sim = 12.5
    ros.spin_once(0.1)
    rates = [s for s in samples(run_dir) if s["kind"] == "rate"]
    assert rates
    assert rates[-1]["topic"] == "/scan"
    assert rates[-1]["hz"] == pytest.approx(1.5)  # 3 of 6 messages in the last 2 s of sim time
    assert rates[-1]["total"] == 6
    clocks = [s for s in samples(run_dir) if s["kind"] == "clock"]
    assert clocks[-1]["sim"] == 12.5
    metrics = json.loads(ros.published["/fieldproof/metrics"][-1])
    assert metrics["poses"]["ugv1"]["x"] == 1.2
    assert metrics["rates_hz"]["/scan"] == pytest.approx(1.5)
    assert metrics["recording"] is False
    clock.t = 104.5
    ros.sim = 13.0
    ros.spin_once(0.1)
    assert json.loads(ros.published["/fieldproof/metrics"][-1])["rtf"] == pytest.approx(0.5)
    assert ros.published["/fieldproof/events"] == ['{"event": "run.start"}']
    with (run_dir / "assertions.jsonl").open("a") as file:
        file.write('{"name": "reached_goal", "status": "passed"}\n')
    ros.spin_once(0.1)
    assert ros.published["/fieldproof/assertions"] == [
        '{"name": "reached_goal", "status": "passed"}'
    ]
    # The ground station's link measurements (radio links) onto /fieldproof/network; the file
    # appears only in runs with links.
    assert ros.published["/fieldproof/network"] == []
    (run_dir / "network.jsonl").write_text('{"kind": "link", "vehicle": "ugv1"}\n')
    ros.spin_once(0.1)
    assert ros.published["/fieldproof/network"] == ['{"kind": "link", "vehicle": "ugv1"}']
    # Stop.
    watcher.stop()
    assert watcher.run() == 0
    assert ros.shut_down
    assert samples(run_dir)[-1]["kind"] == "stopped"


def test_observer_reports_subscription_problems(run_dir: Path) -> None:
    ros, gz = FakeRos(), FakeGz(refuse={"/contacts"})
    ros.refuse = {"/cmd_vel"}
    sink = Sink(run_dir / "samples.jsonl")
    watcher = Observer(config(run_dir), sink, ros, gz)
    watcher.start()
    messages = [s["message"] for s in samples(run_dir) if s["kind"] == "error"]
    assert messages == [
        "cannot subscribe to /contacts",
        "cannot subscribe to /cmd_vel (geometry_msgs/msg/Twist): no such type",
    ]
    assert watcher.ready, "problems are reported, the observer still serves"
    assert watcher.errors == 2
    watcher.stop()
    assert watcher.run() == 0


def test_observer_without_gazebo_or_channels(run_dir: Path) -> None:
    ros = FakeRos()
    sink = Sink(run_dir / "samples.jsonl")
    watcher = Observer(config(run_dir, side_channels={}, rates=[], values=[]), sink, ros, None)
    watcher.stop()
    assert watcher.run() == 0
    assert ros.published == {}
    assert [s["kind"] for s in samples(run_dir)] == ["ready", "stopped"]


class FakeRecorder:
    def __init__(self, alive_for: int | None = None, code: int | None = None) -> None:
        self.command = ["ros2", "bag", "record"]
        self.started = False
        self.stopped = False
        self.polls = 0
        self.alive_for = alive_for
        self.code = code

    def start(self) -> None:
        self.started = True

    @property
    def alive(self) -> bool:
        self.polls += 1
        if self.stopped:
            return False
        return self.alive_for is None or self.polls <= self.alive_for

    @property
    def exit_code(self) -> int | None:
        return None if self.alive_for is None and not self.stopped else self.code

    def stop(self, timeout_s: float = 1.0) -> None:
        self.stopped = True


def is_ready(watcher: Observer) -> bool:
    """`watcher.ready` through a call, so the type checker does not narrow it across spins."""
    return watcher.ready


def test_observer_waits_for_the_recorder(run_dir: Path) -> None:
    ros, clock = FakeRos(), FakeClock(50.0)
    recorder = FakeRecorder(code=0)
    sink = Sink(run_dir / "samples.jsonl", clock=clock)
    watcher = Observer(
        config(run_dir, rates=[], values=[], contacts=[]),
        sink,
        ros,
        FakeGz(),
        recorder=recorder,  # type: ignore[arg-type]
        clock=clock,
        sleep=lambda _s: None,
    )
    watcher.start()
    assert recorder.started
    assert not is_ready(watcher), "the recorder gets a warm-up before ready"
    clock.t = 50.0 + observer.RECORDER_WARMUP_S
    ros.spin_once(0.1)
    assert is_ready(watcher)
    ready = next(s for s in samples(run_dir) if s["kind"] == "ready")
    assert ready["recording"] is True
    assert samples(run_dir)[0]["kind"] == "recording"
    slept: list[float] = []
    watcher.sleep = slept.append
    watcher.stop()
    assert watcher.run() == 0
    assert recorder.stopped
    assert slept == [observer.RELAY_GRACE_S], "the recorder gets the last messages before SIGINT"
    assert any(s["kind"] == "recorder.stopped" for s in samples(run_dir))


def test_observer_fails_when_the_recorder_dies(run_dir: Path) -> None:
    ros, clock = FakeRos(), FakeClock(50.0)
    recorder = FakeRecorder(alive_for=0, code=2)
    sink = Sink(run_dir / "samples.jsonl", clock=clock)
    watcher = Observer(
        config(run_dir, rates=[], values=[], contacts=[]),
        sink,
        ros,
        None,
        recorder=recorder,  # type: ignore[arg-type]
        clock=clock,
        sleep=lambda _s: None,
    )
    clock.t = 60.0
    assert watcher.run() == 1, "never ready: the recorder exited during start-up"
    errors = [s["message"] for s in samples(run_dir) if s["kind"] == "error"]
    assert errors == ["the recorder exited (2) during start-up"]
    assert ros.spins == 0, "noticed during start-up, before the loop"


def test_observer_stops_when_the_recorder_dies_later(run_dir: Path) -> None:
    ros, clock = FakeRos(), FakeClock(50.0)
    recorder = FakeRecorder(alive_for=3, code=1)
    sink = Sink(run_dir / "samples.jsonl", clock=clock)
    watcher = Observer(
        config(run_dir, rates=[], values=[], contacts=[]),
        sink,
        ros,
        None,
        recorder=recorder,  # type: ignore[arg-type]
        clock=clock,
        sleep=lambda _s: None,
    )
    clock.t = 60.0
    assert watcher.run() == 0, "ready, then the recorder died: stopped, reported, exit 0"
    assert watcher.ready
    assert any(
        s["message"] == "the recorder exited (1)" for s in samples(run_dir) if s["kind"] == "error"
    )


def test_main_reads_the_config(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    class StubObserver:
        def __init__(
            self, cfg: Any, sink: Any, ros: Any, gz: Any, *, recorder: Any, pool: Any = None
        ) -> None:
            created.update(cfg=cfg, ros=ros, gz=gz, recorder=recorder, pool=pool)
            sink.close()

        def run(self) -> int:
            return 0

        def stop(self) -> None:
            pass

    monkeypatch.setattr(observer, "Observer", StubObserver)
    monkeypatch.setattr(observer, "RclpyRos", lambda: "ros")
    monkeypatch.setattr(observer, "GzTransport", lambda: "gz")
    cfg = config(run_dir, record={"enabled": True, "topics": ["/odom"], "output": "/r/recording"})
    (run_dir / "observe.json").write_text(json.dumps(cfg))
    assert observer.main(["--config", str(run_dir / "observe.json")]) == 0
    assert created["ros"] == "ros"
    assert created["gz"] == "gz"
    assert created["recorder"].command == recorder_command("/r/recording", ["/odom"])
    assert observer.main(["--config", str(run_dir / "observe.json"), "--no-gazebo"]) == 0
    assert created["gz"] is None
    (run_dir / "observe.json").write_text("[]")
    with pytest.raises(ValueError, match="not a JSON object"):
        observer.main(["--config", str(run_dir / "observe.json")])
