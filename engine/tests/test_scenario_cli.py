"""Tests of `fieldproof validate`, `fieldproof run` and `fieldproof schema` (task P1-T05)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeBackend
from typer.testing import CliRunner

from fieldproof import scenario_cli
from fieldproof.backend import BackendError, ContainerSpec
from fieldproof.cli import app
from fieldproof.scenario import json_schema

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/scenarios/ugv_nav_to_goal.yaml"
runner = CliRunner()
MANIFEST = REPO / "examples/vehicles/ugv_nav2/robot.yaml"


def example_copy(path: Path, **replacements: str) -> Path:
    """The example written elsewhere, its manifest path made absolute, with replacements."""
    text = EXAMPLE.read_text().replace("../vehicles/ugv_nav2/robot.yaml", str(MANIFEST))
    for old, new in replacements.items():
        text = text.replace(old, new)
    path.write_text(text)
    return path


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return " ".join(ANSI.sub("", text).split())


class ReadyFake(FakeBackend):
    """A fake that has every image (the CLI builds the backend itself)."""

    def image_exists(self, image: str) -> bool:
        return True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> ReadyFake:
    backend = ReadyFake()
    monkeypatch.setattr("fieldproof.docker_backend.DockerBackend", lambda: backend)
    monkeypatch.setattr("fieldproof.runner.POLL_S", 0.0)
    monkeypatch.setattr("fieldproof.runner.Run.check_assets", lambda self: None)
    return backend


def test_validate_example() -> None:
    result = runner.invoke(app, ["validate", str(EXAMPLE)])
    assert result.exit_code == 0, result.output
    out = plain(result.output)
    assert out.startswith(f"ok {EXAMPLE}: ugv_nav_to_goal (world depot, 1 vehicle(s): ugv1")
    assert "1 timeline entry, 1 assertion(s), time sim, budget 600 s" in out


def test_validate_reports_errors(tmp_path: Path) -> None:
    bad = example_copy(tmp_path / "bad.yaml", spawn_dock="spawn_moon")
    result = runner.invoke(app, ["validate", str(EXAMPLE), str(bad), str(tmp_path / "none.yaml")])
    assert result.exit_code == 2
    out = plain(result.output)
    assert "ok " in out
    assert f"{bad}: vehicles[ugv1].spawn: world depot has no frame 'spawn_moon'" in out
    assert "none.yaml: No such file" in out, "the file is named once"


def test_validate_notes_realtime(tmp_path: Path) -> None:
    file = example_copy(tmp_path / "rt.yaml", **{"mode: sim": "mode: realtime"})
    result = runner.invoke(app, ["validate", str(file)])
    assert result.exit_code == 0, result.output
    assert "P2-T04" in plain(result.output)


def test_validate_with_config(tmp_path: Path) -> None:
    config = tmp_path / "fieldproof.toml"
    config.write_text('[stack]\nworlds = "nowhere"\n')
    result = runner.invoke(app, ["validate", str(EXAMPLE), "--config", str(config)])
    assert result.exit_code == 2
    assert "world.file: 'depot.sdf' is not a file in" in plain(result.output)
    config.write_text("[docker\n")
    result = runner.invoke(app, ["validate", str(EXAMPLE), "-c", str(config)])
    assert result.exit_code == 2
    assert "not valid TOML" in plain(result.output)


def test_schema() -> None:
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == json_schema()


def test_run_dry_run(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            str(EXAMPLE),
            "--dry-run",
            "--render",
            "software",
            "--runs-dir",
            str(tmp_path),
            "--run-id",
            "dry-1",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["run_id"] == "dry-1"
    assert data["render"] == "software"
    assert data["run_dir"] == str(tmp_path / "dry-1")
    assert data["containers"][0]["name"] == "fieldproof-1-sim"
    assert not (tmp_path / "dry-1").exists(), "a dry run writes nothing"


def test_run_rejects_bad_arguments(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", str(EXAMPLE), "--render", "metal", "--dry-run"])
    assert result.exit_code == 2
    assert "--render 'metal'" in plain(result.output)
    result = runner.invoke(app, ["run", str(tmp_path / "none.yaml")])
    assert result.exit_code == 2
    assert "No such file" in plain(result.output)


def test_run_passes_with_a_fake_backend(fake: ReadyFake, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            str(EXAMPLE),
            "--render",
            "software",
            "--runs-dir",
            str(tmp_path),
            "--run-id",
            "cli-1",
            "--domain-id",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    out = plain(result.output)
    assert "passed ugv_nav_to_goal (cli-1): the timeline completed; run directory" in out
    assert "drive_ahead: goal SUCCEEDED" in out
    assert "waiting (3/9 ready)" in out, "the probe's lines are shown"
    assert fake.containers == {}
    assert fake.networks == {"fieldproof"}
    assert fake.containers == {}
    assert fake.started[0] == "fieldproof-1-sim"
    assert json.loads((tmp_path / "cli-1" / "run.json").read_text())["stack"]["ros_domain_id"] == 3
    assert (tmp_path / "cli-1" / "engine.log").is_file()


def test_run_quiet_hides_command_output(fake: ReadyFake, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["run", str(EXAMPLE), "--render", "software", "--runs-dir", str(tmp_path), "-q"]
    )
    assert result.exit_code == 0, result.output
    assert "waiting (3/9 ready)" not in plain(result.output)
    assert "ugv1: ready: 9 conditions hold" in plain(result.output), "the info line stays"


def test_run_failure_exit_code(fake: ReadyFake, tmp_path: Path) -> None:
    from fieldproof.backend import ExecResult  # noqa: PLC0415

    fake.goal_result = ExecResult(0, "Goal finished with status: CANCELED\n")
    result = runner.invoke(
        app, ["run", str(EXAMPLE), "--render", "software", "--runs-dir", str(tmp_path)]
    )
    assert result.exit_code == 1, result.output
    assert "failed ugv_nav_to_goal" in plain(result.output)
    assert "goal CANCELED" in plain(result.output)
    assert fake.containers == {}


def test_run_interrupted_exit_code(fake: ReadyFake, tmp_path: Path) -> None:
    def interrupt(spec: ContainerSpec) -> None:
        if spec.role == "vehicle":
            raise KeyboardInterrupt
        FakeBackend.start(fake, spec)

    fake.start = interrupt  # type: ignore[method-assign]
    result = runner.invoke(
        app, ["run", str(EXAMPLE), "--render", "software", "--runs-dir", str(tmp_path)]
    )
    assert result.exit_code == 130, result.output
    assert "interrupted ugv_nav_to_goal" in plain(result.output)
    assert fake.containers == {}


def test_run_keep_and_leftovers(fake: ReadyFake, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["run", str(EXAMPLE), "--render", "software", "--runs-dir", str(tmp_path), "--keep"]
    )
    assert result.exit_code == 0, result.output
    assert len(fake.containers) == 3
    assert "left behind" not in plain(result.output)


def test_run_warns_about_leftovers(
    fake: ReadyFake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def remove(name: str) -> None:
        if name.endswith("-sim"):
            return
        FakeBackend.remove(fake, name)

    fake.remove = remove  # type: ignore[method-assign]
    result = runner.invoke(
        app,
        [
            "run",
            str(EXAMPLE),
            "--render",
            "software",
            "--runs-dir",
            str(tmp_path),
            "--run-id",
            "x-1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "containers left behind: fieldproof-1-sim" in plain(result.output)


def test_run_without_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def no_daemon() -> Any:
        raise BackendError("cannot reach the Docker daemon: refused")

    monkeypatch.setattr("fieldproof.docker_backend.DockerBackend", no_daemon)
    result = runner.invoke(
        app, ["run", str(EXAMPLE), "--render", "software", "--runs-dir", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "cannot reach the Docker daemon" in plain(result.output)


def test_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["--help"])
    out = plain(result.output)
    assert "validate" in out
    assert "run" in out
    assert "schema" in out
    assert scenario_cli.run.__doc__ is not None
