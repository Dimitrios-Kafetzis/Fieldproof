"""Tests of `fieldproof runs list|show|diff|index` (task P2-T06)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from fieldproof.cli import app
from fieldproof.report import write_reports
from fieldproof.rundb import MatrixContext, RunDatabase
from fieldproof.runs_cli import diff_reports

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return " ".join(ANSI.sub("", text).split())


def record(run_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": run_id,
        "run_dir": f"/runs/{run_id}",
        "scenario": {"name": "ugv_nav_to_goal", "path": "/w/s.yaml", "digest": run_id[:8]},
        "engine_version": "0.18.0",
        "started_at": "2026-10-28T10:00:00.000+00:00",
        "finished_at": "2026-10-28T10:01:30.000+00:00",
        "phase": "finished",
        "status": "passed",
        "reason": None,
        "phases": [{"name": "running", "duration_s": 40.0}],
        "vehicles": [{"name": "ugv1", "manifest": "ugv_nav2", "image": "img:local"}],
        "timeline": [],
        "assertions": [
            {
                "name": "reached_goal",
                "type": "reach_pose",
                "status": "passed",
                "reason": "ok",
                "t_s": 18.2,
            },
            {
                "name": "speed_limit",
                "type": "topic_value",
                "status": "passed",
                "reason": "ok",
                "t_s": 3.0,
            },
        ],
        "kpis": {
            "drive.length_m": {"value": 4.0, "unit": "m", "source": "drive", "type": "path"},
            "mission.duration_s": {
                "value": 30.0,
                "unit": "s",
                "source": "mission",
                "type": "mission_time",
            },
            "stack.peak": {"value": None, "unit": "MiB", "source": "stack", "type": "resources"},
        },
        "time": {"mode": "sim"},
        "seed": 1,
        "overrides": {},
        "stack": {"world": "depot"},
        "observation": {"sim_elapsed_s": 30.0},
    }
    base.update(overrides)
    return base


def write_run(runs: Path, run_id: str, **overrides: Any) -> Path:
    run_dir = runs / run_id
    run_dir.mkdir(parents=True)
    data = record(run_id, run_dir=str(run_dir), **overrides)
    (run_dir / "run.json").write_text(json.dumps(data))
    write_reports(data, run_dir)
    return run_dir


def workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A workspace with two runs: the second slower, seeded differently, one assertion worse."""
    (tmp_path / "fieldproof.toml").write_text('[runs]\ndirectory = "runs"\n')
    runs = tmp_path / "runs"
    first = write_run(runs, "aaaa1111-20261028T100000Z-r1")
    second = write_run(
        runs,
        "aaaa2222-20261028T110000Z-r2",
        status="failed",
        reason="assertion speed_limit failed",
        seed=2,
        started_at="2026-10-28T11:00:00.000+00:00",
        finished_at="2026-10-28T11:02:00.000+00:00",
        overrides={"time.rtf": 2.0},
        assertions=[
            {
                "name": "reached_goal",
                "type": "reach_pose",
                "status": "passed",
                "reason": "ok",
                "t_s": 20.0,
            },
            {
                "name": "speed_limit",
                "type": "topic_value",
                "status": "failed",
                "reason": "1.8",
                "t_s": 3.0,
            },
            {
                "name": "extra",
                "type": "within_time",
                "status": "skipped",
                "reason": "-",
                "t_s": None,
            },
        ],
        kpis={
            "drive.length_m": {"value": 4.5, "unit": "m", "source": "drive", "type": "path"},
            "mission.duration_s": {
                "value": 24.0,
                "unit": "s",
                "source": "mission",
                "type": "mission_time",
            },
            "new.kpi": {"value": 1.0, "unit": "", "source": "new", "type": "value"},
        },
    )
    return tmp_path / "fieldproof.toml", first, second


