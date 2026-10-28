"""``fieldproof matrix`` (task P2-T06): run a matrix file.

The command reads the matrix, expands and resolves its cases against the workspace (every case
planned before anything runs), measures the host through the Docker daemon, then drives the
cases through :class:`fieldproof.matrix.MatrixRun`: each case a ``fieldproof run`` process,
as many at once as the host fits, failed cases retried and flagged when flaky. The matrix
directory ends up with ``matrix.json``, ``matrix.log``, one ``junit.xml`` for every case,
``report.md``, ``report.html`` and the attempts' run directories; the run database gets the
matrix and its runs. Exit code 0 when every case passed, 1 otherwise, 130 when interrupted, 2
for a bad matrix or case.
"""

from __future__ import annotations

import fnmatch
import json
import os
import signal
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import typer
from rich.markup import escape

from fieldproof.backend import BackendError, HostInfo
from fieldproof.config import ConfigError, load_config
from fieldproof.matrix import (
    MATRIX_HTML,
    MATRIX_JUNIT,
    MATRIX_LOG,
    MATRIX_MD,
    MATRIX_RECORD,
    Capacity,
    Case,
    MatrixError,
    MatrixRun,
    command_from_env,
    host_info,
    load_matrix,
    make_matrix_id,
    render_matrix_markdown,
    resolve_cases,
)
from fieldproof.runner import RunLog
from fieldproof.scenario_cli import ConfigOption, RenderOption, console, errors, fail

GITHUB_ACTIONS = "GITHUB_ACTIONS"


def select_cases(cases: list[Case], patterns: list[str] | None) -> list[Case]:
    """The cases whose id or name matches one of the patterns (all without patterns)."""
    if not patterns:
        return cases
    return [
        c
        for c in cases
        if any(fnmatch.fnmatchcase(c.id, p) or fnmatch.fnmatchcase(c.name, p) for p in patterns)
    ]


def parse_parallel(value: str | None) -> int | None:
    """`--parallel`: a positive number, or `auto` as None."""
    if value is None or value.strip() == "auto":
        return None
    if not value.strip().isdigit() or int(value) < 1:
        raise MatrixError(f"--parallel {value!r}: expected a positive number or auto")
    return int(value)


