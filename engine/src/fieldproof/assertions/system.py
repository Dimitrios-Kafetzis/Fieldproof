"""The system assertions (task P2-T05): ``resource_usage`` and ``lifecycle_state``.

``resource_usage`` reads the engine's resource monitor (:mod:`fieldproof.resources`: one
``resource`` sample per container and second, CPU in percent of one core and memory in MiB);
``lifecycle_state`` reads the observer's ``lifecycle`` samples (a managed node's state, asked
of its ``get_state`` service once a second and taken from its transition events). The
:class:`ResourceTracker` is shared with the KPI collectors.
"""

from __future__ import annotations

from collections import deque
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveFloat, model_validator

from fieldproof.assertions.core import (
    FAILED,
    PASSED,
    AssertionSpec,
    AssertionType,
    Checker,
    Metric,
    PlanContext,
    PlannedAssertion,
    Sample,
    Watch,
    WindowedChecker,
    rounded,
)
from fieldproof.dsl import AssertionBase, Duration
from fieldproof.manifest import Name

LIFECYCLE_STATES = ("unconfigured", "inactive", "active", "finalized")
CONTAINERS = ("vehicle", "bridge")


# ------------------------------------------------------------------------------- the specs
class ResourceUsage(AssertionBase):
    """A container of the vehicle stays within a CPU and memory footprint.

    CPU is the mean over a sliding `window_s` of the monitor's samples (one a second, in
    percent of one core as `docker stats` shows it; a two-core quota is 200), judged once the
    window holds that many seconds of samples; memory is each sample's working set in MiB.
    Fails at the first window (or sample) over a limit between `after_s` and
    `within_s`, and at the end of the timeline if nothing was sampled.
    """

    type: Literal["resource_usage"]
    vehicle: Name = Field(description="The vehicle whose container is measured.")
    container: Literal["vehicle", "bridge"] = Field(
        default="vehicle", description="Which of its containers: the stack's or its bridge."
    )
    max_cpu_pct: PositiveFloat | None = Field(
        default=None, description="CPU limit, in percent of one core, over the window."
    )
    max_memory_mb: PositiveFloat | None = Field(default=None, description="Memory limit, in MiB.")
    window_s: Duration = Field(default=5.0, description="The CPU averaging window.")
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")
    within_s: Duration | None = Field(
        default=None, description="End of the window (absent: the end of the timeline)."
    )

    @model_validator(mode="after")
    def _limits(self) -> ResourceUsage:
        if self.max_cpu_pct is None and self.max_memory_mb is None:
            raise ValueError("give max_cpu_pct or max_memory_mb (or both)")
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        return self


class LifecycleState(AssertionBase):
    """A managed (lifecycle) node of the vehicle is in a state.

    `eventually`: passes at the first sample in `state` after `after_s`, fails at `within_s`
    or the end; `always`: every sample between `after_s` and `within_s` must be in `state`
    (a node whose service is unavailable or unresponsive counts as not), passes when the
    window closes.
    """

    type: Literal["lifecycle_state"]
    vehicle: Name = Field(description="The vehicle the node belongs to.")
    node: str = Field(description="The node (relative to the vehicle's namespace, or absolute).")
    state: Literal["unconfigured", "inactive", "active", "finalized"] = Field(
        description="The lifecycle state wanted."
    )
    mode: Literal["eventually", "always"] = Field(default="eventually")
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")
    within_s: Duration | None = Field(
        default=None, description="The deadline of `eventually`, the end of `always`."
    )

    @model_validator(mode="after")
    def _window(self) -> LifecycleState:
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        return self


