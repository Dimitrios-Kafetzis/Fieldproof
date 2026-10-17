# fieldproof_worlds

Gazebo Harmonic worlds for Fieldproof scenarios, the primitive models they use and the Gazebo
Fuel models they include. Reference: [docs/reference/worlds.md](../../docs/reference/worlds.md).

| Directory | Contents |
| --- | --- |
| `worlds/` | `fieldproof_empty.sdf` (ground plane and sun), `depot.sdf` (the navigation course below) |
| `models/` | `pallet`, `traffic_cone`: primitive models (box and cylinder collisions) |
| `scripts/world_map.py` | draws the top-down SVG map of a world from its collision geometry |
| `test/test_worlds.py` | the worlds against `assets/fuel.lock`, the installed models and the map |
| `env-hooks/` | puts `share/fieldproof_worlds/worlds` and `models` on `GZ_SIM_RESOURCE_PATH` |

Every world fixes the physics step at 1 ms with a real-time factor of 1.0 and loads the
Physics, UserCommands, SceneBroadcaster, Contact, Sensors (ogre2), Imu and NavSat systems, so
the reference vehicles spawn into any of them. No model has a mesh collision: against the
ground plane it costs a large share of every physics step.

## depot

A depot building (x −16..16, y 0..20: a racking hall, a corridor, an office, a workshop and a
loading bay, connected by door openings) and a fenced yard (y −22..0) with a 10 % ramp up to a
0.8 m plateau and a 16 % ramp down, a chicane of jersey barriers at the gate, and cones, drums,
pallets and boxes as obstacles. Named frames (`spawn_dock`, `spawn_hall`, `goal_office`,
`goal_workshop`, `goal_aisle`, `goal_plateau`, `goal_gate`) give the poses scenarios use.

![Top view of the depot world](../../docs/reference/worlds/depot-map.svg)

The map is generated; after editing the world run `make assets` at the repository root, then

```bash
sim/fieldproof_worlds/scripts/world_map.py sim/fieldproof_worlds/worlds/depot.sdf \
    -o docs/reference/worlds/depot-map.svg --resource-path assets/fuel/models \
    --resource-path sim/fieldproof_worlds/models
```

Run it headless with the UGV (dev container, after `make test-ros` or `tools/ros/colcon.sh build`):

```bash
gz sim -s -r --headless-rendering depot.sdf &
ros2 launch fieldproof_robots spawn_ugv_ref.launch.py world:=depot x:=10 y:=3 yaw:=-1.5708
```

`make up` runs the same world in the `sim` container with the worlds, models and fetched Fuel
assets bind-mounted; `make test-worlds` is the verification (load time, real-time factor with
the UGV and its sensors over 30 simulated seconds).

The Fuel models (`pallet_rack_section`, `cardboard_box`, `jersey_barrier`, `55gal_drum`) are
pinned in `assets/fuel.lock` and fetched by the build (`tools/assets/fetch_fuel.py`), see
[assets/README.md](../../assets/README.md).
