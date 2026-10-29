"""Tests of the vehicle manifest schema v0 (task P1-T04): the reference and example manifests
validate, `extends` merges, the runtime section is checked, errors name their location."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from fieldproof.cli import app
from fieldproof.manifest import (
    BRIDGE_TOPICS,
    DEFAULT_ROSDISTRO_COMMIT,
    Resources,
    RobotManifest,
    json_schema,
    load_manifest,
    memory_bytes,
    validate_data,
)
from fieldproof_bridge.manifest import Manifest as BridgeManifest
from fieldproof_bridge.manifest import ManifestError

REPO = Path(__file__).resolve().parents[2]
UGV_MANIFEST = REPO / "robots/fieldproof_robots/models/ugv_ref/robot.yaml"
NAV2_MANIFEST = REPO / "examples/vehicles/ugv_nav2/robot.yaml"
SCHEMA_FILE = REPO / "engine/schemas/robot.schema.json"
runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(scope="module")
def ugv() -> RobotManifest:
    return load_manifest(UGV_MANIFEST)


@pytest.fixture(scope="module")
def nav2() -> RobotManifest:
    return load_manifest(NAV2_MANIFEST)


@pytest.fixture
def runtime() -> dict[str, Any]:
    """A runtime section that builds from one repository and swaps one driver."""
    return {
        "image": "example.test/vehicle:local",
        "build": {
            "repos": [
                {
                    "name": "bot_bringup",
                    "url": "https://example.test/bot_bringup.git",
                    "version": "0123456789abcdef0123456789abcdef01234567",
                }
            ]
        },
        "entrypoint": {"launch": {"package": "bot_bringup", "file": "bringup.launch.py"}},
        "drivers": [{"name": "lidar", "package": "rplidar_ros", "provides": ["scan"]}],
        "ready": {"conditions": [{"lifecycle": "/bt_navigator"}, {"topic": "/map"}]},
        "resources": {"cpus": 1.5, "memory": "1g"},
    }


# ----------------------------------------------------------------------------------- the files
def test_reference_ugv_validates(ugv: RobotManifest) -> None:
    assert ugv.name == "ugv_ref"
    assert ugv.runtime is None
    assert ugv.path == UGV_MANIFEST
    assert ugv.directory == UGV_MANIFEST.parent
    assert [s.name for s in ugv.sensors] == ["lidar", "imu", "camera", "gps"]
    assert ugv.drive is not None
    assert ugv.drive.joint_states.gz_rate == "physics"
    assert ugv.drive.tf.qos.depth == 100
    assert ugv.sensors[3].optional is True
    assert ugv.sensors[3].outputs[0].gz_header_frame_id is False
    assert ugv.sensors[2].outputs[0].qos.depth == 5
    assert ugv.sensors[2].resolution == (640, 480)


def test_reference_interfaces_match_the_bridge(ugv: RobotManifest) -> None:
    bridge = BridgeManifest.load(UGV_MANIFEST)
    assert list(ugv.interfaces()) == [i.ros_topic for i in bridge.interfaces]
    for interface in bridge.interfaces:
        topic = ugv.interfaces()[interface.ros_topic]
        assert topic.gz_topic == interface.gz_topic
        assert topic.ros_type == interface.ros_type
        assert topic.qos.model_dump() == interface.qos.as_dict()


def test_example_vehicle_extends_the_reference(nav2: RobotManifest, ugv: RobotManifest) -> None:
    assert nav2.name == "ugv_nav2"
    assert nav2.extends is None, "resolved before validation"
    assert nav2.metadata.domain == "ground"
    assert nav2.frames == ugv.frames
    assert nav2.drive == ugv.drive
    assert nav2.sensors == ugv.sensors
    assert nav2.description is not None
    assert ugv.description is not None
    assert (nav2.directory / nav2.description.xacro).resolve() == (
        ugv.directory / ugv.description.xacro
    ).resolve()
    runtime = nav2.runtime
    assert runtime is not None
    assert runtime.tier == "T1"
    assert runtime.build is not None
    assert runtime.build.sources == ["src"]
    assert runtime.build.base is None, "the workspace's base image (task P2-T08)"
    assert runtime.build.rosdistro_commit == DEFAULT_ROSDISTRO_COMMIT
    assert runtime.entrypoint.argv() == [
        "ros2",
        "launch",
        "ugv_nav2_bringup",
        "bringup.launch.py",
        "use_sim_time:=true",
        "drivers:=false",
    ]
    for params in runtime.params:
        assert (nav2.directory / params).is_file(), params
    assert nav2.unswapped() == [], "every interface of the model has a field driver"
    provided = {t for d in runtime.drivers for t in d.provides}
    assert provided == set(nav2.interfaces()) | {"tf_static", "robot_description"}
    assert runtime.entrypoint.launch is not None
    assert runtime.entrypoint.launch.namespace_arg == "namespace"
    assert runtime.entrypoint.argv(namespace="ugv2")[-1] == "namespace:=ugv2"
    assert runtime.dds_profile == "src/ugv_nav2_bringup/config/fastdds.xml"
    assert (nav2.directory / runtime.dds_profile).is_file()
    assert runtime.dds_profile_env() == {
        "FASTRTPS_DEFAULT_PROFILES_FILE": "/opt/fieldproof/dds/profile.xml"
    }
    assert runtime.ready is not None
    assert runtime.ready.timeout_s == 180
    assert runtime.resources is not None
    assert runtime.resources.memory == "2g"


def test_example_vehicle_reads_in_the_bridge(nav2: RobotManifest) -> None:
    """The bridge launch takes the vehicle manifest and finds the reference model's interfaces."""
    bridge = BridgeManifest.load(NAV2_MANIFEST)
    assert bridge.name == "ugv_nav2"
    assert [i.ros_topic for i in bridge.interfaces] == list(nav2.interfaces())
    assert bridge.xacro_path().is_file()


