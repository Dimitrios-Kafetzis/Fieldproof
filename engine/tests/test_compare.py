"""Tests of the trajectory comparison (task P2-T04): `fieldproof.determinism` and `compare`."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fieldproof.cli import app
from fieldproof.determinism import (
    CompareError,
    Point,
    compare,
    compare_runs,
    interpolate,
    trajectories,
)


def write_run(
    run_dir: Path,
    poses: list[tuple[float, float, float]],
    *,
    origin: float = 10.0,
    model: str = "ugv1",
    seed: int = 7,
    mode: str = "lockstep",
    run_id: str = "run-a",
) -> Path:
    """A run directory with a record and a stream of ground-truth poses (sim, x, y)."""
    run_dir.mkdir(parents=True)
    record: dict[str, Any] = {
        "run_id": run_id,
        "status": "passed",
        "scenario": {"name": "ugv_lockstep"},
        "seed": seed,
        "clock": {"mode": mode, "origin_sim_s": origin},
    }
    (run_dir / "run.json").write_text(json.dumps(record))
    lines = [json.dumps({"kind": "ready", "wall": 1.0, "sim": None})]
    for sim, x, y in poses:
        lines.append(
            json.dumps(
                {
                    "kind": "pose",
                    "wall": 1.0,
                    "sim": sim,
                    "model": model,
                    "x": x,
                    "y": y,
                    "yaw": 0.0,
                }
            )
        )
    lines.append("not json")
    (run_dir / "samples.jsonl").write_text("\n".join(lines) + "\n")
    return run_dir


def test_trajectories_count_from_the_origin_and_skip_what_is_before(tmp_path: Path) -> None:
    run = write_run(
        tmp_path / "a", [(9.0, 0, 0), (10.0, 1, 0), (10.5, 2, 0), (10.5, 2, 1), (11, 3, 0)]
    )
    (points,) = trajectories(run).values()
    assert [(p.t, p.x) for p in points] == [(0.0, 1.0), (0.5, 2.0), (1.0, 3.0)]


def test_trajectories_need_the_clock_origin(tmp_path: Path) -> None:
    run = write_run(tmp_path / "a", [(10.0, 1, 0)])
    record = json.loads((run / "run.json").read_text())
    record["clock"] = None
    (run / "run.json").write_text(json.dumps(record))
    with pytest.raises(CompareError, match="origin_sim_s"):
        trajectories(run)
    (run / "samples.jsonl").unlink()
    record["clock"] = {"origin_sim_s": 10.0}
    (run / "run.json").write_text(json.dumps(record))
    with pytest.raises(CompareError, match=r"samples\.jsonl"):
        trajectories(run)


def test_interpolate_between_samples_and_outside() -> None:
    points = [Point(0.0, 0.0, 0.0, 0.0), Point(2.0, 2.0, 0.0, math.pi / 2)]
    middle = interpolate(points, 1.0)
    assert middle is not None
    assert (middle.x, middle.y, middle.yaw) == pytest.approx((1.0, 0.0, math.pi / 4))
    assert interpolate(points, -0.1) is None
    assert interpolate(points, 2.1) is None
    assert interpolate([], 1.0) is None
    same = interpolate([Point(1.0, 5.0, 5.0, 0.0), Point(1.0, 6.0, 6.0, 0.0)], 1.0)
    assert same is not None
    assert same.x == 5.0


def test_compare_measures_the_largest_and_mean_deviation() -> None:
    a = [Point(t, t, 0.0, 0.0) for t in (0.0, 1.0, 2.0, 3.0)]
    b = [Point(t, t, 0.1 if t == 2.0 else 0.0, 0.0) for t in (0.0, 1.0, 2.0, 3.0, 4.0)]
    result = compare(a, b)
    assert result["samples"] == 4
    assert result["max_m"] == pytest.approx(0.1)
    assert result["at_s"] == 2.0
    assert result["mean_m"] == pytest.approx(0.025)
    assert result["final_m"] == 0.0
    assert result["span_s"] == 3.0
    assert (result["a_span_s"], result["b_span_s"]) == (3.0, 4.0)
    assert compare(a, [Point(10.0, 0.0, 0.0, 0.0)])["samples"] == 0


def test_compare_runs_vehicle_by_vehicle(tmp_path: Path) -> None:
    drive = [(10.0 + k * 0.5, k * 0.2, 0.0) for k in range(10)]
    a = write_run(tmp_path / "a", drive)
    b = write_run(
        tmp_path / "b", [(s + 1.0, x + 0.05, y) for s, x, y in drive], origin=11.0, run_id="run-b"
    )
    result = compare_runs(a, b, 0.1)
    assert result["within_tolerance"]
    assert result["max_m"] == pytest.approx(0.05)
    assert result["run_ids"] == ["run-a", "run-b"]
    assert result["seeds"] == [7, 7]
    assert result["modes"] == ["lockstep", "lockstep"]
    assert result["vehicles"]["ugv1"]["samples"] == 10
    assert result["missing"] == []
    assert not compare_runs(a, b, 0.01)["within_tolerance"]
    c = write_run(tmp_path / "c", drive, model="ugv2")
    with pytest.raises(CompareError, match="share no vehicle"):
        compare_runs(a, c)


def test_compare_command(tmp_path: Path) -> None:
    drive = [(10.0 + k * 0.5, k * 0.2, 0.0) for k in range(10)]
    a = write_run(tmp_path / "a", drive)
    b = write_run(tmp_path / "b", [(s, x, y + 0.3) for s, x, y in drive], run_id="run-b")
    runner = CliRunner()
    result = runner.invoke(app, ["compare", str(a), str(b)])
    assert result.exit_code == 1, result.output
    assert "largest deviation 0.300 m" in result.output
    assert "DIFFERENT" in result.output
    result = runner.invoke(app, ["compare", str(a), str(b), "--tolerance-m", "0.5", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["within_tolerance"] is True
    result = runner.invoke(app, ["compare", str(a), str(tmp_path / "missing")])
    assert result.exit_code == 2
