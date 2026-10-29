"""Unit tests for the manifest model and the bundle generator (task P1-T03).

Pure Python: the reference UGV manifest from the robots workspace of this checkout and small
inline manifests, no ROS.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from fieldproof_bridge import generator
from fieldproof_bridge.generator import GeneratorError, generate, plan, write_bundle
from fieldproof_bridge.manifest import (
    GZ_TO_ROS,
    ROS_TO_GZ,
    Manifest,
    ManifestError,
    Qos,
    deep_merge,
)

REPO = Path(__file__).resolve().parents[3]
UGV_MANIFEST = REPO / "robots/fieldproof_robots/models/ugv_ref/robot.yaml"
UGV_ROS_TOPICS = {
    "cmd_vel",
    "odom",
    "joint_states",
    "tf",
    "scan",
    "imu/data",
    "camera/image",
    "camera/camera_info",
    "camera/depth/image",
    "camera/points",
    "gps/fix",
}


@pytest.fixture(scope="module")
def ugv() -> Manifest:
    return Manifest.load(UGV_MANIFEST)


@pytest.fixture
def minimal() -> dict[str, Any]:
    """The smallest manifest the bridge accepts: a drive and one plain sensor."""
    return {
        "apiVersion": "fieldproof.dev/v0alpha1",
        "kind": "Robot",
        "metadata": {"name": "bot"},
        "description": {"xacro": "urdf/bot.urdf.xacro", "xacro_args": {"namespace": ""}},
        "frames": {"base": "base_link", "footprint": "base_footprint", "odom": "odom"},
        "drive": {
            "command": {
                "gz_topic": "cmd_vel",
                "gz_type": "gz.msgs.Twist",
                "ros_topic": "cmd_vel",
                "ros_type": "geometry_msgs/msg/Twist",
            },
            "odometry": {
                "gz_topic": "odom",
                "gz_type": "gz.msgs.Odometry",
                "ros_topic": "odom",
                "ros_type": "nav_msgs/msg/Odometry",
                "rate_hz": 50,
                "frame_id": "odom",
                "child_frame_id": "base_link",
            },
            "joint_states": {
                "gz_topic": "joint_states",
                "gz_type": "gz.msgs.Model",
                "ros_topic": "joint_states",
                "ros_type": "sensor_msgs/msg/JointState",
            },
            "tf": {
                "gz_topic": "tf",
                "gz_type": "gz.msgs.Pose_V",
                "ros_topic": "/tf",
                "ros_type": "tf2_msgs/msg/TFMessage",
            },
        },
        "sensors": [
            {
                "name": "lidar",
                "type": "gpu_lidar",
                "frame_id": "lidar_link",
                "rate_hz": 10,
                "gz_topic": "scan",
                "outputs": [
                    {
                        "gz_topic": "scan",
                        "gz_type": "gz.msgs.LaserScan",
                        "ros_topic": "scan",
                        "ros_type": "sensor_msgs/msg/LaserScan",
                        "ros_frame_id": "lidar_link",
                    }
                ],
            }
        ],
    }


# -------------------------------------------------------------------------------- manifest
def test_ugv_manifest_interfaces(ugv: Manifest) -> None:
    assert ugv.name == "ugv_ref"
    assert {i.ros_topic for i in ugv.interfaces} == UGV_ROS_TOPICS
    command = next(i for i in ugv.interfaces if i.key == "drive.command")
    assert command.direction == ROS_TO_GZ
    assert all(i.direction == GZ_TO_ROS for i in ugv.interfaces if i is not command)
    assert ugv.xacro_path().is_file()
    assert ugv.xacro_args["gps"] is True


def test_ugv_manifest_relabels_and_throttles(ugv: Manifest) -> None:
    by_topic = {i.ros_topic: i for i in ugv.interfaces}
    assert by_topic["camera/image"].relabelled
    assert by_topic["camera/image"].gz_frame_id == "camera_link"
    assert by_topic["camera/image"].frame_id == "camera_optical_link"
    assert not by_topic["camera/points"].relabelled
    assert by_topic["gps/fix"].relabelled, "Gazebo sets no frame on the fix"
    assert by_topic["gps/fix"].gz_frame_id == ""
    assert by_topic["joint_states"].throttled
    assert by_topic["joint_states"].rate_hz == 50
    assert by_topic["camera/image"].qos == Qos("reliable", "volatile", 5)
    assert by_topic["tf"].qos.depth == 100
    assert by_topic["scan"].qos == Qos()


def test_minimal_manifest_loads(minimal: dict[str, Any]) -> None:
    manifest = Manifest(minimal)
    assert [i.key for i in manifest.interfaces] == [
        "drive.command",
        "drive.odometry",
        "drive.joint_states",
        "drive.tf",
        "sensors.lidar.outputs[scan]",
    ]
    assert manifest.xacro_path() == Path("urdf/bot.urdf.xacro")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m.__setitem__("apiVersion", "v9"), "apiVersion"),
        (lambda m: m.__setitem__("kind", "Drone"), "kind"),
        (lambda m: m["metadata"].pop("name"), "metadata: missing name"),
        (lambda m: m["drive"].pop("tf"), "drive: missing tf"),
        (lambda m: m["drive"]["odometry"].pop("rate_hz"), "drive.odometry: missing rate_hz"),
        (lambda m: m["drive"]["odometry"].__setitem__("rate_hz", -1), "positive number"),
        (lambda m: m["drive"]["joint_states"].__setitem__("gz_rate", "physics"), "needs a rate_hz"),
        (lambda m: m["sensors"][0]["outputs"][0].pop("ros_frame_id"), "missing ros_frame_id"),
        (lambda m: m["sensors"][0].__setitem__("outputs", []), "non-empty list"),
        (
            lambda m: m["sensors"][0]["outputs"][0].__setitem__("qos", {"reliability": "x"}),
            "reliability",
        ),
        (
            lambda m: m["sensors"][0]["outputs"][0].__setitem__("qos", {"bogus": 1}),
            "unknown qos keys",
        ),
        (
            lambda m: m["sensors"][0]["outputs"][0].__setitem__("ros_topic", "odom"),
            "more than once",
        ),
        (lambda m: m["sensors"][0].__setitem__("noise", {"range_stddev_m": -1}), "non-negative"),
    ],
)
def test_manifest_errors(minimal: dict[str, Any], mutate: Any, message: str) -> None:
    broken = copy.deepcopy(minimal)
    mutate(broken)
    with pytest.raises(ManifestError, match=message):
        Manifest(broken)


def test_manifest_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="no such file"):
        Manifest.load(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: [unclosed\n")
    with pytest.raises(ManifestError, match="not valid YAML"):
        Manifest.load(bad)


def test_manifest_extends_inherits_the_model(tmp_path: Path) -> None:
    """A vehicle manifest inherits the model sections of the one it extends (task P1-T04)."""
    child_dir = tmp_path / "vehicles" / "ugv_nav2"
    child_dir.mkdir(parents=True)
    rel = Path(os.path.relpath(UGV_MANIFEST, child_dir))
    child = child_dir / "robot.yaml"
    child.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "fieldproof.dev/v0alpha1",
                "kind": "Robot",
                "extends": str(rel),
                "metadata": {"name": "ugv_nav2", "description": "Nav2 on the reference UGV"},
                "drive": {"tf": {"qos": {"depth": 50}}},
                "runtime": {"image": "example:local"},
            }
        )
    )
    manifest = Manifest.load(child)
    parent = Manifest.load(UGV_MANIFEST)
    assert manifest.name == "ugv_nav2"
    assert manifest.data["metadata"]["domain"] == "ground"  # inherited key next to overridden ones
    assert "extends" not in manifest.data
    assert manifest.data["runtime"] == {"image": "example:local"}
    # Interfaces are the parent's, except where the child overrides a nested key.
    assert [i.ros_topic for i in manifest.interfaces] == [i.ros_topic for i in parent.interfaces]
    tf = next(i for i in manifest.interfaces if i.key == "drive.tf")
    assert tf.qos.depth == 50
    assert tf.gz_type == "gz.msgs.Pose_V"
    # The inherited description still points at the parent's files.
    assert manifest.xacro_path() == parent.xacro_path()
    assert manifest.xacro_path().is_file()
    assert manifest.data["description"]["meshes"].endswith("ugv_ref/meshes")
    child_plan, parent_plan = plan(manifest), plan(parent)
    assert child_plan.topics != parent_plan.topics, "the child's TF depth differs"
    assert [t.name for t in child_plan.topics] == [t.name for t in parent_plan.topics]
    assert child_plan.description == parent_plan.description


def test_manifest_extends_a_reference_model_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`extends: fieldproof:<model>` finds the reference model through the models' directory
    given to the reader or named by FIELDPROOF_ROBOTS, as in the bridge container (P2-T08)."""
    child = tmp_path / "robot.yaml"
    child.write_text(
        "apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: fieldproof:ugv_ref\n"
        "metadata: {name: bot}\n"
    )
    robots = UGV_MANIFEST.parents[1]
    manifest = Manifest.load(child, robots=robots)
    parent = Manifest.load(UGV_MANIFEST)
    assert [i.ros_topic for i in manifest.interfaces] == [i.ros_topic for i in parent.interfaces]
    assert manifest.xacro_path() == parent.xacro_path()
    monkeypatch.delenv("FIELDPROOF_ROBOTS", raising=False)
    with pytest.raises(ManifestError, match="needs the reference models' directory"):
        Manifest.load(child)
    monkeypatch.setenv("FIELDPROOF_ROBOTS", str(robots))
    assert Manifest.load(child).xacro_path() == parent.xacro_path()
    assert plan(Manifest.load(child)).description == plan(parent).description
    child.write_text(
        "apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: fieldproof:nope\n"
        "metadata: {name: bot}\n"
    )
    with pytest.raises(ManifestError, match="no reference model 'nope' under"):
        Manifest.load(child)


