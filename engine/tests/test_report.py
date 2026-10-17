"""Tests of the run reports (task P1-T06)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fieldproof.report import (
    REPORT_JSON,
    REPORT_MD,
    ReportError,
    build_report,
    load_record,
    render_markdown,
    write_reports,
)


def record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "abcd1234-20261018T120000Z-aaaaaa",
        "run_dir": "/runs/x",
        "scenario": {"name": "ugv_nav_to_goal", "path": "/w/s.yaml", "digest": "abcd1234"},
        "engine_version": "0.8.0",
        "started_at": "2026-10-18T12:00:00.000+00:00",
        "finished_at": "2026-10-18T12:01:30.500+00:00",
        "phase": "finished",
        "status": "passed",
        "reason": None,
        "phases": [
            {"name": "preparing", "duration_s": 0.5},
            {"name": "running", "duration_s": 40.3},
            {"name": "finished", "duration_s": None},
        ],
        "vehicles": [
            {"name": "ugv1", "manifest": "ugv_nav2", "namespace": "", "image": "img:local"}
        ],
        "timeline": [
            {
                "name": "drive_ahead",
                "action": "navigate_to_pose",
                "vehicle": "ugv1",
                "at_s": 0.0,
                "status": "ok",
                "duration_s": 22.1,
                "detail": "goal SUCCEEDED",
            }
        ],
        "assertions": [
            {
                "name": "reached_goal",
                "type": "reach_pose",
                "status": "passed",
                "reason": "0.21 m from (10, -1) at 18.2 s (tolerance 0.5 m)",
                "t_s": 18.2,
                "sim_s": 15.0,
                "evidence": {
                    "reached": {"x": 9.9, "y": -1.2, "distance_m": 0.21, "yaw_error_rad": None},
                    "samples": 180,
                },
            },
            {
                "name": "speed_limit",
                "type": "topic_value",
                "status": "failed",
                "reason": "/cmd_vel linear.x = 1.8 at 3.0 s violates 'value <= 1.5'",
                "t_s": 3.0,
                "sim_s": None,
                "evidence": {"violation": {"t_s": 3.0, "value": 1.8}, "samples": 30},
            },
            {
                "name": "late",
                "type": "within_time",
                "status": "skipped",
                "reason": "ended",
                "t_s": None,
                "sim_s": None,
                "evidence": {},
            },
        ],
        "observation": {"samples": 500, "sim_elapsed_s": 30.5},
        "stack": {"world": "depot", "render": "software"},
    }
    base.update(overrides)
    return base


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "recording").mkdir(parents=True)
    (run / "recording" / "recording_0.mcap").write_bytes(b"\x89MCAP" + b"0" * 2048)
    (run / "recording" / "metadata.yaml").write_text("rosbag2_bagfile_information: {}\n")
    (run / "logs").mkdir()
    (run / "logs" / "sim.log").write_text("gz\n")
    (run / "run.json").write_text(json.dumps(record()))
    (run / "engine.log").write_text("{}\n")
    return run


def test_build_report(run_dir: Path) -> None:
    report = build_report(record(), run_dir)
    assert report["verdict"] == "PASS"
    assert report["exit_code"] == 0
    assert report["duration_s"] == 90.5
    assert report["timeline_duration_s"] == 40.3
    assert report["sim_elapsed_s"] == 30.5
    assert report["world"] == "depot"
    assert report["assertion_counts"] == {"passed": 1, "failed": 1, "skipped": 1, "pending": 0}
    assert report["recording"] == {
        "directory": "recording",
        "mcap": ["recording/recording_0.mcap"],
        "bytes": 2053 + len("rosbag2_bagfile_information: {}\n"),
    }
    paths = [a["path"] for a in report["artifacts"]]
    assert paths == [
        "engine.log",
        "logs/sim.log",
        "recording/metadata.yaml",
        "recording/recording_0.mcap",
        "run.json",
    ]


def test_build_report_without_optional_parts(tmp_path: Path) -> None:
    minimal = {"run_id": "r", "status": "error", "reason": "boom"}
    report = build_report(minimal, tmp_path / "missing")
    assert report["verdict"] == "ERROR"
    assert report["exit_code"] == 1
    assert report["artifacts"] == []
    assert report["recording"] is None
    assert report["duration_s"] is None
    text = render_markdown(report)
    assert "**ERROR** (status `error`, exit code 1): boom" in text
    assert "The scenario declares no assertions." in text
    assert "The scenario has no timeline." in text
    assert "No recording" in text


def test_render_markdown(run_dir: Path) -> None:
    text = render_markdown(
        build_report(record(status="failed", reason="assertion x failed"), run_dir)
    )
    assert text.startswith("# Fieldproof run report: ugv_nav_to_goal\n")
    assert "**FAIL** (status `failed`, exit code 1): assertion x failed" in text
    assert "| `reached_goal` | `reach_pose` | PASS | 18.2 s |" in text
    assert "reached: x 9.90, y -1.20, distance_m 0.21; samples 180" in text
    assert "| `speed_limit` | `topic_value` | FAIL | 3.0 s |" in text
    assert "violates 'value <= 1.5'" in text
    assert "| `late` | `within_time` | SKIP | - |" in text
    assert (
        "| `drive_ahead` (navigate_to_pose) | `ugv1` | 0 s | ok | 22.1 s | goal SUCCEEDED |" in text
    )
    assert "| running | 40.3 s |" in text
    assert "Recording (MCAP, 2.0 KB): `recording/recording_0.mcap`" in text
    assert "`logs/sim.log` (3 B)" in text
    assert "`ugv1` (ugv_nav2, `img:local`)" in text
    assert "90.5 s wall clock, timeline 40.3 s, 30.5 s of simulation" in text
    assert "|" not in text.split("## Artefacts")[1], "artefacts are a list"


def test_write_and_load(run_dir: Path) -> None:
    report = write_reports(record(), run_dir)
    assert (run_dir / REPORT_JSON).is_file()
    assert (run_dir / REPORT_MD).read_text() == render_markdown(report)
    assert json.loads((run_dir / REPORT_JSON).read_text())["run_id"] == report["run_id"]
    again = build_report(record(), run_dir)
    assert [a["path"] for a in again["artifacts"]] == [a["path"] for a in report["artifacts"]], (
        "the reports themselves are not artefacts"
    )
    assert load_record(run_dir)["run_id"] == report["run_id"]
    assert load_record(run_dir / "run.json")["run_id"] == report["run_id"]


def test_load_record_errors(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="No such file"):
        load_record(tmp_path)
    (tmp_path / "run.json").write_text("{not json")
    with pytest.raises(ReportError, match="not valid JSON"):
        load_record(tmp_path)
    (tmp_path / "run.json").write_text("[]")
    with pytest.raises(ReportError, match="not a run record"):
        load_record(tmp_path)
