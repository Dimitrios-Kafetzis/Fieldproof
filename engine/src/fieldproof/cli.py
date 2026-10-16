"""The ``fieldproof`` command line interface.

Task P0-T07 ships the entry point and ``--version``; the commands of the architecture
(``validate``, ``run``, ``replay``, ``report``, ``matrix``, ``cockpit``) arrive with the Phase 1
and Phase 2 tasks.
"""

from typing import Annotated

import typer
from rich.console import Console

from fieldproof import __version__

APP_NAME = "fieldproof"

app = typer.Typer(
    name=APP_NAME,
    help="Fieldproof: run real robot stacks in simulation and prove them before the field.",
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
)
console = Console()


def _print_version(value: bool) -> None:
    """Print the package version and exit when ``--version`` is given."""
    if value:
        console.print(f"{APP_NAME} {__version__}", highlight=False)
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
        console.print(ctx.get_help())


def main() -> None:
    """Console script entry point."""
    app()
