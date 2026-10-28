"""The built-in world events (task P2-T03, session 2).

`spawn_object`, `remove_object`, `teleport` and `set_wind`.

Each one is a request to the `fieldproof::WorldEvents` system of the world
(`sim/fieldproof_gz_plugins`): a gz-transport service under `/world/<name>/fieldproof/` that
the engine calls with the `gz service` CLI inside the simulator's container
(:meth:`~fieldproof.events.EventContext.gz_service`). The plugin applies the request in the
simulator's next step and answers once it has: the reply's `data` is a one-line report, which
starts with `error:` when the request was refused (a name already taken, a model that does
not exist). The entry ends `ok` with that report as its detail, `failed` with the error.

An obstacle is a primitive shape (box, cylinder, sphere; box and cylinder collisions keep the
real-time factor, which a mesh on the ground plane halves) or a `model://` URI from the
simulator's resource path (a Fuel model of the world). Its placement is where its base rests
for a shape (the pose's `z` is the ground); a URI model is placed at the pose as it is.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveFloat, model_validator

from fieldproof.backend import ExecResult
from fieldproof.dsl import Placement, ScenarioError, TimelineBase
from fieldproof.events import FAILED, OK, ActionRecord, Event, EventContext, PlanContext
from fieldproof.manifest import Name
from fieldproof.world import SPAWN_HEIGHT_M, Pose, place

SERVICE_PREFIX = "fieldproof"
ENTITY_FACTORY = "gz.msgs.EntityFactory"
ENTITY = "gz.msgs.Entity"
POSE = "gz.msgs.Pose"
WIND = "gz.msgs.Wind"
STRING = "gz.msgs.StringMsg"
ERROR_PREFIX = "error:"
REPLY_DATA = re.compile(r'^data: (".*")\s*$', re.MULTILINE)
# A model name as Gazebo and SDF accept it (also what `gz model` lists).
MODEL_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
Vector3 = tuple[float, float, float]
Color = tuple[float, float, float]


def service_name(world: str, verb: str) -> str:
    """The plugin's service for `verb` in `world` (`/world/depot/fieldproof/spawn`)."""
    return f"/world/{world}/{SERVICE_PREFIX}/{verb}"


def pose_text(pose: Pose) -> str:
    """A `gz.msgs.Pose` in text format (position and a yaw-only orientation)."""
    return (
        f"position {{ x: {pose.x:g} y: {pose.y:g} z: {pose.z:g} }} "
        f"orientation {{ z: {math.sin(pose.yaw / 2):g} w: {math.cos(pose.yaw / 2):g} }}"
    )


def reply_data(result: ExecResult) -> str | None:
    """The `data` of a `gz.msgs.StringMsg` reply as `gz service` prints it, or None."""
    match = REPLY_DATA.search(result.output)
    if match is None:
        return None
    try:
        return str(json.loads(match.group(1)))
    except ValueError:
        return match.group(1).strip('"')


def judge(result: ExecResult, record: ActionRecord, what: str) -> str | None:
    """Set the record from the plugin's reply; the report when the request was applied."""
    data = reply_data(result)
    if not result.ok or data is None:
        record.status = FAILED
        record.detail = f"{what}: the simulator did not answer (exit {result.exit_code}): " + (
            result.tail(3) or "no output"
        )
        return None
    if data.startswith(ERROR_PREFIX):
        record.status, record.detail = FAILED, data[len(ERROR_PREFIX) :].strip()
        return None
    record.status, record.detail = OK, data
    return data


# ----------------------------------------------------------------------------- the shapes
def inertia(shape: str, mass: float, size: Vector3, radius: float, length: float) -> str:
    """The `<inertial>` of a solid shape of `mass` kilograms."""
    if shape == "box":
        x, y, z = size
        ixx, iyy, izz = (
            mass * (y * y + z * z) / 12,
            mass * (x * x + z * z) / 12,
            mass * (x * x + y * y) / 12,
        )
    elif shape == "cylinder":
        ixx = iyy = mass * (3 * radius * radius + length * length) / 12
        izz = mass * radius * radius / 2
    else:
        ixx = iyy = izz = 2 * mass * radius * radius / 5
    return (
        f"<inertial><mass>{mass:g}</mass><inertia><ixx>{ixx:g}</ixx><iyy>{iyy:g}</iyy>"
        f"<izz>{izz:g}</izz><ixy>0</ixy><ixz>0</ixz><iyz>0</iyz></inertia></inertial>"
    )


