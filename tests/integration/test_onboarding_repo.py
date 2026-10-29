"""Integration test of the vehicle onboarding SDK (task P2-T08, its verification).

The example vehicle repository (``examples/fieldproof-example-vehicle``, the copy of
``fieldproof-example-vehicle``) is what an external team's repository looks like once it has
followed docs/reference/onboarding.md: a manifest that ``extends: fieldproof:ugv_ref``, its own
``fieldproof.toml``, its own stack and scenarios, no path into Fieldproof. The test copies it
next to this checkout, the way a team clones it next to theirs, and drives it through its own
Makefile with the checkout as ``FIELDPROOF_ROOT``:

1. ``make validate``: the manifest and both scenarios validate from the copy, against the
   checkout's stack (the reference model by name, the depot world);
2. ``make image``: the vehicle image builds on the local base image (``FIELDPROOF_IMAGE_TAG``,
   ``local`` by default: the images ``make images`` built), nav2_msgs and tf2 from the apt
   snapshots, the package's navigator installed, no compiler left;
3. ``make run``: ``scenarios/dock_to_yard.yaml`` passes: the run directory is the copy's
   (``runs/<run_id>`` under the repository, not the checkout), every assertion passed, the
   record names the checkout as the stack root and the copy as the workspace, and no
   container of the run is left;
4. ``fieldproof vehicle init`` writes a repository whose manifest and smoke scenario validate
   from outside the checkout the same way.

Nothing of Fieldproof's source is read or changed by the repository: the definition of done
of task P2-T08. Needs the stack images (``make images``) and the Fuel models (``make assets``);
the vehicle image is built here (about a minute on a cached base). Software rendering keeps the
simulator below real time on a CI runner, so the wall-clock limits are generous.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/fieldproof-example-vehicle"
RUN_LABEL = "io.fieldproof.run"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
BUILD_TIMEOUT_S = 1800.0
RUN_TIMEOUT_S = 1500.0
IMAGE_TAG = os.environ.get("FIELDPROOF_IMAGE_TAG", "local")
ASSERTIONS = 10
"""The assertions of dock_to_yard.yaml."""


def make(copy: Path, *targets: str, timeout_s: float, **variables: str) -> str:
    """`make -C copy TARGETS` with the checkout as FIELDPROOF_ROOT; the output, on a failure too."""
    command = [
        "make",
        "-C",
        str(copy),
        *targets,
        f"FIELDPROOF_ROOT={REPO}",
        *(f"{key}={value}" for key, value in variables.items()),
    ]
    env = {**os.environ, "FIELDPROOF_IMAGE_TAG": IMAGE_TAG, "FORCE_COLOR": "0"}
    completed = subprocess.run(  # noqa: S603  (fixed argument list)
        command, capture_output=True, text=True, timeout=timeout_s, env=env, check=False
    )
    output = ANSI.sub("", completed.stdout + completed.stderr)
    assert completed.returncode == 0, f"{' '.join(command)} failed:\n{output[-6000:]}"
    return output


def containers_of(run_id: str) -> list[str]:
    completed = subprocess.run(  # noqa: S603  (fixed argument list)
        ["docker", "ps", "-a", "--filter", f"label={RUN_LABEL}={run_id}", "--format", "{{.Names}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.split()


@pytest.fixture(scope="module")
def copy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The example repository copied outside the checkout, as a team would clone it."""
    target = tmp_path_factory.mktemp("team") / "fieldproof-example-vehicle"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns("runs", "__pycache__", "*.mcap"))
    assert not (target / "runs").exists()
    return target


def test_01_validates_from_outside(copy: Path) -> None:
    output = make(copy, "validate", timeout_s=300.0)
    assert "ok robot.yaml: example_ugv" in output
    assert "ok scenarios/dock_to_yard.yaml" in output
    assert "ok scenarios/obstacle_hold.yaml" in output
    # The repository holds no path into the checkout: the model comes by name.
    assert "extends: fieldproof:ugv_ref" in (copy / "robot.yaml").read_text()
    for file in copy.rglob("*"):
        if file.is_file() and file.suffix in (".yaml", ".toml", ".py", ".xml", ".md"):
            assert str(REPO) not in file.read_text(), f"{file} names the checkout"


def test_02_image_builds(copy: Path) -> None:
    output = make(copy, "image", timeout_s=BUILD_TIMEOUT_S)
    assert "built fieldproof-vehicle-example-ugv:local" in output
    probe = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "/usr/local/bin/fieldproof-vehicle-entrypoint",
        "fieldproof-vehicle-example-ugv:local",
        "bash",
        "-c",
        "ros2 pkg prefix example_ugv_bringup && ros2 pkg prefix nav2_msgs && "
        "test -x /opt/vehicle/lib/example_ugv_bringup/waypoint_navigator && "
        "python3 -c 'import tf2_ros, nav2_msgs.action' && ! command -v gcc && echo probe-ok",
    ]
    completed = subprocess.run(probe, capture_output=True, text=True, timeout=300, check=False)  # noqa: S603
    assert "probe-ok" in completed.stdout, completed.stdout + completed.stderr


def test_03_scenario_passes_from_outside(copy: Path) -> None:
    run_id = "onboarding-dock"
    output = make(
        copy, "run", timeout_s=RUN_TIMEOUT_S, RUN_FLAGS=f"--run-id {run_id} --render software"
    )
    try:
        assert f"PASS dock_to_yard ({run_id})" in output, output[-4000:]
        run_dir = copy / "runs" / run_id
        assert run_dir.is_dir(), "the run lands in the repository's runs directory"
        record = json.loads((run_dir / "run.json").read_text())
        assert record["status"] == "passed"
        assert record["stack"]["world"] == "depot"
        # The repository is the workspace; the stack is the checkout's.
        assert record["stack"]["workspace"] == str(copy.resolve())
        assert record["stack"]["stack_root"] == str(REPO)
        assert record["vehicles"][0]["manifest"] == "example_ugv"
        report = json.loads((run_dir / "report.json").read_text())
        assert report["verdict"] == "PASS"
        assert [a["status"] for a in report["assertions"]] == ["passed"] * ASSERTIONS
        assert "vehicle.ready" in (run_dir / "engine.log").read_text()
        assert list((run_dir / "recording").glob("*.mcap")), "the recording"
        assert (run_dir / "junit.xml").is_file()
    finally:
        print(output[-3000:])  # noqa: T201  (CI keeps nothing else of the run)
    assert containers_of(run_id) == [], "no container of the run is left"


def test_04_scaffold_validates_from_outside(tmp_path: Path) -> None:
    directory = tmp_path / "acme-rover"
    completed = subprocess.run(  # noqa: S603  (fixed argument list)
        [sys.executable, "-m", "fieldproof", "vehicle", "init", str(directory)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = make(directory, "validate", timeout_s=300.0)
    assert "ok robot.yaml: acme_rover" in output
    assert "ok scenarios/acme_rover_smoke.yaml" in output
