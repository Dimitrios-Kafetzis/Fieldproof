#!/usr/bin/env python3
"""The replay player of a run (task P2-T07): the clock and the field log's topics.

The runner bind-mounts this file into the player container (the bridge image, which has ROS 2
and rosbag2 with the MCAP and SQLite storages) together with the log and the run directory,
and starts it on ``replay.json`` (:meth:`fieldproof.replay.ReplayPlan.player_config`). It

- publishes ``/clock`` from the log's timestamps: the clock starts ``lead_in_s`` before the
  log's first played message and runs at ``rate`` log seconds per wall-clock second, so the
  vehicles' stacks come up on a moving clock before the data starts and every message of the
  log is published at the clock time its header carries;
- starts ``ros2 bag play`` paused, with the mapped topics only (never the log's own
  ``/clock``), each remapped into its vehicle's namespace (a log recorded at the root plays
  into ``/ugv1/scan``), the QoS overrides the plan asks for (a vehicle's ``tf_static`` is
  transient local, whatever the log says), ``--rate``, ``--start-offset`` and
  ``--playback-duration`` from the section, and **resumes** it through the player's service
  the moment the clock reaches the log's start, so the playback and the clock agree to a few
  milliseconds;
- relays the log's TF topic through a frame filter where the plan asks for one (only the
  listed ``parent -> child`` transforms reach the vehicle's TF topic, so the log's own
  localisation never competes with the stack's);
- reports what it does in ``replay.jsonl`` of the run directory (``started``, ``ready`` once
  the paused player answers, ``playing`` when the playback was resumed, ``done`` when the log
  is played, ``error``), which the runner tails: the run's timeline ends once the log is
  played, and the player keeps the clock going until it is stopped.

Like the observer, this file depends on nothing but the standard library inside the container
(rclpy, rosgraph_msgs, tf2_msgs and rosbag2_interfaces come from the image); the pure parts
are tested in the engine, the ROS adapter by the integration test.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, Protocol

NODE_NAME = "fieldproof_player"
PLAYER_NODE = "rosbag2_player"
"""The node name of `ros2 bag play`, whose `resume` service the player calls."""
RESUME_SERVICE = f"/{PLAYER_NODE}/resume"
TF_TYPE = "tf2_msgs/msg/TFMessage"
STARTING = "starting"
READY = "ready"
PLAYING = "playing"
DONE = "done"
ERROR = "error"
READY_TIMEOUT_S = 90.0
"""How long `ros2 bag play` may take to open the log and offer its services."""
STOP_GRACE_S = 10.0
TICK_S = 0.01
PROGRESS_S = 30.0
"""How often a playing log is reported in the container's log."""


# -------------------------------------------------------------------------------- pure parts
def play_command(config: Mapping[str, Any], qos_file: str | None) -> list[str]:
    """The `ros2 bag play` command line of a configuration."""
    storage = config.get("storage")
    command = ["ros2", "bag", "play"]
    if storage:
        command += ["--input", str(config["log"]), str(storage)]
    else:
        command.append(str(config["log"]))
    command += ["--start-paused", "--disable-keyboard-controls", "--log-level", "warn"]
    rate = float(config.get("rate") or 1.0)
    if rate != 1.0:
        command += ["--rate", f"{rate:g}"]
    start_s = float(config.get("start_s") or 0.0)
    if start_s > 0:
        command += ["--start-offset", f"{start_s:g}"]
    duration_s = config.get("duration_s")
    if duration_s is not None:
        command += ["--playback-duration", f"{float(duration_s):g}"]
    topics = [str(t) for t in config.get("topics") or []]
    if topics:
        command += ["--topics", *topics]
    remaps = {str(k): str(v) for k, v in (config.get("remaps") or {}).items()}
    if remaps:
        command += ["--remap", *(f"{old}:={new}" for old, new in remaps.items())]
    if qos_file:
        command += ["--qos-profile-overrides-path", qos_file]
    return command


