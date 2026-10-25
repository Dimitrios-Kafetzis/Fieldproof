"""Integration tests of the radio links (task P2-T02, its verification).

``test_01``: ping and iperf containers on a link the engine shapes. The engine's own Docker
backend and netem scripts (:mod:`fieldproof.network`) build what a run builds for one vehicle: an
onboard and a radio network, a link holder with ``NET_ADMIN`` on both, a "vehicle" container in
the holder's network namespace, and a "ground" container on the radio network. For every preset
the ground pings the vehicle (the probes the ground station sends: the measured one-way latency,
half the mean round trip, must be within 10 % of the profile plus 5 ms for the host's own round
trip, the definition of done measured on a loaded runner) and runs
``iperf3`` over UDP both ways faster than the link's rate (the received throughput must be the
profile's rate, less its loss and the packets' headers, within 10 %). Then the link drops (no
reply), comes back (replies again), and loses its shaping.

``test_02``: ``fieldproof run examples/scenarios/ugv_link_loss.yaml``, the scenario of the
definition of done: the example vehicle drives along the depot's hall over a rural LTE link
that drops for 30 s. Exit code 0; the seven assertions passed (the vehicle reports the link
lost, holds position and commands zero while it is down, reports it back, and reaches its
goal); every link period the ground station measured passed against its profile (the run
verifies with ``network.verify: fail``); the MCAP holds ``/link_state`` and the
``/fieldproof/network`` side channel; and neither a container nor a network of the run is left.

Needs a kernel with the ``sch_netem`` and ``ifb`` modules (loaded on demand on Ubuntu; CI loads
them explicitly), the stack images (``make images``: the bridge image carries ``tc``, ``ping``
and ``iperf3``), and for ``test_02`` the vehicle image (``make vehicle-image``) and the Fuel
models (``make assets``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fieldproof.backend import ContainerSpec
from fieldproof.config import load_config
from fieldproof.docker_backend import DockerBackend
from fieldproof.network import (
    DROP_PROFILE,
    LATENCY_TOLERANCE,
    PRESETS,
    LinkProfile,
    expected_latency_ms,
    latency_tolerance_ms,
    link_script,
)

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "examples/scenarios/ugv_link_loss.yaml"
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RUN_TIMEOUT_S = 1200.0
PINGS = 100
# iperf3 over UDP: datagrams of this payload, sent this much faster than the link's rate.
UDP_PAYLOAD = 1200
UDP_OVERDRIVE = 1.25
UDP_SECONDS = 6
UDP_ATTEMPTS = 3
UDP_FIRST_PORT = 5301
# What a datagram weighs at the qdisc (Ethernet, IPv4, UDP headers), where netem's rate applies.
UDP_WIRE = 14 + 20 + 8 + UDP_PAYLOAD


def docker(
    *args: str, timeout: float = 120.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (fixed argument list)
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def plain(text: str) -> str:
    return ANSI.sub("", text)


# --------------------------------------------------------------------- ping/iperf on a link
class Link:
    """One vehicle's link, built as a run builds it, with a ground and a vehicle to measure."""

    def __init__(self, backend: DockerBackend, image: str) -> None:
        self.backend = backend
        self.token = uuid.uuid4().hex[:6]
        self.labels = {RUN_LABEL: f"it-network-{self.token}"}
        self.onboard = f"fieldproof-it-{self.token}-onboard"
        self.radio = f"fieldproof-it-{self.token}-radio"
        self.holder = ContainerSpec(
            name=f"fieldproof-it-{self.token}-link",
            image=image,
            role="link",
            command=("sleep", "infinity"),
            labels=self.labels,
            network=self.onboard,
            networks=(self.radio,),
            aliases=("vehicle",),
            hostname="vehicle",
            user="0:0",
            cap_add=("NET_ADMIN",),
        )
        self.vehicle = ContainerSpec(
            name=f"fieldproof-it-{self.token}-vehicle",
            image=image,
            role="vehicle",
            command=("sleep", "infinity"),
            labels=self.labels,
            network_mode=f"container:{self.holder.name}",
        )
        # Root in the ground container only so ping may send faster than every 200 ms.
        self.ground = ContainerSpec(
            name=f"fieldproof-it-{self.token}-ground",
            image=image,
            role="ground",
            command=("sleep", "infinity"),
            labels=self.labels,
            network=self.radio,
            hostname="ground",
            user="0:0",
        )
        self.address = ""
        self.port = UDP_FIRST_PORT

    def up(self) -> None:
        for network in (self.onboard, self.radio):
            self.backend.ensure_network(network, self.labels)
        for spec in (self.holder, self.vehicle, self.ground):
            self.backend.start(spec)
        self.address = self.backend.address(self.holder.name, self.radio)

    def down(self) -> None:
        for spec in (self.ground, self.vehicle, self.holder):
            self.backend.remove(spec.name)
        for network in (self.radio, self.onboard):
            self.backend.remove_network(network)

    def shape(self, profile: LinkProfile | None) -> str:
        result = self.backend.exec(
            self.holder.name, ("bash", "-c", link_script(self.address, profile))
        )
        assert result.ok, result.output
        return result.output

    def ping(self, count: int, interval_s: float = 0.05) -> tuple[int, float | None]:
        """Replies and the mean round trip (ms) of `count` probes from the ground."""
        result = self.backend.exec(
            self.ground.name,
            ("ping", "-n", "-q", "-c", str(count), "-i", str(interval_s), "-W", "1", "-s", "16",
             "vehicle"),
        )  # fmt: skip
        received = re.search(r"(\d+) received", result.output)
        rtt = re.search(r"= [\d.]+/([\d.]+)/", result.output)
        assert received is not None, result.output
        return int(received.group(1)), float(rtt.group(1)) if rtt else None

    def udp(self, rate_kbit: float, reverse: bool) -> dict[str, Any]:
        """iperf3 over UDP from the ground to the vehicle (or back with `reverse`).

        iperf3 opens a UDP test with a handshake datagram, which a lossy link can lose ("unable
        to read from stream socket"): the tool's flake, not the link's. A server stays busy with
        the failed test for a while, so every attempt gets a one-off server on a port of its own.
        """
        for _attempt in range(UDP_ATTEMPTS):
            self.port += 1
            server = self.backend.exec(
                self.vehicle.name,
                ("iperf3", "--server", "--one-off", "--daemon", "--port", str(self.port)),
            )
            assert server.ok, server.output
            argv = [
                "iperf3", "--client", "vehicle", "--port", str(self.port), "--udp", "--json",
                "--bitrate", f"{rate_kbit * UDP_OVERDRIVE:.0f}K", "--length", str(UDP_PAYLOAD),
                "--time", str(UDP_SECONDS),
            ]  # fmt: skip
            if reverse:
                argv.append("--reverse")
            result = self.backend.exec(self.ground.name, argv)
            if result.ok:
                break
        assert result.ok, result.output
        report = json.loads(result.output)
        received = report["end"].get("sum_received") or report["end"]["sum"]
        return {
            "received_kbit": received["bits_per_second"] / 1000.0,
            "lost_percent": report["end"]["sum"].get("lost_percent"),
        }


