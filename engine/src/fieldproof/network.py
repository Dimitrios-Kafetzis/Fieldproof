"""Radio links between the vehicles and the ground (task P2-T02, ADR-0004).

A scenario with a ``network`` section runs every vehicle behind a radio link. The topology, built
by :mod:`fieldproof.runner`:

- a **radio** network per run, shared by the vehicles and the **ground station** (a container
  of the bridge image running :mod:`fieldproof.ground`): what a vehicle says to the ground or to
  another vehicle crosses it;
- an **onboard** network per vehicle, shared with its bridge (the vehicle's sensors and
  actuators) and the recorder (the run's instruments): what the vehicle says to its own hardware
  never crosses the radio, and the recorder sees the truth;
- a **link holder** per vehicle: a container that owns the vehicle's network namespace (the
  vehicle container joins it, ``--network container:<holder>``) and has ``NET_ADMIN``, so the
  link is shaped from the vehicle's first packet without giving the stack under test any
  privilege. The holder applies the link's **profile** to its radio interface with ``tc netem``,
  outbound on the interface itself and inbound through an ``ifb`` device, so both directions of
  the link carry it: a profile's latency is one way, a round trip takes twice as long.

A :class:`LinkProfile` is latency, jitter, loss, reordering and rate; :data:`PRESETS` names the
usual ones, a scenario adds its own. :func:`link_script` turns a profile into the shell script
the holder runs (``tc qdisc replace`` is idempotent, so a profile changes in place), which is
how the timeline's ``link.set``, ``link.drop`` and ``link.restore`` act.

The ground station probes every link with ICMP echoes and writes one window of probes per second
to ``network.jsonl``; :class:`LinkMonitor` splits the windows into the periods between profile
changes and checks each period's measured latency against its profile
(:func:`expected_latency_ms`, within :data:`LATENCY_TOLERANCE`). The probes skip the queue the
rate builds (a discovery burst of a Nav2 stack fills a 4 Mbit/s link for seconds), so they
measure the profile; :func:`traffic_script` reads what the link carried and dropped.
"""

from __future__ import annotations

import math
import shlex
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveFloat, model_validator

# The ground station's probes: an ICMP echo with this much payload every interval, given up on
# after the timeout, gathered in windows of probes sent during one window.
PROBE_PAYLOAD_BYTES = 16
PROBE_INTERVAL_S = 0.1
PROBE_TIMEOUT_S = 1.0
PROBE_WINDOW_S = 1.0
# The ground station's configuration and its measurements, in the run directory.
GROUND_CONFIG = "ground.json"
NETWORK_FILE = "network.jsonl"
# A period's measured latency must be within 10 % of the profile's (the task's definition of
# done), or within the absolute floor for very short latencies, which the host's scheduling noise
# (tenths of a millisecond under a loaded simulation) would otherwise decide.
LATENCY_TOLERANCE = 0.10
LATENCY_FLOOR_MS = 0.5
# A period is judged on this many replies (a dropped link on this many probes); fewer is
# `insufficient`, not a failure.
MIN_REPLIES = 20
MIN_PROBES = 10
IFB_DEVICE = "ifb-radio"
# netem's queue, in packets: a slow link fills it with a burst (the camera's images, discovery)
# and drops what does not fit, as a radio modem's buffer does.
NETEM_LIMIT = 1000

DROP = "drop"
"""The profile a dropped link carries: every packet lost, both ways."""
DROP_EDGE_TOLERANCE = 1
"""Replies a dropped period may still count: a probe in flight when the drop began (issue #67)."""
PASSED = "passed"
FAILED = "failed"
INSUFFICIENT = "insufficient"


