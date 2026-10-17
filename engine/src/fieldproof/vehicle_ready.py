#!/usr/bin/env python3
"""Readiness probe of a Fieldproof vehicle container (task P1-T04).

Evaluates the ``runtime.ready`` conditions of the manifest the image carries
(``/opt/fieldproof/robot.yaml``, named by ``FIELDPROOF_MANIFEST``) against the ROS graph as seen
from inside the container, polling until every condition holds (exit code 0) or the timeout
passes first (exit code 1). The vehicle image runs it as its ``HEALTHCHECK`` with a short timeout;
``fieldproof vehicle wait`` runs it through ``docker exec`` with the manifest's ``ready.timeout_s``
and shows which conditions are still pending::

    fieldproof-vehicle-ready [--manifest FILE] [--timeout S] [--interval S] [--json] [--quiet]

Conditions (``docs/reference/manifest.md``): ``lifecycle`` (the node's ``get_state`` service
answers with the wanted state; a node that offers no service or does not answer within
``LIFECYCLE_TIMEOUT_S`` is pending, which is what a Nav2 server blocked in its activation looks
like), ``topic`` (a publisher, of the type when given), ``service`` (offered, of the type when
given), ``action`` (an action server, recognised by its ``_action/send_goal`` service), ``node``
(in the graph). Names are resolved against the probe's namespace unless absolute.

``fieldproof vehicle dockerfile`` copies this file unchanged into every vehicle image as
``/usr/local/bin/fieldproof-vehicle-ready``, so it depends on nothing but the standard library,
PyYAML and rclpy (all in ``ros-base``): no ``fieldproof`` import. The engine's unit tests drive
:func:`evaluate`, :func:`wait` and :func:`main` with a fake graph; the vehicle and integration
tests run it in the container.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

import yaml

DEFAULT_MANIFEST = "/opt/fieldproof/robot.yaml"
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_INTERVAL_S = 1.0
LIFECYCLE_TIMEOUT_S = 2.0
PROBE_NODE_NAME = "fieldproof_ready_probe"
LIFECYCLE_DEFAULT_STATE = "active"
# What ``lifecycle_states`` reports instead of a state label.
UNAVAILABLE = "unavailable"  # the node offers no get_state service (not in the graph)
UNRESPONSIVE = "unresponsive"  # the service exists but did not answer in time


class Graph(Protocol):
    """What the probe asks of the ROS graph (``RosGraph`` below, a fake in the unit tests)."""

    def lifecycle_states(self, nodes: Sequence[str]) -> dict[str, str]:
        """The state label of each lifecycle node, or ``UNAVAILABLE`` / ``UNRESPONSIVE``."""

    def publishers(self, topic: str) -> list[str]:
        """The message types published on ``topic`` (one entry per publisher)."""

    def services(self) -> dict[str, list[str]]:
        """Every service in the graph, hidden ones included, with its types."""

    def nodes(self) -> list[str]:
        """The fully qualified names of the nodes in the graph."""

    def resolve(self, name: str) -> str:
        """``name`` made absolute against the probe's namespace."""


@dataclass(frozen=True)
class Status:
    """The outcome of one condition."""

    condition: dict[str, Any]
    ok: bool
    detail: str

    @property
    def label(self) -> str:
        """``<name> <kind>``, as the progress lines print it."""
        return describe(self.condition)

    def as_dict(self) -> dict[str, Any]:
        """The JSON form (``--json``)."""
        return {"condition": self.condition, "ok": self.ok, "detail": self.detail}


def kind_of(condition: dict[str, Any]) -> str:
    """The condition's kind (its discriminating key)."""
    for kind in ("lifecycle", "topic", "service", "action", "node"):
        if kind in condition:
            return kind
    raise ValueError(f"unknown ready condition {condition!r}")


def describe(condition: dict[str, Any]) -> str:
    """``/bt_navigator lifecycle``, ``/map topic``, ..."""
    kind = kind_of(condition)
    return f"{condition[kind]} {kind}"


