"""``fieldproof log info|synth`` (task P2-T07): field logs for the replay source.

``info`` prints what a log holds (its topics with their types and message counts, its span and
storage) from its summary, without decoding a message: what a scenario's ``source.vehicles``
mapping is written from. ``synth`` writes the synthetic field log of the reference UGV
(:mod:`fieldproof.synthlog`), the stand-in for a real one; ``make replay-log`` writes the one
``examples/scenarios/ugv_replay.yaml`` plays.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.markup import escape
from rich.table import Table

from fieldproof.dsl import ScenarioError
from fieldproof.replay import inspect_log
from fieldproof.scenario_cli import console, fail
from fieldproof.synthlog import DEFAULT_SECONDS, DEFAULT_SEED, write_synthetic_log

log_app = typer.Typer(
    name="log",
    help="Field logs for the replay source: what a log holds, the synthetic UGV log.",
    no_args_is_help=True,
)
JsonOption = Annotated[bool, typer.Option("--json", help="Print as JSON.")]


@log_app.command("info")
def info(
    log: Annotated[
        Path, typer.Argument(help="An MCAP file or a rosbag2 directory.", show_default=False)
    ],
    as_json: JsonOption = False,
) -> None:
    """What LOG holds: its topics, types, message counts, span and storage.

    Read from the MCAP summary or the bag's metadata.yaml; no message is decoded. Exit code 2
    when the log cannot be read.
    """
    try:
        found = inspect_log(log)
    except ScenarioError as error:
        raise fail(error) from error
    if as_json:
        typer.echo(json.dumps(found.to_dict(), indent=2))
        return
    console.print(
        f"{escape(str(found.path))}: {found.storage}"
        + (" bag directory" if found.directory else " file")
        + f", {found.duration_s:.1f} s, {found.messages} message(s) on "
        f"{len(found.topics)} topic(s), from {found.start_ns / 1e9:.3f} s",
        highlight=False,
        soft_wrap=True,
    )
    table = Table(box=box.SIMPLE_HEAD)
    table.add_column("Topic", style="bold", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Messages", justify="right")
    for name, kind in sorted(found.topics.items()):
        table.add_row(name, kind or "-", str(found.counts.get(name, "-")))
    console.print(table)


@log_app.command("synth")
def synth(  # noqa: PLR0917
    out: Annotated[
        Path, typer.Argument(help="The MCAP file to write (`.mcap`).", show_default=False)
    ],
    seconds: Annotated[
        float, typer.Option("--seconds", help="The log's length (rest, a loop, rest).")
    ] = DEFAULT_SECONDS,
    seed: Annotated[int, typer.Option("--seed", help="The noise's seed.")] = DEFAULT_SEED,
    start_x: Annotated[
        float, typer.Option("--start-x", help="Where the hall's origin is in the world (x).")
    ] = 0.0,
    start_y: Annotated[
        float, typer.Option("--start-y", help="Where the hall's origin is in the world (y).")
    ] = 0.0,
    start_yaw: Annotated[
        float, typer.Option("--start-yaw", help="The hall's heading in the world (radians).")
    ] = 0.0,
    force: Annotated[bool, typer.Option("--force", help="Overwrite OUT if it exists.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Write the synthetic field log of the reference UGV to OUT.

    A 20 m by 12 m hall with crates, the vehicle at rest, a driven rectangle, at rest again:
    `/scan` (720 beams, 10 Hz), `/odom` and `/tf` (20 Hz, the wheel odometry drifting), `/tf_static`
    and `/ground_truth/pose` (10 Hz, the true pose in the world). Deterministic for a seed.
    """
    if out.suffix != ".mcap":
        raise fail(ValueError(f"{out}: the synthetic log is an MCAP file (.mcap)"))
    if out.exists() and not force:
        raise fail(ValueError(f"{out}: exists (--force overwrites it)"))
    if seconds <= 0:
        raise fail(ValueError("--seconds must be positive"))
    try:
        summary = write_synthetic_log(
            out, seconds=seconds, seed=seed, start=(start_x, start_y, start_yaw)
        )
    except OSError as error:
        raise fail(error) from error
    if as_json:
        typer.echo(json.dumps(summary.to_dict(), indent=2))
        return
    counts = ", ".join(f"{t} {n}" for t, n in summary.messages.items())
    console.print(
        f"wrote {escape(str(summary.path))}: {summary.seconds:g} s, seed {summary.seed}, "
        f"{summary.bytes} bytes; {counts}",
        highlight=False,
        soft_wrap=True,
    )
