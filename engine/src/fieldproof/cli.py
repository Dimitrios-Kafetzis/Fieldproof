"""The ``fieldproof`` command line interface.

Task P0-T07 ships the entry point and ``--version``, P1-T03 the ``bridge`` commands, P1-T04 the
``manifest`` and ``vehicle`` commands, P1-T05 ``validate``, ``run`` and ``schema`` (scenarios),
P1-T06 ``report``; the remaining commands of the architecture (``replay``, ``matrix``,
``cockpit``) arrive with the Phase 2 and 3 tasks.
"""

from typing import Annotated

import typer

from fieldproof import __version__
from fieldproof.bridge import app as bridge_app
from fieldproof.scenario_cli import report, run, schema, validate
from fieldproof.vehicle_cli import manifest_app, vehicle_app

APP_NAME = "fieldproof"

app = typer.Typer(
    name=APP_NAME,
    help="Fieldproof: run real robot stacks in simulation and prove them before the field.",
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
)

app.add_typer(bridge_app, name="bridge")
app.add_typer(manifest_app, name="manifest")
app.add_typer(vehicle_app, name="vehicle")
app.command("validate")(validate)
app.command("run")(run)
app.command("schema")(schema)
app.command("report")(report)


def _print_version(value: bool) -> None:
    """Print ``<name> <version>`` as plain text (no styling, scripts parse it) and exit."""
    if value:
        typer.echo(f"{APP_NAME} {__version__}")
        raise typer.Exit


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: Annotated[  # noqa: ARG001  (consumed by the eager callback)
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Fieldproof: run real robot stacks in simulation and prove them before the field."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


def main() -> None:
    """Console script entry point."""
    app()
