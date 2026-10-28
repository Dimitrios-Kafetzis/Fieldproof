"""Tests of the run database (task P2-T06): migrations, records, queries, indexing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from fieldproof.report import build_report, write_reports
from fieldproof.rundb import (
    MIGRATIONS,
    SCHEMA_VERSION,
    DatabaseError,
    MatrixContext,
    Migration,
    RunDatabase,
    index_runs,
    run_directories,
)


def record(run_id: str = "aaaa1111-20261028T100000Z-abc123", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": run_id,
        "run_dir": f"/runs/{run_id}",
        "scenario": {"name": "ugv_nav_to_goal", "path": "/w/s.yaml", "digest": run_id[:8]},
        "engine_version": "0.18.0",
        "started_at": "2026-10-28T10:00:00.000+00:00",
        "finished_at": "2026-10-28T10:01:30.500+00:00",
        "phase": "finished",
        "status": "passed",
        "reason": None,
        "phases": [{"name": "running", "duration_s": 40.3}],
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
                "status": "failed",
                "reason": "1.8",
                "t_s": 3.0,
            },
            {
                "name": "late",
                "type": "within_time",
                "status": "skipped",
                "reason": "-",
                "t_s": None,
            },
        ],
        "kpis": {
            "drive.length_m": {"value": 4.2, "unit": "m", "source": "drive", "type": "path"},
            "stack.peak": {"value": "n/a", "unit": None, "source": "stack", "type": "resources"},
        },
        "time": {"mode": "sim"},
        "seed": 7,
        "overrides": {"time.rtf": 2.0},
        "stack": {"world": "depot"},
        "observation": {"sim_elapsed_s": 30.0},
    }
    base.update(overrides)
    return base


def report_of(run_dir: Path, **overrides: Any) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    data = record(run_id=overrides.pop("run_id", run_dir.name), run_dir=str(run_dir), **overrides)
    (run_dir / "run.json").write_text(json.dumps(data))
    return write_reports(data, run_dir)


def test_fresh_file_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "fieldproof.sqlite"
    with RunDatabase(path) as db:
        assert db.applied == [m.version for m in MIGRATIONS]
        assert db.version() == SCHEMA_VERSION
        history = db.history()
        assert [h["version"] for h in history] == [m.version for m in MIGRATIONS]
        assert history[0]["name"] == MIGRATIONS[0].name
        assert db.counts() == {"runs": 0, "matrices": 0}
    with RunDatabase(path) as db:
        assert db.applied == [], "a second open applies nothing"
        assert db.version() == SCHEMA_VERSION


def test_a_new_migration_applies_to_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "fieldproof.sqlite"
    with RunDatabase(path) as db:
        db.record_run(build_report(record(), tmp_path))
    future = (
        *MIGRATIONS,
        Migration(SCHEMA_VERSION + 1, "notes", "ALTER TABLE runs ADD COLUMN notes TEXT;"),
    )
    with RunDatabase(path) as db:
        assert db.migrate(future) == [SCHEMA_VERSION + 1]
        assert db.version() == SCHEMA_VERSION + 1
        columns = [r[1] for r in db.connection.execute("PRAGMA table_info(runs)")]
        assert "notes" in columns
        assert db.counts()["runs"] == 1, "the data survives"
    # The engine refuses a file newer than it knows.
    with pytest.raises(DatabaseError, match="newer than this engine"):
        RunDatabase(path)
    # A broken migration is rolled back whole.
    broken = (
        *future,
        Migration(SCHEMA_VERSION + 2, "bad", "CREATE TABLE x (a); CREATE TABLE x (a);"),
    )
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("DELETE FROM schema_version WHERE version > ?", (SCHEMA_VERSION,))
    connection.execute("ALTER TABLE runs DROP COLUMN notes")
    connection.close()
    with RunDatabase(path) as db, pytest.raises(sqlite3.OperationalError):
        db.migrate(broken)
    with RunDatabase(path, migrations=future) as db:
        assert db.applied == [], "the notes migration went through before the broken one"
        assert db.version() == SCHEMA_VERSION + 1
        tables = [r[0] for r in db.connection.execute("SELECT name FROM sqlite_master")]
        assert "x" not in tables, "the broken migration was rolled back whole"


def test_record_and_query_runs(tmp_path: Path) -> None:
    path = tmp_path / "fieldproof.sqlite"
    first = build_report(record(), tmp_path / "a")
    second = build_report(
        record("bbbb2222-20261028T110000Z-def456", status="failed", reason="speed", seed=8),
        tmp_path / "b",
    )
    with RunDatabase(path) as db:
        db.record_run(first)
        db.record_run(second)
        assert db.counts() == {"runs": 2, "matrices": 0}
        rows = db.runs()
        assert [r["run_id"] for r in rows] == [second["run_id"], first["run_id"]], "newest first"
        assert rows[1]["verdict"] == "PASS"
        assert rows[1]["assertions_passed"] == 1
        assert rows[1]["assertions_failed"] == 1
        assert rows[1]["assertions_skipped"] == 1
        assert rows[1]["overrides"] == {"time.rtf": 2.0}
        assert rows[1]["flaky"] is False
        assert rows[1]["matrix_id"] is None
        assert rows[1]["time_mode"] == "sim"
        assert rows[1]["duration_s"] == pytest.approx(90.5)
        assert [r["run_id"] for r in db.runs(status="failed")] == [second["run_id"]]
        assert db.runs(scenario="other") == []
        assert len(db.runs(limit=1)) == 1
        # By id, by prefix, ambiguous, unknown.
        found = db.run(first["run_id"])
        assert found is not None
        assert found["report"]["verdict"] == "PASS"
        assert db.report("bbbb2222") == second
        with pytest.raises(DatabaseError, match="more than one run"):
            db.run("")
        assert db.run("zzzz") is None
        assert db.kpis(first["run_id"]) == {
            "drive.length_m": {"value": 4.2, "unit": "m", "source": "drive", "type": "path"},
            "stack.peak": {"value": "n/a", "unit": None, "source": "stack", "type": "resources"},
        }
        assert [a["name"] for a in db.assertions(first["run_id"])] == [
            "reached_goal",
            "speed_limit",
            "late",
        ]
        assert db.assertions(first["run_id"])[1]["status"] == "failed"
        # Recording again replaces the row and its tables; the matrix columns are kept.
        db.record_run(
            first,
            MatrixContext("m1", "c01", "seed=7", 2, flaky=True),
        )
        changed = dict(first, status="failed", verdict="FAIL", exit_code=1, kpis={})
        db.record_run(changed)
        row = db.run(first["run_id"])
        assert row is not None
        assert row["verdict"] == "FAIL"
        assert row["matrix_id"] == "m1"
        assert row["case_id"] == "c01"
        assert row["attempt"] == 2
        assert row["flaky"] is True
        assert db.kpis(first["run_id"]) == {}
        assert [r["run_id"] for r in db.runs(matrix_id="m1")] == [first["run_id"]]
        assert db.counts()["runs"] == 2


def test_record_matrix(tmp_path: Path) -> None:
    path = tmp_path / "fieldproof.sqlite"
    matrix = {
        "matrix_id": "mmmm1111-20261028T100000Z-m1",
        "matrix_dir": str(tmp_path / "m"),
        "matrix": {"name": "sweep", "path": "/w/m.yaml", "digest": "mmmm1111"},
        "scenario": {"path": "/w/s.yaml"},
        "status": "passed",
        "verdict": "PASS",
        "exit_code": 0,
        "started_at": "2026-10-28T10:00:00.000+00:00",
        "finished_at": "2026-10-28T10:30:00.000+00:00",
        "duration_s": 1800.0,
        "schedule": {"parallel": "auto"},
        "counts": {"cases": 2, "passed": 2, "failed": 0, "flaky": 1},
        "cases": [],
    }
    with RunDatabase(path) as db:
        db.record_matrix(matrix)
        db.record_matrix(dict(matrix, status="failed", verdict="FAIL", exit_code=1))
        rows = db.matrices()
        assert len(rows) == 1
        assert rows[0]["verdict"] == "FAIL"
        assert rows[0]["cases"] == 2
        assert rows[0]["flaky"] == 1
        found = db.matrix("mmmm1111")
        assert found is not None
        assert found["record"]["counts"]["passed"] == 2
        assert db.matrix("nope") is None
        assert db.counts() == {"runs": 0, "matrices": 1}


def test_index_runs(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    report_of(runs / "aaaa1111-20261028T100000Z-r1")
    matrix_dir = runs / "mmmm1111-20261028T100000Z-m1"
    second = report_of(matrix_dir / "bbbb2222-20261028T100000Z-m1c01a1", status="failed")
    third = report_of(matrix_dir / "bbbb2222-20261028T100100Z-m1c01a2")
    (matrix_dir / "matrix.json").write_text(
        json.dumps(
            {
                "matrix_id": matrix_dir.name,
                "matrix_dir": str(matrix_dir),
                "matrix": {"name": "sweep"},
                "status": "passed",
                "verdict": "PASS",
                "exit_code": 0,
                "counts": {"cases": 1, "passed": 1, "flaky": 1},
                "cases": [
                    {
                        "id": "c01",
                        "name": "seed=7",
                        "flaky": True,
                        "attempts": [
                            {"run_id": second["run_id"], "number": 1},
                            {"run_id": third["run_id"], "number": 2},
                        ],
                    }
                ],
            }
        )
    )
    (runs / "broken").mkdir()
    (runs / "broken" / "run.json").write_text("{bad")
    (runs / "other.json").write_text("{}")
    (tmp_path / "stray" / "deep" / "deeper" / "deepest").mkdir(parents=True)
    assert [p.name for p in run_directories(runs)] == [
        "aaaa1111-20261028T100000Z-r1",
        "broken",
        "bbbb2222-20261028T100000Z-m1c01a1",
        "bbbb2222-20261028T100100Z-m1c01a2",
    ]
    with RunDatabase(runs / "fieldproof.sqlite") as db:
        recorded = index_runs(db, runs)
        assert recorded["runs"] == [
            "aaaa1111-20261028T100000Z-r1",
            second["run_id"],
            third["run_id"],
        ]
        assert recorded["matrices"] == [matrix_dir.name]
        assert len(recorded["problems"]) == 1
        assert "not valid JSON" in recorded["problems"][0]
        row = db.run(third["run_id"])
        assert row is not None
        assert row["matrix_id"] == matrix_dir.name
        assert row["case_id"] == "c01"
        assert row["attempt"] == 2
        assert row["flaky"] is True
        assert db.counts() == {"runs": 3, "matrices": 1}
        # Indexing again changes nothing.
        index_runs(db, runs)
        assert db.counts() == {"runs": 3, "matrices": 1}


def test_open_errors(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    with pytest.raises(DatabaseError, match="cannot open"):
        RunDatabase(tmp_path / "file" / "fieldproof.sqlite")
    (tmp_path / "garbage.sqlite").write_text("not a database at all, just text" * 10)
    with pytest.raises(DatabaseError):
        RunDatabase(tmp_path / "garbage.sqlite")