def load_conditions(manifest: Path | str) -> tuple[list[dict[str, Any]], float]:
    """The ``runtime.ready`` conditions and timeout of a manifest file (empty without them)."""
    data = yaml.safe_load(Path(manifest).read_text()) or {}
    runtime = data.get("runtime") or {}
    ready = runtime.get("ready") or {}
    conditions = [dict(c) for c in ready.get("conditions") or []]
    for condition in conditions:
        kind_of(condition)  # raises on a malformed entry
    timeout = float(ready.get("timeout_s", DEFAULT_TIMEOUT_S))
    return conditions, timeout


def evaluate(conditions: Sequence[dict[str, Any]], graph: Graph) -> list[Status]:
    """One evaluation of every condition against the graph, in the manifest's order."""
    lifecycle = [
        graph.resolve(str(c["lifecycle"])) for c in conditions if kind_of(c) == "lifecycle"
    ]
    states = graph.lifecycle_states(lifecycle) if lifecycle else {}
    services: dict[str, list[str]] | None = None
    nodes: list[str] | None = None
    statuses: list[Status] = []
    for condition in conditions:
        kind = kind_of(condition)
        name = graph.resolve(str(condition[kind]))
        wanted_type = condition.get("type")
        if kind == "lifecycle":
            wanted = str(condition.get("state", LIFECYCLE_DEFAULT_STATE))
            state = states.get(name, UNAVAILABLE)
            statuses.append(Status(condition, state == wanted, state))
        elif kind == "topic":
            types = graph.publishers(name)
            statuses.append(Status(condition, *_offered(types, wanted_type, "publisher")))
        elif kind == "service":
            services = graph.services() if services is None else services
            types = services.get(name, [])
            statuses.append(Status(condition, *_offered(types, wanted_type, "server")))
        elif kind == "action":
            services = graph.services() if services is None else services
            types = [
                t.removesuffix("_SendGoal") for t in services.get(f"{name}/_action/send_goal", [])
            ]
            statuses.append(Status(condition, *_offered(types, wanted_type, "server")))
        else:  # node
            nodes = graph.nodes() if nodes is None else nodes
            present = name in nodes
            statuses.append(Status(condition, present, "present" if present else "absent"))
    return statuses


def _offered(types: list[str], wanted: Any, what: str) -> tuple[bool, str]:
    """Whether something is offered (of the wanted type when one is named) and why not."""
    if not types:
        return False, f"no {what}"
    if wanted is not None and str(wanted) not in types:
        return False, f"{what} of type {', '.join(sorted(set(types)))}, not {wanted}"
    return True, f"{what} of type {', '.join(sorted(set(types)))}"


def pending(statuses: Sequence[Status]) -> list[Status]:
    """The conditions that do not hold."""
    return [s for s in statuses if not s.ok]


def summary(statuses: Sequence[Status]) -> str:
    """``/bt_navigator lifecycle inactive, /map topic no publisher`` for the pending ones."""
    return ", ".join(f"{s.label} {s.detail}" for s in pending(statuses))


def wait(
    conditions: Sequence[dict[str, Any]],
    graph: Graph,
    *,
    timeout_s: float,
    interval_s: float = DEFAULT_INTERVAL_S,
    report: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Status]:
    """Poll the graph until every condition holds or ``timeout_s`` passes.

    ``report`` receives one line each time the set of pending conditions changes and a final
    ``ready`` or ``not ready`` line. Returns the last evaluation.
    """
    say = report or (lambda _line: None)
    started = clock()
    last: str | None = None
    while True:
        statuses = evaluate(conditions, graph)
        left = pending(statuses)
        elapsed = clock() - started
        if not left:
            say(f"ready: {len(statuses)} conditions hold after {elapsed:.1f} s")
            return statuses
        line = f"waiting ({len(statuses) - len(left)}/{len(statuses)} ready): {summary(statuses)}"
        if line != last:
            say(line)
            last = line
        if elapsed >= timeout_s:
            say(
                f"not ready after {elapsed:.1f} s: {len(left)} of {len(statuses)} pending: "
                f"{summary(statuses)}"
            )
            return statuses
        sleep(min(interval_s, max(timeout_s - elapsed, 0.0)))


