#!/usr/bin/env python3
"""The ground station of a run with radio links (task P2-T02).

The runner bind-mounts this file into the ground container (the bridge image, on the run's radio
network only) together with the run directory, and starts it on ``ground.json``. It

- publishes each vehicle's **heartbeat** (``std_msgs/msg/Header`` stamped with the wall clock,
  best effort, depth 1) on the topic the vehicle's manifest names (``runtime.link.heartbeat``),
  under its namespace, at the manifest's rate: what a ground control station sends and a
  vehicle's loss-of-link failsafe listens for. A heartbeat crosses the vehicle's radio link like
  any other traffic, so a dropped link stops it;
- **probes** every link with ICMP echoes (an unprivileged ``SOCK_DGRAM`` ICMP socket, which a
  container's ``ping_group_range`` allows), ten a second, and appends one JSON line per link and
  window of probes to ``network.jsonl``: probes sent, replies, the sum, sum of squares, minimum
  and maximum of the round trips. The runner turns the windows into each link's measured latency,
  jitter and loss (:class:`fieldproof.network.LinkMonitor`), and the observer relays the file
  onto ``/fieldproof/network`` so the recording holds it.

It writes ``{"kind": "ready"}`` once every vehicle's address is resolved and the heartbeats are
published, and stops on SIGTERM. Like the observer it depends on the standard library only
(rclpy and std_msgs come from the image); the probe accounting is tested in the engine, the ROS
side by the integration tests.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import select
import signal
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

NODE_NAME = "fieldproof_ground"
ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0
RESOLVE_TIMEOUT_S = 30.0


# -------------------------------------------------------------------------------- pure parts
class Sink:
    """`network.jsonl`: one JSON object per line, flushed at once, from any thread."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] = path.open("a")
        self._lock = threading.Lock()
        self.clock = clock

    def emit(self, kind: str, **fields: Any) -> None:
        """Append one line."""
        line = json.dumps({"kind": kind, "wall": round(self.clock(), 3), **fields}) + "\n"
        with self._lock:
            self._file.write(line)
            self._file.flush()

    def close(self) -> None:
        """Close the file."""
        with self._lock:
            self._file.close()


def echo_request(sequence: int, payload_bytes: int) -> bytes:
    """An ICMP echo request; the kernel fills in the identifier and the checksum."""
    return struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, 0, sequence & 0xFFFF) + bytes(
        payload_bytes
    )


def echo_reply_sequence(packet: bytes) -> int | None:
    """The sequence number of an ICMP echo reply (a ping socket returns no IP header)."""
    if len(packet) < 8:
        return None
    kind, _code, _checksum, _identifier, sequence = struct.unpack("!BBHHH", packet[:8])
    return sequence if kind == ICMP_ECHO_REPLY else None


@dataclass
class Window:
    """The probes of one link sent during one window, and their round trips."""

    start: float
    end: float
    sent: int = 0
    received: int = 0
    rtt_sum_ms: float = 0.0
    rtt_sq_sum_ms: float = 0.0
    rtt_min_ms: float | None = None
    rtt_max_ms: float | None = None

    def add(self, rtt_ms: float) -> None:
        """One reply."""
        self.received += 1
        self.rtt_sum_ms += rtt_ms
        self.rtt_sq_sum_ms += rtt_ms * rtt_ms
        self.rtt_min_ms = rtt_ms if self.rtt_min_ms is None else min(self.rtt_min_ms, rtt_ms)
        self.rtt_max_ms = rtt_ms if self.rtt_max_ms is None else max(self.rtt_max_ms, rtt_ms)

    def to_dict(self) -> dict[str, Any]:
        """The window as a `link` line."""
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "sent": self.sent,
            "received": self.received,
            "rtt_sum_ms": round(self.rtt_sum_ms, 4),
            "rtt_sq_sum_ms": round(self.rtt_sq_sum_ms, 4),
            "rtt_min_ms": None if self.rtt_min_ms is None else round(self.rtt_min_ms, 4),
            "rtt_max_ms": None if self.rtt_max_ms is None else round(self.rtt_max_ms, 4),
        }


