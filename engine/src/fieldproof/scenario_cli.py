"""``fieldproof validate``, ``run``, ``schema`` (task P1-T05) and ``report`` (task P1-T06).

``validate`` reads scenarios against schema v0 and resolves them against the workspace (the
world file and its frames, every vehicle's manifest, the timeline's goals, the assertions)
without touching Docker; ``run`` does the same and then executes the plan with the Docker
backend, writing ``runs/<run_id>/`` with its report; ``schema`` prints the JSON Schema of a
scenario file; ``report`` renders the report of a run directory again.
"""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from fieldproof.assertions import parameters_of as assertion_parameters
from fieldproof.assertions import registry as assertion_registry
from fieldproof.assertions.kpi import KPIS
from fieldproof.backend import BackendError
from fieldproof.config import ConfigError, load_config
from fieldproof.determinism import DEFAULT_TOLERANCE_M, CompareError, compare_runs
from fieldproof.events import parameters_of, registry
from fieldproof.report import (
    REPORT_JSON,
    REPORT_JUNIT,
    REPORT_MD,
    ReportError,
    build_report,
    load_record,
    render_junit,
    render_markdown,
    write_reports,
)
from fieldproof.runner import (
    EXIT_CODES,
    LOG_FILE,
    PROBE_LIMIT_S,
    RUN_LABEL,
    Plan,
    Run,
    RunLog,
    Status,
    leftovers,
    plan,
)
from fieldproof.scenario import (
    Scenario,
    ScenarioError,
    VideoOutput,
    json_schema,
    load_scenario,
)
from fieldproof.world import parse_stats
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
    discovery: str | None = None,
    run_id: str | None = None,
    video: bool | None = None,
    seed: int | None = None,
) -> tuple[Scenario, Plan]:
    """Load the scenario and the configuration and plan the run.

    `video` overrides `outputs.video` (`--video` turns the world's default camera on,
    `--no-video` turns the recording off); `seed` overrides the scenario's seed.
    """
    scenario = load_scenario(scenario_path)
    if video is not None:
        scenario.outputs.video = VideoOutput() if video else None
    config = load_config(config_path, start=scenario.directory)
    if render is not None and render not in ("auto", "gpu", "software"):
        raise ScenarioError(f"--render {render!r}: expected auto, gpu or software")
    if discovery is not None and discovery not in ("multicast", "server"):
        raise ScenarioError(f"--discovery {discovery!r}: expected multicast or server")
    resolved = plan(
        scenario,
        config,
        run_id=run_id,
        runs_dir=runs_dir,
        render=render,
        domain_id=domain_id,
        network=network,
        discovery=discovery,
        seed=seed,
    )
    return scenario, resolved


def describe(scenario: Scenario, resolved: Plan) -> str:
    """One line about a resolved scenario."""
    vehicles = ", ".join(
        f"{v.name} ({v.manifest.name}, {v.container.image}"
        + (f", /{v.namespace}" if v.namespace else "")
        + ")"
        for v in resolved.vehicles
    )
    return (
        f"world {resolved.world.name}, {len(resolved.vehicles)} vehicle(s): {vehicles}; "
        f"{len(resolved.actions)} timeline entr{'y' if len(resolved.actions) == 1 else 'ies'}, "
        f"{len(scenario.assertions)} assertion(s), time {scenario.time.mode}, "
        f"budget {scenario.time.timeout_s:g} s"
    )


def clock_note(scenario: Scenario) -> str:
    """What the scenario's time mode means for the run (task P2-T04), for `validate`."""
    time_section = scenario.time
    if time_section.mode == "realtime":
        return (
            "time.mode realtime: the real-time factor is pinned to 1.0 and the drift between "
            f"simulation time and the wall clock is judged over the timeline (budget "
            f"{time_section.max_drift_s:g} s, on drift: {time_section.on_drift})"
        )
    if time_section.mode == "lockstep":
        return (
            f"time.mode lockstep: the simulator steps {time_section.step_s:g} s at a time once "
            "every vehicle acknowledged the last step; the timeline and the assertions count "
            "simulation time"
        )
    return ""


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
        note = clock_note(scenario)
        if note:
            console.print(f"   note {escape(note)}", highlight=False, soft_wrap=True)
    for problem in registry().problems:
        errors.print(
            f"warning: event plugin not loaded: {escape(problem)}", style="yellow", highlight=False
        )
    for problem in assertion_registry().problems:
        errors.print(
            f"warning: assertion plugin not loaded: {escape(problem)}",
            style="yellow",
            highlight=False,
        )
    if failed:
        raise typer.Exit(code=2)


def schema() -> None:
    """Print the JSON Schema of a scenario file."""
    typer.echo(json.dumps(json_schema(), indent=2))


