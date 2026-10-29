"""``fieldproof manifest`` and ``fieldproof vehicle``: manifests and vehicle images (task P1-T04).

``fieldproof manifest validate robot.yaml...`` validates manifests (``extends`` resolved) and
``fieldproof manifest schema`` prints the JSON Schema. ``fieldproof vehicle init`` scaffolds a
vehicle repository (task P2-T08, :mod:`fieldproof.onboarding`), ``fieldproof vehicle dockerfile``
writes the build context of a vehicle, ``fieldproof vehicle build`` writes it and runs ``docker
buildx build``, ``fieldproof vehicle run`` starts the image with the manifest's budget and
environment, ``fieldproof vehicle wait`` runs the readiness probe inside the running container
until the manifest's ``runtime.ready`` conditions hold.

Every command reads the workspace configuration (the nearest ``fieldproof.toml`` above the
manifest, or ``--config``) for two things: the reference models an ``extends:
fieldproof:<model>`` resolves against, and the base image a build starts from when the manifest
names none (the workspace's ``base-ros2`` image at its prefix and tag).
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

from fieldproof.config import Config, ConfigError, load_config
from fieldproof.manifest import BASE_IMAGE_NAME, RobotManifest, json_schema, load_manifest
from fieldproof.onboarding import DEFAULT_LAUNCH, DEFAULT_MODEL, ScaffoldError, scaffold
from fieldproof.vehicle import (
    VehicleError,
    build_command,
    default_context_dir,
    load_vehicle,
    run_command,
    runtime_of,
    wait_command,
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
ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Workspace configuration (default: the nearest fieldproof.toml above the manifest).",
        show_default=False,
    ),
]


def fail(error: Exception) -> typer.Exit:
    """Print the reason and exit with the usage code."""
    errors.print(f"[red]error:[/red] {error}", highlight=False, soft_wrap=True)
    return typer.Exit(code=2)


def workspace_of(path: Path, config_path: Path | None) -> Config:
    """The workspace configuration of a manifest or directory (`ConfigError` raised as is)."""
    start = path if path.is_dir() else path.parent
    return load_config(config_path, start=start)


def load(manifest: Path, config_path: Path | None) -> tuple[RobotManifest, Config]:
    """A manifest with a runtime section and its workspace."""
    config = workspace_of(manifest, config_path)
    return load_vehicle(manifest, robots=config.stack_path("robots")), config


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
    config_path: ConfigOption = None,
) -> None:
    """Validate MANIFESTS against schema v0, with their `extends` chain resolved."""
    failed = False
    for path in manifests:
        try:
            robots = workspace_of(path, config_path).stack_path("robots")
            manifest = load_manifest(path, complete=not partial, robots=robots)
        except (ManifestError, ConfigError) as error:
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


@vehicle_app.command("init")
def init(  # noqa: PLR0917  (Typer reads the options from the signature)
    directory: Annotated[
        Path,
        typer.Argument(
            help="The vehicle repository to scaffold (created if missing).", show_default=False
        ),
    ] = Path(),
    name: Annotated[
        str | None,
        typer.Option(
            "--name", help="Vehicle name (default: the directory's name).", show_default=False
        ),
    ] = None,
    model: Annotated[
        str, typer.Option("--model", help="Reference model the manifest extends.")
    ] = DEFAULT_MODEL,
    package: Annotated[
        str | None,
        typer.Option(
            "--package", help="Bringup package (default: <name>_bringup).", show_default=False
        ),
    ] = None,
    launch: Annotated[
        str, typer.Option("--launch", help="The field bringup launch file of the package.")
    ] = DEFAULT_LAUNCH,
    with_package: Annotated[
        bool,
        typer.Option(
            "--package-files/--no-package",
            help="Write the bringup package under src/ (a repository with its own keeps it).",
        ),
    ] = True,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite files that exist already.")
    ] = False,
    config_path: ConfigOption = None,
) -> None:
    """Scaffold a vehicle repository (docs/reference/onboarding.md).

    Writes robot.yaml (extending the reference model), fieldproof.toml, a smoke scenario, the
    bringup package, the generated build context, a Makefile and a README into DIRECTORY.
    """
    try:
        config = workspace_of(directory if directory.is_dir() else directory.parent, config_path)
        result = scaffold(
            directory,
            name=name,
            model=model,
            package=package,
            launch=launch,
            robots=config.stack_path("robots"),
            with_package=with_package,
            force=force,
        )
    except (ScaffoldError, ConfigError) as error:
        raise fail(error) from error
    for path in result.files:
        console.print(str(path), highlight=False, soft_wrap=True)
    console.print(
        f"[green]scaffolded[/green] {result.name} in {result.directory} (package "
        f"{result.package}, model {model}); next: see {result.directory / 'README.md'}",
        highlight=False,
        soft_wrap=True,
    )


@vehicle_app.command("dockerfile")
def dockerfile(
    manifest: ManifestArg,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Context directory (default: ./<name>_image)."),
    ] = None,
    config_path: ConfigOption = None,
) -> None:
    """Write the build context of MANIFEST: Dockerfile, entry point, resolved manifest, repos."""
    try:
        vehicle, config = load(manifest, config_path)
        context = write_context(
            vehicle,
            out if out is not None else default_context_dir(vehicle),
            base_image=config.image(BASE_IMAGE_NAME),
        )
    except (ManifestError, VehicleError, ConfigError) as error:
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
    builder: Annotated[
        str,
        typer.Option(
            "--builder",
            help="buildx builder; the daemon's own one sees local images (empty: the current one).",
        ),
    ] = "default",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Write the context and print the command only.")
    ] = False,
    docker_args: Annotated[
        list[str] | None,
        typer.Option("--docker-arg", help="Extra `docker buildx build` argument (repeatable)."),
    ] = None,
    config_path: ConfigOption = None,
) -> None:
    """Build the image of MANIFEST with docker buildx (two stages on the Fieldproof base image).

    The base image is the manifest's `runtime.build.base`, else the workspace's `base-ros2`
    image at its prefix and tag (fieldproof.toml, `FIELDPROOF_IMAGE_TAG`); `--base-image`
    overrides either.
    """
    try:
        vehicle, config = load(manifest, config_path)
        directory = (
            context_dir
            if context_dir is not None
            else Path(tempfile.mkdtemp(prefix=f"fieldproof_{vehicle.name}_image_"))
        )
        context = write_context(vehicle, directory, base_image=config.image(BASE_IMAGE_NAME))
        command = build_command(
            vehicle,
            context,
            tag=tag,
            base_image=base_image,
            no_cache=no_cache,
            progress=progress,
            builder=builder or None,
            extra_args=tuple(docker_args or ()),
        )
    except (ManifestError, VehicleError, ConfigError) as error:
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
    namespace: Annotated[
        str,
        typer.Option(
            "--namespace",
            help="Vehicle namespace (FIELDPROOF_NAMESPACE and the launch's namespace_arg).",
        ),
    ] = "",
    frame_prefix: Annotated[
        str,
        typer.Option(
            "--frame-prefix",
            help="Frame prefix (FIELDPROOF_FRAME_PREFIX and the launch's frame_prefix_arg).",
        ),
    ] = "",
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
    config_path: ConfigOption = None,
) -> None:
    """Run the image of MANIFEST with its budget; extra arguments replace the entrypoint."""
    try:
        vehicle, _ = load(manifest, config_path)
        extra_env = dict(item.split("=", 1) for item in env or () if "=" in item)
        command = run_command(
            vehicle,
            name=name,
            network=network or None,
            domain_id=domain_id,
            namespace=namespace,
            frame_prefix=frame_prefix,
            detach=detach,
            remove=not keep,
            image=image,
            env=extra_env,
            extra_args=tuple(docker_args or ()),
            command=tuple(ctx.args),
        )
    except (ManifestError, VehicleError, ConfigError) as error:
        raise fail(error) from error
    console.print(shlex.join(command), highlight=False, soft_wrap=True)
    if dry_run:
        return
    completed = subprocess.run(command, check=False)  # noqa: S603  (the command was just printed)
    if completed.returncode != 0:
        raise typer.Exit(code=completed.returncode)


@vehicle_app.command("wait")
def wait(  # noqa: PLR0917  (Typer reads the options from the signature)
    manifest: ManifestArg,
    name: Annotated[
        str | None, typer.Option("--name", help="Container name (default: the vehicle name).")
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout", help="Seconds to wait (default: the manifest's ready.timeout_s)."
        ),
    ] = None,
    interval: Annotated[
        float | None, typer.Option("--interval", help="Seconds between evaluations (default 1).")
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", help="No progress lines.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the command only.")] = False,
    config_path: ConfigOption = None,
) -> None:
    """Wait until the running container of MANIFEST satisfies its runtime.ready conditions.

    Runs the image's readiness probe (fieldproof-vehicle-ready) through `docker exec`; exit code
    0 once every condition holds, 1 when the timeout passes first (the pending conditions are
    listed), the probe's or docker's code otherwise.
    """
    try:
        vehicle, _ = load(manifest, config_path)
        command = wait_command(
            vehicle, name=name, timeout_s=timeout, interval_s=interval, quiet=quiet
        )
    except (ManifestError, VehicleError, ConfigError) as error:
        raise fail(error) from error
    console.print(shlex.join(command), highlight=False, soft_wrap=True)
    if dry_run:
        return
    completed = subprocess.run(command, check=False)  # noqa: S603  (the command was just printed)
    if completed.returncode != 0:
        raise typer.Exit(code=completed.returncode)
