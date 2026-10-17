"""``fieldproof validate``, ``fieldproof run`` and ``fieldproof schema`` (task P1-T05).

``validate`` reads scenarios against schema v0 and resolves them against the workspace (the
world file and its frames, every vehicle's manifest, the timeline's goals) without touching
Docker; ``run`` does the same and then executes the plan with the Docker backend, writing
``runs/<run_id>/``; ``schema`` prints the JSON Schema of a scenario file.
"""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from fieldproof.backend import BackendError
from fieldproof.config import ConfigError, load_config
from fieldproof.runner import (
    EXIT_CODES,
    LOG_FILE,
    Plan,
    Run,
    RunLog,
    Status,
    leftovers,
    plan,
)
from fieldproof.scenario import Scenario, ScenarioError, json_schema, load_scenario
from fieldproof_bridge.manifest import ManifestError

console = Console()
errors = Console(stderr=True)

ScenarioArg = Annotated[Path, typer.Argument(help="The scenario file.", show_default=False)]
ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Workspace configuration (default: the nearest fieldproof.toml above the scenario).",
        show_default=False,
    ),
]
RenderOption = Annotated[
    str | None,
    typer.Option(
        "--render", help="Simulator rendering: auto, gpu or software.", show_default=False
    ),
]


def with_file(path: Path, error: Exception) -> str:
    """`<path>: <error>`, unless the error already names the file."""
    text = str(error)
    if text.startswith((str(path), str(path.resolve()))):
        return text
    return f"{path}: {text}"


def fail(error: Exception) -> typer.Exit:
    """Print the reason and exit with the usage code."""
    errors.print(f"[red]error:[/red] {escape(str(error))}", highlight=False, soft_wrap=True)
    return typer.Exit(code=2)


def resolve(
    scenario_path: Path,
    config_path: Path | None,
    *,
    runs_dir: Path | None = None,
    render: str | None = None,
    domain_id: int | None = None,
    network: str | None = None,
    run_id: str | None = None,
) -> tuple[Scenario, Plan]:
    """Load the scenario and the configuration and plan the run."""
    scenario = load_scenario(scenario_path)
    config = load_config(config_path, start=scenario.directory)
    if render is not None and render not in ("auto", "gpu", "software"):
        raise ScenarioError(f"--render {render!r}: expected auto, gpu or software")
    resolved = plan(
        scenario,
        config,
        run_id=run_id,
        runs_dir=runs_dir,
        render=render,
        domain_id=domain_id,
        network=network,
    )
    return scenario, resolved


def describe(scenario: Scenario, resolved: Plan) -> str:
    """One line about a resolved scenario."""
    vehicles = ", ".join(
        f"{v.name} ({v.manifest.name}, {v.container.image})" for v in resolved.vehicles
    )
    return (
        f"world {resolved.world.name}, {len(resolved.vehicles)} vehicle(s): {vehicles}; "
        f"{len(resolved.actions)} timeline entr{'y' if len(resolved.actions) == 1 else 'ies'}, "
        f"{len(scenario.assertions)} assertion(s), time {scenario.time.mode}, "
        f"budget {scenario.time.timeout_s:g} s"
    )


def validate(
    scenarios: Annotated[
        list[Path], typer.Argument(help="Scenario files to validate.", show_default=False)
    ],
    config_path: ConfigOption = None,
) -> None:
    """Validate SCENARIOS against schema v0 and resolve them against the workspace.

    The world file must exist in the workspace's worlds directory and declare every frame the
    scenario names; every vehicle manifest must load with a runtime section; no container is
    started. Exit code 2 with one line per problem otherwise.
    """
    failed = False
    for path in scenarios:
        try:
            scenario, resolved = resolve(path, config_path)
        except (ScenarioError, ConfigError, ManifestError) as error:
            errors.print(
                with_file(path, error), style="red", highlight=False, soft_wrap=True, markup=False
            )
            failed = True
            continue
        console.print(
            f"[green]ok[/green] {escape(str(path))}: {scenario.name} "
            f"({escape(describe(scenario, resolved))})",
            highlight=False,
            soft_wrap=True,
        )
        if scenario.time.mode == "realtime":
            console.print(
                "   [yellow]note[/yellow] time.mode realtime: the real-time-locked mode arrives "
                "with task P2-T04, so this scenario validates but does not run yet",
                highlight=False,
            )
    if failed:
        raise typer.Exit(code=2)


