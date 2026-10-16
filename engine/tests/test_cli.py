"""Unit tests for the ``fieldproof`` CLI skeleton."""

import re
import subprocess
import sys

from typer.testing import CliRunner

from fieldproof import __version__
from fieldproof.cli import app

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """Strip terminal styling: with FORCE_COLOR set (CI) Rich splits words across escape codes."""
    return ANSI.sub("", text)


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+([-+].*)?", __version__), __version__


def test_version_flag_prints_name_and_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"fieldproof {__version__}"
    assert "\x1b[" not in result.output, "the version line must stay plain text"


def test_short_version_flag() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "Usage" in plain(result.output)
    assert "--version" in plain(result.output)


def test_module_entry_point_matches_console_script() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "fieldproof", "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert completed.stdout.strip() == f"fieldproof {__version__}"
