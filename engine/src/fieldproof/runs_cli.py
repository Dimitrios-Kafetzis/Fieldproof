"""``fieldproof runs list|show|diff|index`` (task P2-T06): the run database.

``list`` prints the runs the database knows, newest first (filtered by scenario, status or
matrix); ``show`` prints one run's facts, assertions, KPIs and artefacts; ``diff`` compares two
runs: the KPI deltas, the assertions whose verdict changed, the durations, the seeds and the
overrides; ``index`` reads run directories (and matrix records) into the database, for runs
from before the database or copied from CI artefacts. A run is named by its id, a unique prefix
of it, or a run directory (``show`` and ``diff`` read a directory's ``run.json`` directly, so
they work without the database).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich import box
from rich.markup import escape
from rich.table import Table

from fieldproof.config import ConfigError, load_config
from fieldproof.report import ReportError, build_report, load_record
from fieldproof.rundb import DatabaseError, RunDatabase, index_runs
from fieldproof.scenario_cli import ConfigOption, console, errors, fail

runs_app = typer.Typer(
    name="runs",
    help="The run database: list, show and diff runs, index run directories.",
    no_args_is_help=True,
)

RunsDirOption = Annotated[
    Path | None,
    typer.Option(
        "--runs-dir",
        help="The runs directory whose database to use (default: the workspace's).",
        show_default=False,
    ),
]
DatabaseOption = Annotated[
    Path | None,
    typer.Option("--database", help="The database file itself.", show_default=False),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print as JSON.")]


def database_path(config_path: Path | None, runs_dir: Path | None, database: Path | None) -> Path:
    """Where the database is (a `typer.Exit` when the configuration is unreadable)."""
    if database is not None:
        return database.resolve()
    try:
        config = load_config(config_path)
    except ConfigError as error:
        raise fail(error) from error
    base = runs_dir.resolve() if runs_dir is not None else config.resolve(config.runs.directory)
    name = config.runs.database or "fieldproof.sqlite"
    return base / name


def open_database(path: Path) -> RunDatabase:
    """The database at `path` (a `typer.Exit` when it cannot be opened)."""
    try:
        return RunDatabase(path)
    except DatabaseError as error:
        raise fail(error) from error


def load_report(reference: str, database: Path) -> dict[str, Any]:
    """The report of the run `reference` names: a run directory (or its run.json), or an id."""
    path = Path(reference)
    if reference and path.exists():
        try:
            run_dir = path.parent if path.is_file() else path
            return build_report(load_record(path), run_dir)
        except ReportError as error:
            raise fail(error) from error
    if not database.is_file():
        raise fail(ValueError(f"{reference}: no such run directory, and no database at {database}"))
    with open_database(database) as db:
        try:
            report = db.report(reference)
        except DatabaseError as error:
            raise fail(error) from error
    if report is None:
        raise fail(ValueError(f"{reference}: no such run in {database} (fieldproof runs list)"))
    return report


def _duration(value: Any) -> str:
    return f"{value:.1f} s" if isinstance(value, int | float) else "-"


@runs_app.command("list")
def list_runs(  # noqa: PLR0917
    config_path: ConfigOption = None,
    runs_dir: RunsDirOption = None,
    database: DatabaseOption = None,
    scenario: Annotated[
        str | None, typer.Option("--scenario", help="Only this scenario name.", show_default=False)
    ] = None,
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="Only this status (passed, failed, timeout, error, interrupted).",
            show_default=False,
        ),
    ] = None,
    matrix: Annotated[
        str | None,
        typer.Option("--matrix", help="Only the runs of this matrix id.", show_default=False),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="How many runs.")] = 20,
    as_json: JsonOption = False,
) -> None:
    """List the runs the database knows, newest first."""
    path = database_path(config_path, runs_dir, database)
    if not path.is_file():
        raise fail(
            ValueError(f"no run database at {path} (a run creates it; fieldproof runs index)")
        )
    with open_database(path) as db:
        rows = db.runs(scenario=scenario, status=status, matrix_id=matrix, limit=limit)
        counts = db.counts()
    if as_json:
        typer.echo(json.dumps({"database": str(path), "counts": counts, "runs": rows}, indent=2))
        return
    # One line per run, nothing truncated (the ids are for copying into show and diff).
    for row in rows:
        verdict = str(row["verdict"])
        colour = {"PASS": "green", "FAIL": "red"}.get(verdict, "yellow")
        matrix_cell = (
            f", matrix {row['matrix_id'].rsplit('-', 1)[-1]}/{row['case_id']}#{row['attempt']}"
            + (" flaky" if row.get("flaky") else "")
            if row.get("matrix_id")
            else ""
        )
        console.print(
            f"{escape(str(row['run_id']))}  [{colour}]{verdict:<11}[/{colour}] "
            f"{escape(str(row['scenario_name']))}  {str(row.get('started_at') or '-')[:19]}  "
            f"{_duration(row.get('duration_s'))}, seed "
            f"{row['seed'] if row.get('seed') is not None else '-'}, assertions "
            f"{row['assertions_passed']}/{row['assertions_failed']}/{row['assertions_skipped']}"
            f"{matrix_cell}",
            highlight=False,
            soft_wrap=True,
        )
    console.print(
        f"{len(rows)} of {counts['runs']} run(s) and {counts['matrices']} matrix(es) in {path} "
        "(assertions: passed/failed/skipped)",
        highlight=False,
        soft_wrap=True,
    )


@runs_app.command("show")
def show(
    run: Annotated[
        str,
        typer.Argument(
            help="A run id (or a unique prefix) or a run directory.", show_default=False
        ),
    ],
    config_path: ConfigOption = None,
    runs_dir: RunsDirOption = None,
    database: DatabaseOption = None,
    as_json: JsonOption = False,
) -> None:
    """Show one run: its facts, assertions, KPIs and artefacts."""
    report = load_report(run, database_path(config_path, runs_dir, database))
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return
    scenario = report.get("scenario") or {}
    verdict = str(report.get("verdict"))
    colour = {"PASS": "green", "FAIL": "red"}.get(verdict, "yellow")
    console.print(
        f"[{colour}]{verdict}[/{colour}] {escape(str(scenario.get('name')))} "
        f"({escape(str(report['run_id']))})"
        + (f": {escape(str(report['reason']))}" if report.get("reason") else ""),
        highlight=False,
        soft_wrap=True,
    )
    facts = [
        f"scenario {scenario.get('path')} (sha {scenario.get('digest')}), world "
        f"{report.get('world')}",
        f"started {report.get('started_at')}, finished {report.get('finished_at')}; "
        f"{_duration(report.get('duration_s'))} wall clock, timeline "
        f"{_duration(report.get('timeline_duration_s'))}, simulation "
        f"{_duration(report.get('sim_elapsed_s'))}",
        f"time mode {(report.get('time') or {}).get('mode')}, seed {report.get('seed')}, engine "
        f"{report.get('engine_version')}",
        f"run directory {report.get('run_dir')}",
    ]
    if report.get("overrides"):
        facts.append(
            "overrides " + ", ".join(f"{k}={json.dumps(v)}" for k, v in report["overrides"].items())
        )
    for fact in facts:
        console.print(f"  {escape(fact)}", highlight=False, soft_wrap=True)
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, title="Assertions")
    for column in ("Assertion", "Type", "Verdict", "At", "Reason"):
        table.add_column(column, no_wrap=column != "Reason")
    for assertion in report.get("assertions") or []:
        status = str(assertion.get("status"))
        mark = {"passed": "[green]PASS[/green]", "failed": "[red]FAIL[/red]"}.get(
            status, f"[yellow]{status.upper()}[/yellow]"
        )
        table.add_row(
            escape(str(assertion.get("name"))),
            escape(str(assertion.get("type"))),
            mark,
            _duration(assertion.get("t_s")),
            escape(str(assertion.get("reason") or "-")),
        )
    console.print(table)
    kpis = report.get("kpis") or {}
    if kpis:
        table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, title="KPIs")
        for column in ("KPI", "Value", "Unit", "Source"):
            table.add_column(column, no_wrap=True, justify="right" if column == "Value" else "left")
        for name, kpi in sorted(kpis.items()):
            value = kpi.get("value") if isinstance(kpi, dict) else kpi
            table.add_row(
                escape(name),
                f"{value:.3f}" if isinstance(value, float) else escape(str(value)),
                escape(str(kpi.get("unit") or "-")) if isinstance(kpi, dict) else "-",
                escape(str(kpi.get("source") or "-")) if isinstance(kpi, dict) else "-",
            )
        console.print(table)
    artifacts = report.get("artifacts") or []
    if artifacts:
        console.print("  artefacts: " + ", ".join(escape(str(a.get("path"))) for a in artifacts))


def _numbers(first: Any, second: Any) -> tuple[float, float] | None:
    """Both values as numbers, or None when one is not a number."""
    if any(isinstance(v, bool) or not isinstance(v, int | float) for v in (first, second)):
        return None
    return float(first), float(second)


def diff_reports(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """What differs between two runs' reports: KPIs, assertions, durations, seeds, overrides."""
    kpis_a, kpis_b = a.get("kpis") or {}, b.get("kpis") or {}
    kpis = []
    for name in sorted(set(kpis_a) | set(kpis_b)):
        first, second = kpis_a.get(name) or {}, kpis_b.get(name) or {}
        value_a, value_b = first.get("value"), second.get("value")
        numbers = _numbers(value_a, value_b)
        delta = (numbers[1] - numbers[0]) if numbers is not None else None
        pct = (
            (delta / abs(numbers[0]) * 100.0)
            if delta is not None and numbers and numbers[0]
            else None
        )
        kpis.append(
            {
                "name": name,
                "unit": first.get("unit") or second.get("unit"),
                "a": value_a,
                "b": value_b,
                "delta": delta,
                "pct": pct,
                "same": value_a == value_b,
            }
        )
    by_name_a = {str(x.get("name")): x for x in a.get("assertions") or []}
    by_name_b = {str(x.get("name")): x for x in b.get("assertions") or []}
    assertions = []
    for name in list(by_name_a) + [n for n in by_name_b if n not in by_name_a]:
        status_a = (by_name_a.get(name) or {}).get("status")
        status_b = (by_name_b.get(name) or {}).get("status")
        assertions.append(
            {
                "name": name,
                "a": status_a,
                "b": status_b,
                "changed": status_a != status_b,
                "regressed": status_a == "passed" and status_b == "failed",
                "fixed": status_a == "failed" and status_b == "passed",
            }
        )
    durations = {}
    for key in ("duration_s", "timeline_duration_s", "sim_elapsed_s"):
        value_a, value_b = a.get(key), b.get(key)
        numbers = _numbers(value_a, value_b)
        durations[key] = {
            "a": value_a,
            "b": value_b,
            "delta": (numbers[1] - numbers[0]) if numbers is not None else None,
        }
    overrides_a, overrides_b = a.get("overrides") or {}, b.get("overrides") or {}
    changed_overrides = {
        path: [overrides_a.get(path), overrides_b.get(path)]
        for path in sorted(set(overrides_a) | set(overrides_b))
        if overrides_a.get(path) != overrides_b.get(path)
    }
    scenario_a, scenario_b = a.get("scenario") or {}, b.get("scenario") or {}
    return {
        "runs": [a["run_id"], b["run_id"]],
        "verdicts": [a.get("verdict"), b.get("verdict")],
        "scenario": {
            "names": [scenario_a.get("name"), scenario_b.get("name")],
            "digests": [scenario_a.get("digest"), scenario_b.get("digest")],
            "same": scenario_a.get("digest") == scenario_b.get("digest"),
        },
        "seeds": [a.get("seed"), b.get("seed")],
        "overrides": {"a": overrides_a, "b": overrides_b, "changed": changed_overrides},
        "durations": durations,
        "assertions": assertions,
        "kpis": kpis,
        "counts": {
            "kpis_changed": sum(1 for k in kpis if not k["same"]),
            "assertions_changed": sum(1 for x in assertions if x["changed"]),
            "regressed": sum(1 for x in assertions if x["regressed"]),
            "fixed": sum(1 for x in assertions if x["fixed"]),
        },
    }


