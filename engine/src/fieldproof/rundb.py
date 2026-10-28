"""The run database (task P2-T06): one SQLite file per runs directory.

Every run records itself at its end (:meth:`RunDatabase.record_run` from the runner, after the
reports are written), a matrix records its cases and itself, and ``fieldproof runs index``
reads run directories back into the database (runs from before the database, runs copied from
CI artefacts). The file lives in the runs directory in effect (``fieldproof.toml`` ``[runs]
database``, relative to ``[runs] directory``, ``--runs-dir`` or a scenario's
``outputs.directory``), so the runs of a workspace and their database travel together.

The schema is versioned: :data:`MIGRATIONS` is the ordered list of versions, each a name and
the SQL that takes the database from the previous version to it, applied in one transaction
each by :meth:`RunDatabase.migrate` when the file is opened; ``schema_version`` records what
was applied and when. An engine that meets a newer database than it knows refuses it rather
than guessing. A run's row holds the facts ``fieldproof runs list`` shows, the assertions and
the KPIs in tables of their own (``runs diff`` reads them), the artefacts, and the whole report
as JSON (``runs show`` prints it even when the run directory is gone).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fieldproof.report import RECORD_FILE, ReportError, build_report, load_record

DATABASE_FILE = "fieldproof.sqlite"
MATRIX_RECORD = "matrix.json"


class DatabaseError(RuntimeError):
    """The database cannot be opened, migrated or written."""


@dataclass(frozen=True)
class Migration:
    """One step of the schema: the version it produces, its name and its SQL."""

    version: int
    name: str
    sql: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "runs, assertions, kpis, artifacts and matrices",
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            scenario_name TEXT NOT NULL,
            scenario_path TEXT,
            scenario_digest TEXT,
            status TEXT NOT NULL,
            verdict TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            reason TEXT,
            started_at TEXT,
            finished_at TEXT,
            duration_s REAL,
            timeline_duration_s REAL,
            sim_elapsed_s REAL,
            seed INTEGER,
            time_mode TEXT,
            world TEXT,
            engine_version TEXT,
            run_dir TEXT NOT NULL,
            overrides TEXT NOT NULL DEFAULT '{}',
            matrix_id TEXT,
            case_id TEXT,
            case_name TEXT,
            attempt INTEGER,
            flaky INTEGER,
            assertions_passed INTEGER NOT NULL DEFAULT 0,
            assertions_failed INTEGER NOT NULL DEFAULT 0,
            assertions_skipped INTEGER NOT NULL DEFAULT 0,
            recorded_at TEXT NOT NULL,
            report TEXT NOT NULL
        );
        CREATE INDEX runs_by_scenario ON runs (scenario_name, started_at);
        CREATE INDEX runs_by_matrix ON runs (matrix_id, case_id, attempt);
        CREATE TABLE assertions (
            run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            t_s REAL,
            PRIMARY KEY (run_id, name)
        );
        CREATE TABLE kpis (
            run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            value REAL,
            text TEXT,
            unit TEXT,
            source TEXT,
            type TEXT,
            PRIMARY KEY (run_id, name)
        );
        CREATE TABLE artifacts (
            run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            kind TEXT,
            bytes INTEGER,
            PRIMARY KEY (run_id, path)
        );
        CREATE TABLE matrices (
            matrix_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            matrix_path TEXT,
            matrix_digest TEXT,
            scenario_path TEXT,
            status TEXT NOT NULL,
            verdict TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            duration_s REAL,
            cases INTEGER NOT NULL DEFAULT 0,
            passed INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            flaky INTEGER NOT NULL DEFAULT 0,
            parallel INTEGER,
            matrix_dir TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            record TEXT NOT NULL
        );
        """,
    ),
)
SCHEMA_VERSION = MIGRATIONS[-1].version


@dataclass(frozen=True)
class MatrixContext:
    """What a matrix adds to a run's row."""

    matrix_id: str
    case_id: str
    case_name: str
    attempt: int
    flaky: bool = False


def statements(sql: str) -> list[str]:
    """The statements of a migration script (no literal holds a semicolon)."""
    return [part.strip() for part in sql.split(";") if part.strip()]


