"""Tests of the radio link profiles, the netem scripts and the link measurements (task P2-T02)."""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from fieldproof.network import (
    DROP_PROFILE,
    FAILED,
    IFB_DEVICE,
    INSUFFICIENT,
    LATENCY_FLOOR_MS,
    PASSED,
    PRESETS,
    LinkMonitor,
    LinkPeriod,
    LinkProfile,
    LinkWindow,
    expected_latency_ms,
    latency_tolerance_ms,
    link_script,
    netem_args,
    traffic,
    traffic_script,
)

LTE = PRESETS["lte_rural"]


def window(
    start: float,
    rtts: list[float],
    sent: int | None = None,
    vehicle: str = "ugv1",
    length: float = 1.0,
) -> dict[str, Any]:
    """A `link` line of network.jsonl with these round trips (ms)."""
    return {
        "kind": "link",
        "wall": start + length + 1.0,
        "vehicle": vehicle,
        "start": start,
        "end": start + length,
        "sent": len(rtts) if sent is None else sent,
        "received": len(rtts),
        "rtt_sum_ms": sum(rtts),
        "rtt_sq_sum_ms": sum(r * r for r in rtts),
        "rtt_min_ms": min(rtts) if rtts else None,
        "rtt_max_ms": max(rtts) if rtts else None,
    }


# ------------------------------------------------------------------------------------ profiles
def test_presets_are_the_documented_three() -> None:
    assert set(PRESETS) == {"wifi_good", "lte_rural", "rf_900mhz_5km"}
    for profile in PRESETS.values():
        assert profile.description
        assert profile.latency_ms > 0
        assert profile.rate_kbit
        # Jitter well inside the latency, so a normal distribution rarely clips at zero.
        assert profile.jitter_ms <= profile.latency_ms / 2


def test_profile_validation() -> None:
    with pytest.raises(ValidationError, match="reorder_pct needs a latency_ms"):
        LinkProfile(reorder_pct=10)
    with pytest.raises(ValidationError, match="jitter_ms needs a latency_ms"):
        LinkProfile(jitter_ms=5)
    with pytest.raises(ValidationError, match="loss_pct"):
        LinkProfile(loss_pct=120)
    with pytest.raises(ValidationError, match="extra"):
        LinkProfile.model_validate({"latency": 5})
    assert LinkProfile().summary() == "0 ms, unlimited rate"
    assert DROP_PROFILE.dropped
    assert DROP_PROFILE.summary() == "dropped (100 % loss)"
    assert LTE.summary() == "60 ms ± 20 ms, 1 % loss, 4000 kbit/s"
    assert (
        LinkProfile(latency_ms=10, reorder_pct=25).summary()
        == "10 ms, 25 % reordered, unlimited rate"
    )


def test_netem_args() -> None:
    assert netem_args(LTE) == [
        "delay",
        "60ms",
        "20ms",
        "distribution",
        "normal",
        "loss",
        "1%",
        "rate",
        "4000kbit",
        "limit",
        "1000",
    ]
    assert netem_args(LinkProfile(latency_ms=10, reorder_pct=25)) == [
        "delay",
        "10ms",
        "reorder",
        "25%",
        "limit",
        "1000",
    ]
    assert netem_args(LinkProfile()) == ["limit", "1000"]
    assert netem_args(DROP_PROFILE) == ["loss", "100%", "limit", "1000"]
    assert "rate" not in netem_args(LTE, rate=False)
    assert netem_args(DROP_PROFILE, rate=False) == ["loss", "100%", "limit", "1000"]


