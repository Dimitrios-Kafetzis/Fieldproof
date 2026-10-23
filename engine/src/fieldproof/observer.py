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
  run;
- manages the clock (task P2-T04, :class:`ClockManager`): it follows the world's statistics
  (paused, the simulator's own real-time factor), reports the drift between simulation time and
  the wall clock in the metrics and, in lockstep mode, steps the world through its
  ``/world/<name>/control`` service: one step of ``step_s`` at a time, the next only once every
  vehicle's clock agent (:mod:`fieldproof.lockstep`, in the vehicle's network namespace) has
  acknowledged the previous one on ``/fieldproof/lockstep``, up to the hold the engine sets
  through ``clock.jsonl`` in the run directory (the next due time of the timeline), so the
  engine starts every entry at an exact simulation time.

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
from concurrent.futures import Executor, ThreadPoolExecutor
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
# The clock protocol between the engine and the observer (task P2-T04; fieldproof.clock reads
# these): the engine appends commands to CLOCK_FILE in the run directory, the observer answers
# with `clock` samples. The clock agents acknowledge steps on ACK_TOPIC.
CLOCK_FILE = "clock.jsonl"
ACK_TOPIC = "/fieldproof/lockstep"
ACK_TYPE = "std_msgs/msg/String"
CMD_LOCKSTEP = "lockstep"
"""Pause the world at the next step boundary (the origin) and hold there."""
CMD_RUN_UNTIL = "run_until"
"""Step until `t_s` simulation seconds after the origin (rounded up to the step grid; null:
no limit) and hold there."""
CMD_RELEASE = "release"
"""Let the world run free again."""
CONTROL_PERIOD_S = 0.05
"""How often the command file is read and the step in flight is checked."""
WAITING_REPORT_S = 1.0
"""How often a step waiting for acknowledgements is reported."""
LOCKSTEP_LEAD_S = 0.5
"""How far ahead of the current simulation time the origin is put, so the request lands."""
LOCKSTEP_EPS = 1e-6
"""Tolerance when comparing simulation times on the step grid."""
WORLD_CONTROL_TIMEOUT_MS = 2000
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

    def subscribe_stats(self, topic: str, callback: Callable[[Any], None]) -> bool:
        """`gz.msgs.WorldStatistics` messages."""

    def world_control(
        self,
        service: str,
        *,
        pause: bool | None = None,
        multi_step: int | None = None,
        run_to_sim_time: float | None = None,
    ) -> bool:
        """A `gz.msgs.WorldControl` request to the world's control service; its answer."""


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
        from rclpy.clock import Clock, ClockType  # noqa: PLC0415

        self._steady = Clock(clock_type=ClockType.STEADY_TIME)
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
        """See :class:`Ros`.

        On the steady clock, not the node's simulation clock: the observer keeps measuring,
        relaying and (in lockstep) stepping while the world is paused, when a sim-time timer
        would never fire again (task P2-T04).
        """
        self._handles.append(self.node.create_timer(period_s, callback, clock=self._steady))

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
        from gz.msgs10.boolean_pb2 import Boolean  # noqa: PLC0415
        from gz.msgs10.contacts_pb2 import Contacts  # noqa: PLC0415
        from gz.msgs10.pose_v_pb2 import Pose_V  # noqa: PLC0415
        from gz.msgs10.world_control_pb2 import WorldControl  # noqa: PLC0415
        from gz.msgs10.world_stats_pb2 import WorldStatistics  # noqa: PLC0415
        from gz.transport13 import Node  # noqa: PLC0415

        self._node = Node()
        self._pose_v = Pose_V
        self._contacts = Contacts
        self._stats = WorldStatistics
        self._world_control = WorldControl
        self._boolean = Boolean

    def subscribe_poses(self, topic: str, callback: Callable[[Any], None]) -> bool:
        """See :class:`Gz`."""
        return bool(self._node.subscribe(self._pose_v, topic, callback))

    def subscribe_contacts(self, topic: str, callback: Callable[[Any], None]) -> bool:
        """See :class:`Gz`."""
        return bool(self._node.subscribe(self._contacts, topic, callback))

    def subscribe_stats(self, topic: str, callback: Callable[[Any], None]) -> bool:
        """See :class:`Gz`."""
        return bool(self._node.subscribe(self._stats, topic, callback))

    def world_control(
        self,
        service: str,
        *,
        pause: bool | None = None,
        multi_step: int | None = None,
        run_to_sim_time: float | None = None,
    ) -> bool:
        """See :class:`Gz`."""
        request = self._world_control()
        if pause is not None:
            request.pause = pause
        if multi_step is not None:
            request.multi_step = multi_step
        if run_to_sim_time is not None:
            seconds = int(run_to_sim_time)
            request.run_to_sim_time.sec = seconds
            request.run_to_sim_time.nsec = round((run_to_sim_time - seconds) * 1e9)
        ok, reply = self._node.request(
            service, request, self._world_control, self._boolean, WORLD_CONTROL_TIMEOUT_MS
        )
        return bool(ok) and bool(getattr(reply, "data", False))