def geometry(shape: str, size: Vector3, radius: float, length: float) -> str:
    """The `<geometry>` of a shape."""
    if shape == "box":
        return f"<geometry><box><size>{size[0]:g} {size[1]:g} {size[2]:g}</size></box></geometry>"
    if shape == "cylinder":
        return (
            f"<geometry><cylinder><radius>{radius:g}</radius><length>{length:g}</length>"
            "</cylinder></geometry>"
        )
    return f"<geometry><sphere><radius>{radius:g}</radius></sphere></geometry>"


def shape_sdf(
    name: str,
    shape: str,
    size: Vector3,
    radius: float,
    length: float,
    *,
    static: bool,
    mass_kg: float,
    color: Color,
) -> str:
    """One line of SDF: a model of one link with the shape's collision and visual.

    Single-quoted attributes, so the text fits a double-quoted protobuf string as it is.
    """
    geom = geometry(shape, size, radius, length)
    r, g, b = color
    material = (
        f"<material><ambient>{r:g} {g:g} {b:g} 1</ambient><diffuse>{r:g} {g:g} {b:g} 1</diffuse>"
        "</material>"
    )
    body = "<static>true</static>" if static else ""
    inertial = "" if static else inertia(shape, mass_kg, size, radius, length)
    return (
        f"<?xml version='1.0'?><sdf version='1.11'><model name='{name}'>{body}"
        f"<link name='link'>{inertial}<collision name='collision'>{geom}</collision>"
        f"<visual name='visual'>{geom}{material}</visual></link></model></sdf>"
    )


def model_sdf(name: str, uri: str) -> str:
    """A model that merges in the model at `uri` (a Fuel or package model of the world)."""
    return (
        f"<?xml version='1.0'?><sdf version='1.11'><model name='{name}'>"
        f"<include merge='true'><uri>{uri}</uri></include></model></sdf>"
    )


# ---------------------------------------------------------------------------- the entries
class SpawnObject(TimelineBase):
    """Spawn an obstacle into the world: a primitive shape, or a model of the resource path."""

    action: Literal["spawn_object"]
    model: str = Field(pattern=MODEL_PATTERN, description="The name of the model in the world.")
    pose: Placement = Field(
        description="Where it goes: a frame of the world or coordinates; for a shape, where its "
        "base rests (`z` is the ground)."
    )
    shape: Literal["box", "cylinder", "sphere"] | None = Field(
        default=None, description="A primitive shape (or `uri` for a model of the world)."
    )
    size: Vector3 | None = Field(default=None, description="A box's size in metres (x, y, z).")
    radius: PositiveFloat | None = Field(
        default=None, description="A cylinder's or a sphere's radius in metres."
    )
    length: PositiveFloat | None = Field(
        default=None, description="A cylinder's length (height) in metres."
    )
    uri: str | None = Field(
        default=None,
        description="A `model://<name>` URI of the simulator's resource path (a Fuel model).",
    )
    static: bool = Field(default=True, description="Fixed in place (false: a dynamic body).")
    mass_kg: PositiveFloat = Field(default=1.0, description="The mass of a dynamic shape.")
    color: Color = Field(default=(0.8, 0.3, 0.1), description="The shape's colour (r, g, b).")

    @model_validator(mode="after")
    def _shape_or_uri(self) -> SpawnObject:
        if (self.shape is None) == (self.uri is None):
            raise ValueError("give either shape (box, cylinder, sphere) or uri (model://...)")
        if self.uri is not None:
            if not self.uri.startswith("model://") or len(self.uri) <= len("model://"):
                raise ValueError("uri: must be a model://<name> URI")
            if any(v is not None for v in (self.size, self.radius, self.length)):
                raise ValueError("size, radius and length belong to a shape, not a uri")
            return self
        if self.shape == "box":
            if self.size is None or any(v <= 0 for v in self.size):
                raise ValueError("a box needs a positive size (x, y, z)")
            if self.radius is not None or self.length is not None:
                raise ValueError("a box has a size, not a radius or a length")
        elif self.shape == "cylinder":
            if self.radius is None or self.length is None:
                raise ValueError("a cylinder needs a radius and a length")
            if self.size is not None:
                raise ValueError("a cylinder has a radius and a length, not a size")
        elif self.radius is None or self.size is not None or self.length is not None:
            raise ValueError("a sphere needs a radius only")
        if any(not 0.0 <= c <= 1.0 for c in self.color):
            raise ValueError("color: each of r, g, b is between 0 and 1")
        return self

    @property
    def height(self) -> float:
        """How tall the shape is (0 for a uri model)."""
        if self.shape == "box" and self.size is not None:
            return self.size[2]
        if self.shape == "cylinder" and self.length is not None:
            return self.length
        if self.shape == "sphere" and self.radius is not None:
            return 2 * self.radius
        return 0.0

    def sdf(self) -> str:
        """The model's SDF."""
        if self.uri is not None:
            return model_sdf(self.model, self.uri)
        return shape_sdf(
            self.model,
            str(self.shape),
            self.size or (0.0, 0.0, 0.0),
            self.radius or 0.0,
            self.length or 0.0,
            static=self.static,
            mass_kg=self.mass_kg,
            color=self.color,
        )