def utc_now() -> str:
    """The current time as ISO 8601 with milliseconds, UTC."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    return None


class RunDatabase:
    """A connection to the run database at `path`, migrated to the engine's schema."""

    def __init__(
        self,
        path: Path | str,
        *,
        timeout_s: float = 30.0,
        migrations: tuple[Migration, ...] = MIGRATIONS,
    ) -> None:
        self.path = Path(path)
        self.migrations = migrations
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(
                self.path, timeout=timeout_s, isolation_level=None, check_same_thread=False
            )
        except (OSError, sqlite3.Error) as error:
            raise DatabaseError(f"{self.path}: cannot open the run database: {error}") from error
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.applied = self.migrate(migrations)
        except sqlite3.Error as error:
            self.connection.close()
            raise DatabaseError(f"{self.path}: {error}") from error

    def __enter__(self) -> RunDatabase:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection."""
        self.connection.close()

    # ------------------------------------------------------------------------------ schema
    def version(self) -> int:
        """The schema version of the file (0 for a fresh one)."""
        row = self.connection.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    def migrate(self, migrations: tuple[Migration, ...] = MIGRATIONS) -> list[int]:
        """Apply the migrations the file has not seen; return the versions applied."""
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        current = self.version()
        latest = migrations[-1].version if migrations else 0
        if current > latest:
            raise DatabaseError(
                f"{self.path}: schema version {current} is newer than this engine's {latest}; "
                "upgrade the engine or move the file aside"
            )
        applied: list[int] = []
        for migration in migrations:
            if migration.version <= current:
                continue
            # One transaction per migration (`executescript` would commit on its own).
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements(migration.sql):
                    self.connection.execute(statement)
                self.connection.execute(
                    "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, utc_now()),
                )
                self.connection.execute("COMMIT")
            except sqlite3.Error:
                self.connection.execute("ROLLBACK")
                raise
            applied.append(migration.version)
        return applied

    def history(self) -> list[dict[str, Any]]:
        """The migrations applied to the file, in order."""
        rows = self.connection.execute(
            "SELECT version, name, applied_at FROM schema_version ORDER BY version"
        ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------------------------- writes
    def record_run(self, report: Mapping[str, Any], matrix: MatrixContext | None = None) -> None:
        """Insert or replace a run from its report (:func:`fieldproof.report.build_report`)."""
        scenario = report.get("scenario") or {}
        counts = report.get("assertion_counts") or {}
        run_id = str(report["run_id"])
        row = {
            "run_id": run_id,
            "scenario_name": str(scenario.get("name") or run_id),
            "scenario_path": scenario.get("path"),
            "scenario_digest": scenario.get("digest"),
            "status": str(report.get("status") or "error"),
            "verdict": str(report.get("verdict") or "ERROR"),
            "exit_code": int(report.get("exit_code") or 0),
            "reason": report.get("reason"),
            "started_at": report.get("started_at"),
            "finished_at": report.get("finished_at"),
            "duration_s": _number(report.get("duration_s")),
            "timeline_duration_s": _number(report.get("timeline_duration_s")),
            "sim_elapsed_s": _number(report.get("sim_elapsed_s")),
            "seed": report.get("seed"),
            "time_mode": (report.get("time") or {}).get("mode"),
            "world": report.get("world"),
            "engine_version": report.get("engine_version"),
            "run_dir": str(report.get("run_dir") or ""),
            "overrides": json.dumps(report.get("overrides") or {}, sort_keys=True),
            "matrix_id": matrix.matrix_id if matrix else None,
            "case_id": matrix.case_id if matrix else None,
            "case_name": matrix.case_name if matrix else None,
            "attempt": matrix.attempt if matrix else None,
            "flaky": int(matrix.flaky) if matrix else None,
            "assertions_passed": int(counts.get("passed") or 0),
            "assertions_failed": int(counts.get("failed") or 0),
            "assertions_skipped": int(counts.get("skipped") or 0) + int(counts.get("pending") or 0),
            "recorded_at": utc_now(),
            "report": json.dumps(report, sort_keys=False),
        }
        columns = ", ".join(row)
        placeholders = ", ".join(f":{k}" for k in row)
        kept = ("matrix_id", "case_id", "case_name", "attempt", "flaky")
        updates = ", ".join(
            f"{k} = COALESCE(excluded.{k}, runs.{k})" if k in kept else f"{k} = excluded.{k}"
            for k in row
            if k != "run_id"
        )
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                f"INSERT INTO runs ({columns}) VALUES ({placeholders}) "  # noqa: S608
                f"ON CONFLICT (run_id) DO UPDATE SET {updates}",
                row,
            )
            for table in ("assertions", "kpis", "artifacts"):
                self.connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))  # noqa: S608
            self.connection.executemany(
                "INSERT INTO assertions (run_id, position, name, type, status, reason, t_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        index,
                        str(a.get("name")),
                        str(a.get("type") or ""),
                        str(a.get("status") or "pending"),
                        a.get("reason"),
                        _number(a.get("t_s")),
                    )
                    for index, a in enumerate(report.get("assertions") or [])
                ],
            )
            self.connection.executemany(
                "INSERT INTO kpis (run_id, name, value, text, unit, source, type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        str(name),
                        _number(kpi.get("value")),
                        None
                        if _number(kpi.get("value")) is not None
                        else json.dumps(kpi.get("value")),
                        kpi.get("unit"),
                        kpi.get("source"),
                        kpi.get("type"),
                    )
                    for name, kpi in (report.get("kpis") or {}).items()
                    if isinstance(kpi, Mapping)
                ],
            )
            self.connection.executemany(
                "INSERT INTO artifacts (run_id, path, kind, bytes) VALUES (?, ?, ?, ?)",
                [
                    (run_id, str(a.get("path")), a.get("kind"), a.get("bytes"))
                    for a in (report.get("artifacts") or [])
                    if a.get("path")
                ],
            )
            self.connection.execute("COMMIT")
        except sqlite3.Error as error:
            self.connection.execute("ROLLBACK")
            raise DatabaseError(f"{self.path}: cannot record {run_id}: {error}") from error

    def record_matrix(self, record: Mapping[str, Any]) -> None:
        """Insert or replace a matrix from its record (``matrix.json``)."""
        counts = record.get("counts") or {}
        matrix = record.get("matrix") or {}
        row = {
            "matrix_id": str(record["matrix_id"]),
            "name": str(matrix.get("name") or record["matrix_id"]),
            "matrix_path": matrix.get("path"),
            "matrix_digest": matrix.get("digest"),
            "scenario_path": (record.get("scenario") or {}).get("path"),
            "status": str(record.get("status") or "error"),
            "verdict": str(record.get("verdict") or "ERROR"),
            "exit_code": int(record.get("exit_code") or 0),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "duration_s": _number(record.get("duration_s")),
            "cases": int(counts.get("cases") or 0),
            "passed": int(counts.get("passed") or 0),
            "failed": int(counts.get("failed") or 0),
            "flaky": int(counts.get("flaky") or 0),
            "parallel": (record.get("schedule") or {}).get("parallel"),
            "matrix_dir": str(record.get("matrix_dir") or ""),
            "recorded_at": utc_now(),
            "record": json.dumps(record, sort_keys=False),
        }
        columns = ", ".join(row)
        placeholders = ", ".join(f":{k}" for k in row)
        updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "matrix_id")
        try:
            self.connection.execute(
                f"INSERT INTO matrices ({columns}) VALUES ({placeholders}) "  # noqa: S608
                f"ON CONFLICT (matrix_id) DO UPDATE SET {updates}",
                row,
            )
        except sqlite3.Error as error:
            raise DatabaseError(
                f"{self.path}: cannot record matrix {row['matrix_id']}: {error}"
            ) from error

    # --------------------------------------------------------------------------------- reads
    def runs(
        self,
        *,
        scenario: str | None = None,
        status: str | None = None,
        matrix_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """The runs, newest first, as the list command shows them."""
        clauses, values = [], []
        if scenario:
            clauses.append("scenario_name = ?")
            values.append(scenario)
        if status:
            clauses.append("status = ?")
            values.append(status)
        if matrix_id:
            clauses.append("matrix_id = ?")
            values.append(matrix_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            "SELECT run_id, scenario_name, scenario_digest, status, verdict, exit_code, reason, "  # noqa: S608  (the clauses are fixed)
            "started_at, finished_at, duration_s, timeline_duration_s, sim_elapsed_s, seed, "
            "time_mode, world, engine_version, run_dir, overrides, matrix_id, case_id, case_name, "
            "attempt, flaky, assertions_passed, assertions_failed, assertions_skipped, "
            f"recorded_at FROM runs {where} "
            "ORDER BY COALESCE(started_at, recorded_at) DESC, run_id DESC LIMIT ?",
            (*values, max(1, limit)),
        ).fetchall()
        return [self._run_row(r) for r in rows]

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["overrides"] = json.loads(data.get("overrides") or "{}")
        data["flaky"] = bool(data.get("flaky"))
        return data

    def run(self, run_id: str) -> dict[str, Any] | None:
        """One run by its id, or by a prefix that matches one run; the report included."""
        row = self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            rows = self.connection.execute(
                "SELECT * FROM runs WHERE run_id LIKE ? ORDER BY run_id LIMIT 3",
                (run_id.replace("%", "") + "%",),
            ).fetchall()
            if len(rows) > 1:
                raise DatabaseError(
                    f"{run_id}: matches more than one run ({', '.join(r['run_id'] for r in rows)})"
                )
            row = rows[0] if rows else None
        if row is None:
            return None
        data = self._run_row(row)
        data["report"] = json.loads(data["report"])
        return data

    def report(self, run_id: str) -> dict[str, Any] | None:
        """The report of a run as recorded."""
        found = self.run(run_id)
        return None if found is None else dict(found["report"])

    def kpis(self, run_id: str) -> dict[str, dict[str, Any]]:
        """The KPIs of a run by name."""
        rows = self.connection.execute(
            "SELECT name, value, text, unit, source, type FROM kpis WHERE run_id = ? ORDER BY name",
            (run_id,),
        ).fetchall()
        return {
            r["name"]: {
                "value": r["value"] if r["text"] is None else json.loads(r["text"]),
                "unit": r["unit"],
                "source": r["source"],
                "type": r["type"],
            }
            for r in rows
        }

    def assertions(self, run_id: str) -> list[dict[str, Any]]:
        """The assertions of a run, in the scenario's order."""
        rows = self.connection.execute(
            "SELECT name, type, status, reason, t_s FROM assertions WHERE run_id = ? "
            "ORDER BY position",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def matrices(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """The matrices, newest first."""
        rows = self.connection.execute(
            "SELECT matrix_id, name, status, verdict, exit_code, started_at, finished_at, "
            "duration_s, cases, passed, failed, flaky, parallel, matrix_dir, recorded_at "
            "FROM matrices ORDER BY COALESCE(started_at, recorded_at) DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def matrix(self, matrix_id: str) -> dict[str, Any] | None:
        """One matrix by id or unique prefix, its record included."""
        row = self.connection.execute(
            "SELECT * FROM matrices WHERE matrix_id = ?", (matrix_id,)
        ).fetchone()
        if row is None:
            rows = self.connection.execute(
                "SELECT * FROM matrices WHERE matrix_id LIKE ? LIMIT 2",
                (matrix_id.replace("%", "") + "%",),
            ).fetchall()
            if len(rows) > 1:
                raise DatabaseError(f"{matrix_id}: matches more than one matrix")
            row = rows[0] if rows else None
        if row is None:
            return None
        data = dict(row)
        data["record"] = json.loads(data["record"])
        return data

    def counts(self) -> dict[str, int]:
        """How many runs and matrices the file holds."""
        runs = self.connection.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        matrices = self.connection.execute("SELECT COUNT(*) AS n FROM matrices").fetchone()["n"]
        return {"runs": int(runs), "matrices": int(matrices)}


def record_run_directory(database: RunDatabase, run_dir: Path) -> dict[str, Any]:
    """Record a run directory (its ``run.json``) in the database; return the report."""
    record = load_record(run_dir)
    report = build_report(record, run_dir)
    database.record_run(report)
    return report


def run_directories(root: Path, *, depth: int = 3) -> Iterator[Path]:
    """Every directory under `root` (itself included) holding a ``run.json``, sorted."""

    def walk(directory: Path, level: int) -> Iterator[Path]:
        if (directory / RECORD_FILE).is_file():
            yield directory
        if level >= depth:
            return
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            yield from walk(child, level + 1)

    yield from walk(root, 0)


def index_runs(database: RunDatabase, root: Path) -> dict[str, list[str]]:
    """Record every run (and matrix) directory under `root`; return what was recorded."""
    recorded: dict[str, list[str]] = {"runs": [], "matrices": [], "problems": []}
    for run_dir in run_directories(root):
        try:
            report = record_run_directory(database, run_dir)
        except (ReportError, DatabaseError) as error:
            recorded["problems"].append(str(error))
            continue
        recorded["runs"].append(str(report["run_id"]))
    for record_file in sorted(root.glob(f"**/{MATRIX_RECORD}")):
        try:
            record = json.loads(record_file.read_text())
            if not isinstance(record, dict) or "matrix_id" not in record:
                raise ValueError("not a matrix record")
            database.record_matrix(record)
            for case in record.get("cases") or []:
                attempts = case.get("attempts") or []
                for attempt in attempts:
                    run_id = attempt.get("run_id")
                    if run_id and database.run(str(run_id)) is not None:
                        database.record_run(
                            database.report(str(run_id)) or {},
                            MatrixContext(
                                str(record["matrix_id"]),
                                str(case.get("id")),
                                str(case.get("name")),
                                int(attempt.get("number") or 1),
                                bool(case.get("flaky")),
                            ),
                        )
            recorded["matrices"].append(str(record["matrix_id"]))
        except (OSError, ValueError, DatabaseError) as error:
            recorded["problems"].append(f"{record_file}: {error}")
    return recorded
