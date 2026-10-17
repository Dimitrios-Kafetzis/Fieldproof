"""``fieldproof manifest`` and ``fieldproof vehicle``: manifests and vehicle images (task P1-T04).

``fieldproof manifest validate robot.yaml...`` validates manifests (``extends`` resolved) and
``fieldproof manifest schema`` prints the JSON Schema. ``fieldproof vehicle dockerfile`` writes the
build context of a vehicle, ``fieldproof vehicle build`` writes it and runs ``docker buildx build``,
``fieldproof vehicle run`` starts the image with the manifest's budget and environment.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from fieldproof.manifest import json_schema, load_manifest
from fieldproof.vehicle import (
    VehicleError,
    build_command,
    default_context_dir,
    load_vehicle,
    run_command,
    runtime_of,
    write_context,
)
from fieldproof_bridge.manifest import ManifestError

manifest_app = typer.Typer(
    name="manifest",
    help="Validate vehicle manifests (robot.yaml) and export their schema.",
    no_args_is_help=True,
)
vehicle_app = typer.Typer(
    name="vehicle",
    help="Build and run the container image of a vehicle from its manifest.",
    no_args_is_help=True,
)
console = Console()
errors = Console(stderr=True)

ManifestArg = Annotated[
    Path, typer.Argument(help="The vehicle manifest (robot.yaml).", show_default=False)
]


def fail(error: Exception) -> typer.Exit:
    """Print the reason and exit with the usage code."""
    errors.print(f"[red]error:[/red] {error}", highlight=False, soft_wrap=True)
    return typer.Exit(code=2)


@manifest_app.command("validate")
def validate(
    manifests: Annotated[
        list[Path], typer.Argument(help="Manifests to validate.", show_default=False)
    ],
    partial: Annotated[
        bool,
        typer.Option(
            "--partial", help="Accept a manifest without model sections (meant to be extended)."
        ),
    ] = False,
) -> None:
    """Validate MANIFESTS against schema v0, with their `extends` chain resolved."""
    failed = False
    for path in manifests:
        try:
            manifest = load_manifest(path, complete=not partial)
        except ManifestError as error:
            errors.print(f"[red]{path}[/red]: {error}", highlight=False, soft_wrap=True)
            failed = True
            continue
        runtime = manifest.runtime
        summary = f"{len(manifest.sensors)} sensors, {len(manifest.interfaces())} interfaces"
        if runtime is not None:
            summary += f", runtime {runtime.tier} image {runtime.image}"
            unswapped = manifest.unswapped()
            if unswapped:
                summary += f", no driver swap for {', '.join(unswapped)}"
        console.print(
            f"[green]ok[/green] {path}: {manifest.name} ({summary})",
            highlight=False,
            soft_wrap=True,
        )
    if failed:
        raise typer.Exit(code=2)


@manifest_app.command("schema")
def schema() -> None:
    """Print the JSON Schema of a manifest file."""
    typer.echo(json.dumps(json_schema(), indent=2))


@vehicle_app.command("dockerfile")
def dockerfile(
    manifest: ManifestArg,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Context directory (default: ./<name>_image)."),
    ] = None,
) -> None:
    """Write the build context of MANIFEST: Dockerfile, entry point, resolved manifest, repos."""
    try:
        vehicle = load_vehicle(manifest)
        context = write_context(vehicle, out if out is not None else default_context_dir(vehicle))
    except (ManifestError, VehicleError) as error:
        raise fail(error) from error
    for path in context.files:
        console.print(str(path), highlight=False, soft_wrap=True)


@vehicle_app.command("build")
def build(  # noqa: PLR0917  (Typer reads the options from the signature)
    manifest: ManifestArg,
    context_dir: Annotated[
        Path | None,
        typer.Option(
            "--context", help="Where to write the context (default: a temporary directory)."
        ),
    ] = None,
    tag: Annotated[
        str | None, typer.Option("--tag", "-t", help="Image tag (default: runtime.image).")
    ] = None,
    base_image: Annotated[
        str | None, typer.Option("--base-image", help="Override the manifest's base image.")
    ] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Ignore the layer cache.")] = False,
    progress: Annotated[
        str | None, typer.Option("--progress", help="BuildKit progress output (auto, plain, tty).")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Write the context and print the command only.")
    ] = False,
    docker_args: Annotated[
        list[str] | None,
        typer.Option("--docker-arg", help="Extra `docker buildx build` argument (repeatable)."),
    ] = None,
) -> None:
    """Build the image of MANIFEST with docker buildx (two stages on the Fieldproof base image)."""
    try:
        vehicle = load_vehicle(manifest)
        directory = (
            context_dir
            if context_dir is not None
            else Path(tempfile.mkdtemp(prefix=f"fieldproof_{vehicle.name}_image_"))
        )
        context = write_context(vehicle, directory)
        command = build_command(
            vehicle,
            context,
            tag=tag,
            base_image=base_image,
            no_cache=no_cache,
            progress=progress,
            extra_args=tuple(docker_args or ()),
        )
    except (ManifestError, VehicleError) as error:
        raise fail(error) from error
    console.print(shlex.join(command), highlight=False, soft_wrap=True)
    if dry_run:
        return
    completed = subprocess.run(command, check=False)  # noqa: S603  (the command was just printed)
    if completed.returncode != 0:
        raise typer.Exit(code=1)
    console.print(f"[green]built[/green] {tag or runtime_of(vehicle).image}", highlight=False)


@vehicle_app.command(
    "run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run(  # noqa: PLR0917  (Typer reads the options from the signature)
    ctx: typer.Context,
    manifest: ManifestArg,
    name: Annotated[
        str | None, typer.Option("--name", help="Container name (default: the vehicle name).")
    ] = None,
    network: Annotated[
        str, typer.Option("--network", help="Docker network to join (empty for Docker's default).")
    ] = "fieldproof",
    domain_id: Annotated[
        int | None, typer.Option("--domain-id", help="ROS_DOMAIN_ID of the container.")
    ] = None,
    detach: Annotated[bool, typer.Option("--detach", "-d", help="Run in the background.")] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Do not remove the container on exit.")
    ] = False,
    image: Annotated[
        str | None, typer.Option("--image", help="Image to run (default: runtime.image).")
    ] = None,
    env: Annotated[
        list[str] | None, typer.Option("--env", "-e", help="Extra KEY=VALUE (repeatable).")
    ] = None,
    docker_args: Annotated[
        list[str] | None,
        typer.Option("--docker-arg", help="Extra `docker run` argument (repeatable)."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the command only.")] = False,
) -> None:
    """Run the image of MANIFEST with its budget; extra arguments replace the entrypoint."""
    try:
        vehicle = load_vehicle(manifest)
        extra_env = dict(item.split("=", 1) for item in env or () if "=" in item)
        command = run_command(
            vehicle,
            name=name,
            network=network or None,
            domain_id=domain_id,
            detach=detach,
            remove=not keep,
            image=image,
            env=extra_env,
            extra_args=tuple(docker_args or ()),
            command=tuple(ctx.args),
        )
    except (ManifestError, VehicleError) as error:
        raise fail(error) from error
    console.print(shlex.join(command), highlight=False, soft_wrap=True)
    if dry_run:
        return
    completed = subprocess.run(command, check=False)  # noqa: S603  (the command was just printed)
    if completed.returncode != 0:
        raise typer.Exit(code=completed.returncode)
