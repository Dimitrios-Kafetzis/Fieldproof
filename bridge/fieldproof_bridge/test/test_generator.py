"""Unit tests for the manifest model and the bundle generator (task P1-T03).

Pure Python: the reference UGV manifest from the robots workspace of this checkout and small
inline manifests, no ROS.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from fieldproof_bridge import generator
from fieldproof_bridge.generator import GeneratorError, generate, plan, write_bundle
from fieldproof_bridge.manifest import GZ_TO_ROS, ROS_TO_GZ, Manifest, ManifestError, Qos

REPO = Path(__file__).resolve().parents[3]
UGV_MANIFEST = REPO / "robots/fieldproof_robots/models/ugv_ref/robot.yaml"
UGV_ROS_TOPICS = {
    "cmd_vel",
    "odom",
    "joint_states",
    "/tf",
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
    assert by_topic["/tf"].qos.depth == 100
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


# ------------------------------------------------------------------------------------ plan
def test_ugv_plan_root_namespace(ugv: Manifest) -> None:
    bundle = plan(ugv)
    assert bundle.namespace == ""
    assert [n.name for n in bundle.nodes] == [
        "ros_gz_bridge",
        "ros_gz_bridge_camera_optical_link",
        "joint_states_throttle",
        "imu_adapter",
        "gps_adapter",
        "robot_state_publisher",
    ]
    main = bundle.node("ros_gz_bridge")
    entries = {e.ros_topic: e for e in main.entries}
    assert entries["/cmd_vel"].direction == "ROS_TO_GZ"
    assert entries["/odom"].gz_topic == "/odom"
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
        "/_bridge/joint_states",
        "/_bridge/imu/data",
        "/_bridge/gps/fix",
    ]


def test_ugv_plan_qos_overrides(ugv: Manifest) -> None:
    bundle = plan(ugv)
    main = bundle.node("ros_gz_bridge").params
    assert main["qos_overrides./scan.publisher.reliability"] == "reliable"
    assert main["qos_overrides./tf.publisher.depth"] == 100
    assert main["qos_overrides./camera/points.publisher.reliability"] == "reliable"
    assert main["qos_overrides./camera/points.publisher.depth"] == 5
    assert main["qos_overrides./cmd_vel.subscription.depth"] == 10
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
        "/clock",
        "/tf_static",
        "/robot_description",
    }
    assert topics["/cmd_vel"].role == "subscriber"
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
    assert entries["/ugv1/scan"].gz_topic == "/ugv1/scan"
    assert entries["/tf"].gz_topic == "/ugv1/tf", "the manifest's /tf stays global"
    assert entries["/clock"].gz_topic == "/clock"
    assert entries["/ugv1/_bridge/imu/data"].gz_topic == "/ugv1/imu"
    topics = {t.name: t for t in bundle.topics}
    assert topics["/ugv1/scan"].frame_id == "ugv1_lidar_link"
    assert topics["/ugv1/odom"].child_frame_id == "ugv1_base_link"
    assert topics["/tf"].frames == [{"parent": "ugv1_odom", "child": "ugv1_base_link"}]
    assert bundle.node("ros_gz_bridge_ugv1_camera_optical_link").params["override_frame_id"] == (
        "ugv1_camera_optical_link"
    )
    assert bundle.node("gps_adapter").params["frame_id"] == "ugv1_gps_link"
    assert bundle.description is not None
    assert bundle.description["args"]["namespace"] == "ugv1"
    assert bundle.description["args"]["frame_prefix"] == "ugv1_"
    assert topics["/ugv1/robot_description"].role == "publisher"


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
    assert [n.name for n in bundle.nodes] == ["ros_gz_bridge"]
    assert bundle.internal_topics == []
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
        "/ugv1/joint_states_throttle",
        "/ugv1/imu_adapter",
        "/ugv1/gps_adapter",
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
    assert bridge["description"]["xacro"] == str(ugv.xacro_path())
    contract = yaml.safe_load((out / "contract.yaml").read_text())
    assert contract["kind"] == "BridgeContract"
    assert {t["name"] for t in contract["topics"]} >= {"/ugv1/scan", "/tf", "/clock"}
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