# ---------------------------------------------------------------------------------- the schema
def test_minimal_runtime(ugv: RobotManifest, runtime: dict[str, Any]) -> None:
    data = yaml.safe_load(UGV_MANIFEST.read_text())
    data["runtime"] = runtime
    manifest = validate_data(data)
    assert manifest.runtime is not None
    assert manifest.runtime.tier == "T1"
    assert manifest.runtime.build is not None
    assert manifest.runtime.build.repos[0].type == "git"
    assert manifest.runtime.ready is not None
    conditions = manifest.runtime.ready.conditions
    assert conditions[0].model_dump() == {"lifecycle": "/bt_navigator", "state": "active"}
    assert conditions[1].model_dump() == {"topic": "/map", "type": None}
    assert manifest.unswapped() == [t for t in ugv.interfaces() if t != "scan"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r.__setitem__("tier", "T4"), "runtime.tier"),
        (lambda r: r.pop("image"), "runtime.image: Field required"),
        (lambda r: r.pop("entrypoint"), "runtime.entrypoint: Field required"),
        (lambda r: r["entrypoint"].__setitem__("command", ["bash"]), "exactly one of launch"),
        (lambda r: r.__setitem__("entrypoint", {}), "exactly one of launch or command"),
        (lambda r: r["build"].__setitem__("repos", []), "build needs repos or sources"),
        (lambda r: r["build"].__setitem__("sources", ["../escape"]), "inside the manifest"),
        (lambda r: r["build"].__setitem__("sources", ["/abs"]), "inside the manifest"),
        (lambda r: r["build"]["repos"][0].__setitem__("version", "main"), "version: String"),
        (lambda r: r["build"]["repos"][0].__setitem__("version", "abc123"), "version: String"),
        (lambda r: r["build"].__setitem__("rosdistro_commit", "HEAD"), "rosdistro_commit"),
        (lambda r: r["drivers"][0].__setitem__("provides", ["lidar/points"]), "not an interface"),
        (lambda r: r["drivers"][0].__setitem__("provides", []), "provides"),
        (
            lambda r: r["drivers"].append({"name": "x", "package": "p", "provides": ["scan"]}),
            "provided for both",
        ),
        (
            lambda r: r["drivers"].append({"name": "lidar", "package": "p", "provides": ["odom"]}),
            "names must be unique",
        ),
        (lambda r: r["drivers"][0].__setitem__("name", "Lidar-1"), "drivers.0.name"),
        (lambda r: r["ready"].__setitem__("conditions", []), "ready.conditions"),
        (lambda r: r["ready"]["conditions"].append({"lifecycle": "/x", "state": "up"}), "state"),
        (lambda r: r["ready"]["conditions"].append({"nope": "/x"}), "ready.conditions.2"),
        (lambda r: r["ready"]["conditions"].append({"tf": "map"}), "child"),
        (lambda r: r["ready"].__setitem__("timeout_s", 0), "timeout_s"),
        (lambda r: r["resources"].__setitem__("memory", "lots"), "resources.memory"),
        (lambda r: r["resources"].__setitem__("cpus", -1), "resources.cpus"),
        (lambda r: r["resources"].__setitem__("cpuset", "0-"), "resources.cpuset"),
        (lambda r: r["resources"].__setitem__("cpuset", "a"), "resources.cpuset"),
        (lambda r: r.__setitem__("dds_profile", "/etc/fastdds.xml"), "dds_profile: .*relative"),
        (lambda r: r.__setitem__("dds_profile", "../other.xml"), "dds_profile: .*relative"),
        (
            lambda r: r["entrypoint"]["launch"].__setitem__("namespace_arg", "my ns"),
            "namespace_arg",
        ),
        (lambda r: r.__setitem__("typo", 1), "runtime.typo: Extra inputs"),
    ],
)
def test_runtime_errors(runtime: dict[str, Any], mutate: Any, message: str) -> None:
    data = yaml.safe_load(UGV_MANIFEST.read_text())
    broken = copy.deepcopy(runtime)
    mutate(broken)
    data["runtime"] = broken
    with pytest.raises(ManifestError, match=message):
        validate_data(data)