def qos_overrides_text(qos: Mapping[str, Mapping[str, Any]]) -> str:
    """The QoS override file of `ros2 bag play` (one mapping per topic, by its log name)."""
    lines = []
    for topic, profile in qos.items():
        lines.append(f"{topic}:")
        lines.extend(f"  {key}: {value}" for key, value in profile.items())
    return "\n".join(lines) + "\n"


def frame_pairs(frames: Iterable[Any]) -> set[tuple[str, str]]:
    """`(parent, child)` pairs from the plan's lists (leading slashes dropped)."""
    pairs = set()
    for item in frames:
        parent, child = (str(v).lstrip("/") for v in item)
        pairs.add((parent, child))
    return pairs


def filter_transforms(transforms: Sequence[Any], allowed: set[tuple[str, str]]) -> list[Any]:
    """The transforms whose `(header.frame_id, child_frame_id)` is allowed."""
    kept = []
    for transform in transforms:
        parent = str(getattr(getattr(transform, "header", None), "frame_id", "")).lstrip("/")
        child = str(getattr(transform, "child_frame_id", "")).lstrip("/")
        if (parent, child) in allowed:
            kept.append(transform)
    return kept


class Status:
    """The status lines: a JSON object per line in the status file, echoed to stdout."""

    def __init__(
        self,
        path: Path | None,
        out: IO[str] | None = None,
        *,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._file: IO[str] | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a")
        self.out = out
        self.wall = wall
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, sim: float | None = None, **fields: Any) -> None:
        """One status line."""
        record = {"event": event, "wall": round(self.wall(), 3), "sim": sim, **fields}
        self.events.append(record)
        line = json.dumps(record, default=str)
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()
        if self.out is not None:
            self.out.write(f"player: {line}\n")
            self.out.flush()

    def close(self) -> None:
        """Close the file."""
        if self._file is not None:
            self._file.close()
            self._file = None


class Process(Protocol):
    """What the player needs of the `ros2 bag play` process."""

    def poll(self) -> int | None:
        """The exit code, or None while it runs."""

    def send_signal(self, sig: int) -> None:
        """Send a signal."""

    def wait(self, timeout: float | None = None) -> int:
        """Wait for it to end."""

    def kill(self) -> None:
        """Kill it."""


class Ros(Protocol):
    """What the player needs of ROS (rclpy inside the container, a fake in the tests)."""

    def publish_clock(self, nanoseconds: int) -> None:
        """Publish `/clock`."""

    def relay_tf(self, source: str, target: str, allowed: set[tuple[str, str]]) -> None:
        """Republish the allowed transforms of `source` on `target`."""

    def resume_ready(self) -> bool:
        """Whether the player's resume service answers."""

    def resume(self) -> None:
        """Resume the paused playback (asynchronously)."""

    def spin_once(self, timeout_s: float) -> None:
        """Deliver callbacks for up to `timeout_s`."""

    def shutdown(self) -> None:
        """Release ROS."""


