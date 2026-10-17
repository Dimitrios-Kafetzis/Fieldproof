"""Check a running stack against the ROS interface contract of a bridge bundle.

``contract.yaml`` (written by :mod:`fieldproof_bridge.generator`) lists every topic of the
vehicle with its type, role, frame, rate and QoS. :class:`ContractChecker` subscribes to the
published topics from an rclpy node and reports, as :class:`Finding` values:

- **graph**: a publisher (or, for the drive command, a subscription) exists with the contract's
  type, and every endpoint uses the contract's reliability and durability (the depth is not
  reported by the graph);
- **frame**: every message received carries the contract's ``frame_id`` (and
  ``child_frame_id``), or, for a TF topic, every transform listed in ``frames``;
- **rate**: the rate measured over the header stamps (simulation time, so a slow renderer does
  not fail it; the first stamp, published when a sensor is created, is discarded) is within a
  tolerance of ``rate_hz``;
- **topics**: no topic other than the contract's (and ROS's own) is visible under the namespace.

``ros2 run fieldproof_bridge contract_check <bundle>`` runs the same checks from a shell; the
launch test of the package runs them against the reference UGV.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from fieldproof_bridge.manifest import Qos

HIDDEN_PREFIX = "_"
ROS_OWN_TOPICS = frozenset({"/parameter_events", "/rosout"})
DEFAULT_SAMPLES = 10
DEFAULT_TOLERANCE = 0.10
DEFAULT_TIMEOUT_S = 300.0


class ContractError(ValueError):
    """The contract file cannot be read."""


@dataclass(frozen=True)
class TopicSpec:
    """One entry of ``contract.yaml``."""

    name: str
    type: str
    role: str
    via: str = ""
    qos: Qos = field(default_factory=Qos)
    frame_id: str | None = None
    child_frame_id: str | None = None
    rate_hz: float | None = None
    frames: tuple[tuple[str, str], ...] | None = None  # (parent or "", child)

    @property
    def is_tf(self) -> bool:
        """Whether the topic is a TF topic, checked per transform."""
        return self.frames is not None


@dataclass
class Contract:
    """The parsed ``contract.yaml``."""

    robot: str
    namespace: str
    frame_prefix: str
    topics: list[TopicSpec]

    @classmethod
    def load(cls, path: Path | str) -> Contract:
        """Read ``contract.yaml`` (or the bundle directory holding it)."""
        file = Path(path)
        if file.is_dir():
            file = file / "contract.yaml"
        try:
            data = yaml.safe_load(file.read_text())
        except FileNotFoundError as error:
            raise ContractError(f"{file}: no such file") from error
        except yaml.YAMLError as error:
            raise ContractError(f"{file}: not valid YAML ({error})") from error
        if not isinstance(data, dict) or data.get("kind") != "BridgeContract":
            raise ContractError(f"{file}: not a BridgeContract")
        topics = []
        for raw in data.get("topics") or []:
            frames = raw.get("frames")
            topics.append(
                TopicSpec(
                    name=str(raw["name"]),
                    type=str(raw["type"]),
                    role=str(raw["role"]),
                    via=str(raw.get("via", "")),
                    qos=Qos.from_mapping(raw.get("qos"), raw["name"]),
                    frame_id=raw.get("frame_id"),
                    child_frame_id=raw.get("child_frame_id"),
                    rate_hz=float(raw["rate_hz"]) if raw.get("rate_hz") is not None else None,
                    frames=(
                        tuple((str(f.get("parent", "")), str(f["child"])) for f in frames)
                        if frames is not None
                        else None
                    ),
                )
            )
        return cls(
            robot=str(data.get("robot", "")),
            namespace=str(data.get("namespace", "")),
            frame_prefix=str(data.get("frame_prefix", "")),
            topics=topics,
        )

    def names(self) -> set[str]:
        """Every topic name of the contract."""
        return {t.name for t in self.topics}


@dataclass(frozen=True)
class Finding:
    """The result of one check on one topic."""

    topic: str
    check: str  # graph | qos | frame | rate | topics
    ok: bool
    detail: str

    def __str__(self) -> str:
        """``ok    /scan rate: 10.02 Hz`` style line."""
        return f"{'ok  ' if self.ok else 'FAIL'}  {self.topic} {self.check}: {self.detail}"


def is_hidden(topic: str) -> bool:
    """ROS hides topics with a name token starting with an underscore (``/ns/_bridge/scan``)."""
    return any(token.startswith(HIDDEN_PREFIX) for token in topic.split("/") if token)


def stamp_seconds(stamp: Any) -> float:
    """A ``builtin_interfaces/Time`` as seconds."""
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


def observations(msg: Any) -> list[tuple[float, str | None, str | None]]:
    """``(stamp, frame_id, child_frame_id)`` for each stamped thing in ``msg``.

    A TF message yields one per transform; a message with a header yields one; a Clock yields
    its time without frames; anything else yields nothing.
    """
    transforms = getattr(msg, "transforms", None)
    if transforms is not None:
        return [
            (stamp_seconds(t.header.stamp), str(t.header.frame_id), str(t.child_frame_id))
            for t in transforms
        ]
    header = getattr(msg, "header", None)
    if header is not None:
        child = getattr(msg, "child_frame_id", None)
        return [(stamp_seconds(header.stamp), str(header.frame_id), child)]
    clock = getattr(msg, "clock", None)
    if clock is not None:
        return [(stamp_seconds(clock), None, None)]
    return []


class Collector:
    """Keeps the observations received on one topic, keyed by ``(frame_id, child_frame_id)``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stamps: dict[tuple[str | None, str | None], list[float]] = {}
        self.count = 0

    def __call__(self, msg: Any) -> None:
        """Record one message."""
        with self.lock:
            self.count += 1
            for stamp, frame, child in observations(msg):
                self.stamps.setdefault((frame, child), []).append(stamp)

    def frame_pairs(self) -> set[tuple[str | None, str | None]]:
        """The frame pairs seen so far."""
        with self.lock:
            return set(self.stamps)

    def samples(self, key: tuple[str | None, str | None] | None = None) -> list[float]:
        """The stamps of ``key`` (or of every key together) in order of arrival."""
        with self.lock:
            if key is not None:
                return list(self.stamps.get(key, []))
            return [s for stamps in self.stamps.values() for s in stamps]


