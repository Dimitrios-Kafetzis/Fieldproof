"""Tests of the ground station's probe accounting (task P2-T02); the ROS side is integration."""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path
from typing import Any

import pytest

from fieldproof.ground import (
    Prober,
    Probes,
    Sink,
    Window,
    echo_reply_sequence,
    echo_request,
    load_config,
    resolve,
)


class Clock:
    """Monotonic and wall time that move together, only when told to."""

    def __init__(self, wall: float = 1000.0) -> None:
        self.t = 0.0
        self.wall0 = wall

    def monotonic(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.wall0 + self.t


class FakeSocket:
    """An ICMP socket whose peers answer after a delay (None: never)."""

    def __init__(self, clock: Clock, delays: dict[str, float | None]) -> None:
        self.clock = clock
        self.delays = delays
        self.sent: list[tuple[bytes, str]] = []
        self.queue: list[tuple[float, bytes, str]] = []
        self.blocking: bool | None = None

    def setblocking(self, flag: bool) -> None:
        self.blocking = flag

    def sendto(self, packet: bytes, address: tuple[str, int]) -> int:
        host = address[0]
        if host == "unreachable":
            raise OSError("Network is unreachable")
        self.sent.append((packet, host))
        delay = self.delays.get(host)
        if delay is not None:
            reply = struct.pack("!BBHHH", 0, 0, 0, 77, struct.unpack("!H", packet[6:8])[0])
            self.queue.append((self.clock.t + delay, reply + packet[8:], host))
        return len(packet)

    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
        due = [item for item in self.queue if item[0] <= self.clock.t]
        if not due:
            raise BlockingIOError
        item = min(due)
        self.queue.remove(item)
        return item[1], (item[2], 0)


@pytest.fixture
def fake_select(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """`select` on the fake socket: time jumps to the next reply or the timeout."""
    clock = Clock()

    def select(
        read: list[Any], _w: Any, _x: Any, timeout: float
    ) -> tuple[list[Any], list[Any], list[Any]]:
        sock = read[0]
        pending = sorted(t for t, _, _ in sock.queue)
        if pending and pending[0] <= clock.t + timeout:
            clock.t = max(clock.t, pending[0])
            return [sock], [], []
        clock.t += timeout
        return [], [], []

    monkeypatch.setattr("fieldproof.ground.select.select", select)
    return clock


def test_echo_packets() -> None:
    packet = echo_request(258, 16)
    assert len(packet) == 24
    assert packet[:8] == bytes([8, 0, 0, 0, 0, 0, 1, 2])
    assert echo_request(0x1_0001, 0)[6:8] == b"\x00\x01", "16-bit sequence"
    reply = bytes([0, 0, 0xAB, 0xCD, 0, 77, 1, 2]) + bytes(16)
    assert echo_reply_sequence(reply) == 258
    assert echo_reply_sequence(packet) is None, "a request is not a reply"
    assert echo_reply_sequence(b"\x00\x00") is None


def test_window_statistics() -> None:
    window = Window(10.0, 11.0, sent=3)
    window.add(100.0)
    window.add(140.0)
    assert window.to_dict() == {
        "start": 10.0,
        "end": 11.0,
        "sent": 3,
        "received": 2,
        "rtt_sum_ms": 240.0,
        "rtt_sq_sum_ms": 29600.0,
        "rtt_min_ms": 100.0,
        "rtt_max_ms": 140.0,
    }
    assert Window(0.0, 1.0).to_dict()["rtt_min_ms"] is None


def test_probes_account_per_window() -> None:
    probes = Probes("ugv1", window_s=1.0, timeout_s=1.0)
    assert probes.next_sequence() == 1
    probes.sent(1, monotonic=0.0, wall=100.2)
    probes.sent(2, monotonic=0.5, wall=100.7)
    probes.sent(3, monotonic=1.0, wall=101.2)
    assert probes.replied(1, 0.12) == pytest.approx(120.0)
    assert probes.replied(1, 0.13) is None, "answered already"
    assert probes.replied(99, 0.13) is None, "never sent"
    assert probes.replied(3, 2.5) is None, "later than the timeout"
    assert probes.due(101.9) == [], "window 100 waits for its probes' timeout"
    (first,) = probes.due(102.0)
    assert (first.start, first.end, first.sent, first.received) == (100.0, 101.0, 2, 1)
    assert probes.outstanding == {2: (0.5, 100.7, 100)}, "probe 2 is still out"
    (second,) = probes.due(103.5)
    assert (second.start, second.sent, second.received) == (101.0, 1, 0)
    assert probes.outstanding == {}, "probes older than a window and a timeout are forgotten"
    probes.sequence = 0xFFFF
    assert probes.next_sequence() == 0


def test_prober_measures_every_link(tmp_path: Path, fake_select: Clock) -> None:
    clock = fake_select
    sink = Sink(tmp_path / "network.jsonl", clock=clock.wall)
    sock = FakeSocket(clock, {"10.0.0.2": 0.12, "10.0.0.3": None})
    prober = Prober(
        {"ugv1": "10.0.0.2", "ugv2": "10.0.0.3", "ugv3": "unreachable"},
        sink,
        interval_s=0.1,
        window_s=1.0,
        timeout_s=1.0,
        payload_bytes=16,
        sock=sock,  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        wall=clock.wall,
    )
    assert sock.blocking is False
    rounds = 0

    def stop_after_three_seconds() -> None:
        nonlocal rounds
        rounds += 1
        Prober.flush(prober)
        if clock.t >= 3.0:
            prober.stop()

    prober.flush = stop_after_three_seconds  # type: ignore[method-assign]
    prober.run()
    sink.close()
    assert rounds == pytest.approx(30, abs=1)
    assert {host for _, host in sock.sent} == {"10.0.0.2", "10.0.0.3"}
    lines = [json.loads(line) for line in (tmp_path / "network.jsonl").read_text().splitlines()]
    windows = {(w["vehicle"], w["start"]) for w in lines}
    # Windows 1000 and 1001 closed after a window and a timeout; 1002 is still open at t=3.
    assert windows == {(v, s) for v in ("ugv1", "ugv2", "ugv3") for s in (1000.0, 1001.0)}
    for line in lines:
        assert line["kind"] == "link"
        assert line["sent"] == pytest.approx(10, abs=1)
        if line["vehicle"] == "ugv1":
            assert line["received"] == line["sent"]
            assert line["rtt_sum_ms"] / line["received"] == pytest.approx(120.0)
        else:
            assert line["received"] == 0


def test_receive_ignores_strangers_and_requests(fake_select: Clock, tmp_path: Path) -> None:
    clock = fake_select
    sock = FakeSocket(clock, {})
    prober = Prober(
        {"ugv1": "10.0.0.2"},
        Sink(tmp_path / "n.jsonl"),
        interval_s=0.1,
        window_s=1.0,
        timeout_s=1.0,
        payload_bytes=0,
        sock=sock,  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        wall=clock.wall,
    )
    sock.queue = [
        (0.0, echo_request(1, 0), "10.0.0.2"),
        (0.0, bytes([0, 0, 0, 0, 0, 0, 0, 1]), "10.9.9.9"),
    ]
    prober.receive(0.5)
    assert sock.queue == []
    assert prober.probes["ugv1"].windows == {}


def test_sink_resolve_and_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = Sink(tmp_path / "deep" / "network.jsonl", clock=lambda: 12.3456)
    sink.emit("ready", addresses={"ugv1": "10.0.0.2"})
    sink.close()
    assert json.loads((tmp_path / "deep" / "network.jsonl").read_text()) == {
        "kind": "ready",
        "wall": 12.346,
        "addresses": {"ugv1": "10.0.0.2"},
    }
    known = {"ugv1": "10.0.0.2"}

    def lookup(host: str) -> str:
        if host not in known:
            raise socket.gaierror("unknown")
        return known[host]

    monkeypatch.setattr("fieldproof.ground.socket.gethostbyname", lookup)
    assert resolve({"ugv1": "ugv1"}) == {"ugv1": "10.0.0.2"}
    assert resolve({"ugv1": "ugv1", "ugv9": "ugv9"}, timeout_s=0.0) == {"ugv1": "10.0.0.2"}
    config = tmp_path / "ground.json"
    config.write_text(json.dumps({"output": "/x", "links": []}))
    assert load_config(config)["output"] == "/x"
    config.write_text("[]")
    with pytest.raises(ValueError, match="not a JSON object"):
        load_config(config)