def test_manifest_extends_errors(tmp_path: Path) -> None:
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text("apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: b.yaml\n")
    b.write_text("apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: a.yaml\n")
    with pytest.raises(ManifestError, match="extends cycle"):
        Manifest.load(a)
    a.write_text("apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: missing.yaml\n")
    with pytest.raises(ManifestError, match="no such file"):
        Manifest.load(a)
    a.write_text("apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nextends: [b.yaml]\n")
    with pytest.raises(ManifestError, match="extends must be a relative path or fieldproof:"):
        Manifest.load(a)
    a.write_text("- not a mapping\n")
    with pytest.raises(ManifestError, match="must be a mapping"):
        Manifest.load(a)


def test_deep_merge() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 2, "c": {"k": "v"}}
    override = {"a": {"y": [3], "z": 0}, "c": "flat", "d": None}
    merged = deep_merge(base, override)
    assert merged == {"a": {"x": 1, "y": [3], "z": 0}, "b": 2, "c": "flat", "d": None}
    assert base["a"] == {"x": 1, "y": [1, 2]}, "the inputs are not modified"


# ------------------------------------------------------------------------------------ plan
def test_ugv_plan_root_namespace(ugv: Manifest) -> None:
    bundle = plan(ugv)
    assert bundle.namespace == ""
    assert [n.name for n in bundle.nodes] == [
        "ros_gz_bridge",
        "ros_gz_bridge_camera_optical_link",
        "cmd_vel_relay",
        "odom_relay",
        "joint_states_throttle",
        "scan_relay",
        "imu_adapter",
        "gps_adapter",
        "battery",
        "robot_state_publisher",
    ]
    main = bundle.node("ros_gz_bridge")
    entries = {e.ros_topic: e for e in main.entries}
    assert entries["/_bridge/cmd_vel"].direction == "ROS_TO_GZ", "the command relay in front"
    assert entries["/_bridge/cmd_vel"].gz_topic == "/cmd_vel"
    assert "/cmd_vel" not in entries, "the relay owns the contract topic"
    assert entries["/_bridge/odom"].gz_topic == "/odom"
    assert entries["/_bridge/scan"].gz_topic == "/scan"
    assert entries["/tf"].gz_topic == "/tf"
    assert entries["/tf"].gz_type == "gz.msgs.Pose_V"
    assert entries["/clock"].gz_topic == "/clock"
    assert entries["/_bridge/imu/data"].gz_topic == "/imu"
    assert entries["/_bridge/gps/fix"].gz_topic == "/gps/fix"
    assert entries["/_bridge/joint_states"].gz_topic == "/joint_states"
    assert "/imu/data" not in entries, "the adapter owns the contract topic"
    camera = bundle.node("ros_gz_bridge_camera_optical_link")
    assert camera.params["override_frame_id"] == "camera_optical_link"
    assert {e.ros_topic for e in camera.entries} == {
        "/camera/image",
        "/camera/camera_info",
        "/camera/depth/image",
    }
    assert entries["/camera/points"].direction == "GZ_TO_ROS", "points keep camera_link"
    assert bundle.internal_topics == [
        "/_bridge/cmd_vel",
        "/_bridge/odom",
        "/_bridge/joint_states",
        "/_bridge/scan",
        "/_bridge/imu/data",
        "/_bridge/gps/fix",
    ]
    # The fault injectors (task P2-T03): what each one serves, by sensor and by topic.
    injectors = {n.name: (n.kind, n.topic, n.sensor) for n in bundle.injectors()}
    assert injectors == {
        "cmd_vel_relay": ("command_relay", "/cmd_vel", "command"),
        "odom_relay": ("relay", "/odom", "odometry"),
        "joint_states_throttle": ("throttle", "/joint_states", "joint_states"),
        "scan_relay": ("relay", "/scan", "lidar"),
        "imu_adapter": ("imu_adapter", "/imu/data", "imu"),
        "gps_adapter": ("navsat_adapter", "/gps/fix", "gps"),
        "battery": ("battery", "/battery/state", "battery"),
    }
    assert bundle.injector("lidar") is bundle.node("scan_relay")
    assert bundle.injector("scan") is bundle.node("scan_relay")
    assert bundle.injector("/scan") is bundle.node("scan_relay")
    assert bundle.injector("camera") is None, "images stay on the C++ path"
    assert bundle.injector("tf") is None
    relay = bundle.node("scan_relay")
    assert relay.params["type"] == "sensor_msgs/msg/LaserScan"
    assert relay.params["input"] == "/_bridge/scan"
    assert relay.params["output"] == "/scan"
    command = bundle.node("cmd_vel_relay")
    assert command.params["input"] == "/cmd_vel"
    assert command.params["output"] == "/_bridge/cmd_vel"
    battery = bundle.node("battery")
    assert battery.params["output"] == "/battery/state"
    assert battery.params["full_voltage"] == 29.4
    assert battery.params["empty_voltage"] == 21.0
    assert battery.params["capacity_ah"] == 20.0
    assert battery.params["rate_hz"] == 1.0