def schema() -> None:
    """Print the JSON Schema of a scenario file."""
    typer.echo(json.dumps(json_schema(), indent=2))


def run(  # noqa: PLR0917  (Typer reads the options from the signature)
    scenario_path: ScenarioArg,
    config_path: ConfigOption = None,
    runs_dir: Annotated[
        Path | None,
        typer.Option(
            "--runs-dir",
            help="Where to write runs/<run_id> (default: the workspace's runs directory).",
            show_default=False,
        ),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id", help="Use this run id instead of a generated one.", show_default=False
        ),
    ] = None,
    render: RenderOption = None,
    domain_id: Annotated[
        int | None,
        typer.Option("--domain-id", help="ROS_DOMAIN_ID of the containers.", show_default=False),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Docker network the containers join.", show_default=False),
    ] = None,
    keep: Annotated[
        bool, typer.Option("--keep", help="Leave the containers running at the end.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="No output of the containers' commands.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Resolve the scenario and print the plan as JSON.")
    ] = False,
) -> None:
    """Run SCENARIO: start the simulator, the bridges and the vehicles, run the timeline.

    Everything the run did lands in runs/<run_id>/ (the scenario as run, run.json, engine.log,
    the containers' logs). Exit code 0 when the run passed, 1 when it failed, timed out or could
    not start, 130 when interrupted; the containers are stopped and removed in every case
    unless --keep is given.
    """
    try:
        scenario, resolved = resolve(
            scenario_path,
            config_path,
            runs_dir=runs_dir,
            render=render,
            domain_id=domain_id,
            network=network,
            run_id=run_id,
        )
    except (ScenarioError, ConfigError, ManifestError) as error:
        raise fail(error) from error
    if dry_run:
        typer.echo(json.dumps(resolved.to_dict(), indent=2))
        return
    try:
        from fieldproof.docker_backend import DockerBackend  # noqa: PLC0415  (Docker only here)

        backend = DockerBackend()
    except BackendError as error:
        raise fail(error) from error
    log = RunLog(
        resolved.run_id, file=resolved.run_dir / LOG_FILE, console=console, verbose=not quiet
    )
    execution = Run(resolved, backend, log, keep=keep)
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    def interrupt(signum: int, _frame: FrameType | None) -> None:
        if execution.tearing_down:
            log.event(
                "signal.ignored",
                f"{signal.Signals(signum).name} during the teardown",
                level="warning",
            )
            return
        raise KeyboardInterrupt

    for signum in previous:
        signal.signal(signum, interrupt)
    try:
        record = execution.execute()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    status = Status(record.status or Status.ERROR)
    colour = {Status.PASSED: "green", Status.INTERRUPTED: "yellow"}.get(status, "red")
    console.print(
        f"[{colour}]{status}[/{colour}] {scenario.name} ({record.run_id}): "
        f"{escape(record.reason or 'the timeline completed')}; run directory {record.run_dir}",
        highlight=False,
        soft_wrap=True,
    )
    remaining = leftovers(backend, record.run_id) if not keep else []
    if remaining:
        errors.print(
            f"[red]warning:[/red] containers left behind: {escape(', '.join(remaining))}",
            highlight=False,
        )
    raise typer.Exit(code=EXIT_CODES[status])


def print_plan(resolved: Plan) -> None:
    """Print a plan as JSON (used by tests of the dry run)."""
    data: dict[str, Any] = resolved.to_dict()
    sys.stdout.write(json.dumps(data, indent=2) + "\n")