@dataclass
class Probes:
    """The probes of one link: what was sent when, which window each belongs to.

    Windows are aligned on the wall clock (`window_s` slices of the epoch), so the windows of
    every link start together and the runner can tell which profile each was sent under. A
    window closes once its last probe's timeout has passed; a reply later than the timeout counts
    as lost.
    """

    vehicle: str
    window_s: float
    timeout_s: float
    outstanding: dict[int, tuple[float, float, int]] = field(default_factory=dict)
    """Sequence number to (monotonic send time, wall send time, window index)."""
    windows: dict[int, Window] = field(default_factory=dict)
    sequence: int = 0

    def _window(self, wall: float) -> tuple[int, Window]:
        index = math.floor(wall / self.window_s)
        window = self.windows.get(index)
        if window is None:
            window = Window(index * self.window_s, (index + 1) * self.window_s)
            self.windows[index] = window
        return index, window

    def next_sequence(self) -> int:
        """The sequence number of the next probe."""
        self.sequence = (self.sequence + 1) & 0xFFFF
        return self.sequence

    def sent(self, sequence: int, monotonic: float, wall: float) -> None:
        """A probe left."""
        index, window = self._window(wall)
        window.sent += 1
        self.outstanding[sequence] = (monotonic, wall, index)

    def replied(self, sequence: int, monotonic: float) -> float | None:
        """A reply arrived: its round trip in ms, or None if unknown or too late."""
        probe = self.outstanding.pop(sequence, None)
        if probe is None:
            return None
        sent_at, _wall, index = probe
        rtt_s = monotonic - sent_at
        window = self.windows.get(index)
        if window is None or rtt_s > self.timeout_s:
            return None
        window.add(rtt_s * 1000.0)
        return rtt_s * 1000.0

    def due(self, wall: float) -> list[Window]:
        """The windows whose probes have all timed out or answered, oldest first."""
        closed = []
        for index in sorted(self.windows):
            window = self.windows[index]
            if window.end + self.timeout_s > wall:
                break
            closed.append(self.windows.pop(index))
        horizon = wall - self.timeout_s - self.window_s
        for sequence in [s for s, (_, w, _) in self.outstanding.items() if w < horizon]:
            del self.outstanding[sequence]
        return closed