class LinkProfile(BaseModel):
    """How a radio link behaves: each direction delays, varies, loses, reorders and paces."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(default="", description="What the profile stands for.")
    latency_ms: NonNegativeFloat = Field(default=0.0, description="One-way delay, milliseconds.")
    jitter_ms: NonNegativeFloat = Field(
        default=0.0,
        description="Standard deviation of the delay (normal distribution), milliseconds.",
    )
    loss_pct: float = Field(
        default=0.0, ge=0.0, le=100.0, description="Packets lost in each direction, percent."
    )
    reorder_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Packets sent at once, ahead of the delayed ones, percent (needs a latency).",
    )
    rate_kbit: PositiveFloat | None = Field(
        default=None, description="Throughput of each direction, kbit/s (absent: unlimited)."
    )

    @model_validator(mode="after")
    def _consistent(self) -> LinkProfile:
        if self.reorder_pct > 0 and self.latency_ms <= 0:
            raise ValueError("reorder_pct needs a latency_ms: netem reorders delayed packets")
        if self.jitter_ms > 0 and self.latency_ms <= 0:
            raise ValueError("jitter_ms needs a latency_ms to vary")
        return self

    @property
    def dropped(self) -> bool:
        """Whether nothing gets through."""
        return self.loss_pct >= 100.0

    def summary(self) -> str:
        """`60 ms ± 20 ms, 1 % loss, 4000 kbit/s` (what the log shows)."""
        if self.dropped:
            return "dropped (100 % loss)"
        parts = [
            f"{self.latency_ms:g} ms" + (f" ± {self.jitter_ms:g} ms" if self.jitter_ms else "")
        ]
        if self.loss_pct:
            parts.append(f"{self.loss_pct:g} % loss")
        if self.reorder_pct:
            parts.append(f"{self.reorder_pct:g} % reordered")
        parts.append(f"{self.rate_kbit:g} kbit/s" if self.rate_kbit else "unlimited rate")
        return ", ".join(parts)


PRESETS: dict[str, LinkProfile] = {
    "wifi_good": LinkProfile(
        description="802.11ac in line of sight of the access point, light contention",
        latency_ms=5.0,
        jitter_ms=2.0,
        loss_pct=0.1,
        rate_kbit=50_000.0,
    ),
    "lte_rural": LinkProfile(
        description="LTE at the edge of a rural cell: long and variable delay, a few Mbit/s up",
        latency_ms=60.0,
        jitter_ms=20.0,
        loss_pct=1.0,
        rate_kbit=4_000.0,
    ),
    "rf_900mhz_5km": LinkProfile(
        description="900 MHz telemetry modem at 5 km: low throughput, frequent loss",
        latency_ms=40.0,
        jitter_ms=10.0,
        loss_pct=3.0,
        rate_kbit=200.0,
    ),
}
DROP_PROFILE = LinkProfile(description="the link is down", loss_pct=100.0)


# ---------------------------------------------------------------------------------- tc netem
def netem_args(profile: LinkProfile, *, rate: bool = True) -> list[str]:
    """The `tc qdisc ... netem` parameters of a profile (without its rate when `rate` is off)."""
    if profile.dropped:
        return ["loss", "100%", "limit", str(NETEM_LIMIT)]
    args: list[str] = []
    if profile.latency_ms > 0:
        args += ["delay", f"{profile.latency_ms:g}ms"]
        if profile.jitter_ms > 0:
            args += [f"{profile.jitter_ms:g}ms", "distribution", "normal"]
    if profile.loss_pct > 0:
        args += ["loss", f"{profile.loss_pct:g}%"]
    if profile.reorder_pct > 0:
        args += ["reorder", f"{profile.reorder_pct:g}%"]
    if rate and profile.rate_kbit is not None:
        args += ["rate", f"{profile.rate_kbit:g}kbit"]
    return [*args, "limit", str(NETEM_LIMIT)]


def link_script(address: str, profile: LinkProfile | None) -> str:
    """The shell script a link holder runs to put `profile` on its radio interface.

    `address` is the holder's address on the radio network, which names the interface (Docker
    does not promise interface names). The first run creates the `ifb` device and redirects the
    interface's inbound traffic to it. Each direction (the interface outbound, the `ifb` device
    for inbound) gets the same tree: a two-band `prio` root whose first band, the ground
    station's ICMP probes, goes through the profile's delay, jitter, loss and reordering, and
    whose second band, everything else, goes through the whole profile, rate included. The
    probes thus measure what the profile sets, not the queue the traffic builds behind the rate
    (which the link's counters show, :func:`traffic_script`). Every run replaces the netem
    queues, so the same script sets, drops and restores a link. `None` removes the shaping.
    """
    lines = [
        "set -eu",
        f"address={shlex.quote(address)}",
        'dev=$(ip -o -4 addr show | awk -v a="$address" \'{split($4, p, "/"); '
        "if (p[1] == a) {print $2; exit}}')",
        'dev="${dev%%@*}"',
        'if [ -z "$dev" ]; then echo "no interface has the address $address" >&2; exit 3; fi',
        f"ifb={IFB_DEVICE}",
        'if ! ip link show "$ifb" >/dev/null 2>&1; then',
        '  ip link add "$ifb" type ifb',
        '  ip link set "$ifb" up',
        "fi",
        'if ! tc qdisc show dev "$dev" ingress | grep -q ingress; then',
        '  tc qdisc add dev "$dev" handle ffff: ingress',
        '  tc filter add dev "$dev" parent ffff: matchall action mirred egress redirect dev "$ifb"',
        "fi",
    ]
    if profile is None:
        lines += [
            'tc qdisc del dev "$dev" root 2>/dev/null || true',
            'tc qdisc del dev "$ifb" root 2>/dev/null || true',
        ]
    else:
        probes = shlex.join(netem_args(profile, rate=False))
        traffic = shlex.join(netem_args(profile))
        lines += [
            'for d in "$dev" "$ifb"; do',
            '  if ! tc qdisc show dev "$d" root | grep -q "qdisc prio 1:"; then',
            '    tc qdisc replace dev "$d" root handle 1: prio bands 2 '
            "priomap 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1",
            '    tc filter add dev "$d" parent 1: protocol ip prio 1 u32 '
            "match ip protocol 1 0xff flowid 1:1",
            "  fi",
            f'  tc qdisc replace dev "$d" parent 1:1 handle 10: netem {probes}',
            f'  tc qdisc replace dev "$d" parent 1:2 handle 20: netem {traffic}',
            "done",
        ]
    lines.append('echo "$dev"; tc qdisc show dev "$dev"; tc qdisc show dev "$ifb"')
    return "\n".join(lines) + "\n"


def traffic_script(address: str) -> str:
    """The shell script that prints a link's queue counters as JSON (`tc -s -j`), both ways."""
    return (
        "\n".join(
            [
                "set -eu",
                f"address={shlex.quote(address)}",
                'dev=$(ip -o -4 addr show | awk -v a="$address" \'{split($4, p, "/"); '
                "if (p[1] == a) {print $2; exit}}')",
                'dev="${dev%%@*}"',
                'echo "{\\"out\\": $(tc -s -j qdisc show dev "$dev"), '
                f'\\"in\\": $(tc -s -j qdisc show dev {IFB_DEVICE})}}"',
            ]
        )
        + "\n"
    )


def traffic(counters: dict[str, Any]) -> dict[str, dict[str, int]]:
    """What crossed a link, from :func:`traffic_script`'s output.

    Per direction, the traffic band's bytes, packets and drops (loss and queue overflow) and the
    backlog left.
    """
    found: dict[str, dict[str, int]] = {}
    for direction in ("out", "in"):
        for qdisc in counters.get(direction) or []:
            if qdisc.get("kind") == "netem" and str(qdisc.get("handle")) == "20:":
                found[direction] = {
                    key: int(qdisc.get(key) or 0)
                    for key in ("bytes", "packets", "drops", "backlog")
                }
    return found


def expected_latency_ms(profile: LinkProfile) -> float:
    """The mean one-way delay a probe should see on a link with `profile`.

    netem holds every probe for the profile's latency (normally distributed around it, so the
    mean is the latency), except the reordered share, which leaves at once. The probes do not
    queue behind the rate (:func:`link_script`).
    """
    return profile.latency_ms * (1.0 - profile.reorder_pct / 100.0)


def latency_tolerance_ms(expected_ms: float) -> float:
    """How far a period's measured latency may be from the expected one."""
    return max(expected_ms * LATENCY_TOLERANCE, LATENCY_FLOOR_MS)


# ------------------------------------------------------------------------------ measurement
@dataclass(frozen=True)
class LinkWindow:
    """The ground station's probes of one link sent during one window (a `link` sample)."""

    vehicle: str
    start: float
    """Wall-clock time the window opened (epoch seconds)."""
    end: float
    sent: int
    received: int
    rtt_sum_ms: float = 0.0
    rtt_sq_sum_ms: float = 0.0
    rtt_min_ms: float | None = None
    rtt_max_ms: float | None = None

    @classmethod
    def from_sample(cls, data: dict[str, Any]) -> LinkWindow:
        """A window from a line of `network.jsonl` (`kind: link`); raises ValueError."""
        try:
            return cls(
                vehicle=str(data["vehicle"]),
                start=float(data["start"]),
                end=float(data["end"]),
                sent=int(data["sent"]),
                received=int(data["received"]),
                rtt_sum_ms=float(data.get("rtt_sum_ms") or 0.0),
                rtt_sq_sum_ms=float(data.get("rtt_sq_sum_ms") or 0.0),
                rtt_min_ms=_optional(data.get("rtt_min_ms")),
                rtt_max_ms=_optional(data.get("rtt_max_ms")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"not a link window: {error}") from error


def _optional(value: Any) -> float | None:
    return None if value is None else float(value)


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


@dataclass
class LinkPeriod:
    """A link under one profile, from one change to the next, and what was measured in it."""

    vehicle: str
    profile_name: str
    profile: LinkProfile
    start: float
    cause: str
    """What set the profile: `initial`, or the timeline entry's label."""
    end: float | None = None
    windows: list[LinkWindow] = field(default_factory=list)
    judged_from: float | None = None
    """Windows before this time are kept but not judged: the initial period's vehicle bring-up,
    when the stack's discovery traffic saturates a slow link (issue #67), is not a measurement
    of the link."""

    def holds(self, window: LinkWindow) -> bool:
        """Whether the window's probes were all sent under this period's profile."""
        return window.start >= self.start and (self.end is None or window.end <= self.end)

    def judged(self) -> list[LinkWindow]:
        """The windows the verdict is drawn from (from `judged_from` on, when set)."""
        if self.judged_from is None:
            return list(self.windows)
        return [w for w in self.windows if w.start >= self.judged_from]

    def measured(self) -> dict[str, Any]:
        """Probes, replies, one-way latency, jitter and loss estimated from the round trips.

        Both directions carry the profile, so the one-way latency is half the mean round trip,
        the jitter the round trips' deviation over sqrt(2) (two independent delays), and the
        one-way loss `1 - sqrt(replies / probes)`.
        """
        windows = self.judged()
        sent = sum(w.sent for w in windows)
        received = sum(w.received for w in windows)
        rtt_sum = sum(w.rtt_sum_ms for w in windows)
        rtt_sq = sum(w.rtt_sq_sum_ms for w in windows)
        mins = [w.rtt_min_ms for w in windows if w.rtt_min_ms is not None]
        maxs = [w.rtt_max_ms for w in windows if w.rtt_max_ms is not None]
        mean = rtt_sum / received if received else None
        deviation = None
        if received > 1 and mean is not None:
            deviation = math.sqrt(
                max(rtt_sq / received - mean * mean, 0.0) * received / (received - 1)
            )
        return {
            "windows": len(windows),
            "probes": sent,
            "replies": received,
            "latency_ms": _round(mean / 2 if mean is not None else None),
            "jitter_ms": _round(deviation / math.sqrt(2) if deviation is not None else None),
            "loss_pct": _round(100.0 * (1.0 - math.sqrt(received / sent)) if sent else None, 2),
            "rtt_mean_ms": _round(mean),
            "rtt_min_ms": _round(min(mins) if mins else None),
            "rtt_max_ms": _round(max(maxs) if maxs else None),
        }

    def verdict(self) -> tuple[str, str]:
        """`passed`, `failed` or `insufficient`, and why."""
        measured = self.measured()
        sent, received = measured["probes"], measured["replies"]
        if self.profile.dropped:
            if sent < MIN_PROBES:
                return INSUFFICIENT, f"{sent} probe(s) while dropped, {MIN_PROBES} needed"
            if received > DROP_EDGE_TOLERANCE:
                return FAILED, f"{received} of {sent} probe(s) answered while the link was dropped"
            answered = (
                f"{received} of {sent} probe(s) answered at the edge of the drop (tolerated)"
                if received
                else f"none of {sent} probe(s) answered while the link was dropped"
            )
            return PASSED, answered
        if received < MIN_REPLIES:
            return INSUFFICIENT, f"{received} reply(ies) of {sent} probe(s), {MIN_REPLIES} needed"
        expected = expected_latency_ms(self.profile)
        tolerance = latency_tolerance_ms(expected)
        latency = float(measured["latency_ms"])
        text = (
            f"latency {latency:.2f} ms over {received} replies, profile {expected:.2f} ms "
            f"(± {tolerance:.2f} ms)"
        )
        if abs(latency - expected) <= tolerance:
            return PASSED, text
        return FAILED, text

    def to_dict(self, origin: float | None = None) -> dict[str, Any]:
        """The period as the record carries it (times relative to `origin` when given)."""
        status, reason = self.verdict()
        expected = None if self.profile.dropped else expected_latency_ms(self.profile)

        def rel(t: float | None) -> float | None:
            return None if t is None or origin is None else round(t - origin, 3)

        return {
            "profile": self.profile_name,
            "cause": self.cause,
            "from_s": rel(self.start),
            "to_s": rel(self.end),
            "judged_from_s": rel(self.judged_from),
            "expected": {
                **self.profile.model_dump(exclude={"description"}),
                "latency_ms": _round(expected),
            },
            "measured": self.measured(),
            "status": status,
            "reason": reason,
        }


@dataclass
class LinkState:
    """One vehicle's link: its configured profile and the periods it went through."""

    vehicle: str
    configured: str
    """The profile the scenario gives the link, which `link.restore` brings back."""
    periods: list[LinkPeriod] = field(default_factory=list)

    @property
    def current(self) -> LinkPeriod | None:
        """The period in force."""
        return self.periods[-1] if self.periods else None


class LinkMonitor:
    """The links of a run: profile changes and the ground station's windows, period by period."""

    def __init__(self, configured: dict[str, str]) -> None:
        self.links = {name: LinkState(name, profile) for name, profile in configured.items()}
        self.bad_lines = 0
        self.origin: float | None = None
        """Wall-clock time the timeline started (period times in the record are relative)."""

    def start_timeline(self, wall: float) -> None:
        """The timeline starts: the record's origin, and where the initial periods are judged from.

        The windows before it cover the vehicles' bring-up, when a stack's discovery traffic can
        saturate a slow link (issue #67); they stay in the record but not in the verdict.
        """
        self.origin = wall
        for link in self.links.values():
            for period in link.periods:
                if period.cause == "initial":
                    period.judged_from = wall

    def change(
        self,
        vehicle: str,
        profile_name: str,
        profile: LinkProfile,
        wall: float,
        cause: str,
        *,
        since: float | None = None,
    ) -> LinkPeriod:
        """A new profile is in force on the vehicle's link from `wall` on.

        `since` is when the change began (before the holder ran its script): the previous profile
        held until then, and windows between the two times count for neither.
        """
        link = self.links[vehicle]
        if link.current is not None and link.current.end is None:
            link.current.end = wall if since is None else min(since, wall)
        period = LinkPeriod(vehicle, profile_name, profile, wall, cause)
        link.periods.append(period)
        return period

    def feed(self, data: dict[str, Any]) -> LinkWindow | None:
        """A sample of `network.jsonl`: a `link` window goes to the period that holds it."""
        if data.get("kind") != "link":
            return None
        try:
            window = LinkWindow.from_sample(data)
        except ValueError:
            self.bad_lines += 1
            return None
        link = self.links.get(window.vehicle)
        if link is None:
            return window
        for period in reversed(link.periods):
            if period.holds(window):
                period.windows.append(window)
                break
        return window

    def failures(self) -> list[tuple[str, LinkPeriod, str]]:
        """Every period whose measurement contradicts its profile: (vehicle, period, reason)."""
        found = []
        for link in self.links.values():
            for period in link.periods:
                status, reason = period.verdict()
                if status == FAILED:
                    found.append((link.vehicle, period, reason))
        return found

    def to_dict(self) -> list[dict[str, Any]]:
        """Every link with its periods (`run.json` `network.links`)."""
        return [
            {
                "vehicle": link.vehicle,
                "profile": link.configured,
                "current": link.current.profile_name if link.current else None,
                "periods": [p.to_dict(self.origin) for p in link.periods],
            }
            for link in self.links.values()
        ]