# ---------------------------------------------------------------------------- the trackers
class ResourceTracker:
    """A container's `resource` samples: CPU over a sliding window, memory, peaks."""

    def __init__(
        self,
        container: str | None,
        *,
        window_s: float = 5.0,
        after_s: float = 0.0,
        within_s: float | None = None,
    ) -> None:
        self.container = container
        self.window_s = window_s
        self.after_s = after_s
        self.within_s = within_s
        self.cpu: deque[tuple[float, float]] = deque()
        self.samples = 0
        self.cpu_total = 0.0
        self.memory_total = 0.0
        self.peak_cpu: float | None = None
        self.peak_memory: float | None = None
        self.last: dict[str, Any] | None = None

    def on_sample(self, sample: Sample, t: float) -> dict[str, float] | None:
        """A sample of the container inside the window.

        As `cpu_pct`, `cpu_mean_pct` and `memory_mb` (the window mean is None until the
        window is covered).
        """
        if sample.kind != "resource" or sample.data.get("container") != self.container:
            return None
        if t < self.after_s or (self.within_s is not None and t > self.within_s):
            return None
        cpu = float(sample.data.get("cpu_pct") or 0.0)
        memory = float(sample.data.get("memory_mb") or 0.0)
        self.samples += 1
        self.cpu_total += cpu
        self.memory_total += memory
        self.peak_cpu = cpu if self.peak_cpu is None else max(self.peak_cpu, cpu)
        self.peak_memory = memory if self.peak_memory is None else max(self.peak_memory, memory)
        self.cpu.append((t, cpu))
        while self.cpu and self.cpu[0][0] <= t - self.window_s:
            self.cpu.popleft()
        # One sample a second: the window is covered once its samples span window_s seconds
        # counting each as one (three samples at 1, 2 and 3 s cover a 3 s window).
        covered = t - self.cpu[0][0] >= self.window_s - 1.0 - 1e-6
        mean = sum(c for _t, c in self.cpu) / len(self.cpu) if covered else None
        self.last = {"t_s": rounded(t), "cpu_pct": rounded(cpu, 1), "memory_mb": rounded(memory, 1)}
        return {"cpu_pct": cpu, "cpu_mean_pct": mean, "memory_mb": memory}  # type: ignore[dict-item]

    def metrics(self) -> dict[str, Metric]:
        """`peak_cpu_pct`, `mean_cpu_pct`, `peak_memory_mb`, `mean_memory_mb`, `samples`."""
        n = self.samples
        return {
            "peak_cpu_pct": (self.peak_cpu, "%"),
            "mean_cpu_pct": (self.cpu_total / n if n else None, "%"),
            "peak_memory_mb": (self.peak_memory, "MiB"),
            "mean_memory_mb": (self.memory_total / n if n else None, "MiB"),
            "samples": (n, ""),
        }


