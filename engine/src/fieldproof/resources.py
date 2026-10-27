"""The resource monitor (task P2-T05): every container's CPU and memory, once a second.

The engine samples the containers of a run through the backend's statistics stream (the
Docker daemon's ``stats``, the same numbers ``docker stats`` shows) from the moment the
timeline starts until the teardown, one thread per container, and appends one JSON line per
sample to ``resources.jsonl`` in the run directory: ``{"kind": "resource", "wall": ...,
"container": "<name>", "role": "vehicle", "vehicle": "ugv1", "cpu_pct": 142.3, "memory_mb":
812.4, "memory_limit_mb": 2048.0, "pids": 61}``. The runner tails the file and feeds the
lines to the evaluator like the observer's samples (``resource_usage`` assertions, the
``resources`` KPI collector); the observer relays them onto ``/fieldproof/resources`` so the
recording holds them. :func:`usage_from_stats` turns one message of the daemon's stream into
a :class:`~fieldproof.backend.ResourceUsage` the way ``docker stats`` computes it.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fieldproof.backend import Backend, BackendError, ResourceUsage

KIND = "resource"
MIB = 1024.0 * 1024.0


def usage_from_stats(data: Mapping[str, Any]) -> ResourceUsage | None:
    """One message of the daemon's statistics stream as a sample.

    None for the first message, which has no previous CPU reading to compute a percentage
    from.
    """
    cpu = data.get("cpu_stats") or {}
    previous = data.get("precpu_stats") or {}
    total = (cpu.get("cpu_usage") or {}).get("total_usage")
    previous_total = (previous.get("cpu_usage") or {}).get("total_usage")
    system = cpu.get("system_cpu_usage")
    previous_system = previous.get("system_cpu_usage")
    if total is None or previous_total is None or system is None or previous_system is None:
        return None
    cpu_delta = float(total) - float(previous_total)
    system_delta = float(system) - float(previous_system)
    if system_delta <= 0 or cpu_delta < 0:
        return None
    cores = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [])
    cpu_pct = cpu_delta / system_delta * float(cores or 1) * 100.0
    memory = data.get("memory_stats") or {}
    usage = float(memory.get("usage") or 0.0)
    details = memory.get("stats") or {}
    # `docker stats` leaves the page cache out: cgroup v2 reports it as inactive_file, v1 as
    # cache (total_inactive_file on some kernels).
    for key in ("inactive_file", "total_inactive_file", "cache"):
        if key in details:
            usage = max(usage - float(details[key] or 0.0), 0.0)
            break
    limit = memory.get("limit")
    pids = (data.get("pids_stats") or {}).get("current")
    return ResourceUsage(
        cpu_pct=round(cpu_pct, 2),
        memory_bytes=int(usage),
        memory_limit_bytes=int(limit) if limit else None,
        pids=int(pids) if pids is not None else None,
        read_at=str(data["read"]) if data.get("read") else None,
    )


@dataclass(frozen=True)
class Monitored:
    """A container to sample and how its samples are labelled."""

    name: str
    role: str
    vehicle: str | None = None


class ResourceMonitor:
    """Samples the containers on threads and writes `resources.jsonl`."""

    def __init__(
        self,
        backend: Backend,
        path: Path,
        *,
        wall: Callable[[], float] = time.time,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        self.backend = backend
        self.path = path
        self.wall = wall
        self.on_error = on_error
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self.samples = 0
        self.last: dict[str, ResourceUsage] = {}
        """The latest sample per container."""

    @property
    def running(self) -> list[str]:
        """The containers being sampled."""
        return [name for name, thread in self._threads.items() if thread.is_alive()]

    def start(self, containers: list[Monitored]) -> None:
        """Sample these containers until :meth:`stop` (one thread each)."""
        for container in containers:
            if container.name in self._threads:
                continue
            thread = threading.Thread(
                target=self._sample,
                args=(container,),
                name=f"fieldproof-stats-{container.name}",
                daemon=True,
            )
            self._threads[container.name] = thread
            thread.start()

    def _sample(self, container: Monitored) -> None:
        try:
            for usage in self.backend.usage(container.name):
                self.write(container, usage)
        except BackendError as error:
            if self.on_error is not None:
                self.on_error(container.name, str(error))
        except Exception as error:
            if self.on_error is not None:
                self.on_error(container.name, f"{type(error).__name__}: {error}")

    def write(self, container: Monitored, usage: ResourceUsage) -> None:
        """Append one sample (the thread's, or a scripted one)."""
        line = {
            "kind": KIND,
            "wall": round(self.wall(), 3),
            "sim": None,
            "container": container.name,
            "role": container.role,
            "vehicle": container.vehicle,
            "cpu_pct": usage.cpu_pct,
            "memory_mb": round(usage.memory_bytes / MIB, 2),
            "memory_limit_mb": (
                round(usage.memory_limit_bytes / MIB, 2)
                if usage.memory_limit_bytes is not None
                else None
            ),
            "pids": usage.pids,
            "read_at": usage.read_at,
        }
        with self._lock:
            self.last[container.name] = usage
            self.samples += 1
            with self.path.open("a") as file:
                file.write(json.dumps(line) + "\n")

    def join(self, timeout_s: float) -> None:
        """Wait for the streams to end on their own (a backend whose streams are finite)."""
        for thread in self._threads.values():
            thread.join(timeout=timeout_s)

    def stop(self, timeout_s: float = 0.2) -> None:
        """Stop reading the samples.

        A stream ends with its container (the threads are daemons and the teardown closes the
        streams), so a thread blocked on one is left to end then; `timeout_s` bounds the wait
        for each. What a stream still delivers afterwards lands in the file too, so the
        recording holds the teardown, and is not judged.
        """
        self._stop.set()
        for thread in self._threads.values():
            thread.join(timeout=timeout_s)

    @property
    def stopped(self) -> bool:
        """Whether :meth:`stop` was called."""
        return self._stop.is_set()
