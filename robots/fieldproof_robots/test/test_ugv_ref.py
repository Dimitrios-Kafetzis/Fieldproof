"""Tests for the reference UGV model (task P1-T01) on the installed layout.

The files are installed, the generated SDF and URDF agree with the manifest (frames, drive
parameters, sensors, topics, rates), every sensor declares a noise model, the mesh URIs resolve,
the xacro defaults equal the manifest, the GNSS receiver is optional, and the chassis
carries the ground-truth contact sensor the scenario runner reads (not an interface).
"""

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from ament_index_python.packages import get_package_share_directory

Manifest = dict[str, Any]

SHARE = Path(get_package_share_directory("fieldproof_robots"))
MODEL = SHARE / "models" / "ugv_ref"
GENERATOR = SHARE.parent.parent / "lib" / "fieldproof_robots" / "generate_model.py"
XACRO_NS = "{http://www.ros.org/wiki/xacro}"


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    data: Manifest = yaml.safe_load((MODEL / "robot.yaml").read_text())
    return data


@pytest.fixture(scope="module")
def sdf_model() -> ET.Element:
    return load_sdf_model(MODEL / "model.sdf")


@pytest.fixture(scope="module")
def urdf() -> ET.Element:
    return ET.parse(MODEL / "ugv_ref.urdf").getroot()  # noqa: S314


def load_sdf_model(path: Path) -> ET.Element:
    model = ET.parse(path).getroot().find("model")  # noqa: S314
    assert model is not None, f"{path} holds no <model>"
    return model


def child(element: ET.Element, path: str) -> ET.Element:
    found = element.find(path)
    assert found is not None, f"missing <{path}> in <{element.tag}>"
    return found


def text(element: ET.Element, path: str) -> str:
    value = element.findtext(path)
    assert value is not None, f"missing <{path}> in <{element.tag}>"
    return value.strip()


def attr(element: ET.Element, path: str, name: str) -> str:
    value = child(element, path).get(name)
    assert value is not None, f"missing {name} on <{path}> in <{element.tag}>"
    return value


def plugin(sdf_model: ET.Element, name: str) -> ET.Element:
    found = [p for p in sdf_model.findall("plugin") if p.get("name") == name]
    assert len(found) == 1, f"expected one {name} plugin, found {len(found)}"
    return found[0]


def sensors(sdf_model: ET.Element) -> dict[str, ET.Element]:
    """The interface sensors of the model: the ground-truth contact sensor is not one of them."""
    return {s.get("name", ""): s for s in sdf_model.iter("sensor") if s.get("type") != "contact"}


def contact_sensors(sdf_model: ET.Element) -> dict[str, ET.Element]:
    return {s.get("name", ""): s for s in sdf_model.iter("sensor") if s.get("type") == "contact"}


def link(sdf_model: ET.Element, name: str) -> ET.Element:
    found = [x for x in sdf_model.findall("link") if x.get("name") == name]
    assert len(found) == 1, f"expected one link {name}, found {len(found)}"
    return found[0]


# ----------------------------------------------------------------------------- installed files
def test_model_files_are_installed() -> None:
    for relative in (
        "model.config",
        "model.sdf",
        "ugv_ref.urdf",
        "robot.yaml",
        "urdf/ugv_ref.urdf.xacro",
        "urdf/inertials.xacro",
        "urdf/sensors.xacro",
        "meshes/chassis.obj",
        "meshes/chassis.mtl",
        "meshes/LICENSE.txt",
    ):
        assert (MODEL / relative).is_file(), relative
    assert (SHARE / "launch" / "spawn_ugv_ref.launch.py").is_file()
    assert GENERATOR.is_file()


def test_model_config_names_the_generated_sdf(manifest: Manifest) -> None:
    config = ET.parse(MODEL / "model.config").getroot()  # noqa: S314
    assert text(config, "name") == manifest["metadata"]["name"]
    assert text(config, "sdf") == manifest["description"]["sdf"]
    assert attr(config, "sdf", "version") == "1.11"


def test_mesh_uris_resolve_inside_the_model(sdf_model: ET.Element) -> None:
    uris = [u.text.strip() for u in sdf_model.iter("uri") if u.text]
    assert uris, "the model references no mesh"
    for uri in uris:
        assert uri.startswith("model://ugv_ref/"), uri
        assert (MODEL / uri.removeprefix("model://ugv_ref/")).is_file(), uri


def test_mesh_licence_covers_every_mesh() -> None:
    licence = (MODEL / "meshes" / "LICENSE.txt").read_text()
    for mesh in (MODEL / "meshes").glob("*.obj"):
        assert mesh.name in licence, f"{mesh.name} is not listed in meshes/LICENSE.txt"
    assert "Apache-2.0" in licence