class Player:
    """The clock and the playback of one run."""

    def __init__(
        self,
        config: Mapping[str, Any],
        ros: Ros,
        status: Status,
        *,
        spawn: Callable[[Sequence[str]], Process] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        ready_timeout_s: float = READY_TIMEOUT_S,
    ) -> None:
        self.config = config
        self.ros = ros
        self.status = status
        self.spawn = spawn
        self.clock = clock
        self.ready_timeout_s = ready_timeout_s
        self.rate = float(config.get("rate") or 1.0)
        self.t0_ns = int(config["t0_ns"])
        self.lead_in_ns = round(float(config.get("lead_in_s") or 0.0) * 1e9)
        self.clock_period_s = 1.0 / float(config.get("clock_hz") or 100.0)
        self.state = STARTING
        self.process: Process | None = None
        self.started_at: float | None = None
        self.last_clock: float | None = None
        self.last_progress: float | None = None
        self.resumed_at: float | None = None
        self.exit_code: int | None = None
        self.stopping = False

    # ------------------------------------------------------------------------------ set-up
    def qos_file(self) -> str | None:
        """Write the QoS override file, if the plan has overrides; its path."""
        qos = self.config.get("qos") or {}
        path = self.config.get("qos_file")
        if not qos or not path:
            return None
        Path(str(path)).write_text(qos_overrides_text(qos))
        return str(path)

    def start(self) -> None:
        """Start the paused playback, the TF relays and the clock."""
        for relay in self.config.get("tf_filters") or []:
            self.ros.relay_tf(str(relay["from"]), str(relay["to"]), frame_pairs(relay["frames"]))
        command = play_command(self.config, self.qos_file())
        self.started_at = self.clock()
        self.process = self.spawn(command)
        self.status.emit(
            "started",
            self.sim_s(),
            command=command,
            t0_ns=self.t0_ns,
            lead_in_s=self.lead_in_ns / 1e9,
            rate=self.rate,
        )

    # --------------------------------------------------------------------------- the clock
    def sim_ns(self, now: float | None = None) -> int:
        """The clock now: the log's start, less the lead-in, plus the paced wall clock."""
        if self.started_at is None:
            return self.t0_ns - self.lead_in_ns
        elapsed = (self.clock() if now is None else now) - self.started_at
        return self.t0_ns - self.lead_in_ns + round(elapsed * self.rate * 1e9)

    def sim_s(self, now: float | None = None) -> float:
        """:meth:`sim_ns` in seconds."""
        return self.sim_ns(now) / 1e9

    # -------------------------------------------------------------------------------- the loop
    def tick(self) -> None:
        """Publish the clock when due and move the playback along."""
        now = self.clock()
        if self.last_clock is None or now - self.last_clock >= self.clock_period_s:
            self.last_clock = now
            self.ros.publish_clock(self.sim_ns(now))
        if self.state in (DONE, ERROR):
            return
        exit_code = self.process.poll() if self.process is not None else None
        if exit_code is not None:
            self._exited(exit_code, now)
            return
        if self.state == STARTING:
            if self.ros.resume_ready():
                self.state = READY
                self.status.emit(READY, self.sim_s(now), after_s=round(now - self.started_s(), 3))
            elif now - self.started_s() > self.ready_timeout_s:
                self._fail(
                    f"ros2 bag play offered no {RESUME_SERVICE} service within "
                    f"{self.ready_timeout_s:g} s",
                    now,
                )
                return
        if self.state == READY and self.sim_ns(now) >= self.t0_ns:
            self.ros.resume()
            self.state = PLAYING
            self.resumed_at = now
            late = (self.sim_ns(now) - self.t0_ns) / 1e9
            self.status.emit(PLAYING, self.sim_s(now), late_s=round(late, 3))
            self.last_progress = now
        if (
            self.state == PLAYING
            and self.last_progress is not None
            and now - self.last_progress >= PROGRESS_S
        ):
            self.last_progress = now
            self.status.emit("progress", self.sim_s(now), played_s=round(self.played_s(now), 1))

    def started_s(self) -> float:
        """When the player started, on the monotonic clock."""
        return self.started_at if self.started_at is not None else self.clock()

    def played_s(self, now: float) -> float:
        """Log seconds played so far."""
        if self.resumed_at is None:
            return 0.0
        return (now - self.resumed_at) * self.rate

    def _exited(self, exit_code: int, now: float) -> None:
        self.exit_code = exit_code
        if self.state == PLAYING:
            self.state = DONE
            self.status.emit(
                DONE, self.sim_s(now), exit_code=exit_code, played_s=round(self.played_s(now), 3)
            )
            return
        self._fail(f"ros2 bag play exited ({exit_code}) before the playback started", now)

    def _fail(self, message: str, now: float) -> None:
        self.state = ERROR
        self.status.emit(ERROR, self.sim_s(now), message=message)

    @property
    def failed(self) -> bool:
        """Whether the playback failed (the clock still runs until the player is stopped)."""
        return self.state == ERROR

    # ---------------------------------------------------------------------------------- run
    def run(self) -> int:
        """Start, tick until told to stop, end the playback; exit 0, or 1 on a failure."""
        try:
            self.start()
            while not self.stopping:
                self.tick()
                self.ros.spin_once(TICK_S)
        finally:
            self.end_process()
            self.status.emit("stopped", self.sim_s(), state=self.state)
            self.status.close()
            self.ros.shutdown()
        return 1 if self.failed else 0

    def end_process(self) -> None:
        """Interrupt the playback if it still runs, and wait for it."""
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(STOP_GRACE_S)

    def stop(self) -> None:
        """Ask the loop to end (the signal handler)."""
        self.stopping = True