class RemoveObject(TimelineBase):
    """Remove a model from the world (one spawned by `spawn_object`, or any of the world's)."""

    action: Literal["remove_object"]
    model: str = Field(pattern=MODEL_PATTERN, description="The name of the model to remove.")


class Teleport(TimelineBase):
    """Move a model of the world to a pose at once: a vehicle, or any model by name."""

    action: Literal["teleport"]
    vehicle: Name | None = Field(default=None, description="The vehicle to move (its model).")
    model: str | None = Field(
        default=None, pattern=MODEL_PATTERN, description="Another model to move, by name."
    )
    pose: Placement = Field(
        description="Where to: a frame of the world or coordinates (`z` is the ground for a "
        "vehicle, which keeps its spawn height above it)."
    )

    @model_validator(mode="after")
    def _vehicle_or_model(self) -> Teleport:
        if (self.vehicle is None) == (self.model is None):
            raise ValueError("give either vehicle or model")
        return self


class SetWind(TimelineBase):
    """Set the world's wind: a velocity in the world frame, on the links that enable wind."""

    action: Literal["set_wind"]
    velocity: Vector3 = Field(
        description="The wind velocity in metres per second, in the world frame (x east, y "
        "north, z up)."
    )
    enabled: bool = Field(default=True, description="False disables the wind altogether.")


