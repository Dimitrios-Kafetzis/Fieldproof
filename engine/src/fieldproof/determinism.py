"""Comparing the trajectories of two runs (task P2-T04; `fieldproof compare`).

A run's trajectories are the ground-truth `pose` samples of its vehicles in `samples.jsonl`,
in simulation seconds after the origin of its timeline's clock (`run.json` `clock.origin_sim_s`,
the simulation time the stream had when the timeline started, or in lockstep the time the clock
was locked at). Two runs of the same scenario compare vehicle by vehicle: the second run's
trajectory is interpolated at the first run's times over the span both cover, and the distance
between the two positions is taken at each; the largest is the deviation. Two lockstep runs with
the same seed deviate by the stack's own non-determinism only (what it computed in the wall-clock
time of each step); two free-running runs also by the timing of their commands.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fieldproof.assertions import SAMPLES_FILE
from fieldproof.report import load_record

DEFAULT_TOLERANCE_M = 0.25


class CompareError(ValueError):
    """A run cannot be compared."""


@dataclass(frozen=True)
class Point:
    """A position at a time of the timeline."""

    t: float
    x: float
    y: float
    yaw: float


def trajectories(run_dir: Path) -> dict[str, list[Point]]:
    """Every vehicle's ground-truth trajectory of a run, in timeline seconds."""
    record = load_record(run_dir)
    clock = record.get("clock") or {}
    origin = clock.get("origin_sim_s")
    if origin is None:
        raise CompareError(f"{run_dir}: run.json has no clock.origin_sim_s (did the timeline run?)")
    samples = run_dir / SAMPLES_FILE
    if not samples.is_file():
        raise CompareError(f"{run_dir}: no {SAMPLES_FILE}")
    result: dict[str, list[Point]] = {}
    for line in samples.read_text().splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("kind") != "pose" or data.get("sim") is None:
            continue
        t = float(data["sim"]) - float(origin)
        if t < 0:
            continue
        model = str(data.get("model"))
        points = result.setdefault(model, [])
        if points and t <= points[-1].t:
            continue
        points.append(Point(t, float(data["x"]), float(data["y"]), float(data.get("yaw", 0.0))))
    return result


def interpolate(points: list[Point], t: float) -> Point | None:
    """The trajectory's position at `t` (linear between samples; None outside its span)."""
    if not points or t < points[0].t or t > points[-1].t:
        return None
    low, high = 0, len(points) - 1
    while high - low > 1:
        mid = (low + high) // 2
        if points[mid].t <= t:
            low = mid
        else:
            high = mid
    a, b = points[low], points[high]
    if b.t == a.t:
        return a
    f = (t - a.t) / (b.t - a.t)
    yaw = a.yaw + math.remainder(b.yaw - a.yaw, math.tau) * f
    return Point(t, a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f, yaw)


def compare(a: list[Point], b: list[Point]) -> dict[str, Any]:
    """How far two trajectories of one vehicle are apart."""
    distances: list[tuple[float, float]] = []
    for point in a:
        other = interpolate(b, point.t)
        if other is None:
            continue
        distances.append((point.t, math.hypot(other.x - point.x, other.y - point.y)))
    if not distances:
        return {"samples": 0, "span_s": 0.0, "max_m": None, "at_s": None, "mean_m": None}
    at, worst = max(distances, key=lambda d: d[1])
    return {
        "samples": len(distances),
        "span_s": round(distances[-1][0] - distances[0][0], 3),
        "max_m": round(worst, 4),
        "at_s": round(at, 3),
        "mean_m": round(sum(d for _, d in distances) / len(distances), 4),
        "final_m": round(distances[-1][1], 4),
        "a_span_s": round(a[-1].t - a[0].t, 3),
        "b_span_s": round(b[-1].t - b[0].t, 3),
    }


def compare_runs(
    run_a: Path, run_b: Path, tolerance_m: float = DEFAULT_TOLERANCE_M
) -> dict[str, Any]:
    """Compare two runs vehicle by vehicle; `within_tolerance` says whether every one is."""
    records = [load_record(run_a), load_record(run_b)]
    first, second = trajectories(run_a), trajectories(run_b)
    vehicles = sorted(set(first) & set(second))
    if not vehicles:
        raise CompareError("the runs share no vehicle with a recorded trajectory")
    result = {name: compare(first[name], second[name]) for name in vehicles}
    worst = max((r["max_m"] for r in result.values() if r["max_m"] is not None), default=None)
    return {
        "runs": [str(run_a), str(run_b)],
        "run_ids": [r.get("run_id") for r in records],
        "scenarios": [(r.get("scenario") or {}).get("name") for r in records],
        "seeds": [r.get("seed") for r in records],
        "modes": [(r.get("clock") or {}).get("mode") for r in records],
        "tolerance_m": tolerance_m,
        "vehicles": result,
        "max_m": worst,
        "within_tolerance": worst is not None and worst <= tolerance_m,
        "missing": sorted((set(first) | set(second)) - set(vehicles)),
    }