def test_resources_merge_and_memory_units() -> None:
    base = Resources(cpus=2.0, memory="2g")
    assert base.merged(None) is base
    merged = base.merged(Resources(memory="3g", cpuset="0-1"))
    assert merged.to_dict() == {"cpus": 2.0, "cpuset": "0-1", "memory": "3g"}
    assert base.to_dict() == {"cpus": 2.0, "memory": "2g"}, "unchanged"
    assert Resources().to_dict() == {}
    assert memory_bytes("2g") == 2 * 1024**3
    assert memory_bytes("512m") == 512 * 1024**2
    assert memory_bytes("1.5G") == int(1.5 * 1024**3)
    assert memory_bytes("64k") == 64 * 1024
    assert memory_bytes("1024") == 1024
    assert memory_bytes("2gb") == 2 * 1024**3


def test_launch_arguments_for_namespace_and_prefix(runtime: dict[str, Any]) -> None:
    data = yaml.safe_load(UGV_MANIFEST.read_text())
    runtime["entrypoint"]["launch"]["args"] = {"use_sim_time": True}
    runtime["entrypoint"]["launch"]["namespace_arg"] = "robot_namespace"
    runtime["entrypoint"]["launch"]["frame_prefix_arg"] = "tf_prefix"
    data["runtime"] = runtime
    entrypoint = validate_data(data).runtime.entrypoint  # type: ignore[union-attr]
    assert entrypoint.takes_namespace()
    assert entrypoint.argv() == [
        "ros2",
        "launch",
        "bot_bringup",
        "bringup.launch.py",
        "use_sim_time:=true",
    ]
    assert entrypoint.argv(namespace="ugv2", frame_prefix="ugv2/") == [
        "ros2",
        "launch",
        "bot_bringup",
        "bringup.launch.py",
        "use_sim_time:=true",
        "robot_namespace:=ugv2",
        "tf_prefix:=ugv2/",
    ]
    assert entrypoint.argv(namespace="", frame_prefix="")[-1] == "use_sim_time:=true"
    runtime["entrypoint"] = {"command": ["ros2", "run", "bot", "main"]}
    data["runtime"] = runtime
    entrypoint = validate_data(data).runtime.entrypoint  # type: ignore[union-attr]
    assert not entrypoint.takes_namespace()
    assert entrypoint.argv(namespace="ugv2") == ["ros2", "run", "bot", "main"]


def test_dds_profile_environment(runtime: dict[str, Any]) -> None:
    data = yaml.safe_load(UGV_MANIFEST.read_text())
    runtime["dds_profile"] = "config/dds.xml"
    data["runtime"] = runtime
    assert validate_data(data).runtime.dds_profile_env() == {  # type: ignore[union-attr]
        "FASTRTPS_DEFAULT_PROFILES_FILE": "/opt/fieldproof/dds/profile.xml"
    }
    runtime["env"] = {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}
    data["runtime"] = runtime
    assert validate_data(data).runtime.dds_profile_env() == {  # type: ignore[union-attr]
        "CYCLONEDDS_URI": "file:///opt/fieldproof/dds/profile.xml"
    }
    runtime["env"] = {"RMW_IMPLEMENTATION": "rmw_zenoh_cpp"}
    data["runtime"] = runtime
    with pytest.raises(ManifestError, match="no known profile variable for RMW 'rmw_zenoh_cpp'"):
        validate_data(data).runtime.dds_profile_env()  # type: ignore[union-attr]
    runtime.pop("dds_profile")
    data["runtime"] = runtime
    assert validate_data(data).runtime.dds_profile_env() == {}  # type: ignore[union-attr]


