"""Tests of `fieldproof validate`, `run`, `schema` (task P1-T05) and `report` (tasks P1-T06 and
P1-T07, the JUnit form)."""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
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


GOOD_SAMPLES = [
    {"kind": "pose", "sim": 9.0, "model": "ugv1", "x": 10.0, "y": -0.8, "z": 0.1, "yaw": -1.57},
    {"kind": "rate", "sim": 6.0, "topic": "/scan", "hz": 10.0, "window_s": 5.0, "total": 60},
    {"kind": "value", "sim": 6.0, "topic": "/cmd_vel", "field": "linear.x", "value": 0.4},
]


class ReadyFake(FakeBackend):
    """A fake that has every image (the CLI builds the backend itself) and a happy vehicle."""

    def __init__(self) -> None:
        super().__init__()
        self.samples_on_goal = list(GOOD_SAMPLES)
        # The runner reads the real clock here and the fake goal returns at once: stamp the
        # samples a little ahead, so they land after the timeline's start and its grace periods.
        self.wall = lambda: time.time() + 10.0

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
    assert "1 timeline entry, 5 assertion(s), time sim, budget 600 s" in out


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
    assert (
        "PASS ugv_nav_to_goal (cli-1): the timeline completed and every assertion passed; "
        "run directory"
    ) in out
    assert "drive_ahead: goal SUCCEEDED" in out
    assert "waiting (3/9 ready)" in out, "the probe's lines are shown"
    assert "PASS reached_goal (reach_pose): 0.20 m from (10, -1)" in out
    assert "PASS no_collision (no_collision): no contact of ugv1" in out
    assert f"report: {tmp_path / 'cli-1' / 'report.md'}" in out
    assert fake.containers == {}
    assert fake.networks == {"fieldproof"}
    assert fake.started[0] == "fieldproof-1-sim"
    assert json.loads((tmp_path / "cli-1" / "run.json").read_text())["stack"]["ros_domain_id"] == 3
    assert (tmp_path / "cli-1" / "engine.log").is_file()
    assert (tmp_path / "cli-1" / "report.json").is_file()


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
    assert "FAIL ugv_nav_to_goal" in plain(result.output)
    assert "goal CANCELED" in plain(result.output)
    assert "SKIPPED lidar_streams (topic_rate): the run ended failed" in plain(result.output)
    assert fake.containers == {}


def test_run_failed_assertion_exit_code(fake: ReadyFake, tmp_path: Path) -> None:
    fake.samples_on_goal = [
        {"kind": "pose", "sim": 9.0, "model": "ugv1", "x": 10.0, "y": 2.0, "z": 0.1, "yaw": -1.57},
        *GOOD_SAMPLES[1:],
    ]
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
            "f1",
        ],
    )
    assert result.exit_code == 1, result.output
    out = plain(result.output)
    assert "FAIL ugv_nav_to_goal (f1): assertion reached_goal failed: not reached by the end" in out
    assert "FAIL reached_goal (reach_pose): not reached by the end of the timeline" in out
    assert "PASS arrived_in_time (within_time)" in out
    report = json.loads((tmp_path / "f1" / "report.json").read_text())
    assert report["verdict"] == "FAIL"
    assert report["exit_code"] == 1


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
    assert "INTERRUPTED ugv_nav_to_goal" in plain(result.output)
    assert fake.containers == {}


def test_run_keep_and_leftovers(fake: ReadyFake, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["run", str(EXAMPLE), "--render", "software", "--runs-dir", str(tmp_path), "--keep"]
    )
    assert result.exit_code == 0, result.output
    assert len(fake.containers) == 4
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


def test_report_command(fake: ReadyFake, tmp_path: Path) -> None:
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
            "r1",
        ],
    )
    assert result.exit_code == 0, result.output
    run_dir = tmp_path / "r1"
    # The run's report was written before its last log lines, so the artefact sizes differ
    # between the two renderings; everything above them is the same.
    markdown = (run_dir / "report.md").read_text().split("## Artefacts")[0]
    result = runner.invoke(app, ["report", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert result.output.split("## Artefacts")[0] == markdown
    assert "## Artefacts" in result.output
    result = runner.invoke(app, ["report", str(run_dir / "run.json"), "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["verdict"] == "PASS"
    result = runner.invoke(app, ["report", str(run_dir), "--format", "junit"])
    assert result.exit_code == 0, result.output
    suite = ET.fromstring(result.output).find("testsuite")  # noqa: S314 (our own document)
    assert suite is not None
    assert suite.get("name") == "ugv_nav_to_goal"
    assert suite.get("failures") == "0"
    assert suite.get("errors") == "0"
    assert (run_dir / "junit.xml").read_text() == result.output
    (run_dir / "report.md").unlink()
    (run_dir / "junit.xml").unlink()
    result = runner.invoke(app, ["report", str(run_dir), "--write"])
    assert result.exit_code == 0, result.output
    assert (run_dir / "report.md").read_text().startswith(markdown)
    assert (run_dir / "junit.xml").is_file()
    assert "written:" in plain(result.output)
    # A run id is looked up under the workspace's runs directory.
    (tmp_path / "fieldproof.toml").write_text('[runs]\ndirectory = "."\n')
    result = runner.invoke(app, ["report", "r1", "--config", str(tmp_path / "fieldproof.toml")])
    assert result.exit_code == 0, result.output
    assert result.output.startswith(markdown)
    result = runner.invoke(app, ["report", "r9", "--config", str(tmp_path / "fieldproof.toml")])
    assert result.exit_code == 2
    assert "no such run directory or run id" in plain(result.output)
    result = runner.invoke(app, ["report", str(run_dir), "--format", "html"])
    assert result.exit_code == 2
    assert "expected md, json or junit" in plain(result.output)
    (run_dir / "run.json").write_text("{bad")
    result = runner.invoke(app, ["report", str(run_dir)])
    assert result.exit_code == 2
    assert "not valid JSON" in plain(result.output)


def test_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["--help"])
    out = plain(result.output)
    assert "validate" in out
    assert "run" in out
    assert "schema" in out
    assert "report" in out
    assert scenario_cli.run.__doc__ is not None
    assert scenario_cli.report.__doc__ is not None
