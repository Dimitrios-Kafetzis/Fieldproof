"""``fieldproof bridge``: the bridge layer from the command line (task P1-T03).

``fieldproof bridge gen robot.yaml`` writes the ros_gz bridge bundle of a vehicle: the
``parameter_bridge`` configurations, the adapter parameters, the ROS interface contract and a
launch file. It is the generator of the ``fieldproof_bridge`` ROS package
(:mod:`fieldproof_bridge.generator`), which the engine installs from ``bridge/fieldproof_bridge``,
so the host and the bridge container produce the same files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from fieldproof.config import ConfigError, load_config
from fieldproof_bridge.generator import GeneratorError, default_out_dir, generate
from fieldproof_bridge.manifest import ManifestError

app = typer.Typer(
    name="bridge",
    help="Generate and check the ros_gz bridge of a vehicle from its manifest.",
    no_args_is_help=True,
)
console = Console()
errors = Console(stderr=True)


@app.command("gen")
def gen(  # noqa: PLR0917  (Typer reads the options from the signature)
    manifest: Annotated[
        Path, typer.Argument(help="The vehicle manifest (robot.yaml).", show_default=False)
    ],
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Output directory (default: ./<robot>_bridge)."),
    ] = None,
    namespace: Annotated[
        str, typer.Option("--namespace", "-n", help="ROS namespace of the vehicle.")
    ] = "",
    frame_prefix: Annotated[
        str, typer.Option("--frame-prefix", help="Prefix of every frame (frame_prefix).")
    ] = "",
    clock: Annotated[bool, typer.Option(help="Bridge /clock from the simulator.")] = True,
    robot_state_publisher: Annotated[
        bool, typer.Option(help="Run robot_state_publisher from the manifest's description.")
    ] = True,
    without: Annotated[
        list[str] | None,
        typer.Option(
            "--without", help="Leave an optional sensor out (repeatable).", show_default=False
        ),
    ] = None,
) -> None:
    """Generate the bridge bundle of MANIFEST into a directory.

    An `extends: fieldproof:<model>` of the manifest resolves against the reference models of
    the workspace's stack (the nearest fieldproof.toml above the manifest).
    """
    try:
        robots = load_config(start=manifest.parent).stack_path("robots")
        out_dir = out if out is not None else default_out_dir(manifest, robots)
        written = generate(
            manifest,
            out_dir,
            namespace=namespace,
            frame_prefix=frame_prefix,
            clock=clock,
            robot_state_publisher=robot_state_publisher,
            without=tuple(without or ()),
            robots=robots,
        )
    except (ManifestError, GeneratorError, ConfigError) as error:
        errors.print(f"[red]error:[/red] {error}", highlight=False)
        raise typer.Exit(code=2) from error
    for path in written:
        console.print(str(path), highlight=False, soft_wrap=True)