def test_link_script_shapes_both_directions() -> None:
    script = link_script("172.20.0.2", LTE)
    assert script.startswith("set -eu\naddress=172.20.0.2\n")
    # The interface is found by its address, the ifb device made once, inbound redirected to it.
    assert "ip -o -4 addr show" in script
    assert f"ifb={IFB_DEVICE}" in script
    assert 'ip link add "$ifb" type ifb' in script
    assert "matchall action mirred egress redirect dev" in script
    # Both directions get the tree: the probes (ICMP) on band 1 without the rate, the rest on
    # band 2 with the whole profile.
    assert 'for d in "$dev" "$ifb"; do' in script
    assert "root handle 1: prio bands 2 priomap 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1" in script
    assert "u32 match ip protocol 1 0xff flowid 1:1" in script
    probes = "delay 60ms 20ms distribution normal loss 1% limit 1000"
    assert f'tc qdisc replace dev "$d" parent 1:1 handle 10: netem {probes}' in script
    rest = "delay 60ms 20ms distribution normal loss 1% rate 4000kbit limit 1000"
    assert f'tc qdisc replace dev "$d" parent 1:2 handle 20: netem {rest}' in script
    assert script.rstrip().endswith('tc qdisc show dev "$ifb"')
    dropped = link_script("172.20.0.2", DROP_PROFILE)
    assert "parent 1:1 handle 10: netem loss 100% limit 1000" in dropped
    assert "parent 1:2 handle 20: netem loss 100% limit 1000" in dropped
    cleared = link_script("10.0.0.1", None)
    assert 'tc qdisc del dev "$dev" root 2>/dev/null || true' in cleared
    assert "netem" not in cleared
    assert "address='a b'" in link_script("a b", LTE), "quoted for the shell"


def test_traffic_counters() -> None:
    script = traffic_script("172.20.0.2")
    assert 'tc -s -j qdisc show dev "$dev"' in script
    assert f"tc -s -j qdisc show dev {IFB_DEVICE}" in script
    counters = {
        "out": [
            {"kind": "prio", "handle": "1:", "bytes": 9},
            {"kind": "netem", "handle": "10:", "bytes": 5},
            {"kind": "netem", "handle": "20:", "bytes": 2048, "packets": 7, "drops": 1},
        ],
        "in": [{"kind": "netem", "handle": "20:", "bytes": 512, "backlog": 64}],
    }
    assert traffic(counters) == {
        "out": {"bytes": 2048, "packets": 7, "drops": 1, "backlog": 0},
        "in": {"bytes": 512, "packets": 0, "drops": 0, "backlog": 64},
    }
    assert traffic({}) == {}


def test_expected_latency() -> None:
    # The probes skip the rate's queue: the latency, less the share sent at once.
    assert expected_latency_ms(LTE) == 60.0
    assert expected_latency_ms(LinkProfile(latency_ms=10)) == 10.0
    assert expected_latency_ms(LinkProfile(latency_ms=10, reorder_pct=25)) == 7.5
    assert latency_tolerance_ms(60.0) == pytest.approx(6.0)
    assert latency_tolerance_ms(1.0) == LATENCY_FLOOR_MS


# ---------------------------------------------------------------------------------- windows
def test_window_from_sample() -> None:
    parsed = LinkWindow.from_sample(window(100.0, [120.0, 130.0], sent=3))
    assert parsed.vehicle == "ugv1"
    assert (parsed.start, parsed.end, parsed.sent, parsed.received) == (100.0, 101.0, 3, 2)
    assert parsed.rtt_min_ms == 120.0
    empty = LinkWindow.from_sample(window(100.0, [], sent=10))
    assert empty.rtt_min_ms is None
    assert empty.rtt_sum_ms == 0.0
    with pytest.raises(ValueError, match="not a link window"):
        LinkWindow.from_sample({"vehicle": "ugv1"})
    with pytest.raises(ValueError, match="not a link window"):
        LinkWindow.from_sample({**window(1.0, [1.0]), "sent": "many"})


def test_period_measurement_and_verdict() -> None:
    period = LinkPeriod("ugv1", "lte_rural", LTE, start=100.0, cause="initial")
    assert period.measured()["latency_ms"] is None
    assert period.verdict() == (INSUFFICIENT, "0 reply(ies) of 0 probe(s), 20 needed")
    # Round trips of 116 to 124 ms: one-way 60 ms, the profile's 60.12 ms.
    rtts = [116.0, 118.0, 120.0, 122.0, 124.0]
    for second in range(5):
        period.windows.append(LinkWindow.from_sample(window(100.0 + second, rtts, sent=5)))
    measured = period.measured()
    assert measured["probes"] == measured["replies"] == 25
    assert measured["latency_ms"] == 60.0
    assert measured["rtt_mean_ms"] == 120.0
    assert measured["rtt_min_ms"] == 116.0
    assert measured["rtt_max_ms"] == 124.0
    deviation = math.sqrt(sum((r - 120.0) ** 2 for r in rtts) * 5 / 24)
    assert measured["jitter_ms"] == pytest.approx(deviation / math.sqrt(2), abs=1e-3)
    assert measured["loss_pct"] == 0.0
    status, reason = period.verdict()
    assert status == PASSED
    assert reason == "latency 60.00 ms over 25 replies, profile 60.00 ms (± 6.00 ms)"
    # Twice as slow: the profile is not what the link does.
    slow = LinkPeriod("ugv1", "lte_rural", LTE, start=100.0, cause="initial")
    slow.windows = [LinkWindow.from_sample(window(100.0, [240.0] * 25))]
    assert slow.verdict()[0] == FAILED
    # Loss: one way is 1 - sqrt(replies / probes).
    lossy = LinkPeriod("ugv1", "lte_rural", LTE, start=100.0, cause="initial")
    lossy.windows = [LinkWindow.from_sample(window(100.0, [120.0] * 81, sent=100))]
    assert lossy.measured()["loss_pct"] == pytest.approx(10.0)