def events(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the registry as JSON instead of a table.")
    ] = False,
) -> None:
    """List the timeline actions the engine knows: the built-in events and the installed plugins.

    Each row is an `action` a scenario's timeline can name (docs/reference/events.md), where it
    comes from (built-in, or the distribution that registered it through the `fieldproof.events`
    entry point group) and its parameters. A plugin that could not be loaded is reported after
    the table; exit code 2 then.
    """
    events_known = registry()
    rows = [
        {
            "action": action,
            "source": registered.source,
            "summary": registered.event.summary,
            "parameters": parameters_of(registered.entry_type),
        }
        for action, registered in sorted(events_known.events.items())
    ]
    if json_output:
        typer.echo(json.dumps({"events": rows, "problems": events_known.problems}, indent=2))
    else:
        table = Table(title=f"{len(rows)} timeline action(s)", box=box.SIMPLE_HEAD)
        table.add_column("Action", style="bold", no_wrap=True)
        table.add_column("Source", no_wrap=True)
        table.add_column("Summary")
        table.add_column("Parameters")
        for row in rows:
            table.add_row(
                str(row["action"]),
                str(row["source"]),
                str(row["summary"]),
                ", ".join(row["parameters"]) or "-",
            )
        console.print(table)
        for problem in events_known.problems:
            errors.print(f"warning: {escape(problem)}", style="yellow", highlight=False)
    if events_known.problems:
        raise typer.Exit(code=2)


def assertions(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the registry as JSON instead of tables.")
    ] = False,
) -> None:
    """List the assertion types and the KPI collectors the engine knows.

    Each row is a `type` a scenario's `assertions` (or `kpis`) can name
    (docs/reference/assertions.md), where it comes from (built-in, or the distribution that
    registered it through the `fieldproof.assertions` entry point group), its parameters and
    the metrics it reports. A plugin that could not be loaded is reported after the tables;
    exit code 2 then.
    """
    known = assertion_registry()
    rows = [
        {
            "type": kind,
            "source": registered.source,
            "summary": registered.assertion_type.summary,
            "parameters": assertion_parameters(registered.spec),
            "metrics": dict(registered.assertion_type.metrics),
        }
        for kind, registered in sorted(known.types.items())
    ]
    collectors = [
        {
            "type": kind,
            "summary": kpi_type.summary,
            "parameters": assertion_parameters(kpi_type.Spec),
            "metrics": dict(kpi_type.metrics),
        }
        for kind, kpi_type in sorted(KPIS.items())
    ]
    if json_output:
        typer.echo(
            json.dumps(
                {"assertions": rows, "kpis": collectors, "problems": known.problems}, indent=2
            )
        )
    else:
        table = Table(title=f"{len(rows)} assertion type(s)", box=box.SIMPLE_HEAD)
        table.add_column("Type", style="bold", no_wrap=True)
        table.add_column("Source", no_wrap=True)
        table.add_column("Summary")
        table.add_column("Parameters")
        for row in rows:
            table.add_row(
                str(row["type"]),
                str(row["source"]),
                str(row["summary"]),
                ", ".join(row["parameters"]) or "-",
            )
        console.print(table)
        table = Table(title=f"{len(collectors)} KPI collector(s)", box=box.SIMPLE_HEAD)
        table.add_column("Type", style="bold", no_wrap=True)
        table.add_column("Summary")
        table.add_column("Metrics")
        for row in collectors:
            table.add_row(str(row["type"]), str(row["summary"]), ", ".join(row["metrics"]) or "-")
        console.print(table)
        for problem in known.problems:
            errors.print(f"warning: {escape(problem)}", style="yellow", highlight=False)
    if known.problems:
        raise typer.Exit(code=2)


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
    discovery: Annotated[
        str | None,
        typer.Option(
            "--discovery",
            help="DDS discovery of the run: multicast, or server (a Fast DDS discovery server).",
            show_default=False,
        ),
    ] = None,
    check_contracts: Annotated[
        bool,
        typer.Option(
            "--check-contracts",
            help="Run every vehicle's bridge contract check once its bridge is up.",
        ),
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Leave the containers running at the end.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="No output of the containers' commands.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Resolve the scenario and print the plan as JSON.")
    ] = False,
    video: Annotated[
        bool | None,
        typer.Option(
            "--video/--no-video",
            help="Record (or not) a video from the world's camera, whatever outputs.video says.",
            show_default=False,
        ),
    ] = None,
    seed: Annotated[
        int | None,
        typer.Option(
            "--seed",
            help="The seed of the run's random numbers, overriding the scenario's.",
            show_default=False,
        ),
    ] = None,
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
            discovery=discovery,
            run_id=run_id,
            video=video,
            seed=seed,
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
    execution = Run(resolved, backend, log, keep=keep, check_contracts=check_contracts)
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
    verdicts = {Status.PASSED: "PASS", Status.FAILED: "FAIL"}
    console.print(
        f"[{colour}]{verdicts.get(status, str(status).upper())}[/{colour}] {scenario.name} "
        f"({record.run_id}): "
        f"{escape(record.reason or 'the timeline completed and every assertion passed')}; "
        f"run directory {record.run_dir}",
        highlight=False,
        soft_wrap=True,
    )
    for assertion in record.assertions:
        mark = {"passed": "[green]PASS[/green]", "failed": "[red]FAIL[/red]"}.get(
            str(assertion["status"]), f"[yellow]{str(assertion['status']).upper()}[/yellow]"
        )
        console.print(
            f"  {mark} {escape(str(assertion['name']))} ({assertion['type']}): "
            f"{escape(str(assertion.get('reason') or ''))}",
            highlight=False,
            soft_wrap=True,
        )
    if (resolved.run_dir / REPORT_MD).is_file():
        console.print(f"  report: {resolved.run_dir / REPORT_MD}", highlight=False, soft_wrap=True)
    if record.video and record.video.get("status") == "saved":
        console.print(
            f"  video: {resolved.run_dir / str(record.video['file'])}",
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


def report(
    run: Annotated[
        Path,
        typer.Argument(
            help="A run directory, its run.json, or a run id under the workspace's runs directory.",
            show_default=False,
        ),
    ],
    config_path: ConfigOption = None,
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="md (Markdown), json or junit (JUnit XML).")
    ] = "md",
    write: Annotated[
        bool,
        typer.Option(
            "--write", help="Rewrite report.md, report.json and junit.xml in the run dir."
        ),
    ] = False,
) -> None:
    """Render the report of a run from its run.json (Markdown by default)."""
    if output_format not in ("md", "json", "junit"):
        raise fail(ValueError(f"--format {output_format!r}: expected md, json or junit"))
    run_dir = run_directory(run, config_path)
    try:
        record = load_record(run_dir)
        rendered = write_reports(record, run_dir) if write else build_report(record, run_dir)
    except ReportError as error:
        raise fail(error) from error
    if output_format == "json":
        typer.echo(json.dumps(rendered, indent=2))
    elif output_format == "junit":
        typer.echo(render_junit(rendered), nl=False)
    else:
        typer.echo(render_markdown(rendered), nl=False)
    if write:
        errors.print(
            f"written: {run_dir / REPORT_MD}, {run_dir / REPORT_JSON}, {run_dir / REPORT_JUNIT}",
            highlight=False,
        )