def test_ugv_plan_qos_overrides(ugv: Manifest) -> None:
    bundle = plan(ugv)
    main = bundle.node("ros_gz_bridge").params
    assert "qos_overrides./scan.publisher.reliability" not in main, "the relay publishes /scan"
    assert bundle.node("scan_relay").params["qos_reliability"] == "reliable"
    assert main["qos_overrides./tf.publisher.depth"] == 100
    assert main["qos_overrides./camera/points.publisher.reliability"] == "reliable"
    assert main["qos_overrides./camera/points.publisher.depth"] == 5
    assert "qos_overrides./cmd_vel.subscription.depth" not in main, "the relay subscribes"
    assert bundle.node("cmd_vel_relay").params["qos_depth"] == 10
    camera = bundle.node("ros_gz_bridge_camera_optical_link").params
    assert camera["qos_overrides./camera/image.publisher.reliability"] == "reliable"
    assert camera["qos_overrides./camera/image.publisher.depth"] == 5


def test_ugv_plan_adapters(ugv: Manifest) -> None:
    bundle = plan(ugv)
    imu = bundle.node("imu_adapter")
    assert imu.kind == "imu_adapter"
    assert imu.params["input"] == "/_bridge/imu/data"
    assert imu.params["output"] == "/imu/data"
    assert imu.params["frame_id"] == "", "Gazebo already labels the IMU with imu_link"
    assert imu.params["angular_velocity_stddev"] == pytest.approx(0.009)
    assert imu.params["linear_acceleration_stddev"] == pytest.approx(0.021)
    gps = bundle.node("gps_adapter")
    assert gps.kind == "navsat_adapter"
    assert gps.params["frame_id"] == "gps_link"
    assert gps.params["horizontal_stddev"] == pytest.approx(1.0)
    assert gps.params["vertical_stddev"] == pytest.approx(2.0)
    assert gps.params["service"] == 1
    throttle = bundle.node("joint_states_throttle")
    assert throttle.params["type"] == "sensor_msgs/msg/JointState"
    assert throttle.params["rate_hz"] == 50.0
    assert throttle.params["qos_reliability"] == "reliable"