def test_bridge_topics_may_be_provided(runtime: dict[str, Any]) -> None:
    data = yaml.safe_load(UGV_MANIFEST.read_text())
    runtime["drivers"].append(
        {
            "name": "description",
            "package": "robot_state_publisher",
            "provides": sorted(BRIDGE_TOPICS),
        }
    )
    data["runtime"] = runtime
    assert validate_data(data).runtime is not None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m.__setitem__("apiVersion", "v1"), "apiVersion"),
        (lambda m: m.__setitem__("kind", "Vehicle"), "kind"),
        (lambda m: m["metadata"].__setitem__("name", "UGV"), "metadata.name"),
        (lambda m: m["sensors"].append(m["sensors"][0]), "sensors: names must be unique"),
        (
            lambda m: m["sensors"][1]["outputs"][0].__setitem__("ros_topic", "scan"),
            "declared more than once",
        ),
        (lambda m: m["drive"]["joint_states"].pop("rate_hz"), "gz_rate needs a rate_hz"),
        (lambda m: m["drive"]["odometry"].__setitem__("rate_hz", -5), "odometry.rate_hz"),
        (lambda m: m["frames"].__setitem__("extra", 1), "frames.extra: Extra inputs"),
        (lambda m: m["sensors"][0].__setitem__("noise", {"range_stddev_m": -1}), "noise"),
    ],
)
def test_model_errors(mutate: Any, message: str) -> None:
    data = yaml.safe_load(UGV_MANIFEST.read_text())
    mutate(data)
    with pytest.raises(ManifestError, match=message):
        validate_data(data)


def test_incomplete_manifest(tmp_path: Path) -> None:
    partial = tmp_path / "robot.yaml"
    partial.write_text(
        "apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nmetadata: {name: shell}\n"
    )
    with pytest.raises(ManifestError, match="missing description, frames, drive"):
        load_manifest(partial)
    manifest = load_manifest(partial, complete=False)
    assert manifest.name == "shell"
    assert manifest.interfaces() == {}


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="no such file"):
        load_manifest(tmp_path / "missing.yaml")
    bad = tmp_path / "robot.yaml"
    bad.write_text("apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: robot.yaml\n")
    with pytest.raises(ManifestError, match="extends cycle"):
        load_manifest(bad)


def test_json_schema_is_committed_and_current() -> None:
    schema = json_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["title"].startswith("Fieldproof vehicle manifest")
    assert set(schema["required"]) == {"apiVersion", "kind", "metadata"}
    assert "Runtime" in schema["$defs"]
    assert "frames" in schema["properties"], "sections stay optional before extends is resolved"
    assert SCHEMA_FILE.is_file(), "run `make manifest-schema`"
    assert json.loads(SCHEMA_FILE.read_text()) == schema, "stale: run `make manifest-schema`"


# -------------------------------------------------------------------------------------- the CLI
def test_cli_validate_both_manifests() -> None:
    result = runner.invoke(app, ["manifest", "validate", str(UGV_MANIFEST), str(NAV2_MANIFEST)])
    assert result.exit_code == 0, result.output
    output = " ".join(ANSI.sub("", result.output).split())
    assert "ugv_ref (4 sensors, 11 interfaces)" in output
    assert "ugv_nav2 (4 sensors, 11 interfaces, runtime T1 image" in output


def test_cli_validate_reports_errors(tmp_path: Path) -> None:
    bad = tmp_path / "robot.yaml"
    bad.write_text("apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nmetadata: {name: bad}\n")
    result = runner.invoke(app, ["manifest", "validate", str(bad), str(UGV_MANIFEST)])
    assert result.exit_code == 2, result.output
    assert "missing description" in result.output
    assert "ugv_ref" in result.output, "the other manifest is still reported"
    result = runner.invoke(app, ["manifest", "validate", "--partial", str(bad)])
    assert result.exit_code == 0, result.output


def test_cli_schema() -> None:
    result = runner.invoke(app, ["manifest", "schema"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == json_schema()