# ---------------------------------------------------------------------------------- prober
class Prober:
    """Sends the probes of every link from one ICMP socket and accounts for the replies."""

    def __init__(
        self,
        addresses: Mapping[str, str],
        sink: Sink,
        *,
        interval_s: float,
        window_s: float,
        timeout_s: float,
        payload_bytes: int,
        sock: socket.socket | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.addresses = dict(addresses)
        self.by_address = {address: name for name, address in self.addresses.items()}
        self.sink = sink
        self.interval_s = interval_s
        self.payload_bytes = payload_bytes
        self.monotonic = monotonic
        self.wall = wall
        self.probes = {name: Probes(name, window_s, timeout_s) for name in self.addresses}
        self.sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        self.sock.setblocking(False)
        self.stopping = False

    def send_all(self) -> None:
        """One probe to every link."""
        for name, address in self.addresses.items():
            probes = self.probes[name]
            sequence = probes.next_sequence()
            # An unreachable link is a lost probe, which the window shows.
            with contextlib.suppress(OSError):
                self.sock.sendto(echo_request(sequence, self.payload_bytes), (address, 0))
            probes.sent(sequence, self.monotonic(), self.wall())

    def receive(self, timeout_s: float) -> None:
        """Read the replies that arrive within `timeout_s`."""
        deadline = self.monotonic() + timeout_s
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return
            readable, _, _ = select.select([self.sock], [], [], remaining)
            if not readable:
                return
            try:
                packet, (address, _port) = self.sock.recvfrom(2048)
            except BlockingIOError:
                continue
            now = self.monotonic()
            name = self.by_address.get(address)
            sequence = echo_reply_sequence(packet)
            if name is not None and sequence is not None:
                self.probes[name].replied(sequence, now)

    def flush(self) -> None:
        """Write every window that closed."""
        wall = self.wall()
        for name, probes in self.probes.items():
            for window in probes.due(wall):
                self.sink.emit("link", vehicle=name, **window.to_dict())

    def run(self) -> None:
        """Probe until stopped."""
        while not self.stopping:
            started = self.monotonic()
            self.send_all()
            self.receive(max(self.interval_s - (self.monotonic() - started), 0.0))
            self.flush()

    def stop(self) -> None:
        """Ask the loop to end."""
        self.stopping = True


def resolve(hosts: Mapping[str, str], timeout_s: float = RESOLVE_TIMEOUT_S) -> dict[str, str]:
    """Each vehicle's address on the radio network (its link holder's DNS name)."""
    deadline = time.monotonic() + timeout_s
    addresses: dict[str, str] = {}
    while True:
        for name, host in hosts.items():
            if name in addresses:
                continue
            try:
                addresses[name] = socket.gethostbyname(host)
            except OSError:
                continue
        if len(addresses) == len(hosts) or time.monotonic() >= deadline:
            return addresses
        time.sleep(0.5)


# ------------------------------------------------------------------------------- heartbeats
class Heartbeats:
    """The heartbeat publishers, on rclpy, spun on their own thread."""

    def __init__(self, heartbeats: Sequence[Mapping[str, Any]]) -> None:
        import rclpy  # noqa: PLC0415  (inside the container only)
        from rclpy.executors import SingleThreadedExecutor  # noqa: PLC0415
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy  # noqa: PLC0415
        from std_msgs.msg import Header  # noqa: PLC0415

        rclpy.init()
        self._rclpy = rclpy
        self.node = rclpy.create_node(NODE_NAME)
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=1, reliability=ReliabilityPolicy.BEST_EFFORT
        )
        self.counts: dict[str, int] = {}
        for entry in heartbeats:
            topic = str(entry["topic"])
            publisher = self.node.create_publisher(Header, topic, qos)
            self.counts[topic] = 0

            def beat(publisher: Any = publisher, topic: str = topic) -> None:
                message = Header()
                now = time.time()
                message.stamp.sec = int(now)
                message.stamp.nanosec = int((now % 1.0) * 1e9)
                message.frame_id = "ground"
                publisher.publish(message)
                self.counts[topic] += 1

            self.node.create_timer(1.0 / float(entry.get("rate_hz", 2.0)), beat)
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)

    def start(self) -> None:
        """Start publishing."""
        self.thread.start()

    def stop(self) -> None:
        """Stop publishing."""
        self.executor.shutdown()
        self.node.destroy_node()
        self._rclpy.try_shutdown()


# ---------------------------------------------------------------------------------- the main
def load_config(path: Path) -> dict[str, Any]:
    """Read `ground.json`."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point inside the ground container."""
    parser = argparse.ArgumentParser(description="Fieldproof ground station")
    parser.add_argument("--config", required=True, type=Path, help="ground.json of the run")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    sink = Sink(Path(config["output"]))
    links = config.get("links", [])
    probe = config.get("probe", {})
    addresses = resolve({link["vehicle"]: link["host"] for link in links})
    missing = sorted({link["vehicle"] for link in links} - set(addresses))
    for name in missing:
        sink.emit("error", message=f"cannot resolve the link holder of {name}")
    heartbeats = Heartbeats([link["heartbeat"] for link in links if link.get("heartbeat")])
    prober: Prober | None = None
    try:
        prober = Prober(
            addresses,
            sink,
            interval_s=float(probe.get("interval_s", 0.1)),
            window_s=float(probe.get("window_s", 1.0)),
            timeout_s=float(probe.get("timeout_s", 1.0)),
            payload_bytes=int(probe.get("payload_bytes", 16)),
        )
    except OSError as error:
        sink.emit("error", message=f"cannot open an ICMP socket, links are not measured: {error}")
    stop = threading.Event()

    def on_signal(_signum: int, _frame: Any) -> None:
        stop.set()
        if prober is not None:
            prober.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    heartbeats.start()
    sink.emit(
        "ready",
        addresses=addresses,
        heartbeats=sorted(heartbeats.counts),
        probing=prober is not None,
    )
    try:
        if prober is not None:
            prober.run()
        else:
            stop.wait()
    finally:
        sink.emit("stopped", heartbeats=heartbeats.counts)
        heartbeats.stop()
        sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