def test_ugv_plan_contract(ugv: Manifest) -> None:
    bundle = plan(ugv)
    topics = {t.name: t for t in bundle.topics}
    assert set(topics) == {
        "/cmd_vel",
        "/odom",
        "/joint_states",
        "/tf",
        "/scan",
        "/imu/data",
        "/camera/image",
        "/camera/camera_info",
        "/camera/depth/image",
        "/camera/points",
        "/gps/fix",
        "/battery/state",
        "/clock",
        "/tf_static",
        "/robot_description",
    }
    assert topics["/cmd_vel"].role == "subscriber"
    assert topics["/cmd_vel"].via == "cmd_vel_relay"
    assert topics["/scan"].via == "scan_relay"
    assert topics["/odom"].via == "odom_relay"
    assert topics["/battery/state"].via == "battery"
    assert topics["/battery/state"].type == "sensor_msgs/msg/BatteryState"
    assert topics["/battery/state"].rate_hz == 1
    assert topics["/scan"].frame_id == "lidar_link"
    assert topics["/scan"].rate_hz == 10
    assert topics["/odom"].child_frame_id == "base_link"
    assert topics["/camera/image"].frame_id == "camera_optical_link"
    assert topics["/camera/image"].via == "ros_gz_bridge_camera_optical_link"
    assert topics["/gps/fix"].frame_id == "gps_link"
    assert topics["/gps/fix"].via == "gps_adapter"
    assert topics["/imu/data"].via == "imu_adapter"
    assert topics["/joint_states"].rate_hz == 50
    assert topics["/tf"].frames == [{"parent": "odom", "child": "base_link"}]
    assert topics["/tf"].rate_hz == 50
    assert "robot_state_publisher" in topics["/tf"].via
    assert topics["/tf_static"].qos.durability == "transient_local"
    assert [f["child"] for f in topics["/tf_static"].frames or []] == [
        "base_footprint",
        "lidar_link",
        "imu_link",
        "camera_link",
        "camera_optical_link",
        "gps_link",
    ]
    assert topics["/clock"].rate_hz is None


