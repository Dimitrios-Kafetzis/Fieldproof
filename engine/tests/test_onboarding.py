"""Tests of the vehicle onboarding SDK (task P2-T08): `fieldproof vehicle init`, the
`fieldproof:<model>` form of `extends`, and a workspace outside the Fieldproof checkout."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from fieldproof import __version__
from fieldproof.cli import app
from fieldproof.config import ENGINE_CHECKOUT, load_config
from fieldproof.manifest import load_manifest
from fieldproof.onboarding import ScaffoldError, scaffold, vehicle_name
from fieldproof.runner import ROBOTS_ENV, ROBOTS_IN_CONTAINER, WORKSPACE, plan
from fieldproof.scenario import load_scenario
from fieldproof.vehicle import load_vehicle
from fieldproof_bridge.manifest import Manifest as BridgeManifest
from fieldproof_bridge.manifest import ManifestError, read_manifest_data

REPO = Path(__file__).resolve().parents[2]
ROBOTS = REPO / "robots/fieldproof_robots/models"
UGV_MANIFEST = ROBOTS / "ugv_ref/robot.yaml"
NAV2_MANIFEST = REPO / "examples/vehicles/ugv_nav2/robot.yaml"
runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")
NO_ENV: dict[str, str] = {}


def plain(text: str) -> str:
    return ANSI.sub("", text)


# ------------------------------------------------------------------- extends: fieldproof:
def test_extends_a_reference_model_by_name(tmp_path: Path) -> None:
    child = tmp_path / "robot.yaml"
    child.write_text(
        "apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: fieldproof:ugv_ref\n"
        "metadata: {name: bot}\nruntime: {image: bot:local, entrypoint: {command: [sleep]}}\n"
    )
    by_name = load_manifest(child, robots=ROBOTS)
    by_path = load_manifest(NAV2_MANIFEST)
    assert by_name.extends is None
    assert by_name.drive == by_path.drive
    assert by_name.sensors == by_path.sensors
    assert by_name.description is not None
    # The inherited description points at the reference model's files from the child's directory.
    assert (by_name.directory / by_name.description.xacro).resolve() == (
        ROBOTS / "ugv_ref/urdf/ugv_ref.urdf.xacro"
    )
    assert by_name.robots == ROBOTS
    # The bridge's reader, as the bridge container runs it, resolves the same document.
    bridge = BridgeManifest.load(child, robots=ROBOTS)
    assert bridge.xacro_path() == BridgeManifest.load(UGV_MANIFEST).xacro_path()


def test_extends_by_name_reads_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "robot.yaml"
    child.write_text(
        "apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: fieldproof:ugv_ref\n"
        "metadata: {name: bot}\n"
    )
    monkeypatch.delenv("FIELDPROOF_ROBOTS", raising=False)
    with pytest.raises(ManifestError, match="needs the reference models' directory"):
        read_manifest_data(child)
    monkeypatch.setenv("FIELDPROOF_ROBOTS", str(ROBOTS))
    assert read_manifest_data(child)["drive"]["type"] == "differential"
    # An explicit directory wins over the environment.
    with pytest.raises(ManifestError, match="no reference model 'ugv_ref' under"):
        read_manifest_data(child, robots=tmp_path)


def test_extends_by_name_errors(tmp_path: Path) -> None:
    child = tmp_path / "robot.yaml"
    for extends, message in (
        ("fieldproof:no_such_model", "no reference model 'no_such_model'"),
        ("fieldproof:Bad-Name", "is not a reference model name"),
        ("fieldproof:", "is not a reference model name"),
    ):
        child.write_text(
            f'apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: "{extends}"\n'
            "metadata: {name: bot}\n"
        )
        with pytest.raises(ManifestError, match=message):
            read_manifest_data(child, robots=ROBOTS)


def test_manifest_validate_resolves_the_workspace_stack(tmp_path: Path) -> None:
    """The CLI finds the reference models through the workspace, wherever the manifest is."""
    child = tmp_path / "robot.yaml"
    child.write_text(
        "apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: fieldproof:ugv_ref\n"
        "metadata: {name: bot}\n"
    )
    result = runner.invoke(app, ["manifest", "validate", str(child)])
    assert result.exit_code == 0, result.output
    assert "ok" in plain(result.output)
    assert "bot (4 sensors" in plain(result.output)
    result = runner.invoke(
        app, ["manifest", "validate", str(child), "--config", str(REPO / "fieldproof.toml")]
    )
    assert result.exit_code == 0, result.output


# ------------------------------------------------------------------------- the scaffold
@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A vehicle repository scaffolded outside the checkout, as a team would."""
    directory = tmp_path / "acme-rover"
    result = runner.invoke(app, ["vehicle", "init", str(directory)])
    assert result.exit_code == 0, result.output
    return directory


