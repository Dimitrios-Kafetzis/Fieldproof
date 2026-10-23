"""What a world file declares that scenarios and events refer to (task P1-T05).

A world's named frames are the poses vehicles spawn at and goals and targets refer to; its
camera sensors with a video recorder are what ``outputs.video`` records from. :func:`place`
turns a :class:`~fieldproof.dsl.Placement` into a :class:`Pose` of the world.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from fieldproof.dsl import Placement, ScenarioError

VIDEO_RECORDER_PLUGIN = "gz::sim::systems::CameraVideoRecorder"
# The spawn launch's default `z`: the reference UGV's base_link height above the ground, which
# a spawn and a teleport of a vehicle add to the ground pose.
SPAWN_HEIGHT_M = 0.12
# Gazebo's defaults when a world declares no `<physics>` (task P2-T04: a lockstep step is a
# multiple of the physics step, and `sim.step` counts physics steps).
DEFAULT_STEP_S = 0.001
DEFAULT_RTF = 1.0


@dataclass(frozen=True)
class Pose:
    """A pose in the world: position and heading (roll and pitch are not used)."""

    x: float
    y: float
    z: float
    yaw: float

    def relative(self, other: Pose) -> Pose:
        """`other` expressed in this pose's frame (a goal in the map frame of a spawn)."""
        dx, dy = other.x - self.x, other.y - self.y
        cos, sin = math.cos(self.yaw), math.sin(self.yaw)
        return Pose(
            cos * dx + sin * dy,
            -sin * dx + cos * dy,
            other.z - self.z,
            math.remainder(other.yaw - self.yaw, math.tau),
        )

    def distance_to(self, other: Pose) -> float:
        """The distance to another pose in the horizontal plane, in metres."""
        return math.hypot(other.x - self.x, other.y - self.y)


@dataclass(frozen=True)
class WorldInfo:
    """What a world file declares that scenarios refer to."""

    file: Path
    name: str
    frames: dict[str, Pose]
    cameras: dict[str, str] = field(default_factory=dict)
    """Camera sensors with a `CameraVideoRecorder` plugin: sensor name to its service."""
    step_s: float = DEFAULT_STEP_S
    """The physics step (`<physics><max_step_size>`), in seconds."""
    rtf: float = DEFAULT_RTF
    """The world's own real-time factor (`<physics><real_time_factor>`)."""


def read_world(file: Path) -> WorldInfo:
    """The world's name, its named frames and the cameras a video can be recorded from.

    Frames are `<frame name="..."><pose>x y z r p y</pose>`; cameras are the `<sensor
    type="camera">` elements carrying a `CameraVideoRecorder` plugin (`outputs.video`); the
    physics step and real-time factor come from `<physics>` (Gazebo's defaults without one).
    """
    try:
        root = ET.parse(file).getroot()  # noqa: S314  (a world file of the workspace)
    except (OSError, ET.ParseError) as error:
        raise ScenarioError(f"{file}: cannot read the world file: {error}") from error
    world = root.find("world")
    if world is None or not world.get("name"):
        raise ScenarioError(f"{file}: no <world name=...> element")
    frames: dict[str, Pose] = {}
    for frame in world.findall("frame"):
        name = frame.get("name")
        values = (frame.findtext("pose") or "0 0 0 0 0 0").split()
        if name and len(values) == 6:
            x, y, z, _roll, _pitch, yaw = (float(v) for v in values)
            frames[name] = Pose(x, y, z, yaw)
    cameras: dict[str, str] = {}
    for sensor in world.iter("sensor"):
        if sensor.get("type") != "camera" or not sensor.get("name"):
            continue
        for plugin in sensor.findall("plugin"):
            if plugin.get("name") == VIDEO_RECORDER_PLUGIN and plugin.findtext("service"):
                cameras[str(sensor.get("name"))] = str(plugin.findtext("service")).strip()
    physics = world.find("physics")
    step_s, rtf = DEFAULT_STEP_S, DEFAULT_RTF
    if physics is not None:
        try:
            step_s = float(physics.findtext("max_step_size") or DEFAULT_STEP_S)
            rtf = float(physics.findtext("real_time_factor") or DEFAULT_RTF)
        except ValueError as error:
            raise ScenarioError(f"{file}: cannot read the <physics> element: {error}") from error
        if step_s <= 0:
            raise ScenarioError(f"{file}: <physics><max_step_size> must be positive")
    return WorldInfo(file, str(world.get("name")), frames, cameras, step_s, rtf)


def place(placement: Placement, world: WorldInfo, what: str) -> Pose:
    """The world pose of a placement (`z` and `yaw` default to 0 with coordinates)."""
    if placement.frame is not None:
        if placement.frame not in world.frames:
            known = ", ".join(sorted(world.frames)) or "none"
            raise ScenarioError(
                f"{what}: world {world.name} has no frame {placement.frame!r} ({known})"
            )
        return world.frames[placement.frame]
    if placement.x is None or placement.y is None:  # the model validator rules this out
        raise ScenarioError(f"{what}: a placement needs a frame or x and y")
    return Pose(placement.x, placement.y, placement.z or 0.0, placement.yaw or 0.0)