RunArg = Annotated[
    Path,
    typer.Argument(
        help="A run directory, its run.json, or a run id under the workspace's runs directory.",
        show_default=False,
    ),
]


def run_directory(run: Path, config_path: Path | None) -> Path:
    """The run directory a command's RUN argument names (a `typer.Exit` when there is none)."""
    run_dir = run
    if not run.exists():
        try:
            config = load_config(config_path)
        except ConfigError as error:
            raise fail(error) from error
        run_dir = config.resolve(config.runs.directory) / run.name
        if not run_dir.is_dir():
            raise fail(ValueError(f"{run}: no such run directory or run id (looked in {run_dir})"))
    if run_dir.is_file():
        run_dir = run_dir.parent
    return run_dir


CLOCK_COMMANDS = ("status", "pause", "resume", "step")


def clock(  # noqa: PLR0917
    run: RunArg,
    command: Annotated[
        str, typer.Argument(help="status, pause, resume or step.", show_default=False)
    ] = "status",
    config_path: ConfigOption = None,
    steps: Annotated[
        int | None,
        typer.Option("--steps", help="step: physics steps to take (one by default)."),
    ] = None,
    seconds: Annotated[
        float | None,
        typer.Option("--seconds", help="step: simulation seconds to advance instead."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the world's statistics as JSON (status).")
    ] = False,
) -> None:
    """Pause, resume or step the simulator of a running RUN, or show its clock.

    The run is found by its directory or id; its simulator container must be running (a run in
    progress, or one kept with --keep). Lockstep runs step their own clock: pause and resume
    them with the scenario's timeline instead.
    """
    if command not in CLOCK_COMMANDS:
        raise fail(ValueError(f"{command!r}: expected one of {', '.join(CLOCK_COMMANDS)}"))
    run_dir = run_directory(run, config_path)
    try:
        record = load_record(run_dir)
    except ReportError as error:
        raise fail(error) from error
    world = str((record.get("stack") or {}).get("world") or "")
    run_id = str(record.get("run_id") or "")
    if not world or not run_id:
        raise fail(ValueError(f"{run_dir}: run.json names no world or run id"))
    try:
        from fieldproof.docker_backend import DockerBackend  # noqa: PLC0415  (Docker only here)

        backend = DockerBackend()
        sims = [
            name
            for name in backend.find(RUN_LABEL, run_id)
            if name.endswith("-sim") and backend.state(name).running
        ]
    except BackendError as error:
        raise fail(error) from error
    if not sims:
        raise fail(ValueError(f"{run_id}: no running simulator container (is the run going?)"))
    sim = sims[0]
    step_s = float(((record.get("stack") or {}).get("world_step_s")) or 0.001)
    request = clock_request(command, steps, seconds, step_s)
    argv = (
        f"timeout {PROBE_LIMIT_S} gz topic -e -n 1 -t /world/{world}/stats"
        if request is None
        else f"timeout {PROBE_LIMIT_S} gz service -s /world/{world}/control "
        f"--reqtype gz.msgs.WorldControl --reptype gz.msgs.Boolean --timeout 5000 "
        f"--req '{request}'"
    )
    try:
        result = backend.exec(sim, ("bash", "-lc", argv))
    except BackendError as error:
        raise fail(error) from error
    if not result.ok or (request is not None and "true" not in result.output):
        errors.print(f"[red]error:[/red] {escape(result.tail())}", highlight=False)
        raise typer.Exit(code=1)
    if request is not None:
        typer.echo(f"{run_id}: {command} accepted ({request}) by {sim}")
        return
    stats = parse_stats(result.output)
    if as_json:
        typer.echo(json.dumps({"run_id": run_id, "world": world, **stats}, indent=2))
    else:
        typer.echo(
            f"{run_id}: world {world} {'paused' if stats.get('paused') else 'running'} at "
            f"simulation time {stats.get('sim_time_s', 0.0):.3f} s, iteration "
            f"{stats.get('iterations', 0)}, real-time factor {stats.get('rtf', 0.0):.3f}"
        )


def clock_request(
    command: str, steps: int | None, seconds: float | None, step_s: float
) -> str | None:
    """The `WorldControl` request of a clock command (None for `status`)."""
    if command == "status":
        return None
    if command == "pause":
        return "pause: true"
    if command == "resume":
        return "pause: false"
    count = steps if steps is not None else 1
    if seconds is not None:
        count = max(1, round(seconds / step_s))
    return f"multi_step: {count} pause: true"


def compare(
    run_a: RunArg,
    run_b: RunArg,
    config_path: ConfigOption = None,
    tolerance_m: Annotated[
        float,
        typer.Option("--tolerance-m", help="The largest deviation that counts as the same."),
    ] = DEFAULT_TOLERANCE_M,
    as_json: Annotated[bool, typer.Option("--json", help="Print the comparison as JSON.")] = False,
) -> None:
    """Compare the vehicles' trajectories of two runs of a scenario (task P2-T04).

    The ground truth of every vehicle both runs recorded is compared in timeline time: the
    second run interpolated at the first run's samples. Exit code 0 when every vehicle stays
    within --tolerance-m, 1 otherwise, 2 when a run cannot be compared.
    """
    first, second = run_directory(run_a, config_path), run_directory(run_b, config_path)
    try:
        result = compare_runs(first, second, tolerance_m)
    except (ReportError, CompareError, OSError) as error:
        raise fail(error) from error
    if as_json:
        typer.echo(json.dumps(result, indent=2))
    else:
        ids = " and ".join(str(i) for i in result["run_ids"])
        modes = "/".join(str(m) for m in result["modes"])
        seeds = "/".join(str(s) for s in result["seeds"])
        typer.echo(f"{ids} ({modes}, seeds {seeds}), tolerance {tolerance_m:g} m:")
        for name, data in result["vehicles"].items():
            if data["max_m"] is None:
                typer.echo(f"  {name}: no overlapping samples")
                continue
            typer.echo(
                f"  {name}: largest deviation {data['max_m']:.3f} m at {data['at_s']:.1f} s, "
                f"mean {data['mean_m']:.3f} m, final {data['final_m']:.3f} m over "
                f"{data['span_s']:.1f} s ({data['samples']} samples)"
            )
        for name in result["missing"]:
            typer.echo(f"  {name}: in one run only")
        verdict = "SAME" if result["within_tolerance"] else "DIFFERENT"
        colour = "green" if result["within_tolerance"] else "red"
        console.print(f"[{colour}]{verdict}[/{colour}] within {tolerance_m:g} m", highlight=False)
    raise typer.Exit(code=0 if result["within_tolerance"] else 1)


def print_plan(resolved: Plan) -> None:
    """Print a plan as JSON (used by tests of the dry run)."""
    data: dict[str, Any] = resolved.to_dict()
    sys.stdout.write(json.dumps(data, indent=2) + "\n")