def test_init_writes_a_repository_that_validates(repository: Path) -> None:
    written = {str(p.relative_to(repository)) for p in repository.rglob("*") if p.is_file()}
    assert {
        "robot.yaml",
        "fieldproof.toml",
        "scenarios/acme_rover_smoke.yaml",
        "Makefile",
        "README.md",
        ".gitignore",
        "docker/Dockerfile",
        "docker/entrypoint.sh",
        "docker/robot.yaml",
        "docker/ready.py",
        "src/acme_rover_bringup/package.xml",
        "src/acme_rover_bringup/setup.py",
        "src/acme_rover_bringup/setup.cfg",
        "src/acme_rover_bringup/resource/acme_rover_bringup",
        "src/acme_rover_bringup/acme_rover_bringup/__init__.py",
        "src/acme_rover_bringup/acme_rover_bringup/status_node.py",
        "src/acme_rover_bringup/launch/bringup.launch.py",
        "src/acme_rover_bringup/launch/drivers.launch.py",
        "src/acme_rover_bringup/config/acme_rover_bringup.yaml",
    } <= written
    manifest = load_vehicle(repository / "robot.yaml", robots=ROBOTS)
    assert manifest.name == "acme_rover"
    assert manifest.runtime is not None
    assert manifest.runtime.image == "fieldproof-vehicle-acme_rover:local"
    assert manifest.runtime.entrypoint.argv() == [
        "ros2",
        "launch",
        "acme_rover_bringup",
        "bringup.launch.py",
        "use_sim_time:=true",
        "drivers:=false",
    ]
    assert manifest.runtime.entrypoint.launch is not None
    assert manifest.runtime.entrypoint.launch.namespace_arg == "namespace"
    # The driver-swap map covers the model: the base, the description, one driver per sensor.
    drivers = {d.name: d.provides for d in manifest.runtime.drivers}
    assert drivers["base"] == ["cmd_vel", "odom", "joint_states", "tf"]
    assert drivers["description"] == ["tf_static", "robot_description"]
    assert drivers["lidar"] == ["scan"]
    assert drivers["gps"] == ["gps/fix"]
    assert manifest.unswapped() == [], "every interface is swapped"
    assert manifest.runtime.ready is not None
    assert manifest.runtime.ready.conditions[0].model_dump() == {
        "topic": "status",
        "type": "std_msgs/msg/String",
    }
    assert manifest.runtime.resources is not None
    assert (manifest.runtime.resources.cpus, manifest.runtime.resources.memory) == (1.0, "1024m")
    assert yaml.safe_load((repository / "robot.yaml").read_text())["extends"] == (
        "fieldproof:ugv_ref"
    )
    # The workspace: this repository, on the published images of the engine's release.
    config = load_config(start=repository, env=NO_ENV)
    assert config.root == repository.resolve()
    assert config.docker.image_tag == f"v{__version__}"
    assert config.stack_root == ENGINE_CHECKOUT == REPO
    assert config.external
    assert config.stack_path("worlds") == REPO / "sim/fieldproof_worlds/worlds"
    # The generated context builds on the workspace's base image.
    dockerfile = (repository / "docker/Dockerfile").read_text()
    assert f"ARG BASE_IMAGE=ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:v{__version__}" in (
        dockerfile
    )
    assert "COPY --from=vehicle-source src/ src/src/" in dockerfile
    resolved = yaml.safe_load((repository / "docker/robot.yaml").read_text())
    assert "extends" not in resolved
    assert resolved["drive"]["type"] == "differential"
    # The manifest and the smoke scenario validate from the repository.
    result = runner.invoke(app, ["manifest", "validate", str(repository / "robot.yaml")])
    assert result.exit_code == 0, result.output
    assert "no driver swap" not in plain(result.output)
    scenario_path = repository / "scenarios/acme_rover_smoke.yaml"
    result = runner.invoke(app, ["validate", str(scenario_path)])
    assert result.exit_code == 0, result.output
    scenario = load_scenario(scenario_path)
    assert [e.action_name for e in scenario.timeline] == ["wait"]
    assert {a.type for a in scenario.assertions} == {
        "topic_rate",
        "message_latency",
        "no_collision",
        "resource_usage",
    }
    assert "/status" in scenario.outputs.topics
    # The README and the Makefile point at the checkout and the release.
    readme = (repository / "README.md").read_text()
    assert f"checkout v{__version__}" in readme
    makefile = (repository / "Makefile").read_text()
    assert "FIELDPROOF_ROOT ?= ../Fieldproof" in makefile
    assert "$(FIELDPROOF) run $(SCENARIO) $(RUN_FLAGS)" in makefile


