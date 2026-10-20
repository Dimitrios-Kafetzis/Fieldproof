#!/usr/bin/env python3
"""The run observer of the recorder container (task P1-T06).

The runner bind-mounts this file into the recorder container (the bridge image, which has ROS 2,
rosbag2 with the MCAP storage and the Gazebo transport bindings) together with the run directory,
and starts it on ``observe.json`` (:func:`fieldproof.assertions.observe_plan`). It then

- records the scenario's topics with ``ros2 bag record`` into ``recording/`` (MCAP) when the
  scenario asks for it, the ``/fieldproof/*`` side channels always included;
- subscribes to the ROS topics the assertions name, measuring rates over sliding windows and
  extracting fields, and to the Gazebo ground truth (``/world/<name>/pose/info`` for the models,
  the vehicles' contact sensors), and appends one JSON line per observation to ``samples.jsonl``
  for the runner's evaluator;
- publishes the side channels: ``/fieldproof/events`` (the engine's log lines as they are
  written), ``/fieldproof/assertions`` (every verdict the runner writes),
  ``/fieldproof/metrics`` (once a second: simulation time, real-time factor, rates, poses) and
  ``/fieldproof/network`` (the ground station's link measurements, task P2-T02, when the run has
  radio links), all as ``std_msgs/msg/String`` carrying JSON, so the recording holds the whole
  run.

It writes ``{"kind": "ready"}`` once everything is up, and stops cleanly on SIGTERM (the
recorder is sent SIGINT and waited for, so the MCAP is finalised). Like the readiness probe, it
depends on nothing but the standard library inside the container (rclpy, std_msgs,
rosidl_runtime_py and the ``gz`` bindings come from the image); the pure parts are tested in the
engine, the ROS and Gazebo adapters are exercised by the integration test.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import IO, Any, Protocol

NODE_NAME = "fieldproof_observer"
DISCOVERY_S = 1.0
RATE_PERIOD_S = 1.0
METRICS_PERIOD_S = 1.0
TAIL_PERIOD_S = 0.5
CONTACT_HZ = 10.0
RECORDER_STOP_S = 15.0
# The recorder is given this long to come up before the observer reports ready.
RECORDER_WARMUP_S = 2.0
# After the last side-channel messages are published, the recorder gets this long to receive
# them before it is stopped (the final verdicts are written just before the teardown).
RELAY_GRACE_S = 1.0
FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)?((?:\[\d+\])*)$")


# -------------------------------------------------------------------------------- pure parts
class Sink:
    """The samples file: one JSON object per line, flushed at once, from any thread."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] = path.open("a")
        self._lock = threading.Lock()
        self.clock = clock
        self.count = 0

    def emit(self, kind: str, sim: float | None = None, **fields: Any) -> None:
        """Append one sample."""
        record = {"kind": kind, "wall": round(self.clock(), 3), "sim": sim, **fields}
        line = json.dumps(record, default=str) + "\n"
        with self._lock:
            self._file.write(line)
            self._file.flush()
            self.count += 1

    def close(self) -> None:
        """Close the file."""
        with self._lock:
            self._file.close()


def extract_field(message: Any, path: str) -> Any:
    """`pose.pose.position.x`, `ranges[0]`, `data` of a message (attributes or mapping keys)."""
    value = message
    for part in path.split("."):
        match = FIELD.match(part)
        if not match or not part:
            raise ValueError(f"{path!r}: {part!r} is not a field")
        name, indexes = match.group(1), match.group(2)
        if name:
            if isinstance(value, Mapping):
                if name not in value:
                    raise ValueError(f"{path!r}: no field {name!r}")
                value = value[name]
            elif hasattr(value, name):
                value = getattr(value, name)
            else:
                raise ValueError(f"{path!r}: no field {name!r} in {type(value).__name__}")
        for index in re.findall(r"\[(\d+)\]", indexes):
            try:
                value = value[int(index)]
            except (IndexError, KeyError, TypeError) as error:
                raise ValueError(f"{path!r}: index {index} out of range") from error
    return value


def json_value(value: Any) -> Any:
    """A field as JSON: numbers, booleans and strings as they are, sequences as lists."""
    if isinstance(value, bool | int | float | str) or value is None:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return str(value)
        return value
    if hasattr(value, "tolist"):
        return json_value(value.tolist())
    if isinstance(value, bytes | bytearray):
        return list(value)
    if isinstance(value, list | tuple):
        return [json_value(v) for v in value]
    return str(value)


