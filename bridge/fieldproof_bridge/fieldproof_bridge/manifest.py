"""The vehicle manifest (``robot.yaml``) as the bridge reads it.

The manifest is the contract between a vehicle model and the rest of the stack (task P1-T01 wrote
the first one, ``robots/fieldproof_robots/models/ugv_ref/robot.yaml``; P1-T04 formalises the format
as manifest schema v0). The bridge needs the interfaces: for every topic the vehicle offers, the
Gazebo and ROS names and types, the direction, the frame its messages carry, the rate and the QoS
profile. :class:`Manifest` validates those sections and exposes them as :class:`Interface` values;
everything else in the file passes through untouched in :attr:`Manifest.data`.

A manifest may ``extends`` another one (a path relative to its own directory): the vehicle
manifest of a stack then inherits the model sections of the reference vehicle it runs on and adds
its own (task P1-T04 adds ``runtime``). :func:`read_manifest_data` resolves the chain before any
validation, so the bridge and the engine read the same merged document.

Plain Python (PyYAML and the standard library only), so the engine can run it on the host.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

API_VERSION = "fieldproof.dev/v0alpha1"
KIND = "Robot"
GZ_TO_ROS = "gz_to_ros"
ROS_TO_GZ = "ros_to_gz"
RELIABILITIES = ("reliable", "best_effort")
DURABILITIES = ("volatile", "transient_local")
# The drive interfaces the bridge requires, with their direction.
DRIVE_INTERFACES = {
    "command": ROS_TO_GZ,
    "odometry": GZ_TO_ROS,
    "joint_states": GZ_TO_ROS,
    "tf": GZ_TO_ROS,
}


# Keys of the `description` section that hold paths relative to the manifest declaring them; an
# inherited description keeps pointing at the parent's files.
DESCRIPTION_PATH_KEYS = ("xacro", "urdf", "sdf", "meshes")


BATTERY_TYPE = "sensor_msgs/msg/BatteryState"


class ManifestError(ValueError):
    """The manifest is missing something the bridge needs, or a value is out of range."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` updated by ``override``: mappings merge key by key, anything else replaces.

    Lists replace as a whole (a child manifest that lists ``sensors`` declares all of them).
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(file: Path) -> Any:
    try:
        return yaml.safe_load(file.read_text())
    except FileNotFoundError as error:
        raise ManifestError(f"{file}: no such file") from error
    except yaml.YAMLError as error:
        raise ManifestError(f"{file}: not valid YAML ({error})") from error