def test_init_plans_against_the_checkout_from_outside(repository: Path) -> None:
    """A run of the repository's scenario mounts the checkout's stack and the workspace."""
    config = load_config(start=repository, env=NO_ENV)
    scenario = load_scenario(repository / "scenarios/acme_rover_smoke.yaml")
    resolved = plan(scenario, config, runs_dir=repository / "runs", render="software")
    assert resolved.sim is not None
    assert resolved.sim.mounts[0].source == REPO / "sim/fieldproof_worlds/worlds"
    assert resolved.run_dir.parent == (repository / "runs").resolve()
    vehicle = resolved.vehicles[0]
    for spec in (vehicle.spawn_job, vehicle.bridge):
        mounts = {m.target: m.source for m in spec.mounts}
        assert mounts[WORKSPACE] == repository.resolve()
        assert mounts[ROBOTS_IN_CONTAINER] == ROBOTS
        assert spec.env[ROBOTS_ENV] == ROBOTS_IN_CONTAINER
    assert vehicle.bridge.env["FIELDPROOF_MANIFEST"] == f"{WORKSPACE}/robot.yaml"
    assert vehicle.spawn_job.command[2:4] == ("fieldproof_robots", "spawn_ugv_ref.launch.py")
    assert vehicle.container.image == "fieldproof-vehicle-acme_rover:local"
    assert resolved.to_dict()["stack_root"] == str(REPO)


def test_init_build_uses_the_workspace_base_image(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIELDPROOF_IMAGE_TAG", "main")
    out = repository / "ctx"
    result = runner.invoke(
        app,
        ["vehicle", "build", str(repository / "robot.yaml"), "--dry-run", "--context", str(out)],
    )
    assert result.exit_code == 0, result.output
    command = plain(result.output).splitlines()[0]
    assert "--tag fieldproof-vehicle-acme_rover:local" in command
    assert f"--build-context vehicle-source={repository.resolve()}" in command
    assert "BASE_IMAGE=" not in command, "the Dockerfile's default is the workspace's image"
    assert "ARG BASE_IMAGE=ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:main" in (
        (out / "Dockerfile").read_text()
    )
    result = runner.invoke(
        app,
        [
            "vehicle",
            "build",
            str(repository / "robot.yaml"),
            "--dry-run",
            "--context",
            str(out),
            "--base-image",
            "example.test/base:1",
        ],
    )
    assert "--build-arg BASE_IMAGE=example.test/base:1" in plain(result.output)


def test_init_bridge_bundle_generates(repository: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    result = runner.invoke(app, ["bridge", "gen", str(repository / "robot.yaml"), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "bridge.yaml").is_file()


def test_init_options_and_refusals(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    result = runner.invoke(
        app,
        [
            "vehicle",
            "init",
            str(bare),
            "--name",
            "rover",
            "--package",
            "rover_stack",
            "--launch",
            "field.launch.py",
            "--no-package",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (bare / "src/rover_stack").exists()
    assert (bare / "src").is_dir()
    manifest = yaml.safe_load((bare / "robot.yaml").read_text())
    assert manifest["runtime"]["entrypoint"]["launch"] == {
        "package": "rover_stack",
        "file": "field.launch.py",
        "args": {"use_sim_time": True, "drivers": False},
        "namespace_arg": "namespace",
    }
    assert manifest["runtime"]["ready"]["conditions"] == [{"topic": "odom"}]
    scenario = yaml.safe_load((bare / "scenarios/rover_smoke.yaml").read_text())
    assert "/status" not in scenario["outputs"]["topics"]
    assert runner.invoke(app, ["validate", str(bare / "scenarios/rover_smoke.yaml")]).exit_code == 0
    # Existing files are not overwritten without --force.
    result = runner.invoke(app, ["vehicle", "init", str(bare), "--name", "rover"])
    assert result.exit_code == 2
    assert "exist(s) already" in plain(result.output)
    result = runner.invoke(app, ["vehicle", "init", str(bare), "--name", "rover", "--force"])
    assert result.exit_code == 0, result.output
    for arguments, message in (
        (["--name", "Rover"], "not a vehicle name"),
        (["--model", "no_such"], "no reference model"),
        (["--package", "my-pkg"], "package 'my-pkg'"),
    ):
        result = runner.invoke(app, ["vehicle", "init", str(tmp_path / "x"), *arguments])
        assert result.exit_code == 2, result.output
        assert message in plain(result.output)


def test_vehicle_name_from_the_directory(tmp_path: Path) -> None:
    assert vehicle_name(tmp_path / "Acme-Rover 2", None) == "acme_rover_2"
    assert vehicle_name(tmp_path / "42", None) == "vehicle_42"
    assert vehicle_name(tmp_path / "---", None) == "vehicle"
    assert vehicle_name(tmp_path / "x", "rover") == "rover"
    with pytest.raises(ScaffoldError, match="not a vehicle name"):
        vehicle_name(tmp_path / "x", "9lives")
    with pytest.raises(ScaffoldError, match="no reference model"):
        scaffold(tmp_path / "x", model="ugv_ref", robots=tmp_path)
