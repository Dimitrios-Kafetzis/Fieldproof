#!/usr/bin/env python3
"""Render a xacro vehicle description to URDF and SDF (task P1-T01).

Runs at build time from the CMake function ``fieldproof_add_robot_model``: xacro expands the
description with the default arguments (or the ``key:=value`` mappings given on the command line)
into the URDF, and ``gz sdf -p`` converts that URDF into the SDF model Gazebo spawns, folding the
``<gazebo>`` blocks (sensors, friction, system plugins) into it. The same conversion happens
inside Gazebo when the launch file spawns the URDF directly, so the two paths agree.

Usage: generate_model.py <description.urdf.xacro> --urdf <out.urdf> --sdf <out.sdf> [key:=value ...]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import xacro


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the command line (``key:=value`` pairs become xacro mappings)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("xacro_file", type=Path, help="the xacro description to render")
    parser.add_argument("--urdf", type=Path, required=True, help="where to write the URDF")
    parser.add_argument("--sdf", type=Path, required=True, help="where to write the SDF")
    parser.add_argument(
        "mappings", nargs="*", metavar="key:=value", help="xacro arguments (default: the file's)"
    )
    args = parser.parse_intermixed_args(argv)  # mappings may follow the options
    for mapping in args.mappings:
        if ":=" not in mapping:
            parser.error(f"xacro arguments are written key:=value, got '{mapping}'")
    return args


def render_urdf(xacro_file: Path, mappings: dict[str, str]) -> str:
    """Expand the xacro description into a URDF document."""
    document = xacro.process_file(str(xacro_file), mappings=mappings)
    return str(document.toprettyxml(indent="  "))


def convert_to_sdf(urdf_file: Path) -> str:
    """Convert a URDF file into SDF with ``gz sdf -p`` (the same parser Gazebo uses to spawn)."""
    gz = shutil.which("gz")
    if gz is None:
        msg = "gz is not on PATH; source the ROS 2 environment (run inside the dev container)"
        raise FileNotFoundError(msg)
    result = subprocess.run(  # noqa: S603  (arguments are paths from the build system)
        [gz, "sdf", "-p", str(urdf_file)], check=True, capture_output=True, text=True
    )
    if result.stderr.strip():
        sys.stderr.write(result.stderr)
    if "<model" not in result.stdout:
        msg = f"gz sdf -p produced no model from {urdf_file}"
        raise RuntimeError(msg)
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    """Render the description; returns the process exit code."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    mappings = dict(mapping.split(":=", maxsplit=1) for mapping in args.mappings)
    args.urdf.parent.mkdir(parents=True, exist_ok=True)
    args.sdf.parent.mkdir(parents=True, exist_ok=True)
    args.urdf.write_text(render_urdf(args.xacro_file, mappings))
    args.sdf.write_text(convert_to_sdf(args.urdf))
    return 0


if __name__ == "__main__":
    sys.exit(main())
