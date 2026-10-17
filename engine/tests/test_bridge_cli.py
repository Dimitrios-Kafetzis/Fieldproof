"""Tests of ``fieldproof bridge gen`` (task P1-T03): the engine runs the bridge's generator."""

import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from fieldproof.cli import app

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")
REPO = Path(__file__).resolve().parents[2]
UGV_MANIFEST = REPO / "robots/fieldproof_robots/models/ugv_ref/robot.yaml"


def test_bridge_gen_writes_the_bundle(tmp_path: Path) -> None:
    out = tmp_path / "ugv"
    result = runner.invoke(
        app, ["bridge", "gen", str(UGV_MANIFEST), "--out", str(out), "--namespace", "ugv1"]
    )
    assert result.exit_code == 0, result.output
    listed = {
        Path(line.strip()).name for line in ANSI.sub("", result.output).splitlines() if line.strip()
    }
    assert listed == {
        "ros_gz_bridge.yaml",
        "ros_gz_bridge_camera_optical_link.yaml",
        "params.yaml",
        "bridge.yaml",
        "contract.yaml",
        "bridge.launch.py",
    }
    contract = yaml.safe_load((out / "contract.yaml").read_text())
    assert contract["namespace"] == "/ugv1"
    names = {t["name"] for t in contract["topics"]}
    assert {
        "/ugv1/scan",
        "/ugv1/imu/data",
        "/ugv1/odom",
        "/ugv1/camera/image",
        "/tf",
        "/clock",
    } <= names


def test_bridge_gen_options(tmp_path: Path) -> None:
    out = tmp_path / "bare"
    result = runner.invoke(
        app,
        [
            "bridge",
            "gen",
            str(UGV_MANIFEST),
            "-o",
            str(out),
            "--no-clock",
            "--no-robot-state-publisher",
            "--without",
            "gps",
        ],
    )
    assert result.exit_code == 0, result.output
    bridge = yaml.safe_load((out / "bridge.yaml").read_text())
    assert bridge["clock"] is False
    assert bridge["description"] is None
    assert [n["name"] for n in bridge["nodes"]] == [
        "ros_gz_bridge",
        "ros_gz_bridge_camera_optical_link",
        "joint_states_throttle",
        "imu_adapter",
    ]


def test_bridge_gen_rejects_a_bad_manifest(tmp_path: Path) -> None:
    bad = tmp_path / "robot.yaml"
    bad.write_text("apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nmetadata: {name: x}\n")
    result = runner.invoke(app, ["bridge", "gen", str(bad), "-o", str(tmp_path / "out")])
    assert result.exit_code == 2, result.output
    assert "missing frames" in ANSI.sub("", result.output)
    assert not (tmp_path / "out").exists()


def test_bridge_help_lists_gen() -> None:
    result = runner.invoke(app, ["bridge", "--help"])
    assert result.exit_code == 0, result.output
    assert "gen" in ANSI.sub("", result.output)
