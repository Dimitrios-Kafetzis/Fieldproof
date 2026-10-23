"""Clock management on the engine's side (task P2-T04, ADR-0006; docs/reference/time.md).

Two helpers the runner drives with the observer's ``clock`` samples:

- :class:`ClockMonitor` measures the run against its ``time`` section in every mode: the
  real-time factor over the metrics windows, the drift between simulation time and the wall
  clock at the target pace since the timeline started (paused periods excluded), the windows
  outside the tolerance; in the real-time-locked mode a drift past ``max_drift_s`` is the
  verdict the run fails or warns on. Its summary is ``run.json``'s ``clock`` and the report's;
- :class:`LockstepControl` speaks the lockstep protocol with the observer's clock manager
  (:class:`fieldproof.observer.ClockManager`): it appends the engine's commands to the clock
  file of the run directory (``lockstep`` to pause the world at the origin, ``run_until`` with
  how far the stepping may go, ``release`` at the end) and reads the manager's ``clock``
  samples back (the origin once locked, the step reached, a hold, the vehicles still to
  acknowledge, a timeout).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fieldproof.assertions import Sample
from fieldproof.observer import (
    ACK_TOPIC,
    CLOCK_FILE,
    CMD_LOCKSTEP,
    CMD_RELEASE,
    CMD_RUN_UNTIL,
)
from fieldproof.scenario import Time

__all__ = ["ACK_TOPIC", "CLOCK_FILE", "ClockMonitor", "LockstepControl"]

LOCKED = "locked"
DRIFTED = "drifted"
HOLD_MAX_S = 10.0
"""Wall-clock seconds an event may hold the lockstep clock while it starts, at most."""
ORIGIN_TIMEOUT_S = 30.0
"""Wall-clock seconds the lockstep origin may take to be acknowledged."""


# ------------------------------------------------------------------------------ the monitor
@dataclass
class ClockMonitor:
    """The clock of a run measured against its `time` section."""

    time: Time
    world_rtf: float = 1.0
    origin_wall: float | None = None
    origin_sim: float | None = None
    last_wall: float | None = None
    last_sim: float | None = None
    paused_s: float = 0.0
    """Wall-clock seconds the world was paused for (a `sim.pause`), excluded from the drift."""
    _last_paused_wall: float | None = field(default=None, repr=False)
    drift_s: float = 0.0
    max_abs_drift_s: float = 0.0
    rtfs: list[float] = field(default_factory=list)
    violations: int = 0
    exceeded: bool = False
    """Whether the drift went past `max_drift_s` at some point (the real-time-locked mode)."""

    @property
    def target_rtf(self) -> float:
        """The real-time factor the run is measured against."""
        target = self.time.target_rtf
        return self.world_rtf if target is None else target

    def start(self, wall: float, sim: float) -> None:
        """The timeline's clock origin, when it is known before the first sample (lockstep)."""
        self.origin_wall, self.origin_sim = wall, sim
        self.last_wall, self.last_sim = wall, sim

    def feed(self, sample: Sample) -> str | None:
        """A sample of the stream during the timeline; a warning when a window drifted."""
        if sample.kind != "clock" or sample.sim is None:
            return None
        paused = bool(sample.data.get("paused"))
        if paused:
            if self._last_paused_wall is not None:
                self.paused_s += max(sample.wall - self._last_paused_wall, 0.0)
            self._last_paused_wall = sample.wall
        else:
            self._last_paused_wall = None
        if self.origin_wall is None or self.origin_sim is None:
            self.origin_wall, self.origin_sim = sample.wall, sample.sim
        self.last_wall, self.last_sim = sample.wall, sample.sim
        target = self.target_rtf
        self.drift_s = round(
            (sample.sim - self.origin_sim)
            - (sample.wall - self.origin_wall - self.paused_s) * target,
            3,
        )
        self.max_abs_drift_s = max(self.max_abs_drift_s, abs(self.drift_s))
        if self.time.mode == "realtime" and abs(self.drift_s) > self.time.max_drift_s:
            self.exceeded = True
        rtf = sample.data.get("rtf")
        if paused or rtf is None or self.time.mode == "lockstep":
            return None
        rtf = float(rtf)
        self.rtfs.append(rtf)
        if abs(rtf - target) > self.time.tolerance:
            self.violations += 1
            return (
                f"real-time factor {rtf:.3f} over the last window, target {target:g} "
                f"± {self.time.tolerance:g}; drift {self.drift_s:+.3f} s"
            )
        return None

    @property
    def verdict(self) -> str | None:
        """`locked` or `drifted` in the real-time-locked mode, None otherwise."""
        if self.time.mode != "realtime":
            return None
        return DRIFTED if self.exceeded else LOCKED

    def summary(self) -> dict[str, Any]:
        """The clock of the run as data (`run.json` `clock`, the report)."""
        sim_elapsed = (
            round(self.last_sim - self.origin_sim, 3)
            if self.last_sim is not None and self.origin_sim is not None
            else None
        )
        wall_elapsed = (
            round(self.last_wall - self.origin_wall, 3)
            if self.last_wall is not None and self.origin_wall is not None
            else None
        )
        rtfs = self.rtfs
        return {
            "mode": self.time.mode,
            "target_rtf": self.target_rtf,
            "tolerance": self.time.tolerance,
            "max_drift_s": self.time.max_drift_s if self.time.mode == "realtime" else None,
            "on_drift": self.time.on_drift if self.time.mode == "realtime" else None,
            "origin_sim_s": self.origin_sim,
            "sim_elapsed_s": sim_elapsed,
            "wall_elapsed_s": wall_elapsed,
            "paused_s": round(self.paused_s, 3),
            "drift_s": self.drift_s if self.origin_sim is not None else None,
            "max_abs_drift_s": round(self.max_abs_drift_s, 3),
            "rtf": {
                "windows": len(rtfs),
                "mean": round(sum(rtfs) / len(rtfs), 3) if rtfs else None,
                "min": round(min(rtfs), 3) if rtfs else None,
                "max": round(max(rtfs), 3) if rtfs else None,
            },
            "violations": self.violations,
            "verdict": self.verdict,
        }