# ----------------------------------------------------------------------------- the events
class WorldEvent(Event):
    """An event of the world: a service of the WorldEvents plugin, judged by its reply."""

    verb: ClassVar[str]
    request_type: ClassVar[str]

    def request(self, entry: Any, resolved: Mapping[str, Any]) -> str:
        """The request in protobuf text format."""
        raise NotImplementedError

    def what(self, entry: Any) -> str:  # noqa: ARG002
        """What the record says the event did (`spawn crate`)."""
        return self.verb

    def apply(
        self,
        entry: Any,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Call the service and judge its reply."""
        what = self.what(entry)
        result = context.gz_service(
            str(resolved["service"]),
            request_type=self.request_type,
            reply_type=STRING,
            request=self.request(entry, resolved),
            timeout_s=min(float(resolved.get("limit_s", 30.0)), 30.0),
        )
        report = judge(result, record, what)
        context.log(
            "world.changed" if report is not None else "world.refused",
            report or record.detail,
            level="info" if report is not None else "warning",
            action=record.name,
            verb=self.verb,
        )


class SpawnObjectEvent(WorldEvent):
    """`spawn_object`."""

    action: ClassVar[str] = "spawn_object"
    Entry: ClassVar[type[TimelineBase]] = SpawnObject
    summary: ClassVar[str] = (
        "Spawn an obstacle (a box, cylinder, sphere or a model:// URI) at a pose of the world."
    )
    verb: ClassVar[str] = "spawn"
    request_type: ClassVar[str] = ENTITY_FACTORY

    def plan(self, entry: SpawnObject, context: PlanContext) -> dict[str, Any]:
        """The model's pose in the world (a shape stands on the placement) and its SDF."""
        context.require_simulator(self.action, "the simulator's world")
        ground = place(entry.pose, context.world, f"{context.where}.pose")
        pose = Pose(ground.x, ground.y, ground.z + entry.height / 2, ground.yaw)
        if any(v.name == entry.model for v in context.vehicles.values()):
            raise ScenarioError(
                f"{context.where}.model: {entry.model!r} is a vehicle of the scenario"
            )
        return {
            "service": service_name(context.world.name, self.verb),
            "model": entry.model,
            "pose": pose,
            "sdf": entry.sdf(),
        }

    def request(self, entry: SpawnObject, resolved: Mapping[str, Any]) -> str:
        """The request in protobuf text format."""
        pose = resolved["pose"]
        pose = pose if isinstance(pose, Pose) else Pose(**pose)
        return (
            f"name: {json.dumps(entry.model)} allow_renaming: false "
            f"sdf: {json.dumps(str(resolved['sdf']))} pose {{ {pose_text(pose)} }}"
        )

    def what(self, entry: SpawnObject) -> str:
        """What the record says the event did."""
        return f"spawn {entry.model}"


class RemoveObjectEvent(WorldEvent):
    """`remove_object`."""

    action: ClassVar[str] = "remove_object"
    Entry: ClassVar[type[TimelineBase]] = RemoveObject
    summary: ClassVar[str] = "Remove a model from the world by name."
    verb: ClassVar[str] = "remove"
    request_type: ClassVar[str] = ENTITY

    def plan(self, entry: RemoveObject, context: PlanContext) -> dict[str, Any]:
        """The service; a vehicle cannot be removed (the run watches its containers)."""
        context.require_simulator(self.action, "the simulator's world")
        if any(v.name == entry.model for v in context.vehicles.values()):
            raise ScenarioError(
                f"{context.where}.model: {entry.model!r} is a vehicle of the scenario "
                "(container.kill ends a vehicle)"
            )
        return {"service": service_name(context.world.name, self.verb), "model": entry.model}

    def request(self, entry: RemoveObject, resolved: Mapping[str, Any]) -> str:  # noqa: ARG002
        """The request in protobuf text format."""
        return f"name: {json.dumps(entry.model)} type: MODEL"

    def what(self, entry: RemoveObject) -> str:
        """What the record says the event did."""
        return f"remove {entry.model}"


class TeleportEvent(WorldEvent):
    """`teleport`."""

    action: ClassVar[str] = "teleport"
    Entry: ClassVar[type[TimelineBase]] = Teleport
    summary: ClassVar[str] = "Move a vehicle (or any model) to a pose of the world at once."
    verb: ClassVar[str] = "teleport"
    request_type: ClassVar[str] = POSE

    def plan(self, entry: Teleport, context: PlanContext) -> dict[str, Any]:
        """The model and its new pose (a vehicle keeps its spawn height above the ground)."""
        context.require_simulator(self.action, "the simulator's world")
        pose = place(entry.pose, context.world, f"{context.where}.pose")
        if entry.vehicle is not None:
            model = context.vehicle(entry.vehicle).name
            pose = Pose(pose.x, pose.y, pose.z + SPAWN_HEIGHT_M, pose.yaw)
        else:
            model = str(entry.model)
        return {
            "service": service_name(context.world.name, self.verb),
            "model": model,
            "pose": pose,
        }

    def request(self, entry: Teleport, resolved: Mapping[str, Any]) -> str:  # noqa: ARG002
        """The request in protobuf text format."""
        pose = resolved["pose"]
        pose = pose if isinstance(pose, Pose) else Pose(**pose)
        return f"name: {json.dumps(str(resolved['model']))} {pose_text(pose)}"

    def what(self, entry: Teleport) -> str:
        """What the record says the event did."""
        return f"teleport {entry.vehicle or entry.model}"


class SetWindEvent(WorldEvent):
    """`set_wind`."""

    action: ClassVar[str] = "set_wind"
    Entry: ClassVar[type[TimelineBase]] = SetWind
    summary: ClassVar[str] = "Set the world's wind velocity (on the links that enable wind)."
    verb: ClassVar[str] = "set_wind"
    request_type: ClassVar[str] = WIND

    def plan(self, entry: SetWind, context: PlanContext) -> dict[str, Any]:  # noqa: ARG002
        """The service."""
        context.require_simulator(self.action, "the simulator's world")
        return {"service": service_name(context.world.name, self.verb)}

    def request(self, entry: SetWind, resolved: Mapping[str, Any]) -> str:  # noqa: ARG002
        """The request in protobuf text format."""
        x, y, z = entry.velocity
        enabled = "true" if entry.enabled else "false"
        return f"enable_wind: {enabled} linear_velocity {{ x: {x:g} y: {y:g} z: {z:g} }}"


EVENTS = (SpawnObjectEvent, RemoveObjectEvent, TeleportEvent, SetWindEvent)
