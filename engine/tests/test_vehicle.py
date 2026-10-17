"""Tests of the vehicle image generator and the `fieldproof vehicle` commands (task P1-T04)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from fieldproof import __version__, vehicle_ready
from fieldproof.cli import app
from fieldproof.manifest import RobotManifest, load_manifest, validate_data
from fieldproof.vehicle import (
    ENTRYPOINT_IN_IMAGE,
    READY_IN_IMAGE,
    SOURCE_CONTEXT,
    VehicleError,
    build_command,
    render_dockerfile,
    render_repos,
    run_command,
    wait_command,
    write_context,
)

REPO = Path(__file__).resolve().parents[2]
UGV_MANIFEST = REPO / "robots/fieldproof_robots/models/ugv_ref/robot.yaml"
NAV2_MANIFEST = REPO / "examples/vehicles/ugv_nav2/robot.yaml"
runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return " ".join(ANSI.sub("", text).split())


@pytest.fixture(scope="module")
def nav2() -> RobotManifest:
    return load_manifest(NAV2_MANIFEST)


@pytest.fixture
def repo_vehicle(tmp_path: Path) -> RobotManifest:
    """A vehicle built from two repositories, extra apt packages and colcon arguments."""
    manifest = tmp_path / "robot.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "fieldproof.dev/v0alpha1",
                "kind": "Robot",
                "extends": str(UGV_MANIFEST),
                "metadata": {"name": "bot", "description": 'A "quoted" description'},
                "runtime": {
                    "image": "example.test/bot:dev",
                    "build": {
                        "base": "example.test/base:1",
                        "rosdistro_commit": "f" * 40,
                        "repos": [
                            {
                                "name": "bot_bringup",
                                "url": "https://example.test/bot_bringup.git",
                                "version": "a" * 40,
                            },
                            {
                                "name": "bot_msgs",
                                "url": "git@example.test:msgs.git",
                                "version": "b" * 40,
                            },
                        ],
                        "apt": ["iproute2", "tcpdump"],
                        "rosdep_skip_keys": ["rviz2", "gazebo"],
                        "colcon_args": [
                            "--packages-skip",
                            "bot_hw tests",
                            "--parallel-workers",
                            "2",
                        ],
                    },
                    "entrypoint": {"command": ["ros2", "run", "bot_bringup", "main"]},
                    "env": {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"},
                    "resources": {"cpus": 2, "memory": "1g", "pids": 512},
                },
            }
        )
    )
    return load_manifest(manifest)


# ---------------------------------------------------------------------------------- dockerfile
def test_dockerfile_of_the_example(nav2: RobotManifest) -> None:
    text = render_dockerfile(nav2)
    lines = text.splitlines()
    froms = [line for line in lines if line.startswith("FROM ")]
    assert froms == ["FROM ${BASE_IMAGE} AS build", "FROM ${BASE_IMAGE} AS runtime"]
    assert "ARG BASE_IMAGE=ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:local" in lines
    assert lines.count("ARG ROSDISTRO_COMMIT=8e9a99d200fd312f106418b2b497b0cc5146e6a7") == 2
    assert "ARG BASE_IMAGE" in lines, "redeclared in the runtime stage for the label"
    assert "RUN install -d -m 755 /opt/fieldproof" in lines
    assert f"COPY --from={SOURCE_CONTEXT} src/ src/src/" in lines
    assert "COPY vehicle.repos ./" not in text, "no repositories in the example"
    assert "rosdep update --rosdistro" in text
    assert "index-v4.yaml" in text
    assert "--dependency-types exec" in text
    assert "colcon build --merge-install --install-base /opt/vehicle" in text
    assert "--skip-keys" not in text
    cmd = 'CMD ["ros2", "launch", "ugv_nav2_bringup", "bringup.launch.py", '
    assert cmd + '"use_sim_time:=true", "drivers:=false"]' in lines
    assert "USER ${FIELDPROOF_USER}" in lines
    assert f"COPY --chmod=755 ready.py {READY_IN_IMAGE}" in lines
    healthcheck = lines.index(
        "HEALTHCHECK --interval=15s --timeout=30s --start-period=60s --retries=3 \\"
    )
    assert lines[healthcheck + 1] == (
        f'    CMD ["{ENTRYPOINT_IN_IMAGE}", "{READY_IN_IMAGE}", "--timeout", "10"]'
    )
    assert healthcheck < lines.index("USER ${FIELDPROOF_USER}"), "the check runs as the user"
    assert lines[-1].strip().startswith("io.fieldproof.rosdistro-commit=")
    assert 'io.fieldproof.vehicle="ugv_nav2"' in text
    assert 'io.fieldproof.tier="T1"' in text
    # The runtime stage installs only what rosdep listed for exec plus nothing else.
    runtime = text[text.index("AS runtime") :]
    assert "fieldproof-apt-install; fi\n" in runtime
    assert "build-essential" not in runtime


def test_dockerfile_without_ready_has_no_healthcheck(repo_vehicle: RobotManifest) -> None:
    text = render_dockerfile(repo_vehicle)
    assert "HEALTHCHECK" not in text
    assert f"COPY --chmod=755 ready.py {READY_IN_IMAGE}" in text, "the probe is always there"


def test_dockerfile_with_repos_apt_and_colcon_args(repo_vehicle: RobotManifest) -> None:
    text = render_dockerfile(repo_vehicle)
    assert "ARG BASE_IMAGE=example.test/base:1" in text
    assert f"ARG ROSDISTRO_COMMIT={'f' * 40}" in text
    assert "COPY vehicle.repos ./\nRUN vcs import --input vehicle.repos src" in text
    assert "COPY --from=" + SOURCE_CONTEXT not in text
    assert '--skip-keys "rviz2 gazebo"' in text
    assert "-DCMAKE_BUILD_TYPE=Release --packages-skip 'bot_hw tests' --parallel-workers 2" in text
    assert "&& fieldproof-apt-install iproute2 tcpdump" in text
    assert 'CMD ["ros2", "run", "bot_bringup", "main"]' in text
    assert "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp" in text
    assert 'org.opencontainers.image.description="A "quoted" description"' in text
    repos = render_repos(repo_vehicle)
    assert repos is not None
    data = yaml.safe_load(repos)
    assert data["repositories"]["bot_bringup"] == {
        "type": "git",
        "url": "https://example.test/bot_bringup.git",
        "version": "a" * 40,
    }
    assert list(data["repositories"]) == ["bot_bringup", "bot_msgs"]


def test_dockerfile_refusals() -> None:
    data = yaml.safe_load(UGV_MANIFEST.read_text())
    with pytest.raises(VehicleError, match="no runtime section"):
        render_dockerfile(validate_data(data))
    data["runtime"] = {
        "image": "example.test/prebuilt:1",
        "entrypoint": {"command": ["true"]},
    }
    with pytest.raises(VehicleError, match="prebuilt"):
        render_dockerfile(validate_data(data))
    data["runtime"]["build"] = {"sources": ["src"]}
    data["runtime"]["tier"] = "T2"
    with pytest.raises(VehicleError, match="Phase 6"):
        render_dockerfile(validate_data(data))


# ------------------------------------------------------------------------------------- context
def test_write_context(nav2: RobotManifest, tmp_path: Path) -> None:
    context = write_context(nav2, tmp_path / "ctx")
    assert context.dockerfile == tmp_path / "ctx" / "Dockerfile"
    assert sorted(p.name for p in context.files) == [
        "Dockerfile",
        "entrypoint.sh",
        "ready.py",
        "robot.yaml",
    ]
    assert (tmp_path / "ctx" / "ready.py").read_text() == Path(vehicle_ready.__file__).read_text()
    entrypoint = (tmp_path / "ctx" / "entrypoint.sh").read_text()
    assert entrypoint.startswith("#!/usr/bin/env bash\n")
    assert 'source "/opt/vehicle/setup.bash"' in entrypoint
    assert entrypoint.rstrip().endswith('exec "$@"')
    resolved = yaml.safe_load((tmp_path / "ctx" / "robot.yaml").read_text())
    assert "extends" not in resolved
    assert resolved["metadata"]["name"] == "ugv_nav2"
    assert resolved["runtime"]["entrypoint"]["launch"]["args"] == {
        "use_sim_time": True,
        "drivers": False,
    }
    assert len(resolved["sensors"]) == 4
    assert resolved["description"]["xacro"].endswith("ugv_ref/urdf/ugv_ref.urdf.xacro")


def test_write_context_with_repos(repo_vehicle: RobotManifest, tmp_path: Path) -> None:
    context = write_context(repo_vehicle, tmp_path / "ctx")
    assert sorted(p.name for p in context.files) == [
        "Dockerfile",
        "entrypoint.sh",
        "ready.py",
        "robot.yaml",
        "vehicle.repos",
    ]


def test_write_context_checks_sources_and_params(tmp_path: Path) -> None:
    data = yaml.safe_load(UGV_MANIFEST.read_text())
    manifest_file = tmp_path / "robot.yaml"
    data["runtime"] = {
        "image": "example.test/bot:1",
        "build": {"sources": ["src"]},
        "entrypoint": {"command": ["true"]},
        "params": ["config/nav2.yaml"],
    }
    data["description"]["xacro"] = str(UGV_MANIFEST.parent / "urdf/ugv_ref.urdf.xacro")
    manifest_file.write_text(yaml.safe_dump(data))
    manifest = load_manifest(manifest_file)
    with pytest.raises(VehicleError, match="sources: 'src' is not a directory"):
        write_context(manifest, tmp_path / "ctx")
    (tmp_path / "src").mkdir()
    with pytest.raises(VehicleError, match=r"params: 'config/nav2\.yaml' is not a file"):
        write_context(manifest, tmp_path / "ctx")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/nav2.yaml").write_text("{}\n")
    assert write_context(manifest, tmp_path / "ctx").dockerfile.is_file()


# ------------------------------------------------------------------------------------ commands
def test_build_command(nav2: RobotManifest, tmp_path: Path) -> None:
    context = write_context(nav2, tmp_path / "ctx")
    command = build_command(nav2, context, progress="plain")
    assert command[:6] == ["docker", "buildx", "build", "--builder", "default", "--load"]
    assert build_command(nav2, context, builder=None)[:4] == ["docker", "buildx", "build", "--load"]
    assert "--builder" not in build_command(nav2, context, builder=None)
    assert command[command.index("--file") + 1] == str(context.dockerfile)
    assert (
        command[command.index("--tag") + 1]
        == "ghcr.io/dimitrios-kafetzis/fieldproof-vehicle-ugv-nav2:local"
    )
    assert f"VERSION={__version__}" in command
    assert any(a.startswith("GIT_SHA=") and len(a) in (len("GIT_SHA=unknown"), 48) for a in command)
    assert any(re.fullmatch(r"BUILD_DATE=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", a) for a in command)
    assert f"{SOURCE_CONTEXT}={NAV2_MANIFEST.parent}" in command
    assert "--progress=plain" in command
    assert command[-1] == str(tmp_path / "ctx")
    tagged = build_command(
        nav2, context, tag="x:y", base_image="b:1", no_cache=True, extra_args=("--pull",)
    )
    assert "x:y" in tagged
    assert "BASE_IMAGE=b:1" in tagged
    assert "--no-cache" in tagged
    assert tagged[-2] == "--pull"


def test_build_command_without_local_sources(repo_vehicle: RobotManifest, tmp_path: Path) -> None:
    context = write_context(repo_vehicle, tmp_path / "ctx")
    command = build_command(repo_vehicle, context)
    assert "--build-context" not in command


def test_run_command(nav2: RobotManifest, repo_vehicle: RobotManifest) -> None:
    command = run_command(nav2, domain_id=7)
    assert command == [
        "docker",
        "run",
        "--rm",
        "--name",
        "ugv_nav2",
        "--hostname",
        "ugv_nav2",
        "--network",
        "fieldproof",
        "--env",
        "ROS_DOMAIN_ID=7",
        "--cpus",
        "2",
        "--memory",
        "2g",
        "ghcr.io/dimitrios-kafetzis/fieldproof-vehicle-ugv-nav2:local",
    ]
    command = run_command(
        repo_vehicle,
        name="bot1",
        network=None,
        detach=True,
        remove=False,
        image="other:1",
        env={"ROS_DOMAIN_ID": "3"},
        extra_args=("--ipc", "host"),
        command=("bash", "-c", "ros2 node list"),
    )
    assert "--rm" not in command
    assert "--network" not in command
    assert command[command.index("--detach") + 1 :][:4] == ["--name", "bot1", "--hostname", "bot1"]
    assert command.count("--env") == 2
    assert "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp" in command
    assert "ROS_DOMAIN_ID=3" in command
    assert command[command.index("--pids-limit") + 1] == "512"
    assert command[-6:] == ["--ipc", "host", "other:1", "bash", "-c", "ros2 node list"]


# ---------------------------------------------------------------------------------------- CLI
def test_cli_dockerfile(tmp_path: Path) -> None:
    out = tmp_path / "ctx"
    result = runner.invoke(app, ["vehicle", "dockerfile", str(NAV2_MANIFEST), "-o", str(out)])
    assert result.exit_code == 0, result.output
    listed = {
        Path(line.strip()).name for line in ANSI.sub("", result.output).splitlines() if line.strip()
    }
    assert listed == {"Dockerfile", "entrypoint.sh", "ready.py", "robot.yaml"}
    assert (out / "Dockerfile").read_text() == render_dockerfile(load_manifest(NAV2_MANIFEST))


def test_cli_dockerfile_default_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["vehicle", "dockerfile", str(NAV2_MANIFEST)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "ugv_nav2_image" / "Dockerfile").is_file()


def test_cli_refuses_a_manifest_without_runtime(tmp_path: Path) -> None:
    result = runner.invoke(app, ["vehicle", "dockerfile", str(UGV_MANIFEST), "-o", str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert "no runtime section" in plain(result.output)
    assert not (tmp_path / "Dockerfile").exists()


def test_cli_build_dry_run(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "vehicle",
            "build",
            str(NAV2_MANIFEST),
            "--context",
            str(tmp_path / "ctx"),
            "--dry-run",
            "-t",
            "t:1",
        ],
    )
    assert result.exit_code == 0, result.output
    output = plain(result.output)
    assert output.startswith("docker buildx build --builder default --load --file")
    assert "--tag t:1" in output
    assert (tmp_path / "ctx" / "Dockerfile").is_file()


def test_cli_run_dry_run() -> None:
    result = runner.invoke(
        app,
        [
            "vehicle",
            "run",
            str(NAV2_MANIFEST),
            "--dry-run",
            "--domain-id",
            "5",
            "-e",
            "A=1",
            "--",
            "bash",
        ],
    )
    assert result.exit_code == 0, result.output
    output = plain(result.output)
    assert output.startswith(
        "docker run --rm --name ugv_nav2 --hostname ugv_nav2 --network fieldproof"
    )
    assert "--env ROS_DOMAIN_ID=5" in output
    assert "--env A=1" in output
    assert output.endswith("fieldproof-vehicle-ugv-nav2:local bash")


def test_wait_command(nav2: RobotManifest, repo_vehicle: RobotManifest) -> None:
    probe = [ENTRYPOINT_IN_IMAGE, READY_IN_IMAGE]
    assert wait_command(nav2) == ["docker", "exec", "ugv_nav2", *probe]
    assert wait_command(repo_vehicle, name="bot-1", timeout_s=30, interval_s=0.5, quiet=True) == [
        "docker",
        "exec",
        "bot-1",
        *probe,
        "--timeout",
        "30",
        "--interval",
        "0.5",
        "--quiet",
    ]
    plain_manifest = load_manifest(UGV_MANIFEST)
    with pytest.raises(VehicleError, match="no runtime section"):
        wait_command(plain_manifest)


def test_cli_wait_dry_run() -> None:
    result = runner.invoke(
        app, ["vehicle", "wait", str(NAV2_MANIFEST), "--dry-run", "--name", "c1", "--timeout", "9"]
    )
    assert result.exit_code == 0, result.output
    assert plain(result.output) == (
        f"docker exec c1 {ENTRYPOINT_IN_IMAGE} {READY_IN_IMAGE} --timeout 9"
    )
    result = runner.invoke(app, ["vehicle", "wait", str(UGV_MANIFEST), "--dry-run"])
    assert result.exit_code == 2
    assert "no runtime section" in plain(result.output)