def test_plan_namespace_and_frame_prefix(ugv: Manifest) -> None:
    bundle = plan(ugv, namespace="ugv1/", frame_prefix="ugv1_")
    assert bundle.namespace == "/ugv1"
    assert bundle.fully_qualified("imu_adapter") == "/ugv1/imu_adapter"
    entries = {e.ros_topic: e for e in bundle.node("ros_gz_bridge").entries}
    assert entries["/ugv1/_bridge/scan"].gz_topic == "/ugv1/scan"
    assert bundle.node("scan_relay").params["output"] == "/ugv1/scan"
    # Gazebo's header already carries the prefix (the xacro renders it), so no relabelling.
    assert bundle.node("scan_relay").params["frame_id"] == ""
    assert bundle.injector("scan") is bundle.node("scan_relay")
    assert bundle.injector("/ugv1/scan") is bundle.node("scan_relay")
    assert bundle.node("battery").params["output"] == "/ugv1/battery/state"
    assert entries["/ugv1/tf"].gz_topic == "/ugv1/tf", "the reference TF follows the namespace"
    assert "/tf" not in entries
    assert entries["/clock"].gz_topic == "/clock"
    assert entries["/ugv1/_bridge/imu/data"].gz_topic == "/ugv1/imu"
    topics = {t.name: t for t in bundle.topics}
    assert topics["/ugv1/scan"].frame_id == "ugv1_lidar_link"
    assert topics["/ugv1/odom"].child_frame_id == "ugv1_base_link"
    assert topics["/ugv1/tf"].frames == [{"parent": "ugv1_odom", "child": "ugv1_base_link"}]
    assert "robot_state_publisher" in topics["/ugv1/tf"].via
    assert topics["/ugv1/tf_static"].qos.durability == "transient_local"
    assert {"/tf", "/tf_static"}.isdisjoint(topics)
    assert bundle.tf == {"tf": "/ugv1/tf", "tf_static": "/ugv1/tf_static"}
    assert bundle.node("ros_gz_bridge_ugv1_camera_optical_link").params["override_frame_id"] == (
        "ugv1_camera_optical_link"
    )
    assert bundle.node("gps_adapter").params["frame_id"] == "ugv1_gps_link"
    assert bundle.description is not None
    assert bundle.description["args"]["namespace"] == "ugv1"
    assert bundle.description["args"]["frame_prefix"] == "ugv1_"
    assert topics["/ugv1/robot_description"].role == "publisher"