def test_dropped_period_verdict() -> None:
    period = LinkPeriod("ugv1", "drop", DROP_PROFILE, start=100.0, cause="cut")
    period.windows = [LinkWindow.from_sample(window(100.0, [], sent=5))]
    assert period.verdict()[0] == INSUFFICIENT
    period.windows.append(LinkWindow.from_sample(window(101.0, [], sent=10)))
    assert period.verdict() == (PASSED, "none of 15 probe(s) answered while the link was dropped")
    period.windows.append(LinkWindow.from_sample(window(102.0, [120.0], sent=10)))
    assert period.verdict() == (FAILED, "1 of 25 probe(s) answered while the link was dropped")
    data = period.to_dict(origin=90.0)
    assert data["expected"]["latency_ms"] is None
    assert data["expected"]["loss_pct"] == 100.0
    assert data["from_s"] == 10.0
    assert data["to_s"] is None


# ---------------------------------------------------------------------------------- monitor
def test_monitor_assigns_windows_to_periods() -> None:
    monitor = LinkMonitor({"ugv1": "lte_rural", "ugv2": "wifi_good"})
    monitor.change("ugv1", "lte_rural", LTE, 100.0, "initial")
    monitor.change("ugv2", "wifi_good", PRESETS["wifi_good"], 100.0, "initial")
    monitor.origin = 105.0
    for second in range(100, 110):
        assert monitor.feed(window(second, [120.0] * 10)) is not None
        monitor.feed(window(second, [120.0] * 10, vehicle="ugv2"))
    monitor.change("ugv1", "drop", DROP_PROFILE, 110.5, "cut")
    # The window that straddles the change counts for neither period.
    monitor.feed(window(110.0, [120.0] * 5, sent=10))
    for second in range(111, 131):
        monitor.feed(window(second, [], sent=10))
    monitor.change("ugv1", "lte_rural", LTE, 131.0, "cut (restore)")
    for second in range(131, 135):
        monitor.feed(window(second, [120.0] * 10))
    assert monitor.feed({"kind": "ready"}) is None
    assert monitor.feed({"kind": "link", "vehicle": "ugv1"}) is None
    assert monitor.bad_lines == 1
    assert monitor.feed(window(100.0, [1.0], vehicle="stranger")) is not None
    first, dropped, restored = monitor.links["ugv1"].periods
    assert (first.start, first.end) == (100.0, 110.5)
    assert len(first.windows) == 10
    assert len(dropped.windows) == 20
    assert len(restored.windows) == 4
    assert first.verdict()[0] == PASSED
    assert dropped.verdict()[0] == PASSED
    assert restored.verdict()[0] == PASSED
    # ugv2's link (wifi_good, 5 ms) saw 60 ms: a mismatch.
    ((vehicle, period, reason),) = monitor.failures()
    assert vehicle == "ugv2"
    assert period.profile_name == "wifi_good"
    assert "latency 60.00 ms" in reason
    summary = monitor.to_dict()
    assert summary[0]["vehicle"] == "ugv1"
    assert summary[0]["profile"] == "lte_rural"
    assert summary[0]["current"] == "lte_rural"
    assert [p["cause"] for p in summary[0]["periods"]] == ["initial", "cut", "cut (restore)"]
    assert [p["from_s"] for p in summary[0]["periods"]] == [-5.0, 5.5, 26.0]
    assert summary[0]["periods"][1]["status"] == PASSED
    assert summary[1]["periods"][0]["status"] == FAILED
    empty = LinkMonitor({"ugv3": "lte_rural"})
    assert empty.to_dict() == [
        {"vehicle": "ugv3", "profile": "lte_rural", "current": None, "periods": []}
    ]
