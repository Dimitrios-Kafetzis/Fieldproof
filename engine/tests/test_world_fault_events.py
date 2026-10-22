"""Tests of the world events and the fault events (task P2-T03, session 2): the entries'
validation, what `plan` resolves against the world and the vehicle's bridge bundle, and what
`apply` asks the simulator (`gz service`) or the bridge (`ros2 param set`) through a scripted
context, with every outcome."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_events import REPO, Context, Vehicle, backend_with, record, world

from fieldproof.backend import ExecResult
from fieldproof.dsl import ScenarioError
from fieldproof.events import PlanContext, registry
from fieldproof.events.faults import (
    ActuatorLimit,
    ActuatorLimitEvent,
    BatteryDrain,
    GpsDeny,
    GpsOffset,
    GpsOffsetEvent,
    ImuBias,
    ImuBiasEvent,
    SensorDropout,
    SensorFreeze,
    float_text,
    vector_text,
)
from fieldproof.events.world import (
    RemoveObject,
    SetWind,
    SpawnObject,
    Teleport,
    pose_text,
    reply_data,
    service_name,
    shape_sdf,
)
from fieldproof.world import Pose

PARAM_OK = ExecResult(0, "Set parameter successful\n")


def context(namespace: str = "ugv1") -> Context:
    return Context(backend_with("ugv1"), [Vehicle(namespace=namespace)])


def pc(ctx: Context, where: str = "timeline[0]") -> PlanContext:
    """A plan context over the fake vehicles."""
    return PlanContext(world(), ctx.vehicles, where=where)


def spawn(**fields: Any) -> SpawnObject:
    data = {
        "action": "spawn_object",
        "model": "crate",
        "pose": {"x": 10.0, "y": 1.0},
        "shape": "box",
        "size": [0.6, 0.6, 0.5],
    }
    data.update(fields)
    return SpawnObject.model_validate(data)


# ------------------------------------------------------------------------- world events
def test_world_events_are_registered() -> None:
    assert {"spawn_object", "remove_object", "teleport", "set_wind"} <= set(registry().actions)
    assert service_name("depot", "spawn") == "/world/depot/fieldproof/spawn"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"shape": None, "size": None}, "either shape"),
        ({"uri": "model://crate"}, "either shape"),
        ({"shape": None, "size": None, "uri": "crate"}, "model://"),
        ({"shape": None, "uri": "model://crate"}, "belong to a shape"),
        ({"size": None}, "a box needs a positive size"),
        ({"size": [1, 0, 1]}, "a box needs a positive size"),
        ({"radius": 0.5}, "a box has a size"),
        ({"shape": "cylinder", "size": None}, "a cylinder needs a radius and a length"),
        ({"shape": "cylinder", "radius": 0.2, "length": 1.0}, "a cylinder has a radius"),
        ({"shape": "sphere", "size": None}, "a sphere needs a radius only"),
        ({"shape": "sphere", "size": None, "radius": 0.2, "length": 1.0}, "a sphere needs"),
        ({"color": [2, 0, 0]}, "between 0 and 1"),
        ({"model": "Crate box"}, "pattern"),
        ({"mass_kg": 0}, "greater than 0"),
    ],
)
def test_spawn_entry_validation(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        spawn(**fields)


def test_shape_sdf_and_heights() -> None:
    box = spawn()
    assert box.height == 0.5
    sdf = box.sdf()
    assert sdf.startswith("<?xml version='1.0'?><sdf version='1.11'><model name='crate'>")
    assert "<static>true</static>" in sdf
    assert "<inertial>" not in sdf
    assert "<box><size>0.6 0.6 0.5</size></box>" in sdf
    assert "<ambient>0.8 0.3 0.1 1</ambient>" in sdf
    assert '"' not in sdf, "fits a double-quoted protobuf string as it is"
    cylinder = spawn(shape="cylinder", size=None, radius=0.2, length=0.7, static=False, mass_kg=3)
    assert cylinder.height == 0.7
    sdf = cylinder.sdf()
    assert "<static>" not in sdf
    assert "<mass>3</mass>" in sdf
    assert "<izz>0.06</izz>" in sdf  # m r² / 2
    assert "<cylinder><radius>0.2</radius><length>0.7</length></cylinder>" in sdf
    sphere = spawn(shape="sphere", size=None, radius=0.25, static=False)
    assert sphere.height == 0.5
    assert "<sphere><radius>0.25</radius></sphere>" in sphere.sdf()
    assert "<ixx>0.025</ixx>" in sphere.sdf()  # 2 m r² / 5
    dynamic_box = spawn(static=False, mass_kg=2)
    assert "<ixx>0.101667</ixx>" in dynamic_box.sdf()  # m (y² + z²) / 12
    model = spawn(shape=None, size=None, uri="model://cardboard_box")
    assert model.height == 0.0
    assert model.sdf() == (
        "<?xml version='1.0'?><sdf version='1.11'><model name='crate'>"
        "<include merge='true'><uri>model://cardboard_box</uri></include></model></sdf>"
    )
    assert shape_sdf("x", "box", (1, 1, 1), 0, 0, static=True, mass_kg=1, color=(0, 0, 0))


def test_spawn_plan_and_apply() -> None:
    event = registry()["spawn_object"].event
    entry = spawn(pose={"x": 10.0, "y": 1.0, "yaw": 1.5})
    ctx = context()
    resolved = event.plan(entry, pc(ctx, "timeline[1]"))
    assert resolved["service"] == "/world/depot/fieldproof/spawn"
    assert resolved["model"] == "crate"
    assert resolved["pose"] == Pose(10.0, 1.0, 0.25, 1.5), "a shape stands on the placement"
    assert resolved["sdf"] == entry.sdf()
    framed = event.plan(spawn(pose={"frame": "spawn_dock"}), pc(ctx))
    assert framed["pose"] == Pose(10.0, 3.0, 0.25, -1.5708)
    with pytest.raises(ScenarioError, match=r"timeline\[1\].pose: world depot has no frame"):
        event.plan(spawn(pose={"frame": "nowhere"}), pc(ctx, "timeline[1]"))
    with pytest.raises(ScenarioError, match=r"timeline\[1\].model: 'ugv1' is a vehicle"):
        event.plan(spawn(model="ugv1"), pc(ctx, "timeline[1]"))

    ctx.gz_result = ExecResult(0, 'data: "spawned model crate at (10, 1, 0.25, yaw 1.5)"\n')
    rec = record("obstacle", "spawn_object")
    event.apply(entry, {**resolved, "limit_s": 100.0}, ctx, rec)
    assert (rec.status, rec.detail) == ("ok", "spawned model crate at (10, 1, 0.25, yaw 1.5)")
    (call,) = ctx.gz_calls
    assert call["service"] == "/world/depot/fieldproof/spawn"
    assert call["request_type"] == "gz.msgs.EntityFactory"
    assert call["reply_type"] == "gz.msgs.StringMsg"
    assert call["timeout_s"] == 30.0
    assert call["request"].startswith('name: "crate" allow_renaming: false sdf: "<?xml')
    assert call["request"].endswith(
        "pose { position { x: 10 y: 1 z: 0.25 } orientation { z: 0.681639 w: 0.731689 } }"
    )
    assert json.loads(call["request"].split("sdf: ", 1)[1].split(" pose {", 1)[0]) == entry.sdf()
    assert ctx.events[-1][0] == "world.changed"

    # The plugin's refusal, the simulator not answering, a dataclass pose from the record.
    ctx.gz_result = ExecResult(0, 'data: "error: spawn: a model called crate already exists"\n')
    rec = record()
    event.apply(entry, {**resolved, "limit_s": 100.0}, ctx, rec)
    assert (rec.status, rec.detail) == ("failed", "spawn: a model called crate already exists")
    assert ctx.events[-1][0] == "world.refused"
    ctx.gz_result = ExecResult(1, "Service call timed out\n")
    rec = record()
    event.apply(entry, {**resolved, "limit_s": 5.0}, ctx, rec)
    assert rec.status == "failed"
    assert (
        rec.detail == "spawn crate: the simulator did not answer (exit 1): Service call timed out"
    )
    assert ctx.gz_calls[-1]["timeout_s"] == 5.0
    ctx.gz_result = ExecResult(0, 'data: "spawned model crate at (10, 1, 0.25, yaw 1.5)"\n')
    as_data = {**resolved, "pose": {"x": 10.0, "y": 1.0, "z": 0.25, "yaw": 1.5}, "limit_s": 9.0}
    rec = record()
    event.apply(entry, as_data, ctx, rec)
    assert rec.status == "ok"


def test_reply_data_and_pose_text() -> None:
    assert reply_data(ExecResult(0, 'data: "ok"\n')) == "ok"
    assert reply_data(ExecResult(0, 'noise\ndata: "a \\"quoted\\" word"\n')) == 'a "quoted" word'
    assert reply_data(ExecResult(0, "")) is None
    assert reply_data(ExecResult(0, "data: true\n")) is None
    assert pose_text(Pose(1, 2, 3, 0)) == ("position { x: 1 y: 2 z: 3 } orientation { z: 0 w: 1 }")


def test_remove_plan_and_apply() -> None:
    event = registry()["remove_object"].event
    entry = RemoveObject.model_validate({"action": "remove_object", "model": "crate"})
    ctx = context()
    resolved = event.plan(entry, pc(ctx))
    assert resolved == {"service": "/world/depot/fieldproof/remove", "model": "crate"}
    with pytest.raises(ScenarioError, match="is a vehicle of the scenario"):
        event.plan(entry.model_copy(update={"model": "ugv1"}), pc(ctx))
    ctx.gz_result = ExecResult(0, 'data: "removed model crate"\n')
    rec = record()
    event.apply(entry, {**resolved, "limit_s": 100.0}, ctx, rec)
    assert (rec.status, rec.detail) == ("ok", "removed model crate")
    assert ctx.gz_calls[-1]["request"] == 'name: "crate" type: MODEL'
    assert ctx.gz_calls[-1]["request_type"] == "gz.msgs.Entity"


def test_teleport_plan_and_apply() -> None:
    event = registry()["teleport"].event
    with pytest.raises(ValueError, match="either vehicle or model"):
        Teleport.model_validate({"action": "teleport", "pose": {"x": 1, "y": 2}})
    with pytest.raises(ValueError, match="either vehicle or model"):
        Teleport.model_validate(
            {"action": "teleport", "vehicle": "ugv1", "model": "crate", "pose": {"x": 1, "y": 2}}
        )
    ctx = context()
    vehicle = Teleport.model_validate(
        {"action": "teleport", "vehicle": "ugv1", "pose": {"frame": "spawn_dock"}}
    )
    resolved = event.plan(vehicle, pc(ctx))
    assert resolved["model"] == "ugv1"
    assert resolved["pose"] == Pose(10.0, 3.0, 0.12, -1.5708), "the spawn height above the ground"
    model = Teleport.model_validate(
        {"action": "teleport", "model": "crate", "pose": {"x": 1, "y": 2, "z": 0.5, "yaw": 0.0}}
    )
    assert event.plan(model, pc(ctx))["pose"] == Pose(1, 2, 0.5, 0.0)
    with pytest.raises(ScenarioError, match="'ugv9' is not a vehicle"):
        event.plan(vehicle.model_copy(update={"vehicle": "ugv9"}), pc(ctx))
    ctx.gz_result = ExecResult(0, 'data: "teleported model ugv1 to (10, 3, 0.12, yaw -1.5708)"\n')
    rec = record()
    event.apply(vehicle, {**resolved, "limit_s": 100.0}, ctx, rec)
    assert (rec.status, rec.detail) == ("ok", "teleported model ugv1 to (10, 3, 0.12, yaw -1.5708)")
    assert ctx.gz_calls[-1]["request"].startswith('name: "ugv1" position { x: 10 y: 3 z: 0.12 }')
    assert ctx.gz_calls[-1]["request_type"] == "gz.msgs.Pose"


def test_set_wind_plan_and_apply() -> None:
    event = registry()["set_wind"].event
    entry = SetWind.model_validate({"action": "set_wind", "velocity": [3.0, -1.0, 0.0]})
    ctx = context()
    resolved = event.plan(entry, pc(ctx))
    assert resolved == {"service": "/world/depot/fieldproof/set_wind"}
    ctx.gz_result = ExecResult(0, 'data: "wind set to (3, -1, 0) m/s"\n')
    rec = record()
    event.apply(entry, {**resolved, "limit_s": 100.0}, ctx, rec)
    assert (rec.status, rec.detail) == ("ok", "wind set to (3, -1, 0) m/s")
    assert ctx.gz_calls[-1]["request"] == "enable_wind: true linear_velocity { x: 3 y: -1 z: 0 }"
    assert ctx.gz_calls[-1]["request_type"] == "gz.msgs.Wind"
    off = SetWind.model_validate({"action": "set_wind", "velocity": [0, 0, 0], "enabled": False})
    rec = record()
    event.apply(off, {**resolved, "limit_s": 100.0}, ctx, rec)
    assert ctx.gz_calls[-1]["request"] == "enable_wind: false linear_velocity { x: 0 y: 0 z: 0 }"
    with pytest.raises(ValueError, match="velocity"):
        SetWind.model_validate({"action": "set_wind", "velocity": [1, 2]})


# ------------------------------------------------------------------------- fault events
def test_fault_events_are_registered() -> None:
    assert {
        "gps.deny",
        "gps.offset",
        "imu.bias",
        "sensor.dropout",
        "sensor.freeze",
        "battery.drain",
        "actuator.limit",
    } <= set(registry().actions)
    assert vector_text((0.1, 0, -2)) == "[0.1, 0.0, -2.0]"
    assert float_text(3) == "3.0"


def fault(action: str, **fields: Any) -> Any:
    entry_type = registry()[action].entry_type
    return entry_type.model_validate({"action": action, "vehicle": "ugv1", **fields})


def apply_fault(ctx: Context, entry: Any, resolved: dict[str, Any], limit_s: float = 100.0) -> Any:
    rec = record(entry.action, entry.action)
    registry()[entry.action].event.apply(entry, {**resolved, "limit_s": limit_s}, ctx, rec)
    return rec


def test_gps_deny_sets_and_lifts_in_the_bridge() -> None:
    event = registry()["gps.deny"].event
    ctx = context()
    entry = fault("gps.deny", duration_s="10s")
    resolved = event.plan(entry, pc(ctx, "timeline[0]"))
    assert resolved == {
        "node": "/ugv1/gps_adapter",
        "set": {"deny": "true"},
        "restore": {"deny": "false"},
    }
    ctx.ros2_result = PARAM_OK
    rec = apply_fault(ctx, entry, resolved)
    assert (rec.status, rec.detail) == ("ok", "fix denied for 10 s, lifted")
    assert [c[1] for c in ctx.bridge_calls] == [
        ("param", "set", "/ugv1/gps_adapter", "deny", "true"),
        ("param", "set", "/ugv1/gps_adapter", "deny", "false"),
    ]
    assert all(c[0] == "fieldproof-run-bridge-ugv1" for c in ctx.bridge_calls)
    assert ctx.ros2_calls == [], "nothing runs in the vehicle's container"
    assert ctx.held == [10.0]
    assert [e[0] for e in ctx.events if e[0].startswith("fault.")] == [
        "fault.injected",
        "fault.lifted",
    ]
    # Without a duration the fault stays; a named sensor; the root namespace.
    ctx = context(namespace="")
    entry = fault("gps.deny", sensor="gps")
    resolved = event.plan(entry, pc(ctx))
    assert resolved["node"] == "/gps_adapter"
    ctx.ros2_result = PARAM_OK
    rec = apply_fault(ctx, entry, resolved)
    assert (rec.status, rec.detail) == ("ok", "fix denied")
    assert len(ctx.bridge_calls) == 1
    assert ctx.held == []


def test_fault_plan_refusals() -> None:
    ctx = context()
    with pytest.raises(ScenarioError, match=r"timeline\[2\].sensor: 'camera' is not routed"):
        registry()["sensor.dropout"].event.plan(
            fault("sensor.dropout", sensor="camera"),
            pc(ctx, "timeline[2]"),
        )
    with pytest.raises(ScenarioError, match="'lidar' is not routed through such an injector"):
        registry()["gps.deny"].event.plan(fault("gps.deny", sensor="lidar"), pc(ctx))
    with pytest.raises(ScenarioError, match="'ugv9' is not a vehicle"):
        registry()["gps.deny"].event.plan(fault("gps.deny", vehicle="ugv9"), pc(ctx))
    # A manifest without a battery, or without a GNSS: no injector of the kind.
    ugv = Vehicle(
        namespace="ugv1", manifest=REPO / "robots/fieldproof_robots/models/ugv_ref/robot.yaml"
    )
    assert ugv.bundle().injector("battery") is not None
    without = ctx.vehicles["ugv1"].bundle()
    without.nodes = [n for n in without.nodes if n.kind not in ("battery", "navsat_adapter")]
    ctx.vehicles["ugv1"].bundle = lambda: without  # type: ignore[method-assign]
    with pytest.raises(ScenarioError, match="'battery' is not routed through such an injector"):
        registry()["battery.drain"].event.plan(fault("battery.drain", duration_s="1m"), pc(ctx))
    with pytest.raises(ScenarioError, match="has no injectors of the kind"):
        registry()["gps.offset"].event.plan(fault("gps.offset", east_m=1.0), pc(ctx))


def test_fault_apply_outcomes() -> None:
    event = registry()["sensor.dropout"].event
    ctx = context()
    entry = fault("sensor.dropout", sensor="lidar", duration_s="5s")
    resolved = event.plan(entry, pc(ctx))
    assert resolved["node"] == "/ugv1/scan_relay"
    # The injector refuses the value: failed, nothing more is asked.
    ctx.ros2_result = ExecResult(1, "Setting parameter failed: dropout: 'x' is not valid\n")
    rec = apply_fault(ctx, entry, resolved)
    assert rec.status == "failed"
    assert rec.detail.startswith("/ugv1/scan_relay refused dropout (exit 1)")
    assert len(ctx.bridge_calls) == 1
    assert ctx.events[-1][0] == "fault.refused"
    # The CLI's graph lags: asked again, then set; the lift at the end.
    ctx = context()
    ctx.ros2_results = [ExecResult(1, "Node not found\n")]
    ctx.ros2_result = PARAM_OK
    rec = apply_fault(ctx, entry, resolved)
    assert (rec.status, rec.detail) == ("ok", "lidar silent for 5 s, lifted")
    assert len(ctx.bridge_calls) == 3
    assert ctx.held == [1.0, 5.0]
    # The budget ends the fault before its duration: the record says so, nothing is lifted.
    ctx = context()
    ctx.ros2_result = PARAM_OK
    rec = apply_fault(ctx, entry, resolved, limit_s=2.0)
    assert rec.status == "ok"
    assert rec.detail == "lidar silent; the budget ended the fault after 2 s"
    assert len(ctx.bridge_calls) == 1
    # The run ends during the hold: the record stays as set.
    ctx = context()
    ctx.ros2_result = PARAM_OK
    ctx.stop_after_hold = True
    rec = apply_fault(ctx, entry, resolved)
    assert (rec.status, rec.detail) == ("ok", "lidar silent")
    # The lift is refused: failed.
    ctx = context()
    ctx.ros2_results = [PARAM_OK]
    ctx.ros2_result = ExecResult(124, "")
    rec = apply_fault(ctx, entry, resolved)
    assert rec.status == "timeout"


def test_each_fault_event_parameters() -> None:
    ctx = context()
    plan = pc(ctx)
    freeze = registry()["sensor.freeze"].event.plan(fault("sensor.freeze", sensor="odometry"), plan)
    assert freeze == {
        "node": "/ugv1/odom_relay",
        "set": {"freeze": "true"},
        "restore": {"freeze": "false"},
    }
    by_topic = registry()["sensor.freeze"].event.plan(
        fault("sensor.freeze", sensor="imu/data"), plan
    )
    assert by_topic["node"] == "/ugv1/imu_adapter"
    command = registry()["sensor.dropout"].event.plan(
        fault("sensor.dropout", sensor="command"), plan
    )
    assert command["node"] == "/ugv1/cmd_vel_relay"
    offset = registry()["gps.offset"].event.plan(
        fault("gps.offset", east_m=5, north_m=-2, duration_s=30), plan
    )
    assert offset == {
        "node": "/ugv1/gps_adapter",
        "set": {"offset_enu_m": "[5.0, -2.0, 0.0]"},
        "restore": {"offset_enu_m": "[0.0, 0.0, 0.0]"},
    }
    bias = registry()["imu.bias"].event.plan(
        fault("imu.bias", gyro_radps=[0.05, 0, 0], accel_mps2=[0, 0, 0.3]), plan
    )
    assert bias["node"] == "/ugv1/imu_adapter"
    assert bias["set"] == {
        "gyro_bias_radps": "[0.05, 0.0, 0.0]",
        "accel_bias_mps2": "[0.0, 0.0, 0.3]",
    }
    assert bias["restore"] == {
        "gyro_bias_radps": "[0.0, 0.0, 0.0]",
        "accel_bias_mps2": "[0.0, 0.0, 0.0]",
    }
    limit = registry()["actuator.limit"].event.plan(
        fault("actuator.limit", max_linear_mps=0.3, duration_s=20), plan
    )
    assert limit == {
        "node": "/ugv1/cmd_vel_relay",
        "set": {"max_linear_mps": "0.3"},
        "restore": {"max_linear_mps": "0.0"},
    }
    both = registry()["actuator.limit"].event.plan(
        fault("actuator.limit", max_linear_mps=0.3, max_angular_radps=0.5), plan
    )
    assert both["set"] == {"max_linear_mps": "0.3", "max_angular_radps": "0.5"}
    with pytest.raises(ValueError, match="give max_linear_mps"):
        fault("actuator.limit")
    with pytest.raises(ValueError, match="greater than 0"):
        fault("actuator.limit", max_linear_mps=0)
    drain = registry()["battery.drain"].event.plan(
        fault("battery.drain", to_pct=20, duration_s="2m"), plan
    )
    assert drain == {
        "node": "/ugv1/battery",
        "set": {"drain_to": "0.2", "drain_duration_s": "120.0"},
        "restore": {},
    }
    with pytest.raises(ValueError, match="duration_s"):
        fault("battery.drain")
    with pytest.raises(ValueError, match="less than or equal to 100"):
        fault("battery.drain", to_pct=120, duration_s=1)
    # The entries' detail strings.
    assert GpsOffsetEvent().detail(fault("gps.offset", east_m=5, north_m=-2)) == (
        "fix offset by (5, -2, 0) m ENU"
    )
    assert ImuBiasEvent().detail(fault("imu.bias", gyro_radps=[0.05, 0, 0])) == (
        "imu biased by [0.05, 0.0, 0.0] rad/s, [0.0, 0.0, 0.0] m/s²"
    )
    assert (
        ActuatorLimitEvent().detail(
            fault("actuator.limit", max_linear_mps=0.3, max_angular_radps=0.5)
        )
        == "actuators limited to 0.3 m/s, 0.5 rad/s"
    )


def test_battery_drain_commands_then_waits() -> None:
    event = registry()["battery.drain"].event
    ctx = context()
    entry = fault("battery.drain", to_pct=0, duration_s="30s")
    resolved = event.plan(entry, pc(ctx))
    ctx.ros2_result = PARAM_OK
    rec = apply_fault(ctx, entry, resolved)
    assert (rec.status, rec.detail) == ("ok", "drained to 0 % over 30 s")
    assert [c[1][3:] for c in ctx.bridge_calls] == [
        ("drain_to", "0.0"),
        ("drain_duration_s", "30.0"),
    ], "the target before the duration, which starts the ramp"
    assert ctx.held == [30.0]
    # Under a shorter budget the entry ends when the budget does.
    ctx = context()
    ctx.ros2_result = PARAM_OK
    rec = apply_fault(ctx, entry, resolved, limit_s=10.0)
    assert (rec.status, rec.detail) == ("ok", "draining to 0 % over 30 s")
    assert ctx.held == [10.0]
    # A refused parameter ends it failed before any wait.
    ctx = context()
    ctx.ros2_result = ExecResult(1, "Node not found\n")
    rec = apply_fault(ctx, entry, resolved, limit_s=3.0)
    assert rec.status == "failed"
    assert rec.detail == "/ugv1/battery not found within 3 s"
    assert ctx.held == [1.0, 1.0]


def test_entries_of_the_faults_validate_as_the_schema_says() -> None:
    assert GpsDeny.model_validate({"action": "gps.deny", "vehicle": "ugv1"}).duration_s is None
    assert GpsOffset.model_validate({"action": "gps.offset", "vehicle": "ugv1"}).east_m == 0.0
    assert ImuBias.model_validate({"action": "imu.bias", "vehicle": "ugv1"}).gyro_radps == (0, 0, 0)
    with pytest.raises(ValueError, match="sensor"):
        SensorDropout.model_validate({"action": "sensor.dropout", "vehicle": "ugv1"})
    with pytest.raises(ValueError, match="sensor"):
        SensorFreeze.model_validate({"action": "sensor.freeze", "vehicle": "ugv1"})
    assert (
        BatteryDrain.model_validate(
            {"action": "battery.drain", "vehicle": "ugv1", "duration_s": "1m"}
        ).to_pct
        == 0.0
    )
    assert (
        ActuatorLimit.model_validate(
            {"action": "actuator.limit", "vehicle": "ugv1", "max_angular_radps": 1}
        ).max_linear_mps
        is None
    )
    with pytest.raises(ValueError, match="vehicle"):
        GpsDeny.model_validate({"action": "gps.deny"})
    assert isinstance(pc(context()), PlanContext)