# -------------------------------------------------------------------------- the clock manager
def on_grid(t: float, step_s: float) -> float:
    """`t` rounded to the nearest multiple of the step (the simulator pauses on the grid)."""
    return round(round(t / step_s) * step_s, 9)


def next_on_grid(t: float, step_s: float) -> float:
    """The first multiple of the step at or after `t`."""
    return round(math.ceil(t / step_s - LOCKSTEP_EPS) * step_s, 9)


def step_of(sim: float, step_s: float) -> int:
    """The step a simulation time is in (the number of whole steps since 0)."""
    return math.floor(sim / step_s + LOCKSTEP_EPS)


class ClockManager:
    """Follows the world's clock and, in lockstep mode, steps it (task P2-T04).

    The world's statistics give `paused` and the simulator's real-time factor for the metrics
    in every mode. In lockstep mode the engine's commands come from the clock file: `lockstep`
    pauses the world at the next step boundary (the origin of the timeline's clock); `run_until`
    sets how far past the origin the stepping may go (rounded up to the grid); `release` lets
    the world run free. A step is requested with `run_to_sim_time` and counts as reached once
    every vehicle's agent has acknowledged a simulation time at or past it; the next step is
    requested at once unless the hold is reached. A step nobody acknowledges within
    `ack_timeout_s` is reported as a `lockstep.timeout` sample, which fails the run.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        sink: Sink,
        gz: Gz | None,
        ros: Ros,
        *,
        clock: Callable[[], float] = time.time,
        error: Callable[[str], None] | None = None,
        pool: Executor | None = None,
    ) -> None:
        self.sink = sink
        self.gz = gz
        self.ros = ros
        self.clock = clock
        self.report_error = error or (lambda _message: None)
        self.pool = pool
        """Where the world-control requests run (None: inline). The real observer gives a
        worker thread, so a slow or lost reply never stalls the spin that delivers the
        acknowledgements and the commands; the acknowledgements confirm a step, not the reply."""
        self.world = str(config.get("world", ""))
        self.mode = str(config.get("mode", "sim"))
        self.target_rtf = float(config.get("target_rtf") or 1.0)
        self.step_s = float(config.get("step_s") or 0.1)
        self.vehicles = [str(v) for v in config.get("vehicles", [])]
        self.ack_timeout_s = float(config.get("ack_timeout_s") or 30.0)
        self.control = (
            FileTail(Path(config["control_file"])) if config.get("control_file") else None
        )
        self.service = f"/world/{self.world}/control"
        # The world as its statistics last reported it.
        self.sim: float | None = None
        self.paused: bool | None = None
        self.gz_rtf: float | None = None
        self.iterations = 0
        # Lockstep state.
        self.acks: dict[str, float] = {}
        """Per vehicle, the simulation time its agent last acknowledged."""
        self.origin: float | None = None
        self.hold_t: float | None = None
        """How far past the origin the stepping may go (None: no limit)."""
        self.target: float | None = None
        """The absolute simulation time of the step in flight (None: none)."""
        self.requested_at: float | None = None
        self.reported_at: float | None = None
        self.step_index = 0
        self.held = False
        self.locked = False
        self.released = False
        self.timed_out = False
        self.steps = 0
        self.holds = 0
        self.max_wait_s = 0.0
        self.wait_total_s = 0.0

    @property
    def lockstep(self) -> bool:
        """Whether this run steps the world."""
        return self.mode == "lockstep"

    def start(self) -> None:
        """Subscribe to the world's statistics and, in lockstep mode, to the acknowledgements."""
        if self.gz is not None and self.world:
            topic = f"/world/{self.world}/stats"
            if not self.gz.subscribe_stats(topic, self.on_stats):
                self.report_error(f"cannot subscribe to {topic}")
        if self.lockstep:
            self.ros.subscribe(ACK_TOPIC, ACK_TYPE, self.on_ack)
            self.ros.timer(CONTROL_PERIOD_S, self.poll)

    # ---------------------------------------------------------------------------- inputs
    def on_stats(self, message: Any) -> None:
        """A `WorldStatistics` message: the simulator's own view of its clock."""
        stamp = getattr(message, "sim_time", None)
        if stamp is not None:
            self.sim = float(getattr(stamp, "sec", 0)) + float(getattr(stamp, "nsec", 0)) / 1e9
        self.paused = bool(getattr(message, "paused", False))
        self.iterations = int(getattr(message, "iterations", 0))
        if not self.paused:
            self.gz_rtf = round(float(getattr(message, "real_time_factor", 0.0)), 3)

    def on_ack(self, message: Any) -> None:
        """An acknowledgement of a vehicle's clock agent."""
        try:
            data = json.loads(str(getattr(message, "data", "")))
            vehicle, sim = str(data["vehicle"]), float(data["sim"])
        except (ValueError, KeyError, TypeError):
            return
        if sim >= self.acks.get(vehicle, -math.inf):
            self.acks[vehicle] = sim
        self.advance()

    def poll(self) -> None:
        """The engine's new commands, and the step in flight against the acknowledgement limit."""
        if self.control is not None:
            for line in self.control.lines():
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    self.report_error(f"clock command not JSON: {line!r}")
                    continue
                if isinstance(data, dict):
                    self.command(data)
        if self.target is None or self.requested_at is None:
            return
        if self.reached():
            self.advance()  # nobody to wait for (no vehicle), or acknowledged meanwhile
            return
        now = self.clock()
        waited = now - self.requested_at
        if waited > self.ack_timeout_s and not self.timed_out:
            self.timed_out = True
            missing = self.missing()
            self.sink.emit(
                "lockstep.timeout",
                self.target,
                step=self.step_index,
                waited_s=round(waited, 3),
                missing=missing,
                acks=dict(self.acks),
            )
            self.report_error(
                f"lockstep: step {self.step_index} (sim {self.target:.3f} s) not acknowledged by "
                f"{', '.join(missing) or 'anyone'} within {self.ack_timeout_s:g} s"
            )
        elif self.reported_at is None or now - self.reported_at >= WAITING_REPORT_S:
            self.reported_at = now
            self.emit_clock(waiting=self.missing(), waited_s=round(waited, 3))

    def command(self, data: Mapping[str, Any]) -> None:
        """One command of the engine."""
        cmd = data.get("cmd")
        if cmd == CMD_LOCKSTEP:
            self.begin()
        elif cmd == CMD_RUN_UNTIL:
            limit = data.get("t_s")
            self.hold_t = None if limit is None else next_on_grid(float(limit), self.step_s)
            self.held = False
            self.advance()
        elif cmd == CMD_RELEASE:
            self.release()
        else:
            self.report_error(f"unknown clock command: {data!r}")

    # --------------------------------------------------------------------------- stepping
    def begin(self) -> None:
        """Pause the world at the next step boundary: the origin."""
        if self.origin is not None or self.gz is None:
            return
        now_sim = self.sim if self.sim is not None else (self.ros.now_sim() or 0.0)
        origin = next_on_grid(now_sim + LOCKSTEP_LEAD_S, self.step_s)
        self.origin, self.hold_t, self.step_index, self.released = origin, 0.0, 0, False
        self.request(origin)

    def release(self) -> None:
        """Let the world run free; nothing is stepped from now on."""
        self.released = True
        self.target = None
        self.hold_t = None
        if self.gz is not None and self.origin is not None:
            self.gz.world_control(self.service, pause=False)
        self.emit_clock(released=True)

    def request(self, target: float) -> None:
        """Ask the world to run to `target` and pause; the step is then in flight."""
        if self.gz is None:
            return
        self.target = target
        self.requested_at = self.clock()
        self.reported_at = None
        self.timed_out = False
        gz = self.gz

        def send() -> None:
            if not gz.world_control(self.service, run_to_sim_time=target):
                self.report_error(
                    f"lockstep: no reply to run_to_sim_time {target:.3f} from {self.service} "
                    "(the acknowledgements decide whether the step was taken)"
                )

        if self.pool is None:
            send()
        else:
            self.pool.submit(send)

    def missing(self) -> list[str]:
        """The vehicles that have not acknowledged the step in flight."""
        if self.target is None:
            return []
        return [
            v for v in self.vehicles if self.acks.get(v, -math.inf) < self.target - LOCKSTEP_EPS
        ]

    def reached(self) -> bool:
        """Whether every vehicle acknowledged the step in flight."""
        return self.target is not None and not self.missing()

    def advance(self) -> None:
        """Settle the step in flight if acknowledged, and request the next unless held."""
        if self.origin is None or self.released:
            return
        if self.target is not None:
            if not self.reached():
                return
            waited = self.clock() - (self.requested_at or self.clock())
            self.max_wait_s = max(self.max_wait_s, waited)
            self.wait_total_s += waited
            reached, self.target = self.target, None
            if not self.locked:
                self.locked = True
                self.emit_clock(sim=reached, locked=True)
            else:
                self.steps += 1
                self.emit_clock(sim=reached)
        next_t = round((self.step_index + 1) * self.step_s, 9)
        if self.hold_t is not None and next_t > self.hold_t + LOCKSTEP_EPS:
            if not self.held:
                self.held = True
                self.holds += 1
                self.emit_clock(held=True)
            return
        self.held = False
        self.step_index += 1
        self.request(round(self.origin + next_t, 9))

    # ----------------------------------------------------------------------------- output
    def state(self) -> dict[str, Any]:
        """The clock as the metrics and the `clock` samples carry it."""
        state: dict[str, Any] = {
            "mode": self.mode,
            "target_rtf": self.target_rtf,
            "paused": self.paused,
            "gz_rtf": self.gz_rtf,
        }
        if self.lockstep:
            state["lockstep"] = {
                "origin": self.origin,
                "step": self.step_index,
                "step_s": self.step_s,
                "t_s": None if self.origin is None else round(self.step_index * self.step_s, 6),
                "hold_t": self.hold_t,
                "held": self.held,
                "in_flight": self.target,
                "steps": self.steps,
                "holds": self.holds,
                "max_wait_s": round(self.max_wait_s, 3),
                "released": self.released,
            }
            state["acks"] = dict(self.acks)
        return state

    def emit_clock(self, sim: float | None = None, **fields: Any) -> None:
        """A `clock` sample of the lockstep protocol."""
        if sim is None:
            sim = self.sim if self.sim is not None else self.ros.now_sim()
        self.sink.emit("clock", sim, rtf=None, **self.state(), **fields)


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
        pool: Executor | None = None,
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
        self.first_metrics: tuple[float, float] | None = None
        """The wall and simulation time of the first metrics sample (the drift's origin)."""
        self.publishers: dict[str, Callable[[str], None]] = {}
        self.tails: dict[str, FileTail] = {}
        self.errors = 0
        self.clock_manager = ClockManager(
            config.get("clock", {"world": config.get("world")}),
            sink,
            gz,
            ros,
            clock=clock,
            error=self.error,
            pool=pool,
        )

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
        self.clock_manager.start()
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
        drift = None
        if sim is not None and self.last_metrics is not None:
            d_wall = now - self.last_metrics[0]
            if d_wall > 0:
                rtf = round((sim - self.last_metrics[1]) / d_wall, 3)
        if sim is not None:
            self.last_metrics = (now, sim)
            if self.first_metrics is None:
                self.first_metrics = (now, sim)
            # Simulation seconds gained (or lost) against the wall clock at the target pace
            # since the first sample; a paused world drifts by design.
            target = self.clock_manager.target_rtf
            drift = round((sim - self.first_metrics[1]) - (now - self.first_metrics[0]) * target, 3)
        state = self.clock_manager.state()
        metrics = {
            "wall": round(now, 3),
            "sim": sim,
            "rtf": rtf,
            "drift_s": drift,
            **state,
            "rates_hz": {t: round(w.rate(now), 2) for t, w in self.rates.items() if w.covered(now)},
            "poses": self.poses,
            "recording": self.recorder.alive if self.recorder is not None else False,
        }
        if "metrics" in self.publishers:
            self.publishers["metrics"](json.dumps(metrics))
        self.sink.emit("clock", sim, rtf=rtf, drift_s=drift, **state)
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
    observer = Observer(
        config,
        sink,
        RclpyRos(),
        gz,
        recorder=recorder,
        pool=ThreadPoolExecutor(max_workers=1, thread_name_prefix="fieldproof-clock"),
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _signum, _frame: observer.stop())
    return observer.run()


if __name__ == "__main__":
    sys.exit(main())