def _value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _changed(text: str, *, changed: bool) -> str:
    return f"[yellow]{text}[/yellow]" if changed else text


@runs_app.command("diff")
def diff(  # noqa: PLR0917
    run_a: Annotated[
        str,
        typer.Argument(
            help="The first run: an id, a unique prefix or a directory.", show_default=False
        ),
    ],
    run_b: Annotated[str, typer.Argument(help="The second run.", show_default=False)],
    config_path: ConfigOption = None,
    runs_dir: RunsDirOption = None,
    database: DatabaseOption = None,
    as_json: JsonOption = False,
) -> None:
    """Compare two runs: KPI deltas, assertion verdicts, durations, seeds and overrides."""
    path = database_path(config_path, runs_dir, database)
    result = diff_reports(load_report(run_a, path), load_report(run_b, path))
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    a_id, b_id = result["runs"]
    scenario = result["scenario"]
    console.print(
        f"{escape(str(a_id))} ({result['verdicts'][0]}) vs {escape(str(b_id))} "
        f"({result['verdicts'][1]}): "
        f"scenario {'/'.join(str(n) for n in scenario['names'])}, "
        + (
            "the same document"
            if scenario["same"]
            else f"documents {'/'.join(str(d) for d in scenario['digests'])}"
        )
        + f", seeds {'/'.join(str(s) for s in result['seeds'])}",
        highlight=False,
        soft_wrap=True,
    )
    for path_name, (first, second) in result["overrides"]["changed"].items():
        console.print(
            f"  override {escape(path_name)}: {json.dumps(first)} -> {json.dumps(second)}"
        )
    for key, data in result["durations"].items():
        console.print(
            f"  {key}: {_value(data['a'])} -> {_value(data['b'])}"
            + (f" ({data['delta']:+.1f} s)" if data["delta"] is not None else ""),
            highlight=False,
        )
    changed = [x for x in result["assertions"] if x["changed"]]
    if changed:
        for item in changed:
            colour = "red" if item["regressed"] else "green" if item["fixed"] else "yellow"
            console.print(
                f"  [{colour}]assertion {escape(item['name'])}: {item['a']} -> {item['b']}"
                f"[/{colour}]",
                highlight=False,
            )
    else:
        console.print(f"  no assertion changed its verdict ({len(result['assertions'])} compared)")
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, title="KPIs")
    for column in ("KPI", "A", "B", "Delta", "%", "Unit"):
        table.add_column(
            column, no_wrap=True, justify="left" if column in ("KPI", "Unit") else "right"
        )
    for kpi in result["kpis"]:
        delta = kpi["delta"]
        table.add_row(
            escape(kpi["name"]),
            _value(kpi["a"]),
            _value(kpi["b"]),
            _changed(f"{delta:+.3f}" if delta is not None else "-", changed=not kpi["same"]),
            _changed(
                f"{kpi['pct']:+.1f}" if kpi["pct"] is not None else "-", changed=not kpi["same"]
            ),
            escape(str(kpi["unit"] or "-")),
        )
    if result["kpis"]:
        console.print(table)
    else:
        console.print("  no KPI in either run")
    counts = result["counts"]
    console.print(
        f"{counts['kpis_changed']} KPI(s) changed, {counts['assertions_changed']} assertion(s) "
        f"changed ({counts['regressed']} regressed, {counts['fixed']} fixed)",
        highlight=False,
    )