# ------------------------------------------------------------------------------ the adapter
class RclpyRos:
    """ROS through rclpy (inside the container only)."""

    def __init__(self) -> None:
        import rclpy  # noqa: PLC0415
        from rclpy.qos import (  # noqa: PLC0415
            DurabilityPolicy,
            HistoryPolicy,
            QoSProfile,
            ReliabilityPolicy,
        )
        from rosbag2_interfaces.srv import Resume  # noqa: PLC0415
        from rosgraph_msgs.msg import Clock  # noqa: PLC0415
        from tf2_msgs.msg import TFMessage  # noqa: PLC0415

        rclpy.init()
        self._rclpy = rclpy
        self._clock_type = Clock
        self._tf_type = TFMessage
        self.node = rclpy.create_node(NODE_NAME)
        reliable = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._clock = self.node.create_publisher(Clock, "/clock", reliable)
        self._tf_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=100,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._handles: list[Any] = []
        self._resume = self.node.create_client(Resume, RESUME_SERVICE)
        self._resume_type = Resume

    def publish_clock(self, nanoseconds: int) -> None:
        """See :class:`Ros`."""
        message = self._clock_type()
        message.clock.sec = nanoseconds // 1_000_000_000
        message.clock.nanosec = nanoseconds % 1_000_000_000
        self._clock.publish(message)

    def relay_tf(self, source: str, target: str, allowed: set[tuple[str, str]]) -> None:
        """See :class:`Ros`."""
        publisher = self.node.create_publisher(self._tf_type, target, self._tf_qos)

        def on_message(message: Any) -> None:
            kept = filter_transforms(list(message.transforms), allowed)
            if kept:
                out = self._tf_type()
                out.transforms = kept
                publisher.publish(out)

        self._handles.append(publisher)
        self._handles.append(
            self.node.create_subscription(self._tf_type, source, on_message, self._tf_qos)
        )

    def resume_ready(self) -> bool:
        """See :class:`Ros`."""
        return bool(self._resume.service_is_ready())

    def resume(self) -> None:
        """See :class:`Ros`."""
        self._handles.append(self._resume.call_async(self._resume_type.Request()))

    def spin_once(self, timeout_s: float) -> None:
        """See :class:`Ros`."""
        self._rclpy.spin_once(self.node, timeout_sec=timeout_s)

    def shutdown(self) -> None:
        """See :class:`Ros`."""
        self.node.destroy_node()
        self._rclpy.shutdown()


def load_config(path: Path) -> dict[str, Any]:
    """Read `replay.json`."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point inside the player container."""
    parser = argparse.ArgumentParser(description="Fieldproof replay player")
    parser.add_argument("--config", type=Path, required=True, help="replay.json of the run")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    status_file = config.get("status_file")
    status = Status(Path(str(status_file)) if status_file else None, sys.stdout)
    player = Player(config, RclpyRos(), status)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _signum, _frame: player.stop())
    return player.run()


if __name__ == "__main__":
    sys.exit(main())