def matrix(  # noqa: PLR0912, PLR0915, PLR0917  (Typer reads the options from the signature)
    matrix_path: Annotated[Path, typer.Argument(help="The matrix file.", show_default=False)],
    config_path: ConfigOption = None,
    runs_dir: Annotated[
        Path | None,
        typer.Option(
            "--runs-dir",
            help="Where the matrix directory goes (default: the workspace's runs directory).",
            show_default=False,
        ),
    ] = None,
    parallel: Annotated[
        str | None,
        typer.Option(
            "--parallel",
            help="Cases at once: a number, or auto (the host's cores and memory decide); "
            "overrides the file's.",
            show_default=False,
        ),
    ] = None,
    retries: Annotated[
        int | None,
        typer.Option(
            "--retries",
            min=0,
            help="Retries per failed case; overrides the file's.",
            show_default=False,
        ),
    ] = None,
    render: RenderOption = None,
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help="Run only the cases whose id or name matches (glob); repeatable.",
            show_default=False,
        ),
    ] = None,
    fail_on_flaky: Annotated[
        bool | None,
        typer.Option(
            "--fail-on-flaky/--allow-flaky",
            help="Whether a case that passed only on a retry fails the matrix; overrides the "
            "file's.",
            show_default=False,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Resolve the cases and print them with the schedule as JSON."
        ),
    ] = False,
    summary: Annotated[
        Path | None,
        typer.Option(
            "--summary",
            help="Append the Markdown report to this file (default: $GITHUB_STEP_SUMMARY when "
            "set).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Run MATRIX: every case of the sweep as its own run, in parallel, retried, in one JUnit.

    Exit code 0 when every case passed, 1 when a case failed (or was flaky with
    --fail-on-flaky), 130 when interrupted, 2 when the matrix or a case cannot be resolved.
    """
    try:
        spec = load_matrix(matrix_path)
        config = load_config(config_path, start=spec.directory)
        requested = parse_parallel(parallel)
        if render is not None and render not in ("auto", "gpu", "software"):
            raise MatrixError(f"--render {render!r}: expected auto, gpu or software")
        cases = select_cases(spec.expand(), only)
        if not cases:
            raise MatrixError(f"--only {' '.join(only or [])}: no case matches")
        resolved = resolve_cases(spec, config, cases, render=render)
    except (MatrixError, ConfigError) as error:
        raise fail(error) from error
    if spec.outputs.directory is not None:
        base = config.resolve(spec.outputs.directory)
    elif runs_dir is not None:
        base = runs_dir.resolve()
    else:
        base = config.resolve(config.runs.directory)
    matrix_id = make_matrix_id(spec, datetime.now(UTC), uuid.uuid4().hex[:6])
    matrix_dir = base / matrix_id
    database = base / config.runs.database if config.runs.database else None
    host: HostInfo
    if dry_run:
        host = host_info()
    else:
        try:
            from fieldproof.docker_backend import DockerBackend  # noqa: PLC0415  (Docker only here)

            host = DockerBackend().host()
        except BackendError as error:
            raise fail(error) from error
    run_args: list[str] = []
    if config_path is not None:
        run_args += ["--config", str(config_path.resolve())]
    if render is not None:
        run_args += ["--render", render]
    log = RunLog(
        matrix_id,
        file=None if dry_run else matrix_dir / MATRIX_LOG,
        console=None if dry_run else console,
        component="matrix",
    )
    execution = MatrixRun(
        spec,
        resolved,
        matrix_id=matrix_id,
        matrix_dir=matrix_dir,
        capacity=Capacity.of(host),
        log=log,
        parallel=requested,
        retries=retries,
        fail_on_flaky=fail_on_flaky,
        command=command_from_env(),
        run_args=run_args,
        database=database,
    )
    if dry_run:
        typer.echo(json.dumps(execution.record(), indent=2))
        return
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        execution.interrupt()

    for signum in previous:
        signal.signal(signum, interrupt)
    try:
        record = execution.execute()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print_cases(record)
    status = str(record.get("status"))
    colour = {"passed": "green", "interrupted": "yellow"}.get(status, "red")
    console.print(
        f"[{colour}]{record['verdict']}[/{colour}] {spec.name} ({matrix_id}): "
        f"{escape(str(record.get('reason')))}; matrix directory {matrix_dir}",
        highlight=False,
        soft_wrap=True,
    )
    console.print(
        f"  junit: {matrix_dir / MATRIX_JUNIT}; report: {matrix_dir / MATRIX_MD}, "
        f"{matrix_dir / MATRIX_HTML}; record: {matrix_dir / MATRIX_RECORD}",
        highlight=False,
        soft_wrap=True,
    )
    summary_file = summary or (
        Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None
    )
    if summary_file is not None:
        try:
            with summary_file.open("a") as handle:
                handle.write(render_matrix_markdown(record))
        except OSError as error:
            errors.print(f"[red]warning:[/red] cannot write the summary: {escape(str(error))}")
    if os.environ.get(GITHUB_ACTIONS) == "true":
        annotate(record)
    code = record.get("exit_code")
    raise typer.Exit(code=int(code) if code is not None else 1)


def print_cases(record: dict[str, Any]) -> None:
    """The cases, one line each (nothing truncated: the run ids are for copying)."""
    for case in record.get("cases") or []:
        attempts = case.get("attempts") or []
        final = attempts[-1] if attempts else {}
        verdict = str(final.get("verdict") or str(case.get("status", "-")).upper())
        colour = {"PASS": "green", "FAIL": "red"}.get(verdict, "yellow")
        duration = final.get("duration_s")
        console.print(
            f"  {case.get('id')} [{colour}]{verdict}[/{colour}]"
            + (" [yellow](flaky)[/yellow]" if case.get("flaky") else "")
            + f" {escape(str(case.get('name')))}: {len(attempts)} attempt(s), "
            + (f"{duration:.1f} s" if isinstance(duration, int | float) else "-")
            + f", run {escape(str(final.get('run_id') or '-'))}",
            highlight=False,
            soft_wrap=True,
        )


def annotate(record: dict[str, Any]) -> None:
    """GitHub Actions annotations for the flaky and the failed cases."""
    matrix_name = (record.get("matrix") or {}).get("name")
    for case in record.get("cases") or []:
        attempts = case.get("attempts") or []
        final = attempts[-1] if attempts else {}
        if case.get("flaky"):
            sys.stdout.write(
                f"::warning title=Flaky case {case.get('id')} of {matrix_name}::"
                f"{case.get('name')} passed on attempt {len(attempts)}; see "
                f"{attempts[0].get('run_dir')}\n"
            )
        elif str(case.get("status")) in ("failed", "timeout", "error"):
            sys.stdout.write(
                f"::error title=Case {case.get('id')} of {matrix_name} {final.get('verdict')}::"
                f"{case.get('name')}: {final.get('reason')}; run directory {final.get('run_dir')}\n"
            )
    sys.stdout.flush()