@runs_app.command("index")
def index(
    config_path: ConfigOption = None,
    runs_dir: RunsDirOption = None,
    database: DatabaseOption = None,
    root: Annotated[
        Path | None,
        typer.Argument(
            help="Where to look for run directories (default: the runs directory).",
            show_default=False,
        ),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Read run directories (and matrix records) under ROOT into the database."""
    path = database_path(config_path, runs_dir, database)
    if root is None:
        root = path.parent
    if not root.is_dir():
        raise fail(ValueError(f"{root}: not a directory"))
    with open_database(path) as db:
        recorded = index_runs(db, root.resolve())
        counts = db.counts()
    if as_json:
        typer.echo(
            json.dumps(
                {"database": str(path), "root": str(root), **recorded, "counts": counts}, indent=2
            )
        )
        return
    console.print(
        f"indexed {len(recorded['runs'])} run(s) and {len(recorded['matrices'])} matrix(es) under "
        f"{root} into {path} ({counts['runs']} run(s), {counts['matrices']} matrix(es) in all)",
        highlight=False,
        soft_wrap=True,
    )
    for problem in recorded["problems"]:
        errors.print(
            f"[yellow]warning:[/yellow] {escape(problem)}", highlight=False, soft_wrap=True
        )
    raise typer.Exit(code=0)