class RateWindow:
    """Message times over a sliding window."""

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self.times: deque[float] = deque()
        self.total = 0
        self.started: float | None = None

    def add(self, t: float) -> None:
        """One message at `t`."""
        if self.started is None:
            self.started = t
        self.times.append(t)
        self.total += 1
        self._prune(t)

    def _prune(self, now: float) -> None:
        while self.times and self.times[0] <= now - self.window_s:
            self.times.popleft()

    def covered(self, now: float) -> bool:
        """Whether a full window has passed since the first message (or the subscription)."""
        return self.started is not None and now - self.started >= self.window_s

    def rate(self, now: float) -> float:
        """Messages per second over the window ending now."""
        self._prune(now)
        return len(self.times) / self.window_s


def quaternion_yaw(x: float, y: float, z: float, w: float) -> float:
    """The heading of a quaternion."""
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def stamp_seconds(header: Any) -> float | None:
    """The time of a message header (ROS `stamp.sec/nanosec`, Gazebo `stamp.sec/nsec`)."""
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return None
    sec = float(getattr(stamp, "sec", 0) or 0)
    nanos = getattr(stamp, "nanosec", None)
    if nanos is None:
        nanos = getattr(stamp, "nsec", 0)
    value = sec + float(nanos or 0) * 1e-9
    return value if value > 0 else None