def test_index_list_show(tmp_path: Path) -> None:  # noqa: PLR0915
    config, first, second = workspace(tmp_path)
    database = tmp_path / "runs" / "fieldproof.sqlite"
    result = runner.invoke(app, ["runs", "list", "--config", str(config)])
    assert result.exit_code == 2, result.output
    assert "no run database" in plain(result.output)
    result = runner.invoke(app, ["runs", "index", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "indexed 2 run(s) and 0 matrix(es)" in plain(result.output)
    assert database.is_file()
    result = runner.invoke(
        app, ["runs", "index", "--config", str(config), "--json", str(tmp_path / "runs")]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["runs"] == [first.name, second.name]
    result = runner.invoke(app, ["runs", "index", "--config", str(config), str(tmp_path / "nope")])
    assert result.exit_code == 2
    assert "not a directory" in plain(result.output)
    # list: newest first, filters, JSON.
    result = runner.invoke(app, ["runs", "list", "--config", str(config)])
    assert result.exit_code == 0, result.output
    text = plain(result.output)
    assert text.index(second.name) < text.index(first.name)
    assert "FAIL" in text
    assert "PASS" in text
    assert "2 of 2 run(s) and 0 matrix(es)" in text
    result = runner.invoke(
        app, ["runs", "list", "--config", str(config), "--status", "passed", "--json"]
    )
    assert result.exit_code == 0, result.output
    listed = json.loads(result.output)
    assert [r["run_id"] for r in listed["runs"]] == [first.name]
    assert listed["counts"] == {"runs": 2, "matrices": 0}
    result = runner.invoke(
        app, ["runs", "list", "--database", str(database), "--scenario", "other"]
    )
    assert result.exit_code == 0, result.output
    assert "0 of 2 run(s)" in plain(result.output)
    result = runner.invoke(app, ["runs", "list", "--runs-dir", str(tmp_path / "runs"), "-n", "1"])
    assert result.exit_code == 0, result.output
    assert "1 of 2 run(s)" in plain(result.output)
    # show: by id, by prefix, by directory, by run.json, JSON, unknown and ambiguous.
    result = runner.invoke(app, ["runs", "show", first.name, "--config", str(config)])
    assert result.exit_code == 0, result.output
    text = plain(result.output)
    assert f"PASS ugv_nav_to_goal ({first.name})" in text
    assert "reached_goal" in text
    assert "drive.length_m" in text
    assert "4.000" in text
    assert "artefacts: run.json" in text
    result = runner.invoke(app, ["runs", "show", "aaaa2222", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "FAIL ugv_nav_to_goal" in plain(result.output)
    assert "overrides time.rtf=2.0" in plain(result.output)
    result = runner.invoke(app, ["runs", "show", str(second / "run.json"), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["run_id"] == second.name
    result = runner.invoke(app, ["runs", "show", str(first)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["runs", "show", "zzzz", "--config", str(config)])
    assert result.exit_code == 2
    assert "no such run" in plain(result.output)
    result = runner.invoke(app, ["runs", "show", "aaaa", "--config", str(config)])
    assert result.exit_code == 2
    assert "more than one run" in plain(result.output)
    result = runner.invoke(app, ["runs", "show", "r1", "--database", str(tmp_path / "none.sqlite")])
    assert result.exit_code == 2
    assert "no database" in plain(result.output)
    (first / "run.json").write_text("{bad")
    result = runner.invoke(app, ["runs", "show", str(first)])
    assert result.exit_code == 2
    assert "not valid JSON" in plain(result.output)


def test_diff(tmp_path: Path) -> None:
    config, first, second = workspace(tmp_path)
    result = runner.invoke(app, ["runs", "diff", str(first), str(second), "--json"])
    assert result.exit_code == 0, result.output
    diff = json.loads(result.output)
    assert diff["runs"] == [first.name, second.name]
    assert diff["verdicts"] == ["PASS", "FAIL"]
    assert diff["seeds"] == [1, 2]
    assert diff["scenario"]["same"] is False
    assert diff["overrides"]["changed"] == {"time.rtf": [None, 2.0]}
    assert diff["durations"]["duration_s"] == {"a": 90.0, "b": 120.0, "delta": 30.0}
    assert diff["durations"]["sim_elapsed_s"]["delta"] == 0.0
    by_name = {k["name"]: k for k in diff["kpis"]}
    assert by_name["drive.length_m"]["delta"] == 0.5
    assert by_name["drive.length_m"]["pct"] == 12.5
    assert by_name["mission.duration_s"]["delta"] == -6.0
    assert by_name["mission.duration_s"]["pct"] == -20.0
    assert by_name["new.kpi"] == {
        "name": "new.kpi",
        "unit": "",
        "a": None,
        "b": 1.0,
        "delta": None,
        "pct": None,
        "same": False,
    }
    assert by_name["stack.peak"]["same"] is True
    assert by_name["stack.peak"]["delta"] is None
    assertions = {a["name"]: a for a in diff["assertions"]}
    assert assertions["speed_limit"] == {
        "name": "speed_limit",
        "a": "passed",
        "b": "failed",
        "changed": True,
        "regressed": True,
        "fixed": False,
    }
    assert assertions["reached_goal"]["changed"] is False
    assert assertions["extra"]["a"] is None
    assert assertions["extra"]["changed"] is True
    assert diff["counts"] == {
        "kpis_changed": 3,
        "assertions_changed": 2,
        "regressed": 1,
        "fixed": 0,
    }
    # The table.
    result = runner.invoke(app, ["runs", "diff", str(first), str(second)])
    assert result.exit_code == 0, result.output
    text = plain(result.output)
    assert f"{first.name} (PASS) vs {second.name} (FAIL)" in text
    assert "override time.rtf: null -> 2.0" in text
    assert "duration_s: 90.000 -> 120.000 (+30.0 s)" in text
    assert "assertion speed_limit: passed -> failed" in text
    assert "drive.length_m 4.000 4.500 +0.500 +12.5 m" in text
    assert "3 KPI(s) changed, 2 assertion(s) changed (1 regressed, 0 fixed)" in text
    # By id through the database, and the fixed direction.
    runner.invoke(app, ["runs", "index", "--config", str(config)])
    result = runner.invoke(app, ["runs", "diff", "aaaa2222", "aaaa1111", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "assertion speed_limit: failed -> passed" in plain(result.output)
    assert "(0 regressed, 1 fixed)" in plain(result.output)
    same = diff_reports(
        json.loads((first / "report.json").read_text()),
        json.loads((first / "report.json").read_text()),
    )
    assert same["counts"] == {
        "kpis_changed": 0,
        "assertions_changed": 0,
        "regressed": 0,
        "fixed": 0,
    }
    assert same["scenario"]["same"] is True
    empty_a = {"run_id": "a", "kpis": {}, "assertions": []}
    empty_b = {"run_id": "b"}
    result_empty = diff_reports(empty_a, empty_b)
    assert result_empty["kpis"] == []
    assert result_empty["assertions"] == []


def test_list_shows_matrix_columns(tmp_path: Path) -> None:
    config, first, _ = workspace(tmp_path)
    database = tmp_path / "runs" / "fieldproof.sqlite"
    with RunDatabase(database) as db:
        report = json.loads((first / "report.json").read_text())
        db.record_run(
            report, MatrixContext("mmmm1111-20261028T100000Z-m1", "c03", "seed=1", 2, flaky=True)
        )
    result = runner.invoke(
        app, ["runs", "list", "--config", str(config), "--matrix", "mmmm1111-20261028T100000Z-m1"]
    )
    assert result.exit_code == 0, result.output
    assert "m1/c03#2 flaky" in plain(result.output)