# ----------------------------------------------------------------------------- structure
def test_sdf_model_name_and_wheel_joints(manifest: Manifest, sdf_model: ET.Element) -> None:
    assert sdf_model.get("name") == manifest["metadata"]["name"]
    assert manifest["frames"]["base"] in {x.get("name") for x in sdf_model.findall("link")}
    joints = {j.get("name"): j for j in sdf_model.findall("joint")}
    for name in manifest["drive"]["joints"]:
        assert name in joints, name
        assert joints[name].get("type") == "revolute"
        assert text(joints[name], "axis/xyz").split() == ["0", "1", "0"]
        assert text(joints[name], "parent") == manifest["frames"]["base"]
        upper = joints[name].findtext("axis/limit/upper")
        assert upper is None or float(upper) > 1e6, "drive wheels must turn freely"


def test_urdf_has_every_frame_of_the_manifest(manifest: Manifest, urdf: ET.Element) -> None:
    links = {x.get("name") for x in urdf.findall("link")}
    frames = {manifest["frames"]["base"], manifest["frames"]["footprint"]}
    for sensor in manifest["sensors"]:
        frames.add(sensor["frame_id"])
        frames.update(output["ros_frame_id"] for output in sensor["outputs"])
    frames.update(joint.replace("_joint", "_link") for joint in manifest["drive"]["joints"])
    missing = frames - links
    assert not missing, f"frames missing from the URDF: {sorted(missing)}"
    assert urdf.get("name") == manifest["metadata"]["name"]


def test_urdf_sensor_frames_are_fixed_to_the_base(manifest: Manifest, urdf: ET.Element) -> None:
    joints = {attr(j, "child", "link"): j for j in urdf.findall("joint")}
    for sensor in manifest["sensors"]:
        joint = joints[sensor["frame_id"]]
        assert joint.get("type") == "fixed", sensor["frame_id"]
        assert attr(joint, "parent", "link") == manifest["frames"]["base"]
    optical = joints["camera_optical_link"]
    assert optical.get("type") == "fixed"
    assert attr(optical, "parent", "link") == "camera_link"
    # The optical frame follows REP 103: z forward, x right, y down (rpy -pi/2 0 -pi/2).
    rpy = [float(v) for v in attr(optical, "origin", "rpy").split()]
    assert rpy == pytest.approx([-1.5707963, 0.0, -1.5707963], abs=1e-6)


# ----------------------------------------------------------------------------- drive systems
def test_diff_drive_plugin_matches_the_manifest(manifest: Manifest, sdf_model: ET.Element) -> None:
    drive = manifest["drive"]
    odometry, limits = drive["odometry"], drive["limits"]
    diff_drive = plugin(sdf_model, "gz::sim::systems::DiffDrive")
    assert diff_drive.get("filename") == "gz-sim-diff-drive-system"
    assert text(diff_drive, "left_joint") == drive["joints"][0]
    assert text(diff_drive, "right_joint") == drive["joints"][1]
    assert float(text(diff_drive, "wheel_radius")) == pytest.approx(drive["wheel_radius_m"])
    assert float(text(diff_drive, "wheel_separation")) == pytest.approx(drive["wheel_separation_m"])
    assert text(diff_drive, "topic") == "/" + drive["command"]["gz_topic"]
    assert text(diff_drive, "odom_topic") == "/" + odometry["gz_topic"]
    assert text(diff_drive, "tf_topic") == "/" + drive["tf"]["gz_topic"]
    assert text(diff_drive, "frame_id") == odometry["frame_id"]
    assert text(diff_drive, "child_frame_id") == odometry["child_frame_id"]
    assert float(text(diff_drive, "odom_publish_frequency")) == odometry["rate_hz"]
    expected_limits = {
        "max_linear_velocity": limits["max_linear_velocity_mps"],
        "max_angular_velocity": limits["max_angular_velocity_radps"],
        "max_linear_acceleration": limits["max_linear_acceleration_mps2"],
        "max_angular_acceleration": limits["max_angular_acceleration_radps2"],
    }
    for element, value in expected_limits.items():
        assert float(text(diff_drive, element)) == value, element
        assert float(text(diff_drive, element.replace("max_", "min_"))) == -value, element


def test_wheel_geometry_matches_the_manifest(manifest: Manifest, sdf_model: ET.Element) -> None:
    drive = manifest["drive"]
    for name in drive["joints"]:
        wheel = link(sdf_model, name.replace("_joint", "_link"))
        radius = float(text(wheel, "collision/geometry/cylinder/radius"))
        assert radius == pytest.approx(drive["wheel_radius_m"])
    poses = {
        j.get("name"): [float(v) for v in text(j, "pose").split()[:3]]
        for j in sdf_model.findall("joint")
        if j.get("name") in drive["joints"]
    }
    separation = abs(poses[drive["joints"][0]][1] - poses[drive["joints"][1]][1])
    assert separation == pytest.approx(drive["wheel_separation_m"])