# ------------------------------------------------------------------------------ the lockstep
class LockstepControl:
    """The engine's side of the lockstep protocol."""

    def __init__(self, run_dir: Path, vehicles: list[str], step_s: float) -> None:
        self.path = run_dir / CLOCK_FILE
        self.vehicles = vehicles
        self.step_s = step_s
        self.origin: float | None = None
        self.t_s: float | None = None
        """The latest simulation time reached, as seconds after the origin."""
        self.held = False
        self.hold_t: float | None = None
        """The limit last asked for (None: none asked yet; inf: no limit)."""
        self.waiting: list[str] = []
        self.waited_s = 0.0
        self.acks: dict[str, float] = {}
        self.timeout: dict[str, Any] | None = None
        self.released = False
        self.steps = 0
        self.holds = 0
        self.max_wait_s = 0.0
        self.commands = 0

    def write(self, cmd: str, **fields: Any) -> None:
        """Append one command."""
        with self.path.open("a") as file:
            file.write(json.dumps({"cmd": cmd, **fields}) + "\n")
        self.commands += 1

    def begin(self) -> None:
        """Pause the world at the next step boundary."""
        self.write(CMD_LOCKSTEP)

    def run_until(self, t_s: float) -> bool:
        """Let the stepping reach `t_s` after the origin (inf: no limit); False if unchanged."""
        if self.hold_t is not None and (
            self.hold_t == t_s or (math.isinf(self.hold_t) and math.isinf(t_s))
        ):
            return False
        self.hold_t = t_s
        self.write(CMD_RUN_UNTIL, t_s=None if math.isinf(t_s) else round(t_s, 6))
        return True

    def release(self) -> None:
        """Let the world run free."""
        if not self.released:
            self.released = True
            self.write(CMD_RELEASE)

    @property
    def acknowledged(self) -> bool:
        """Whether every vehicle's agent has acknowledged at least once."""
        return all(v in self.acks for v in self.vehicles)

    def feed(self, sample: Sample) -> str | None:  # noqa: PLR0911
        """A sample of the stream; what it told, in a few words, when it is the protocol's."""
        if sample.kind == "lockstep.timeout":
            self.timeout = dict(sample.data)
            missing = ", ".join(str(v) for v in sample.data.get("missing", [])) or "anyone"
            return (
                f"step {sample.data.get('step')} (sim {sample.sim}) not acknowledged by "
                f"{missing} within {sample.data.get('waited_s')} s"
            )
        if sample.kind != "clock":
            return None
        acks = sample.data.get("acks")
        if isinstance(acks, dict):
            self.acks.update({str(k): float(v) for k, v in acks.items()})
        state = sample.data.get("lockstep")
        if not isinstance(state, dict):
            return None
        self.steps = int(state.get("steps") or 0)
        self.holds = int(state.get("holds") or 0)
        self.max_wait_s = float(state.get("max_wait_s") or 0.0)
        if sample.data.get("released"):
            self.released = True
            return "released"
        if sample.data.get("locked") and state.get("origin") is not None:
            self.origin = float(state["origin"])
            self.t_s = 0.0
            return f"locked at simulation time {self.origin:.3f} s"
        if self.origin is None:
            return None
        if sample.data.get("waiting") is not None:
            self.waiting = [str(v) for v in sample.data["waiting"]]
            self.waited_s = float(sample.data.get("waited_s") or 0.0)
            return None
        t_s = state.get("t_s")
        if t_s is not None and sample.sim is not None and not sample.data.get("held"):
            self.t_s = round(float(sample.sim) - self.origin, 6)
            self.waiting = []
        self.held = bool(sample.data.get("held"))
        return None

    def summary(self) -> dict[str, Any]:
        """The lockstep as data (`run.json` `clock.lockstep`)."""
        return {
            "step_s": self.step_s,
            "vehicles": list(self.vehicles),
            "origin_sim_s": self.origin,
            "steps": self.steps,
            "holds": self.holds,
            "max_wait_s": self.max_wait_s,
            "commands": self.commands,
            "timeout": self.timeout,
        }