class RosGraph:
    """The graph as rclpy sees it from a probe node (one node for the probe's lifetime)."""

    def __init__(self, name: str = PROBE_NODE_NAME) -> None:
        # Imported here so the engine (no ROS on the host) can import this module for its tests.
        import rclpy  # noqa: PLC0415

        self._rclpy = rclpy
        rclpy.init(args=[])
        self._node: Any = rclpy.create_node(name)
        self._clients: dict[str, Any] = {}

    def close(self) -> None:
        """Destroy the probe node and shut rclpy down."""
        self._node.destroy_node()
        self._rclpy.shutdown()

    def __enter__(self) -> RosGraph:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def resolve(self, name: str) -> str:
        """``name`` made absolute against the probe's namespace."""
        if name.startswith("/"):
            return name
        namespace = str(self._node.get_namespace()).rstrip("/")
        return f"{namespace}/{name}"

    def lifecycle_states(self, nodes: Sequence[str]) -> dict[str, str]:
        """Ask every node's ``get_state`` service at once; see :data:`LIFECYCLE_TIMEOUT_S`."""
        from lifecycle_msgs.srv import GetState  # noqa: PLC0415  (see __init__)

        states: dict[str, str] = {}
        futures: dict[str, Any] = {}
        for node in nodes:
            client = self._clients.get(node)
            if client is None:
                client = self._node.create_client(GetState, f"{node}/get_state")
                self._clients[node] = client
            if not client.service_is_ready():
                states[node] = UNAVAILABLE
                continue
            futures[node] = client.call_async(GetState.Request())
        deadline = time.monotonic() + LIFECYCLE_TIMEOUT_S
        while futures and time.monotonic() < deadline:
            if all(f.done() for f in futures.values()):
                break
            self._rclpy.spin_once(self._node, timeout_sec=0.1)
        for node, future in futures.items():
            if future.done() and future.result() is not None:
                states[node] = str(future.result().current_state.label)
            else:
                self._clients[node].remove_pending_request(future)
                states[node] = UNRESPONSIVE
        return states

    def publishers(self, topic: str) -> list[str]:
        """The message types of the publishers on ``topic``."""
        return [str(info.topic_type) for info in self._node.get_publishers_info_by_topic(topic)]

    def services(self) -> dict[str, list[str]]:
        """Every service with its types, hidden ones included."""
        return {
            str(name): [str(t) for t in types]
            for name, types in self._node.get_service_names_and_types()
        }

    def nodes(self) -> list[str]:
        """The fully qualified node names."""
        return [
            f"{str(namespace).rstrip('/')}/{name}"
            for name, namespace in self._node.get_node_names_and_namespaces()
        ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog="fieldproof-vehicle-ready",
        description="Wait until the manifest's runtime.ready conditions hold in the ROS graph.",
    )
    parser.add_argument(
        "--manifest",
        default=os.environ.get("FIELDPROOF_MANIFEST", DEFAULT_MANIFEST),
        help="the vehicle manifest (default: $FIELDPROOF_MANIFEST, then %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="seconds to wait (default: the manifest's ready.timeout_s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help="seconds between evaluations (default: %(default)s)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the final evaluation as JSON on stdout"
    )
    parser.add_argument("--quiet", action="store_true", help="no progress lines")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    graph_factory: Callable[[], Any] | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Entry point: 0 when every condition holds, 1 on timeout, 2 on a bad manifest."""
    args = parse_args(argv)
    try:
        conditions, timeout_s = load_conditions(args.manifest)
    except (OSError, ValueError, yaml.YAMLError) as error:
        err.write(f"fieldproof-vehicle-ready: {args.manifest}: {error}\n")
        return 2
    timeout_s = args.timeout if args.timeout is not None else timeout_s
    progress = err if args.json else out

    def report(line: str) -> None:
        if not args.quiet:
            progress.write(line + "\n")
            progress.flush()

    if not conditions:
        report("ready: the manifest has no ready conditions")
        if args.json:
            out.write("[]\n")
        return 0
    graph = graph_factory() if graph_factory is not None else RosGraph()
    try:
        statuses = wait(
            conditions, graph, timeout_s=timeout_s, interval_s=args.interval, report=report
        )
    finally:
        close = getattr(graph, "close", None)
        if close is not None:
            close()
    if args.json:
        out.write(json.dumps([s.as_dict() for s in statuses], indent=2) + "\n")
    return 0 if not pending(statuses) else 1


if __name__ == "__main__":
    sys.exit(main())