def test_joint_state_publisher_matches_the_manifest(
    manifest: Manifest, sdf_model: ET.Element
) -> None:
    jsp = plugin(sdf_model, "gz::sim::systems::JointStatePublisher")
    assert text(jsp, "topic") == "/" + manifest["drive"]["joint_states"]["gz_topic"]
    assert [j.text for j in jsp.findall("joint_name")] == manifest["drive"]["joints"]


# ----------------------------------------------------------------------------- sensors
def test_sensors_match_the_manifest(manifest: Manifest, sdf_model: ET.Element) -> None:
    found = sensors(sdf_model)
    declared = {s["name"]: s for s in manifest["sensors"]}
    assert set(found) == set(declared)
    for name, sensor in declared.items():
        element = found[name]
        assert element.get("type") == sensor["type"], name
        assert float(text(element, "update_rate")) == sensor["rate_hz"], name
        assert text(element, "topic") == "/" + sensor["gz_topic"], name
        assert text(element, "gz_frame_id") == sensor["frame_id"], name
        assert text(element, "always_on") in {"1", "true"}, name
        for output in sensor["outputs"]:
            assert output["gz_topic"].startswith(sensor["gz_topic"]), (name, output["gz_topic"])


def test_every_sensor_declares_noise(sdf_model: ET.Element) -> None:
    for name, element in sensors(sdf_model).items():
        noise = list(element.iter("noise"))
        assert noise, f"sensor {name} has no <noise>"
        for n in noise:
            assert (n.get("type") or n.findtext("type")) == "gaussian", name
            assert float(text(n, "stddev")) > 0, name


def test_chassis_contact_sensor_reports_ground_truth(sdf_model: ET.Element) -> None:
    """The contact sensor (task P1-T06) watches the chassis collision only and is not bridged."""
    ((name, element),) = contact_sensors(sdf_model).items()
    assert name == "contact"
    assert text(element, "topic") == "/contacts"
    assert text(element, "always_on") in {"1", "true"}
    collisions = [c.text.strip() for c in child(element, "contact").findall("collision") if c.text]
    assert collisions == ["base_link_collision"], "the chassis collision only (not the casters)"
    base = link(sdf_model, "base_link")
    names = {c.get("name") for c in base.findall("collision")}
    assert "base_link_collision" in names
    assert len(names) > 1, "the casters are lumped into base_link and must stay out of the sensor"


def test_camera_resolution_matches_the_manifest(manifest: Manifest, sdf_model: ET.Element) -> None:
    camera = next(s for s in manifest["sensors"] if s["type"] == "rgbd_camera")
    element = sensors(sdf_model)[camera["name"]]
    width, height = camera["resolution"]
    assert int(text(element, "camera/image/width")) == width
    assert int(text(element, "camera/image/height")) == height
    fov = float(text(element, "camera/horizontal_fov"))
    assert fov == pytest.approx(camera["horizontal_fov_rad"])


def test_sensor_frames_sit_above_the_chassis(manifest: Manifest, sdf_model: ET.Element) -> None:
    """The lumped sensor poses stay where the URDF put them (roof, nose, centre), level."""
    found = sensors(sdf_model)
    roof = manifest["dimensions"]["height_m"] - manifest["drive"]["wheel_radius_m"]
    assert float(text(found["lidar"], "pose").split()[2]) > roof
    for name in ("lidar", "imu", "camera", "gps"):
        pose = [float(v) for v in text(found[name], "pose").split()]
        assert pose[3:] == pytest.approx([0.0, 0.0, 0.0], abs=1e-9), f"{name} must look forward"


# ----------------------------------------------------------------------------- xacro arguments
def test_xacro_defaults_equal_the_manifest(manifest: Manifest) -> None:
    root = ET.parse(MODEL / "urdf" / "ugv_ref.urdf.xacro").getroot()  # noqa: S314
    defaults = {a.get("name"): a.get("default") for a in root.findall(f"{XACRO_NS}arg")}
    expected = {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in manifest["description"]["xacro_args"].items()
    }
    assert defaults == expected


def test_gps_is_optional_and_topics_take_the_namespace(tmp_path: Path) -> None:
    subprocess.run(  # noqa: S603
        [
            str(GENERATOR),
            str(MODEL / "urdf" / "ugv_ref.urdf.xacro"),
            "--urdf",
            str(tmp_path / "ugv.urdf"),
            "--sdf",
            str(tmp_path / "model.sdf"),
            "gps:=false",
            "namespace:=ugv1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    model = load_sdf_model(tmp_path / "model.sdf")
    assert "gps" not in sensors(model)
    assert {"lidar", "imu", "camera"} <= set(sensors(model))
    assert text(plugin(model, "gz::sim::systems::DiffDrive"), "topic") == "/ugv1/cmd_vel"
    assert text(sensors(model)["lidar"], "topic") == "/ugv1/scan"
    assert text(contact_sensors(model)["contact"], "topic") == "/ugv1/contacts"
    urdf_links = ET.parse(tmp_path / "ugv.urdf").getroot().findall("link")  # noqa: S314
    assert "gps_link" not in {x.get("name") for x in urdf_links}