def measured_rate_hz(stamps: list[float]) -> float | None:
    """The rate over ``stamps`` without the first one, or None with fewer than three stamps.

    Gazebo publishes a sensor's first message when the sensor is created, off its update grid, so
    an interval count that includes it is one period short (10/9 of the rate over nine intervals);
    the first stamp is discarded and the rate is ``(n - 1) / (last - first)`` over the rest.
    """
    rest = stamps[1:]
    if len(rest) < 2 or rest[-1] <= rest[0]:
        return None
    return (len(rest) - 1) / (rest[-1] - rest[0])


def _policy_name(value: Any) -> str:
    """``ReliabilityPolicy.BEST_EFFORT`` -> ``best_effort``."""
    return str(getattr(value, "name", value)).lower()


class ContractChecker:
    """Runs the checks of :mod:`fieldproof_bridge.contract` from an rclpy node.

    The node must be spun by the caller (see :func:`spin_in_background`).
    """

    def __init__(
        self,
        node: Any,
        contract: Contract,
        *,
        samples: int = DEFAULT_SAMPLES,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> None:
        """Subscribe to every published topic of ``contract`` with the contract's QoS."""
        from rclpy.qos import HistoryPolicy, QoSProfile  # noqa: PLC0415
        from rosidl_runtime_py.utilities import get_message  # noqa: PLC0415

        from fieldproof_bridge.adapters.common import DURABILITY, RELIABILITY  # noqa: PLC0415

        self.node = node
        self.contract = contract
        self.samples = samples
        self.tolerance = tolerance
        self.collectors: dict[str, Collector] = {}
        self._subscriptions = []
        for spec in contract.topics:
            if spec.role != "publisher":
                continue
            collector = Collector()
            self.collectors[spec.name] = collector
            qos = QoSProfile(
                depth=max(spec.qos.depth, 100),
                reliability=RELIABILITY[spec.qos.reliability],
                durability=DURABILITY[spec.qos.durability],
                history=HistoryPolicy.KEEP_LAST,
            )
            self._subscriptions.append(
                node.create_subscription(get_message(spec.type), spec.name, collector, qos)
            )

    def run(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> list[Finding]:
        """Run every check and return the findings."""
        findings = self.check_graph(timeout_s)
        findings += self.check_messages(timeout_s)
        findings += self.check_topics()
        return findings

    # ------------------------------------------------------------------------------- graph
    def _endpoints(self, spec: TopicSpec) -> list[Any]:
        if spec.role == "publisher":
            return list(self.node.get_publishers_info_by_topic(spec.name))
        return list(self.node.get_subscriptions_info_by_topic(spec.name))

    def check_graph(self, timeout_s: float) -> list[Finding]:
        """Every topic has an endpoint of the right role, type and QoS."""
        findings: list[Finding] = []
        deadline = time.monotonic() + timeout_s
        for spec in self.contract.topics:
            endpoints = self._endpoints(spec)
            while not endpoints and time.monotonic() < deadline:
                time.sleep(0.2)
                endpoints = self._endpoints(spec)
            if not endpoints:
                findings.append(Finding(spec.name, "graph", False, f"no {spec.role} found"))
                continue
            types = {str(e.topic_type) for e in endpoints}
            findings.append(
                Finding(
                    spec.name,
                    "graph",
                    types == {spec.type},
                    f"{len(endpoints)} {spec.role}(s) of type {', '.join(sorted(types))}",
                )
            )
            profiles = {
                (_policy_name(e.qos_profile.reliability), _policy_name(e.qos_profile.durability))
                for e in endpoints
            }
            expected = (spec.qos.reliability, spec.qos.durability)
            findings.append(
                Finding(
                    spec.name,
                    "qos",
                    profiles == {expected},
                    ", ".join(f"{r}/{d}" for r, d in sorted(profiles))
                    + f" (contract {expected[0]}/{expected[1]})",
                )
            )
        return findings

    # ---------------------------------------------------------------------------- messages
    def check_messages(self, timeout_s: float) -> list[Finding]:
        """Frames and rates of every published topic with a frame or a rate in the contract."""
        findings: list[Finding] = []
        deadline = time.monotonic() + timeout_s
        for spec in self.contract.topics:
            if spec.role != "publisher":
                continue
            collector = self.collectors[spec.name]
            if spec.is_tf:
                findings += self._check_tf(spec, collector, deadline)
                continue
            needed = self.samples + 1 if spec.rate_hz is not None else 1
            while collector.count < needed and time.monotonic() < deadline:
                time.sleep(0.1)
            if collector.count < needed:
                findings.append(
                    Finding(spec.name, "rate", False, f"{collector.count} message(s) received")
                )
                continue
            if spec.frame_id is not None:
                keys = collector.frame_pairs()
                expected = (spec.frame_id, spec.child_frame_id)
                seen = {(f, c if spec.child_frame_id is not None else None) for f, c in keys}
                findings.append(
                    Finding(
                        spec.name,
                        "frame",
                        seen == {expected},
                        ", ".join(f"{f}" + (f" -> {c}" if c else "") for f, c in sorted(seen))
                        + f" (contract {expected[0]}"
                        + (f" -> {expected[1]}" if expected[1] else "")
                        + ")",
                    )
                )
            if spec.rate_hz is not None:
                findings.append(self._rate_finding(spec, collector.samples()))
        return findings

    def _check_tf(self, spec: TopicSpec, collector: Collector, deadline: float) -> list[Finding]:
        findings: list[Finding] = []
        frames = spec.frames or ()
        needed = self.samples + 1 if spec.rate_hz is not None else 1

        def key_of(parent: str, child: str) -> tuple[str | None, str | None] | None:
            for frame, kid in collector.frame_pairs():
                if kid == child and (not parent or frame == parent):
                    return (frame, kid)
            return None

        def ready() -> bool:
            return all(
                (k := key_of(p, c)) is not None and len(collector.samples(k)) >= needed
                for p, c in frames
            )

        while not ready() and time.monotonic() < deadline:
            time.sleep(0.1)
        for parent, child in frames:
            key = key_of(parent, child)
            label = f"{parent or '*'} -> {child}"
            if key is None:
                findings.append(Finding(spec.name, "frame", False, f"{label} never received"))
                continue
            findings.append(Finding(spec.name, "frame", True, f"{key[0]} -> {child}"))
            if spec.rate_hz is not None:
                findings.append(self._rate_finding(spec, collector.samples(key), label))
        return findings

    def _rate_finding(self, spec: TopicSpec, stamps: list[float], label: str = "") -> Finding:
        rate = measured_rate_hz(stamps)
        expected = spec.rate_hz or 0.0
        prefix = f"{label}: " if label else ""
        if rate is None:
            return Finding(spec.name, "rate", False, f"{prefix}no usable stamps in {len(stamps)}")
        ok = abs(rate - expected) <= expected * self.tolerance
        return Finding(
            spec.name,
            "rate",
            ok,
            f"{prefix}{rate:.2f} Hz over {len(stamps) - 1} intervals (contract {expected:g} Hz "
            f"±{self.tolerance:.0%})",
        )

    # -------------------------------------------------------------------------------- topics
    def check_topics(self) -> list[Finding]:
        """No topic other than the contract's is visible under the vehicle namespace."""
        prefix = self.contract.namespace + "/" if self.contract.namespace else "/"
        visible = {
            name
            for name, _ in self.node.get_topic_names_and_types()
            if name.startswith(prefix) and not is_hidden(name) and name not in ROS_OWN_TOPICS
        }
        extra = sorted(visible - self.contract.names())
        return [
            Finding(
                prefix,
                "topics",
                not extra,
                "only the contract's topics" if not extra else f"unexpected: {', '.join(extra)}",
            )
        ]


def spin_in_background(node: Any) -> Callable[[], None]:
    """Spin ``node`` in a daemon thread; the returned callable stops it."""
    import rclpy  # noqa: PLC0415

    executor = rclpy.executors.SingleThreadedExecutor()
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True, name="contract-spin")
    thread.start()

    def stop() -> None:
        executor.shutdown(timeout_sec=1.0)
        thread.join(timeout=5.0)

    return stop


def build_parser() -> argparse.ArgumentParser:
    """The command line of ``ros2 run fieldproof_bridge contract_check``."""
    parser = argparse.ArgumentParser(
        prog="contract_check",
        description="Check the running ROS graph against the contract of a bridge bundle.",
    )
    parser.add_argument("bundle", type=Path, help="bundle directory or contract.yaml")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="messages per rate")
    parser.add_argument(
        "--tolerance", type=float, default=DEFAULT_TOLERANCE, help="rate tolerance (fraction)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Console script entry point; exits 1 when a check fails, 2 on a bad contract."""
    import rclpy  # noqa: PLC0415

    args = build_parser().parse_args(argv)
    try:
        contract = Contract.load(args.bundle)
    except ContractError as error:
        sys.stderr.write(f"contract_check: {error}\n")
        return 2
    rclpy.init()
    node = rclpy.create_node("fieldproof_contract_check")
    stop = spin_in_background(node)
    try:
        checker = ContractChecker(node, contract, samples=args.samples, tolerance=args.tolerance)
        findings = checker.run(args.timeout)
    finally:
        stop()
        node.destroy_node()
        rclpy.try_shutdown()
    for finding in findings:
        sys.stdout.write(f"{finding}\n")
    failed = [f for f in findings if not f.ok]
    sys.stdout.write(f"{len(findings) - len(failed)} passed, {len(failed)} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