@pytest.fixture
def link() -> Iterator[Link]:
    config = load_config(REPO / "fieldproof.toml")
    built = Link(DockerBackend(), config.image("bridge"))
    try:
        built.up()
        yield built
    finally:
        built.down()


@pytest.mark.integration
def test_01_ping_and_iperf_on_a_shaped_link(link: Link) -> None:
    received, rtt = link.ping(20)
    assert received == 20
    assert rtt is not None
    assert rtt < 1.0, f"an unshaped bridge network answers in well under a millisecond ({rtt})"
    for name, profile in PRESETS.items():
        applied = link.shape(profile)
        assert "qdisc prio 1:" in applied
        assert "qdisc netem 20:" in applied
        received, rtt = link.ping(PINGS)
        assert rtt is not None, f"{name}: no reply"
        latency = rtt / 2
        expected = expected_latency_ms(profile)
        print(  # noqa: T201
            f"{name}: latency {latency:.2f} ms (profile {expected:.2f} ms), "
            f"{received}/{PINGS} replies",
            flush=True,
        )
        assert abs(latency - expected) <= latency_tolerance_ms(expected), (
            f"{name}: latency {latency:.2f} ms, profile {expected:.2f} ms ± {LATENCY_TOLERANCE:.0%}"
        )
        assert profile.rate_kbit is not None
        # Received throughput: the rate on the wire, less the headers and the profile's loss.
        goodput = profile.rate_kbit * UDP_PAYLOAD / UDP_WIRE * (1 - profile.loss_pct / 100)
        for reverse in (False, True):
            measured = link.udp(profile.rate_kbit, reverse)
            direction = "vehicle to ground" if reverse else "ground to vehicle"
            print(  # noqa: T201
                f"{name} {direction}: {measured['received_kbit']:.0f} kbit/s received "
                f"(expected {goodput:.0f}), {measured['lost_percent']} % lost",
                flush=True,
            )
            assert abs(measured["received_kbit"] - goodput) <= 0.10 * goodput, (
                f"{name} {direction}: {measured['received_kbit']:.0f} kbit/s, expected "
                f"{goodput:.0f} kbit/s ± 10 %"
            )
    link.shape(DROP_PROFILE)
    received, _ = link.ping(20, interval_s=0.1)
    assert received == 0, "a dropped link answers nothing"
    link.shape(PRESETS["lte_rural"])
    received, rtt = link.ping(20)
    assert received >= 15, "the restored link answers again"
    link.shape(None)
    received, rtt = link.ping(20)
    assert received == 20
    assert rtt is not None
    assert rtt < 1.0, "the shaping is gone"