def test_plan_global_tf_convention(minimal: dict[str, Any], tmp_path: Path) -> None:
    """A manifest writing `/tf` keeps one shared tree: the TF topics stay global under a
    namespace, for a stack that tells its vehicles apart by frame prefix."""
    path = tmp_path / "robot.yaml"
    path.write_text(yaml.safe_dump(minimal))
    bundle = plan(Manifest.load(path), namespace="ugv1", frame_prefix="ugv1/")
    topics = {t.name: t for t in bundle.topics}
    assert bundle.tf == {"tf": "/tf", "tf_static": "/tf_static"}
    assert topics["/tf"].frames == [{"parent": "ugv1/odom", "child": "ugv1/base_link"}]
    assert "robot_state_publisher" in topics["/tf"].via
    assert topics["/tf_static"].via == "robot_state_publisher"
    assert "/ugv1/tf" not in topics


def test_plan_without_optional_sensor(ugv: Manifest) -> None:
    bundle = plan(ugv, without=("gps",))
    assert "gps_adapter" not in [n.name for n in bundle.nodes]
    assert "/gps/fix" not in {t.name for t in bundle.topics}
    assert bundle.description is not None
    assert bundle.description["args"]["gps"] is False
    frames = [
        f["child"] for f in next(t for t in bundle.topics if t.name == "/tf_static").frames or []
    ]
    assert "gps_link" not in frames
    with pytest.raises(GeneratorError, match="not optional"):
        plan(ugv, without=("lidar",))
    with pytest.raises(GeneratorError, match="no sensor called"):
        plan(ugv, without=("sonar",))


def test_plan_without_clock_and_tf(ugv: Manifest) -> None:
    bundle = plan(ugv, clock=False, robot_state_publisher=False)
    assert "/clock" not in {e.ros_topic for e in bundle.node("ros_gz_bridge").entries}
    names = {t.name for t in bundle.topics}
    assert "/clock" not in names
    assert "/tf_static" not in names
    assert "/robot_description" not in names
    assert bundle.description is None
    assert "robot_state_publisher" not in [n.name for n in bundle.nodes]
    assert next(t for t in bundle.topics if t.name == "/tf").via == "ros_gz_bridge"


def test_minimal_plan_needs_no_adapter(minimal: dict[str, Any]) -> None:
    bundle = plan(Manifest(minimal), clock=False, robot_state_publisher=False)
    assert [n.name for n in bundle.nodes] == [
        "ros_gz_bridge",
        "cmd_vel_relay",
        "odom_relay",
        "joint_states_relay",
        "scan_relay",
    ], "the fault injectors, and nothing else"
    assert bundle.internal_topics == [
        "/_bridge/cmd_vel",
        "/_bridge/odom",
        "/_bridge/joint_states",
        "/_bridge/scan",
    ]
    assert bundle.injector("battery") is None
    assert next(t for t in bundle.topics if t.name == "/joint_states").rate_hz is None