# ---------------------------------------------------------------------------- the checkers
class ResourceUsageChecker(WindowedChecker):
    """CPU and memory under their limits."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: ResourceUsage = planned.assertion  # type: ignore[assignment]
        self.container = str(planned.resolved.get("container", ""))
        self.tracker = ResourceTracker(
            self.container,
            window_s=self.assertion.window_s,
            after_s=self.after_s,
            within_s=self.within_s,
        )

    def metrics(self) -> dict[str, Metric]:
        """The tracker's peaks and means."""
        return self.tracker.metrics()

    def _stats(self) -> dict[str, Any]:
        return {key: rounded(value, 1) for key, (value, _u) in self.tracker.metrics().items()}

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done:
            return
        usage = self.tracker.on_sample(sample, t)
        if usage is None:
            if self.window_closed(t):
                self.on_tick(t)
            return
        self.note(t, sample, cpu_pct=usage["cpu_pct"], memory_mb=usage["memory_mb"])
        limit_mb = self.assertion.max_memory_mb
        if limit_mb is not None and usage["memory_mb"] > limit_mb:
            self.resolve(
                FAILED,
                f"{self.container} used {usage['memory_mb']:.0f} MiB at {t:.1f} s, over "
                f"{limit_mb:g} MiB",
                t,
                sample.sim,
                violation=self.tracker.last,
                **self._stats(),
            )
            return
        limit_cpu = self.assertion.max_cpu_pct
        mean = usage["cpu_mean_pct"]
        if limit_cpu is not None and mean is not None and mean > limit_cpu:
            self.resolve(
                FAILED,
                f"{self.container} averaged {mean:.0f} % CPU over {self.assertion.window_s:g} s "
                f"at {t:.1f} s, over {limit_cpu:g} %",
                t,
                sample.sim,
                violation={**(self.tracker.last or {}), "cpu_mean_pct": rounded(mean, 1)},
                **self._stats(),
            )

    def _settle(self, t: float) -> None:
        if self.tracker.samples == 0:
            self.resolve(
                FAILED,
                f"no resource sample of {self.container} {self.window_text()}",
                t,
                **self._stats(),
            )
            return
        limits = []
        if self.assertion.max_cpu_pct is not None:
            limits.append(f"CPU {self.assertion.max_cpu_pct:g} %")
        if self.assertion.max_memory_mb is not None:
            limits.append(f"memory {self.assertion.max_memory_mb:g} MiB")
        self.resolve(
            PASSED,
            f"{self.container} peaked at {self.tracker.peak_cpu or 0.0:.0f} % CPU and "
            f"{self.tracker.peak_memory or 0.0:.0f} MiB over {self.tracker.samples} sample(s) "
            f"(limits: {', '.join(limits)})",
            t,
            **self._stats(),
        )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        if not self.done and self.window_closed(t):
            self._settle(t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if not self.done:
            self._settle(t)


class LifecycleStateChecker(WindowedChecker):
    """A node's lifecycle state, eventually or always."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: LifecycleState = planned.assertion  # type: ignore[assignment]
        self.node = str(planned.resolved.get("node", self.assertion.node))
        self.samples = 0
        self.seen: dict[str, int] = {}
        self.last: str | None = None
        self.reached_t: float | None = None

    def metrics(self) -> dict[str, Metric]:
        """`samples`, `reached_at_s`."""
        return {"samples": (self.samples, ""), "reached_at_s": (self.reached_t, "s")}

    def _stats(self) -> dict[str, Any]:
        return {"samples": self.samples, "states": dict(self.seen), "last": self.last}

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done or sample.kind != "lifecycle" or sample.data.get("node") != self.node:
            return
        state = str(sample.data.get("state", ""))
        self.last = state
        if not self.in_window(t):
            if self.window_closed(t):
                self.on_tick(t)
            return
        self.samples += 1
        self.seen[state] = self.seen.get(state, 0) + 1
        self.note(t, sample, state=state)
        wanted = state == self.assertion.state
        if wanted and self.reached_t is None:
            self.reached_t = t
        if self.assertion.mode == "eventually" and wanted:
            self.resolve(
                PASSED,
                f"{self.node} was {state} at {t:.1f} s",
                t,
                sample.sim,
                **self._stats(),
            )
        elif self.assertion.mode == "always" and not wanted:
            self.resolve(
                FAILED,
                f"{self.node} was {state} at {t:.1f} s, not {self.assertion.state}",
                t,
                sample.sim,
                violation={"t_s": rounded(t), "sim_s": rounded(sample.sim), "state": state},
                **self._stats(),
            )

    def _settle(self, t: float) -> None:
        if self.samples == 0:
            self.resolve(
                FAILED,
                f"no lifecycle state of {self.node} was observed {self.window_text()}",
                t,
                **self._stats(),
            )
        elif self.assertion.mode == "always":
            self.resolve(
                PASSED,
                f"{self.node} was {self.assertion.state} in all {self.samples} sample(s) "
                f"{self.window_text()}",
                t,
                **self._stats(),
            )
        else:
            self.resolve(
                FAILED,
                f"{self.node} never reached {self.assertion.state} {self.window_text()} "
                f"(last {self.last})",
                t,
                **self._stats(),
            )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        if not self.done and self.window_closed(t):
            self._settle(t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        if not self.done:
            self._settle(t)


# ------------------------------------------------------------------------------- the types
class ResourceUsageType(AssertionType):
    """`resource_usage`."""

    type: ClassVar[str] = "resource_usage"
    Spec: ClassVar[AssertionSpec] = ResourceUsage
    summary: ClassVar[str] = "A container of the vehicle stays within a CPU and memory footprint."
    metrics: ClassVar[dict[str, str]] = {
        "peak_cpu_pct": "the highest CPU sample (% of one core)",
        "mean_cpu_pct": "the mean CPU (% of one core)",
        "peak_memory_mb": "the highest memory sample (MiB)",
        "mean_memory_mb": "the mean memory (MiB)",
        "samples": "samples judged",
    }

    def plan(self, spec: ResourceUsage, context: PlanContext) -> PlannedAssertion:
        """The container's name."""
        vehicle = context.vehicle(spec.vehicle)
        name = vehicle.container_name if spec.container == "vehicle" else vehicle.bridge_name
        return PlannedAssertion(spec, resolved={"container": name})

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return ResourceUsageChecker(planned)


class LifecycleStateType(AssertionType):
    """`lifecycle_state`."""

    type: ClassVar[str] = "lifecycle_state"
    Spec: ClassVar[AssertionSpec] = LifecycleState
    summary: ClassVar[str] = "A managed node of the vehicle is in a lifecycle state."
    metrics: ClassVar[dict[str, str]] = {
        "samples": "states observed",
        "reached_at_s": "when the state was first seen (s)",
    }

    def plan(self, spec: LifecycleState, context: PlanContext) -> PlannedAssertion:
        """The node's name under the vehicle's namespace."""
        node = context.vehicle(spec.vehicle).topic(spec.node)
        return PlannedAssertion(
            spec, resolved={"node": node}, watch=(Watch("lifecycle", {"node": node}),)
        )

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return LifecycleStateChecker(planned)


ASSERTIONS: tuple[type[AssertionType], ...] = (ResourceUsageType, LifecycleStateType)