# ------------------------------------------------------------------- the scenario of the DoD
def mcap_topics(path: Path) -> dict[str, int]:
    from mcap.reader import make_reader  # noqa: PLC0415

    counts: dict[str, int] = {}
    with path.open("rb") as stream:
        reader = make_reader(stream)
        summary = reader.get_summary()
        assert summary is not None
        assert summary.statistics is not None
        for channel_id, count in summary.statistics.channel_message_counts.items():
            counts[summary.channels[channel_id].topic] = count
    return counts


@pytest.mark.integration
def test_02_link_loss_scenario(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    completed = subprocess.run(  # noqa: S603  (fixed argument list)
        [*FIELDPROOF, "run", str(SCENARIO), "--runs-dir", str(runs)],
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
        cwd=REPO,
        env={**os.environ},
        check=False,
    )
    output = plain(completed.stdout + completed.stderr)
    print(output[-6000:], flush=True)  # noqa: T201
    (run_dir,) = runs.iterdir()
    record = json.loads((run_dir / "run.json").read_text())
    assert completed.returncode == 0, record.get("reason")
    assert record["status"] == "passed"
    verdicts = {a["name"]: a["status"] for a in record["assertions"]}
    assert verdicts == {
        "reports_link_lost": "passed",
        "holds_while_lost": "passed",
        "commands_zero_while_lost": "passed",
        "reports_link_back": "passed",
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "no_collision": "passed",
    }
    network = record["network"]
    (link,) = network["periods"]
    periods = link["periods"]
    assert [p["profile"] for p in periods] == ["lte_rural", "drop", "lte_rural"]
    initial, dropped, restored = periods
    assert initial["status"] == "passed", initial["reason"]
    assert abs(initial["measured"]["latency_ms"] - 60.0) <= latency_tolerance_ms(60.0)
    assert dropped["status"] == "passed", dropped["reason"]
    assert dropped["measured"]["replies"] <= 3, "at most the probes in flight when the drop began"
    assert dropped["to_s"] - dropped["from_s"] == pytest.approx(30.0, abs=1.5)
    assert restored["status"] in ("passed", "insufficient"), restored["reason"]
    assert network["traffic"]["ugv1"]["out"]["bytes"] > 0
    outage = next(a for a in record["timeline"] if a["name"] == "link_outage")
    assert outage["status"] == "ok"
    assert outage["detail"] == "dropped for 30 s, lte_rural restored"
    report = (run_dir / "report.md").read_text()
    assert "## Radio links" in report
    (mcap,) = sorted((run_dir / "recording").glob("*.mcap"))
    topics = mcap_topics(mcap)
    assert topics.get("/link_state", 0) >= 10
    assert topics.get("/fieldproof/network", 0) >= 20, "a window a second while the run lasted"
    left = docker("ps", "-aq", "--filter", f"label={RUN_LABEL}={record['run_id']}").stdout.split()
    assert left == [], "no container of the run is left"
    networks = docker(
        "network", "ls", "-q", "--filter", f"label={RUN_LABEL}={record['run_id']}"
    ).stdout.split()
    assert networks == [], "no network of the run is left"