class FileTail:
    """Reads the complete lines appended to a file since the last call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self._partial = ""

    def lines(self) -> list[str]:
        """The new complete lines (none while the file does not exist)."""
        try:
            with self.path.open("rb") as file:
                file.seek(self.offset)
                chunk = file.read()
        except OSError:
            return []
        if not chunk:
            return []
        self.offset += len(chunk)
        text = self._partial + chunk.decode("utf-8", errors="replace")
        parts = text.split("\n")
        self._partial = parts.pop()
        return [p for p in parts if p.strip()]


def recorder_command(output: str, topics: Sequence[str]) -> list[str]:
    """`ros2 bag record` into `output` as MCAP: the topics given (hidden too), or everything."""
    command = ["ros2", "bag", "record", "-s", "mcap", "-o", output]
    if topics:
        command += ["--include-hidden-topics", "--topics", *topics]
    else:
        command.append("-a")
    return command


class Recorder:
    """The `ros2 bag record` process."""

    def __init__(self, command: Sequence[str], log: IO[str] | None = None) -> None:
        self.command = list(command)
        self.log = log
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        """Start recording."""
        self.process = subprocess.Popen(  # noqa: S603  (our own argument list)
            self.command, stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL
        )

    @property
    def alive(self) -> bool:
        """Whether the recorder is running."""
        return self.process is not None and self.process.poll() is None

    @property
    def exit_code(self) -> int | None:
        """The exit code once it stopped."""
        return None if self.process is None else self.process.poll()

    def stop(self, timeout_s: float = RECORDER_STOP_S) -> None:
        """SIGINT, so rosbag2 finalises the file; SIGTERM and SIGKILL if it does not return."""
        if self.process is None or self.process.poll() is not None:
            return
        for sig, wait in ((signal.SIGINT, timeout_s), (signal.SIGTERM, 5.0)):
            self.process.send_signal(sig)
            try:
                self.process.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue
        self.process.kill()
        self.process.wait(timeout=5.0)


# ------------------------------------------------------------------------------ the adapters
class Ros(Protocol):
    """What the observer asks of ROS (``RclpyRos`` below, a fake in the tests)."""

    def now_sim(self) -> float | None:
        """Simulation time (`/clock`), None before it is known."""

    def topic_types(self) -> dict[str, str]:
        """Every topic of the graph with its (first) type."""

    def subscribe(self, topic: str, type_name: str, callback: Callable[[Any], None]) -> None:
        """Subscribe with a QoS every publisher is compatible with."""

    def publisher(self, topic: str) -> Callable[[str], None]:
        """A `std_msgs/msg/String` publisher."""

    def timer(self, period_s: float, callback: Callable[[], None]) -> None:
        """Call `callback` every `period_s`."""

    def spin_once(self, timeout_s: float) -> None:
        """Process callbacks for up to `timeout_s`."""

    def shutdown(self) -> None:
        """Stop."""


class Gz(Protocol):
    """What the observer asks of Gazebo transport (``GzTransport`` below, a fake in the tests)."""

    def subscribe_poses(self, topic: str, callback: Callable[[Any], None]) -> bool:
        """`gz.msgs.Pose_V` messages."""

    def subscribe_contacts(self, topic: str, callback: Callable[[Any], None]) -> bool:
        """`gz.msgs.Contacts` messages."""


class RclpyRos:
    """ROS through rclpy, with `use_sim_time`."""

    def __init__(self) -> None:
        import rclpy  # noqa: PLC0415  (inside the container only)
        from rclpy.parameter import Parameter  # noqa: PLC0415
        from rclpy.qos import (  # noqa: PLC0415
            DurabilityPolicy,
            HistoryPolicy,
            QoSProfile,
            ReliabilityPolicy,
        )
        from rosidl_runtime_py.utilities import get_message  # noqa: PLC0415
        from std_msgs.msg import String  # noqa: PLC0415

        rclpy.init()
        self._rclpy = rclpy
        self._get_message = get_message
        self._string = String
        self.node = rclpy.create_node(
            NODE_NAME,
            parameter_overrides=[Parameter("use_sim_time", Parameter.Type.BOOL, True)],
        )
        # Compatible with every publisher: best effort matches reliable, volatile matches
        # transient local.
        self._qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=50,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._publish_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=100,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._handles: list[Any] = []

    def now_sim(self) -> float | None:
        """See :class:`Ros`."""
        seconds = self.node.get_clock().now().nanoseconds / 1e9
        return seconds if seconds > 0 else None

    def topic_types(self) -> dict[str, str]:
        """See :class:`Ros`."""
        return {name: types[0] for name, types in self.node.get_topic_names_and_types() if types}

    def subscribe(self, topic: str, type_name: str, callback: Callable[[Any], None]) -> None:
        """See :class:`Ros`."""
        self._handles.append(
            self.node.create_subscription(self._get_message(type_name), topic, callback, self._qos)
        )

    def publisher(self, topic: str) -> Callable[[str], None]:
        """See :class:`Ros`."""
        publisher = self.node.create_publisher(self._string, topic, self._publish_qos)
        self._handles.append(publisher)

        def publish(text: str) -> None:
            publisher.publish(self._string(data=text))

        return publish

    def timer(self, period_s: float, callback: Callable[[], None]) -> None:
        """See :class:`Ros`."""
        self._handles.append(self.node.create_timer(period_s, callback))

    def spin_once(self, timeout_s: float) -> None:
        """See :class:`Ros`."""
        self._rclpy.spin_once(self.node, timeout_sec=timeout_s)

    def shutdown(self) -> None:
        """See :class:`Ros`."""
        self.node.destroy_node()
        self._rclpy.shutdown()


class GzTransport:
    """Gazebo transport through the `gz.transport13` bindings."""

    def __init__(self) -> None:
        from gz.msgs10.contacts_pb2 import Contacts  # noqa: PLC0415
        from gz.msgs10.pose_v_pb2 import Pose_V  # noqa: PLC0415
        from gz.transport13 import Node  # noqa: PLC0415

        self._node = Node()
        self._pose_v = Pose_V
        self._contacts = Contacts

    def subscribe_poses(self, topic: str, callback: Callable[[Any], None]) -> bool:
        """See :class:`Gz`."""
        return bool(self._node.subscribe(self._pose_v, topic, callback))

    def subscribe_contacts(self, topic: str, callback: Callable[[Any], None]) -> bool:
        """See :class:`Gz`."""
        return bool(self._node.subscribe(self._contacts, topic, callback))


# ------------------------------------------------------------------------------ the observer
class Observer:
    """Watches what `observe.json` names and writes the samples."""

    def __init__(
        self,
        config: Mapping[str, Any],
        sink: Sink,
        ros: Ros,
        gz: Gz | None,
        *,
        recorder: Recorder | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.sink = sink
        self.ros = ros
        self.gz = gz
        self.recorder = recorder
        self.clock = clock
        self.sleep = sleep
        self.started = clock()
        self.stopping = False
        self.ready = False
        self.rates: dict[str, RateWindow] = {}
        self.values: dict[str, list[tuple[str, bool]]] = {}
        """Per topic, the fields to extract and whether an error was already reported."""
        self.pending: dict[str, str | None] = {}
        self.subscribed: set[str] = set()
        self.pose_hz = float(config.get("pose_hz", 10.0))
        self.models = list(config.get("models", []))
        self.last_pose: dict[str, float] = {}
        self.poses: dict[str, dict[str, float]] = {}
        self.last_contact: dict[str, float] = {}
        self.contact_models = {c["topic"]: c.get("model") for c in config.get("contacts", [])}
        self.last_metrics: tuple[float, float] | None = None
        self.publishers: dict[str, Callable[[str], None]] = {}
        self.tails: dict[str, FileTail] = {}
        self.errors = 0

    # ----------------------------------------------------------------------------- set-up
    def start(self) -> None:
        """Subscriptions, publishers, timers and the recorder."""
        for entry in self.config.get("rates", []):
            self.rates[entry["topic"]] = RateWindow(float(entry.get("window_s", 5.0)))
            self.pending.setdefault(entry["topic"], entry.get("type"))
        for entry in self.config.get("values", []):
            self.values.setdefault(entry["topic"], []).append((entry["field"], False))
            if self.pending.get(entry["topic"]) is None:
                self.pending[entry["topic"]] = entry.get("type")
        channels = self.config.get("side_channels", {})
        for key, topic in channels.items():
            self.publishers[key] = self.ros.publisher(topic)
        for key, file_key in (
            ("events", "events_file"),
            ("assertions", "assertions_file"),
            ("network", "network_file"),
        ):
            if key in self.publishers and self.config.get(file_key):
                self.tails[key] = FileTail(Path(self.config[file_key]))
        if self.gz is not None:
            world = self.config.get("world")
            if world and self.models:
                topic = f"/world/{world}/pose/info"
                if not self.gz.subscribe_poses(topic, self.on_poses):
                    self.error(f"cannot subscribe to {topic}")
            for topic in self.contact_models:
                if not self.gz.subscribe_contacts(topic, partial(self.on_contacts, topic)):
                    self.error(f"cannot subscribe to {topic}")
        if self.recorder is not None:
            self.recorder.start()
            self.sink.emit("recording", command=self.recorder.command)
        self.discover()
        self.ros.timer(DISCOVERY_S, self.discover)
        self.ros.timer(RATE_PERIOD_S, self.emit_rates)
        self.ros.timer(METRICS_PERIOD_S, self.emit_metrics)
        self.ros.timer(TAIL_PERIOD_S, self.relay_files)

    def error(self, message: str) -> None:
        """Report a problem in the stream (the runner shows it)."""
        self.errors += 1
        self.sink.emit("error", message=message)

    def discover(self) -> None:
        """Subscribe to the pending topics whose type is known or published."""
        waiting = [t for t in self.pending if t not in self.subscribed]
        if not waiting:
            self._check_ready()
            return
        types = self.ros.topic_types()
        for topic in waiting:
            type_name = self.pending[topic] or types.get(topic)
            if type_name is None:
                continue
            try:
                self.ros.subscribe(topic, type_name, partial(self.on_message, topic))
            except Exception as error:
                self.error(f"cannot subscribe to {topic} ({type_name}): {error}")
                self.subscribed.add(topic)
                continue
            self.subscribed.add(topic)
            now = self.clock()
            if topic in self.rates:
                self.rates[topic].started = now
            self.sink.emit("subscribed", topic=topic, type=type_name)
        self._check_ready()

    def _check_ready(self) -> None:
        if self.ready:
            return
        if self.recorder is not None:
            if self.clock() - self.started < RECORDER_WARMUP_S:
                return
            if not self.recorder.alive:
                self.error(f"the recorder exited ({self.recorder.exit_code}) during start-up")
                self.stopping = True
                return
        self.ready = True
        self.sink.emit(
            "ready",
            recording=self.recorder is not None,
            subscribed=sorted(self.subscribed),
            waiting=sorted(t for t in self.pending if t not in self.subscribed),
        )

    # --------------------------------------------------------------------------- callbacks
    def on_message(self, topic: str, message: Any) -> None:
        """A ROS message of a watched topic."""
        now = self.clock()
        window = self.rates.get(topic)
        if window is not None:
            window.add(now)
        fields = self.values.get(topic)
        if not fields:
            return
        sim = self.ros.now_sim()
        for index, (path, failed) in enumerate(fields):
            try:
                value = json_value(extract_field(message, path))
            except ValueError as error:
                if not failed:
                    fields[index] = (path, True)
                    self.sink.emit("value", sim, topic=topic, field=path, error=str(error))
                continue
            self.sink.emit("value", sim, topic=topic, field=path, value=value)

    def on_poses(self, message: Any) -> None:
        """A `Pose_V` of the world: the tracked models at `pose_hz`."""
        now = self.clock()
        sim = stamp_seconds(getattr(message, "header", None))
        for pose in getattr(message, "pose", []):
            name = getattr(pose, "name", "")
            if name not in self.models:
                continue
            position, orientation = pose.position, pose.orientation
            yaw = quaternion_yaw(orientation.x, orientation.y, orientation.z, orientation.w)
            self.poses[name] = {
                "x": round(position.x, 4),
                "y": round(position.y, 4),
                "z": round(position.z, 4),
                "yaw": round(yaw, 4),
            }
            if now - self.last_pose.get(name, -math.inf) < 1.0 / self.pose_hz:
                continue
            self.last_pose[name] = now
            self.sink.emit("pose", sim, model=name, **self.poses[name])

    def on_contacts(self, topic: str, message: Any) -> None:
        """A `Contacts` message of a vehicle's contact sensor."""
        contacts = list(getattr(message, "contact", []))
        if not contacts:
            return
        now = self.clock()
        if now - self.last_contact.get(topic, -math.inf) < 1.0 / CONTACT_HZ:
            return
        self.last_contact[topic] = now
        first = contacts[0]
        self.sink.emit(
            "contact",
            stamp_seconds(getattr(message, "header", None)),
            topic=topic,
            model=self.contact_models.get(topic),
            collision1=str(getattr(getattr(first, "collision1", None), "name", "")),
            collision2=str(getattr(getattr(first, "collision2", None), "name", "")),
            count=len(contacts),
        )

    # -------------------------------------------------------------------------------- timers
    def emit_rates(self) -> None:
        """One rate sample per watched topic whose window is full."""
        now = self.clock()
        sim = self.ros.now_sim()
        for topic, window in self.rates.items():
            if topic in self.subscribed and window.covered(now):
                self.sink.emit(
                    "rate",
                    sim,
                    topic=topic,
                    hz=round(window.rate(now), 3),
                    window_s=window.window_s,
                    total=window.total,
                )

    def emit_metrics(self) -> None:
        """`/fieldproof/metrics` and a `clock` sample: simulation time and real-time factor."""
        now = self.clock()
        sim = self.ros.now_sim()
        rtf = None
        if sim is not None and self.last_metrics is not None:
            d_wall = now - self.last_metrics[0]
            if d_wall > 0:
                rtf = round((sim - self.last_metrics[1]) / d_wall, 3)
        if sim is not None:
            self.last_metrics = (now, sim)
        metrics = {
            "wall": round(now, 3),
            "sim": sim,
            "rtf": rtf,
            "rates_hz": {t: round(w.rate(now), 2) for t, w in self.rates.items() if w.covered(now)},
            "poses": self.poses,
            "recording": self.recorder.alive if self.recorder is not None else False,
        }
        if "metrics" in self.publishers:
            self.publishers["metrics"](json.dumps(metrics))
        self.sink.emit("clock", sim, rtf=rtf)
        if self.recorder is not None and not self.recorder.alive and not self.stopping:
            self.error(f"the recorder exited ({self.recorder.exit_code})")
            self.stopping = True
        self._check_ready()

    def relay_files(self) -> None:
        """New lines of the engine log and the verdicts file onto the side channels."""
        for key, tail in self.tails.items():
            publish = self.publishers[key]
            for line in tail.lines():
                publish(line)

    # ---------------------------------------------------------------------------------- run
    def run(self, spin_timeout_s: float = 0.2) -> int:
        """Start, spin until told to stop, stop the recorder; exit 0, or 1 on an error."""
        try:
            self.start()
            while not self.stopping:
                self.ros.spin_once(spin_timeout_s)
        finally:
            self.relay_files()
            if self.recorder is not None:
                if self.recorder.alive:
                    self.sleep(RELAY_GRACE_S)  # let the recorder receive the last messages
                self.recorder.stop()
                self.sink.emit("recorder.stopped", exit_code=self.recorder.exit_code)
            self.sink.emit("stopped", samples=self.sink.count, errors=self.errors)
            self.ros.shutdown()
            self.sink.close()
        return 1 if self.errors and not self.ready else 0

    def stop(self) -> None:
        """Ask the loop to end (the signal handler)."""
        self.stopping = True


def load_config(path: Path) -> dict[str, Any]:
    """Read `observe.json`."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point inside the recorder container."""
    parser = argparse.ArgumentParser(description="Fieldproof run observer")
    parser.add_argument("--config", required=True, type=Path, help="observe.json of the run")
    parser.add_argument(
        "--no-gazebo", action="store_true", help="do not read the Gazebo ground truth"
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    sink = Sink(Path(config["samples_file"]))
    recorder = None
    record = config.get("record", {})
    if record.get("enabled"):
        recorder = Recorder(
            recorder_command(record["output"], record.get("topics", [])), sys.stdout
        )
    gz = None if args.no_gazebo else GzTransport()
    observer = Observer(config, sink, RclpyRos(), gz, recorder=recorder)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _signum, _frame: observer.stop())
    return observer.run()


if __name__ == "__main__":
    sys.exit(main())