def read_manifest_data(path: Path | str, _chain: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Read the manifest at ``path`` with its ``extends`` chain resolved.

    The parent named by ``extends`` (a path relative to the manifest's directory) is read first,
    recursively; the child's sections are merged over it with :func:`deep_merge`. Relative paths
    of the parent's ``description`` section are rewritten to be relative to the child's
    directory, so the merged document still resolves them. The ``extends`` key itself is removed.
    """
    file = Path(path).resolve()
    if file in _chain:
        cycle = " -> ".join(str(p) for p in (*_chain, file))
        raise ManifestError(f"extends cycle: {cycle}")
    data = _read_yaml(file)
    if not isinstance(data, dict):
        raise ManifestError(f"{file}: the manifest must be a mapping")
    parent = data.pop("extends", None)
    if parent is None:
        return data
    if not isinstance(parent, str) or not parent.strip():
        raise ManifestError(f"{file}: extends must be a relative path")
    parent_file = (file.parent / parent).resolve()
    parent_data = read_manifest_data(parent_file, (*_chain, file))
    description = parent_data.get("description")
    if isinstance(description, dict):
        for key in DESCRIPTION_PATH_KEYS:
            value = description.get(key)
            if isinstance(value, str) and value and not Path(value).is_absolute():
                description[key] = os.path.relpath(parent_file.parent / value, file.parent)
    return deep_merge(parent_data, data)


@dataclass(frozen=True)
class Qos:
    """The QoS profile a ROS topic is published (or subscribed) with."""

    reliability: str = "reliable"
    durability: str = "volatile"
    depth: int = 10

    def as_dict(self) -> dict[str, Any]:
        """Return the profile as the plain mapping the generated files carry."""
        return {"reliability": self.reliability, "durability": self.durability, "depth": self.depth}

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> Qos:
        """Build a profile from a manifest ``qos`` block, filling the defaults."""
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ManifestError(f"{where}: qos must be a mapping")
        unknown = set(raw) - {"reliability", "durability", "depth"}
        if unknown:
            raise ManifestError(f"{where}: unknown qos keys {sorted(unknown)}")
        qos = cls(
            reliability=str(raw.get("reliability", cls.reliability)),
            durability=str(raw.get("durability", cls.durability)),
            depth=int(raw.get("depth", cls.depth)),
        )
        if qos.reliability not in RELIABILITIES:
            raise ManifestError(f"{where}: qos.reliability must be one of {RELIABILITIES}")
        if qos.durability not in DURABILITIES:
            raise ManifestError(f"{where}: qos.durability must be one of {DURABILITIES}")
        if qos.depth < 1:
            raise ManifestError(f"{where}: qos.depth must be at least 1")
        return qos


@dataclass(frozen=True)
class Interface:
    """One topic of the vehicle's interface, as declared in the manifest.

    Topic names are as written in the manifest: relative to the vehicle namespace unless they
    start with a slash. ``frame_id`` is the frame the ROS messages must carry (``None`` when the
    type has no header worth checking, such as the drive command); ``gz_frame_id`` is the frame
    Gazebo puts in the header (empty when it puts none). ``gz_rate`` is set when Gazebo publishes
    at a rate other than ``rate_hz`` (the string ``physics`` for every simulation step).
    """

    key: str
    gz_topic: str
    gz_type: str
    ros_topic: str
    ros_type: str
    direction: str
    frame_id: str | None = None
    gz_frame_id: str | None = None
    child_frame_id: str | None = None
    rate_hz: float | None = None
    gz_rate: str | None = None
    qos: Qos = field(default_factory=Qos)
    sensor: str | None = None
    sensor_type: str | None = None
    noise: dict[str, float] = field(default_factory=dict)
    optional: bool = False

    @property
    def relabelled(self) -> bool:
        """Whether the ROS messages need a frame other than the one Gazebo sets."""
        return self.frame_id is not None and self.frame_id != self.gz_frame_id

    @property
    def throttled(self) -> bool:
        """Whether Gazebo publishes faster than the declared rate."""
        return self.gz_rate is not None and self.rate_hz is not None


@dataclass(frozen=True)
class Battery:
    """The vehicle's battery as the bridge reports it (``power.battery``, task P2-T03).

    The reference vehicles simulate no power system: the bridge's battery node publishes the
    state from these figures, full until a scenario drains it.
    """

    ros_topic: str
    ros_type: str
    rate_hz: float
    full_voltage_v: float
    empty_voltage_v: float
    capacity_ah: float
    frame_id: str = ""
    qos: Qos = field(default_factory=Qos)


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict):
        raise ManifestError(f"{where}: expected a mapping")
    if key not in mapping or mapping[key] is None:
        raise ManifestError(f"{where}: missing {key}")
    return mapping[key]


def _text(mapping: Any, key: str, where: str) -> str:
    value = _require(mapping, key, where)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where}.{key}: must be a non-empty string")
    return value.strip()


def _number(mapping: Any, key: str, where: str) -> float:
    value = _require(mapping, key, where)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ManifestError(f"{where}.{key}: must be a positive number")
    return float(value)


def _noise(raw: Any, where: str) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}.noise: must be a mapping")
    noise: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ManifestError(f"{where}.noise.{key}: must be a non-negative number")
        noise[str(key)] = float(value)
    return noise


class Manifest:
    """A validated vehicle manifest."""

    def __init__(self, data: dict[str, Any], path: Path | None = None) -> None:
        """Validate ``data`` (the parsed YAML); ``path`` resolves the description files."""
        self.data = data
        self.path = path
        if not isinstance(data, dict):
            raise ManifestError("the manifest must be a mapping")
        if data.get("apiVersion") != API_VERSION:
            raise ManifestError(f"apiVersion must be {API_VERSION}, got {data.get('apiVersion')!r}")
        if data.get("kind") != KIND:
            raise ManifestError(f"kind must be {KIND}, got {data.get('kind')!r}")
        self.name = _text(_require(data, "metadata", "manifest"), "name", "metadata")
        frames = _require(data, "frames", "manifest")
        self.base_frame = _text(frames, "base", "frames")
        self.footprint_frame = str(frames.get("footprint", "")).strip() or None
        self.odom_frame = _text(frames, "odom", "frames")
        description = _require(data, "description", "manifest")
        self.xacro = _text(description, "xacro", "description")
        xacro_args = description.get("xacro_args") or {}
        if not isinstance(xacro_args, dict):
            raise ManifestError("description.xacro_args: must be a mapping")
        self.xacro_args: dict[str, Any] = dict(xacro_args)
        self._interfaces = self._read_drive(_require(data, "drive", "manifest"))
        self._interfaces += self._read_sensors(data.get("sensors") or [])
        self.battery = self._read_battery(data.get("power"))
        names = [i.ros_topic for i in self._interfaces]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ManifestError(f"ROS topics declared more than once: {duplicates}")

    @classmethod
    def load(cls, path: Path | str) -> Manifest:
        """Read and validate the manifest at ``path`` (its ``extends`` chain resolved)."""
        file = Path(path).resolve()
        return cls(read_manifest_data(file), file)

    @property
    def interfaces(self) -> list[Interface]:
        """Every topic of the vehicle, drive interfaces first, then the sensors in order."""
        return list(self._interfaces)

    @property
    def sensors(self) -> list[dict[str, Any]]:
        """The ``sensors`` section as written."""
        return list(self.data.get("sensors") or [])

    @property
    def joints(self) -> list[str]:
        """The drive joints (``drive.joints``)."""
        return [str(j) for j in self.data["drive"].get("joints") or []]

    def xacro_path(self) -> Path:
        """The description's xacro file, resolved against the manifest's directory."""
        xacro = Path(self.xacro)
        if xacro.is_absolute() or self.path is None:
            return xacro
        return (self.path.parent / xacro).resolve()

    def _read_drive(self, drive: Any) -> list[Interface]:
        interfaces = []
        for key, direction in DRIVE_INTERFACES.items():
            raw = _require(drive, key, "drive")
            where = f"drive.{key}"
            frame_id: str | None = None
            gz_frame_id: str | None = None
            child_frame_id: str | None = None
            rate: float | None = None
            if key == "odometry":
                frame_id = gz_frame_id = _text(raw, "frame_id", where)
                child_frame_id = _text(raw, "child_frame_id", where)
                rate = _number(raw, "rate_hz", where)
            elif key == "tf":
                # The transform the drive system publishes: odometry frame -> base.
                odometry = _require(drive, "odometry", "drive")
                frame_id = gz_frame_id = _text(odometry, "frame_id", "drive.odometry")
                child_frame_id = _text(odometry, "child_frame_id", "drive.odometry")
                rate = _number(raw, "rate_hz", where) if "rate_hz" in raw else None
            elif key == "joint_states":
                rate = _number(raw, "rate_hz", where) if "rate_hz" in raw else None
            gz_rate = raw.get("gz_rate") if isinstance(raw, dict) else None
            if gz_rate is not None and rate is None:
                raise ManifestError(f"{where}: gz_rate needs a rate_hz to throttle to")
            interfaces.append(
                Interface(
                    key=where,
                    gz_topic=_text(raw, "gz_topic", where),
                    gz_type=_text(raw, "gz_type", where),
                    ros_topic=_text(raw, "ros_topic", where),
                    ros_type=_text(raw, "ros_type", where),
                    direction=direction,
                    frame_id=frame_id,
                    gz_frame_id=gz_frame_id,
                    child_frame_id=child_frame_id,
                    rate_hz=rate,
                    gz_rate=str(gz_rate) if gz_rate is not None else None,
                    qos=Qos.from_mapping(raw.get("qos"), where),
                )
            )
        return interfaces

    def _read_battery(self, power: Any) -> Battery | None:
        """The ``power.battery`` section (task P2-T03), if any."""
        if power is None:
            return None
        if not isinstance(power, dict):
            raise ManifestError("power: must be a mapping")
        raw = power.get("battery")
        if raw is None:
            return None
        where = "power.battery"
        if not isinstance(raw, dict):
            raise ManifestError(f"{where}: must be a mapping")
        ros_type = str(raw.get("ros_type", BATTERY_TYPE))
        if ros_type != BATTERY_TYPE:
            raise ManifestError(f"{where}.ros_type: must be {BATTERY_TYPE}")
        battery = Battery(
            ros_topic=str(raw.get("ros_topic", "battery/state")),
            ros_type=ros_type,
            rate_hz=float(raw.get("rate_hz", 1.0)),
            full_voltage_v=_number(raw, "full_voltage_v", where),
            empty_voltage_v=_number(raw, "empty_voltage_v", where),
            capacity_ah=_number(raw, "capacity_ah", where),
            frame_id=str(raw.get("frame_id", "")),
            qos=Qos.from_mapping(raw.get("qos"), where),
        )
        if battery.rate_hz <= 0:
            raise ManifestError(f"{where}.rate_hz: must be positive")
        if battery.empty_voltage_v >= battery.full_voltage_v:
            raise ManifestError(f"{where}: empty_voltage_v must be below full_voltage_v")
        if battery.ros_topic in {i.ros_topic for i in self._interfaces}:
            raise ManifestError(f"{where}.ros_topic: {battery.ros_topic!r} is an interface already")
        return battery

    def _read_sensors(self, sensors: Any) -> list[Interface]:
        if not isinstance(sensors, list):
            raise ManifestError("sensors: must be a list")
        interfaces = []
        for index, sensor in enumerate(sensors):
            where = f"sensors[{index}]"
            name = _text(sensor, "name", where)
            where = f"sensors.{name}"
            sensor_type = _text(sensor, "type", where)
            frame_id = _text(sensor, "frame_id", where)
            rate = _number(sensor, "rate_hz", where)
            noise = _noise(sensor.get("noise"), where)
            optional = bool(sensor.get("optional", False))
            outputs = _require(sensor, "outputs", where)
            if not isinstance(outputs, list) or not outputs:
                raise ManifestError(f"{where}.outputs: must be a non-empty list")
            for output in outputs:
                ros_topic = _text(output, "ros_topic", f"{where}.outputs")
                out_where = f"{where}.outputs[{ros_topic}]"
                has_gz_frame = bool(output.get("gz_header_frame_id", True))
                interfaces.append(
                    Interface(
                        key=out_where,
                        gz_topic=_text(output, "gz_topic", out_where),
                        gz_type=_text(output, "gz_type", out_where),
                        ros_topic=ros_topic,
                        ros_type=_text(output, "ros_type", out_where),
                        direction=GZ_TO_ROS,
                        frame_id=_text(output, "ros_frame_id", out_where),
                        gz_frame_id=frame_id if has_gz_frame else "",
                        rate_hz=rate,
                        qos=Qos.from_mapping(output.get("qos"), out_where),
                        sensor=name,
                        sensor_type=sensor_type,
                        noise=noise,
                        optional=optional,
                    )
                )
        return interfaces
