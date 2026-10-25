"""Tests of the resource monitor (task P2-T05): the daemon's statistics as a sample, the
threads writing `resources.jsonl`, the errors reported."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fakes import FakeBackend, FakeContainer

from fieldproof.backend import ContainerSpec, ResourceUsage
from fieldproof.resources import MIB, Monitored, ResourceMonitor, usage_from_stats


class Raising:
    """An iterator that raises on its first step (a stream the daemon refuses)."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __iter__(self) -> Iterator[ResourceUsage]:
        return self

    def __next__(self) -> ResourceUsage:
        raise self.error


def stats(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "read": "2026-10-25T10:00:01.000000000Z",
        "cpu_stats": {
            "cpu_usage": {"total_usage": 3_000_000_000},
            "system_cpu_usage": 160_000_000_000,
            "online_cpus": 16,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1_000_000_000},
            "system_cpu_usage": 144_000_000_000,
        },
        "memory_stats": {
            "usage": 900 * 1024 * 1024,
            "limit": 2 * 1024**3,
            "stats": {"inactive_file": 100 * 1024 * 1024},
        },
        "pids_stats": {"current": 61},
    }
    base.update(overrides)
    return base


def test_usage_from_stats_as_docker_stats_computes_it() -> None:
    usage = usage_from_stats(stats())
    assert usage == ResourceUsage(
        cpu_pct=200.0,
        memory_bytes=800 * 1024 * 1024,
        memory_limit_bytes=2 * 1024**3,
        pids=61,
        read_at="2026-10-25T10:00:01.000000000Z",
    )
    # The first message of a stream has no previous reading.
    assert usage_from_stats(stats(precpu_stats={"cpu_usage": {}})) is None
    assert usage_from_stats({}) is None
    # cgroup v1 reports the page cache as `cache`; without stats the usage stands.
    v1 = usage_from_stats(stats(memory_stats={"usage": 500 * MIB, "stats": {"cache": 100 * MIB}}))
    assert v1 is not None
    assert v1.memory_bytes == 400 * MIB
    assert v1.memory_limit_bytes is None
    bare = usage_from_stats(stats(memory_stats={"usage": 500 * MIB}, pids_stats={}))
    assert bare is not None
    assert bare.memory_bytes == 500 * MIB
    assert bare.pids is None
    # Without online_cpus the per-CPU list gives the core count.
    cores = usage_from_stats(
        stats(
            cpu_stats={
                "cpu_usage": {"total_usage": 3_000_000_000, "percpu_usage": [1, 2]},
                "system_cpu_usage": 160_000_000_000,
            }
        )
    )
    assert cores is not None
    assert cores.cpu_pct == 25.0


def test_monitor_writes_one_line_per_sample(tmp_path: Path) -> None:
    backend = FakeBackend()
    spec = ContainerSpec(name="fieldproof-x-ugv1", image="img", role="vehicle", command=())
    backend.containers["fieldproof-x-ugv1"] = FakeContainer(spec, running=True)
    backend.usage_samples["fieldproof-x-ugv1"] = [
        ResourceUsage(120.5, int(300 * MIB), 2 * 1024**3, 40, "t1"),
        ResourceUsage(130.0, int(310 * MIB), None, None, None),
    ]
    path = tmp_path / "resources.jsonl"
    errors: list[tuple[str, str]] = []
    monitor = ResourceMonitor(
        backend, path, wall=lambda: 1000.0, on_error=lambda c, m: errors.append((c, m))
    )
    monitor.start([Monitored("fieldproof-x-ugv1", "vehicle", "ugv1"), Monitored("gone", "sim")])
    monitor.start([Monitored("fieldproof-x-ugv1", "vehicle", "ugv1")])  # already sampled
    monitor.join(timeout_s=5.0)
    monitor.stop()
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [
        {
            "kind": "resource",
            "wall": 1000.0,
            "sim": None,
            "container": "fieldproof-x-ugv1",
            "role": "vehicle",
            "vehicle": "ugv1",
            "cpu_pct": 120.5,
            "memory_mb": 300.0,
            "memory_limit_mb": 2048.0,
            "pids": 40,
            "read_at": "t1",
        },
        {
            "kind": "resource",
            "wall": 1000.0,
            "sim": None,
            "container": "fieldproof-x-ugv1",
            "role": "vehicle",
            "vehicle": "ugv1",
            "cpu_pct": 130.0,
            "memory_mb": 310.0,
            "memory_limit_mb": None,
            "pids": None,
            "read_at": None,
        },
    ]
    assert monitor.samples == 2
    assert monitor.last["fieldproof-x-ugv1"].cpu_pct == 130.0
    assert errors == [("gone", "gone: no such container")]
    assert backend.usage_requested == ["fieldproof-x-ugv1", "gone"]
    assert monitor.running == []


def test_monitor_reports_unexpected_errors(tmp_path: Path) -> None:
    class Broken(FakeBackend):
        def usage(self, name: str) -> Iterator[ResourceUsage]:
            return Raising(RuntimeError("boom"))

    errors: list[tuple[str, str]] = []
    monitor = ResourceMonitor(
        Broken(), tmp_path / "r.jsonl", on_error=lambda c, m: errors.append((c, m))
    )
    monitor.start([Monitored("x", "sim")])
    monitor.join(timeout_s=5.0)
    monitor.stop()
    assert errors == [("x", "RuntimeError: boom")]
    assert threading.active_count() >= 1