# ----------------------------------------------------------------------------------- bundle
def test_write_bundle_files(ugv: Manifest, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    written = write_bundle(plan(ugv, namespace="ugv1"), out)
    assert {p.name for p in written} == {
        "ros_gz_bridge.yaml",
        "ros_gz_bridge_camera_optical_link.yaml",
        "params.yaml",
        "bridge.yaml",
        "contract.yaml",
        "bridge.launch.py",
    }
    for path in written:
        assert path.read_text().splitlines()[0].startswith(("# Generated", '"""Generated'))
    config = yaml.safe_load((out / "ros_gz_bridge.yaml").read_text())
    assert all(
        set(entry)
        == {"ros_topic_name", "gz_topic_name", "ros_type_name", "gz_type_name", "direction", "lazy"}
        for entry in config
    )
    assert {e["direction"] for e in config} == {"GZ_TO_ROS", "ROS_TO_GZ"}
    params = yaml.safe_load((out / "params.yaml").read_text())
    assert set(params) == {
        "/ugv1/ros_gz_bridge",
        "/ugv1/ros_gz_bridge_camera_optical_link",
        "/ugv1/cmd_vel_relay",
        "/ugv1/odom_relay",
        "/ugv1/joint_states_throttle",
        "/ugv1/scan_relay",
        "/ugv1/imu_adapter",
        "/ugv1/gps_adapter",
        "/ugv1/battery",
    }
    assert params["/ugv1/gps_adapter"]["ros__parameters"]["frame_id"] == "gps_link"
    assert "config_file" not in params["/ugv1/ros_gz_bridge"]["ros__parameters"], "relocatable"
    bridge = yaml.safe_load((out / "bridge.yaml").read_text())
    assert bridge["kind"] == "BridgeBundle"
    assert bridge["namespace"] == "/ugv1"
    assert bridge["nodes"][0] == {
        "name": "ros_gz_bridge",
        "kind": "ros_gz_bridge",
        "config": "ros_gz_bridge.yaml",
    }
    assert {"name": "scan_relay", "kind": "relay", "topic": "/ugv1/scan", "sensor": "lidar"} in (
        bridge["nodes"]
    )
    assert bridge["description"]["xacro"] == str(ugv.xacro_path())
    contract = yaml.safe_load((out / "contract.yaml").read_text())
    assert contract["kind"] == "BridgeContract"
    assert {t["name"] for t in contract["topics"]} >= {"/ugv1/scan", "/ugv1/tf", "/clock"}
    assert bridge["tf"] == {"tf": "/ugv1/tf", "tf_static": "/ugv1/tf_static"}
    launch = (out / "bridge.launch.py").read_text()
    assert "fieldproof_bridge" in launch
    assert str(out) in launch
    compile(launch, "bridge.launch.py", "exec")


def test_generate_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    written = generate(UGV_MANIFEST, out, namespace="ugv2", without=("gps",))
    assert (out / "contract.yaml").is_file()
    assert len(written) == 6
    assert generator.main([str(UGV_MANIFEST), "-o", str(tmp_path / "cli"), "-n", "ugv3"]) == 0
    captured = capsys.readouterr()
    assert str(tmp_path / "cli" / "contract.yaml") in captured.out
    assert (tmp_path / "cli" / "ros_gz_bridge.yaml").is_file()
    assert generator.main([str(tmp_path / "missing.yaml"), "-o", str(tmp_path / "x")]) == 2
    assert "no such file" in capsys.readouterr().err
    assert generator.main([str(UGV_MANIFEST), "-o", str(tmp_path / "y"), "--without", "lidar"]) == 2
    assert "not optional" in capsys.readouterr().err


def test_cli_default_output_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert generator.main([str(UGV_MANIFEST), "--no-clock", "--no-robot-state-publisher"]) == 0
    assert (tmp_path / "ugv_ref_bridge" / "bridge.yaml").is_file()
    bridge = yaml.safe_load((tmp_path / "ugv_ref_bridge" / "bridge.yaml").read_text())
    assert bridge["clock"] is False
    assert bridge["description"] is None
