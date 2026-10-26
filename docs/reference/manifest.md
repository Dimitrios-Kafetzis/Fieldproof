# Vehicle manifest

The vehicle manifest, `robot.yaml`, describes one vehicle for the whole stack: what the model is
(frames, drive, sensors, their topics, rates, frames and QoS) and what runs on it (the container
image or how to build it, the entrypoint launch, the parameter files, the driver-swap map, the
readiness conditions, the resource budget). Task P1-T01 wrote the first manifest for the reference
UGV ([robots.md](robots.md#manifest-robotyaml)), P1-T03's bridge generator reads it
([bridge.md](bridge.md)), and P1-T04 formalised the format as **schema v0** with the `runtime`
section ([ADR-0004](../adr/0004-one-container-per-vehicle.md),
[ADR-0007](../adr/0007-fidelity-tiers.md)). The schema is the Pydantic model
`fieldproof.manifest.RobotManifest` in the engine; `engine/schemas/robot.schema.json` is its JSON
Schema export, used by the `check-jsonschema` pre-commit hook on every `robot.yaml`
([tooling.md](../setup/tooling.md)).

```bash
uv run --project engine fieldproof manifest validate robots/fieldproof_robots/models/ugv_ref/robot.yaml \
    examples/vehicles/ugv_nav2/robot.yaml
uv run --project engine fieldproof manifest schema > engine/schemas/robot.schema.json   # make manifest-schema
```

`validate` resolves `extends`, validates every section, checks that the model sections a stack
needs are present (`--partial` for a manifest meant only to be extended) and prints one line per
manifest with its sensors, interfaces, runtime tier and image and, if any, the interfaces no driver
of the swap map provides. Exit code 2 and one line per problem (`runtime.drivers[lidar].provides:
'points' is not an interface of this manifest`) otherwise.

## Document

```yaml
apiVersion: fieldproof.dev/v0alpha1
kind: Robot
extends: ../../../robots/fieldproof_robots/models/ugv_ref/robot.yaml   # optional
metadata: {...}
description: {...}      # model sections, from the reference model when extended
frames: {...}
dimensions: {...}
drive: {...}
sensors: [...]
runtime: {...}          # what runs on the vehicle (P1-T04)
```

Unknown keys are errors in every section, so a typo never passes silently. Topic names are
relative to the vehicle namespace unless they start with a slash. The TF topics follow the
drive's `tf` interface: relative, as in the reference vehicles, every vehicle of a fleet has its
own `/<namespace>/tf` and `/<namespace>/tf_static` with the usual frame names (Nav2's
multi-robot convention, task P2-T01); `/tf` keeps one tree shared by every vehicle, which then
tell their frames apart with a `frame_prefix` ([scenarios.md](scenarios.md#fleets)).

### `extends`

A manifest may inherit another one, named by a path relative to its own directory. The parent is
read first (recursively; a cycle is an error), then the child's sections are merged over it:
mappings merge key by key, anything else (scalars, lists) replaces. The relative paths of the
inherited `description` section are rewritten so they still point at the parent's files from the
child's directory. The example vehicle `examples/vehicles/ugv_nav2/robot.yaml` extends the
reference UGV and adds only `metadata` and `runtime`; the bridge launch
(`bridge.launch.py manifest:=examples/vehicles/ugv_nav2/robot.yaml`) resolves the same chain and
produces the same contract as for the reference model, under the vehicle's name.

### Model sections

| Section | Fields |
| --- | --- |
| `metadata` | `name` (lower case, digits, underscores), `description`, `domain` (`ground`, `aerial`, `surface`), `licence` (SPDX) |
| `description` | `xacro` (required), `urdf`, `sdf`, `model_uri`, `launch`, `meshes`, `xacro_args` (defaults the spawn and the bridge render with); paths relative to the manifest that declares them |
| `frames` | `base`, `footprint`, `odom` |
| `dimensions` | `length_m`, `width_m`, `height_m`, `ground_clearance_m`, `mass_kg` |
| `drive` | `type`, `wheel_radius_m`, `wheel_separation_m`, `joints`, `limits` (`max_linear_velocity_mps`, `max_angular_velocity_radps`, `max_linear_acceleration_mps2`, `max_angular_acceleration_radps2`) and the four interfaces `command`, `odometry` (`rate_hz`, `frame_id`, `child_frame_id`), `joint_states` (`rate_hz`, `gz_rate`: `physics` or a rate the bridge throttles from), `tf` (`rate_hz`) |
| `sensors[]` | `name`, `type`, `frame_id`, `rate_hz`, `gz_topic`, `optional`, `resolution`, `horizontal_fov_rad`, `noise` (standard deviations, SI units), `outputs[]` |
| `power` | `battery` (optional, task P2-T03): the battery as the bridge reports it on `ros_topic` (`battery/state`) as `sensor_msgs/msg/BatteryState` at `rate_hz` (1): `full_voltage_v`, `empty_voltage_v`, `capacity_ah`, `frame_id`, `qos`. The reference vehicles simulate no power system: the bridge's battery node publishes the state from these figures, full until a scenario's `battery.drain` event drains it ([bridge.md](bridge.md#adapters), [events.md](events.md#the-fault-events)) |

Every interface (`drive.*` and `sensors[].outputs[]`) carries `gz_topic`, `gz_type`, `ros_topic`,
`ros_type` and an optional `qos` block (`reliability`: `reliable` or `best_effort`; `durability`:
`volatile` or `transient_local`; `depth`; defaults reliable, volatile, 10). A sensor output adds
`ros_frame_id` (the frame the ROS messages carry, relabelled by the bridge when it differs from
the sensor's `frame_id`) and `gz_header_frame_id: false` where Gazebo leaves the header frame
empty. ROS topics are unique across the manifest; sensor names are unique.

### `runtime`

| Field | Type | Meaning |
| --- | --- | --- |
| `tier` | `T1` (default), `T2`, `T3` | fidelity tier of [ADR-0007](../adr/0007-fidelity-tiers.md); images are generated for T1 (SIL, the stack rebuilt for x86) until Phase 6 |
| `image` | string | image reference to build (`fieldproof vehicle build`) or to run as supplied |
| `build` | mapping, optional | how the image is built from the vehicle's sources (below); absent when `image` is prebuilt |
| `entrypoint` | `launch` or `command` | `launch: {package, file, args, namespace_arg, frame_prefix_arg}` becomes `ros2 launch <package> <file> key:=value ...` (booleans as `true`/`false`), the default command of the image; `namespace_arg` and `frame_prefix_arg` name the launch arguments that take the vehicle's namespace and frame prefix in a fleet (the orchestrator appends `<namespace_arg>:=ugv1`; a namespaced vehicle whose launch names none is refused at validation); `command: [...]` runs as given and reads `FIELDPROOF_NAMESPACE` and `FIELDPROOF_FRAME_PREFIX` from the environment |
| `params` | list of paths | parameter files the launch reads unchanged from the field, relative to the manifest; checked to exist, recorded for reports |
| `drivers` | list | the driver-swap map (below) |
| `ready` | mapping, optional | `timeout_s` (default 120) and `conditions[]`: `{lifecycle: <node>, state: active}` (default state), `{topic, type}`, `{service, type}`, `{action, type}`, `{node}`, `{tf, child}`; evaluated inside the container by the readiness probe (below), which is the image's `HEALTHCHECK` and what `fieldproof vehicle wait` runs; the orchestrator starts the scenario clock once every condition holds. Relative names (`bt_navigator`, `map`) resolve under the vehicle's namespace, so one manifest serves every vehicle of a fleet |
| `resources` | mapping, optional | the container budget in Docker's units, applied as cgroup limits: `cpus` (the CPU quota, `--cpus`), `cpuset` (the cores it may run on, `--cpuset-cpus`: `0-1`, `0,2`), `memory` (`2g`, `512m`), `pids`; a scenario may replace any field per vehicle (`vehicles[].resources`) |
| `dds_profile` | path, optional | the vehicle's own DDS XML profile, as deployed in the field, relative to and inside the manifest's directory (the example's `src/ugv_nav2_bringup/config/fastdds.xml`); bind-mounted read-only at `/opt/fieldproof/dds/profile.xml` by `fieldproof run` and `fieldproof vehicle run` and named to the RMW of `env` through `FASTRTPS_DEFAULT_PROFILES_FILE` (Fast DDS, the default), `CYCLONEDDS_URI` (`file://...`) or `NDDS_QOS_PROFILES`; another RMW is refused |
| `env` | mapping | extra environment of the container (`ROS_DOMAIN_ID`, `FIELDPROOF_NAMESPACE`, `FIELDPROOF_FRAME_PREFIX` and, with a discovery server, `ROS_DISCOVERY_SERVER` are set per run by the orchestrator) |
| `link` | mapping, optional | the radio link to the ground station (below) |

#### `runtime.link`

What the vehicle expects from its ground control station over the radio, and what it does
without it (task P2-T02, [network.md](network.md#a-vehicle-on-a-link)). In a scenario with radio
links, Fieldproof's ground station sends the heartbeat over the vehicle's link, so a dropped or
congested link delays or stops it, and the scenario's assertions check the documented behaviour.

| Field | Meaning |
| --- | --- |
| `heartbeat.topic` | the topic the vehicle listens on for the heartbeat (`std_msgs/msg/Header`, best effort, depth 1; relative names resolve under the vehicle's namespace: `gcs/heartbeat` is `/ugv1/gcs/heartbeat` in a fleet) |
| `heartbeat.rate_hz` | how often the ground station sends it (2 by default) |
| `loss_behaviour` | what the vehicle does when the link is lost and when it comes back: the behaviour a loss-of-link scenario asserts |

Without `heartbeat` the ground station sends the vehicle nothing; its link is still shaped and
measured.

#### `runtime.build`

| Field | Default | Meaning |
| --- | --- | --- |
| `base` | `ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:local` | base of both stages: ROS 2 Jazzy ros-base with snapshot-pinned apt sources, non-root user, `fieldproof-apt-install` ([containers.md](../setup/containers.md)) |
| `ros_distro` | `jazzy` | ROS 2 distribution of the base |
| `rosdistro_commit` | `8e9a99d2…` (2026-10-03) | commit of `ros/rosdistro` rosdep resolves keys against, so a rebuild resolves the same packages |
| `repos[]` | | repositories `vcs import` clones into `src/<name>`: `name`, `url`, `version` (a 40-digit commit SHA: branches and tags are not pins), `type: git` |
| `sources[]` | | local directories, relative to and inside the manifest's directory, copied into `src/`; the example vehicle lists `src` |
| `apt[]` | | run-time apt packages beyond what rosdep resolves |
| `rosdep_skip_keys[]` | | rosdep keys to ignore |
| `colcon_args[]` | | extra `colcon build` arguments (`--packages-skip`, ...) |

At least one of `repos` and `sources` is required.

#### The driver-swap map

Each entry of `runtime.drivers` names one hardware driver the vehicle's launch starts in the
field and the topics of the model's interface that the bridge provides instead
([bridge.md](bridge.md#the-rules)):

```yaml
drivers:
  - name: lidar
    package: rplidar_ros
    executable: rplidar_composition
    provides: [scan]
  - name: description
    package: robot_state_publisher
    provides: [/tf_static, /robot_description]
```

`provides` must name ROS topics of this manifest's interfaces (plus the bridge's own `/tf_static`,
`/robot_description` and `/clock`); a topic is provided for one driver at most. The swap itself is
the vehicle's business: the example's field launch takes `drivers:=false` and does not include its
`drivers.launch.py`, and its `package.xml` declares the driver packages as dependencies only when
built for the vehicle (`<exec_depend condition="$UGV_TARGET == field">`), so the simulation image
carries neither the drivers nor their serial-port parameters. `fieldproof manifest validate`
reports the interfaces no driver provides, which is fine for a sensor the stack does not use.

## Vehicle images

```bash
uv run --project engine fieldproof vehicle dockerfile examples/vehicles/ugv_nav2/robot.yaml -o /tmp/ugv_nav2_image
uv run --project engine fieldproof vehicle build examples/vehicles/ugv_nav2/robot.yaml      # make vehicle-image
uv run --project engine fieldproof vehicle run examples/vehicles/ugv_nav2/robot.yaml --domain-id 7 --detach
uv run --project engine fieldproof vehicle wait examples/vehicles/ugv_nav2/robot.yaml
```

| Command | Effect |
| --- | --- |
| `vehicle dockerfile MANIFEST [-o DIR]` | write the build context: `Dockerfile`, `entrypoint.sh`, the resolved `robot.yaml` the image carries at `/opt/fieldproof/robot.yaml`, and `vehicle.repos` when there are repositories (default directory `./<name>_image`) |
| `vehicle build MANIFEST [--context DIR] [-t TAG] [--base-image REF] [--no-cache] [--progress plain] [--builder NAME] [--docker-arg ARG] [--dry-run]` | write the context (a temporary directory by default) and run `docker buildx build --load` on the daemon's own builder (`default`, which sees a locally built base image; a `docker-container` builder does not) with the labels' build arguments (`GIT_SHA`, `BUILD_DATE`, `VERSION`) and the manifest's directory as the `vehicle-source` build context when there are local sources; the command line is printed first |
| `vehicle run MANIFEST [--name N] [--network fieldproof] [--domain-id N] [--namespace NS] [--frame-prefix P] [-d] [--keep] [--image REF] [-e K=V] [--docker-arg ARG] [--dry-run] [-- CMD...]` | `docker run --rm` of the image with the manifest's `resources` (`--cpus`, `--cpuset-cpus`, `--memory`, `--pids-limit`), its `env` and its `dds_profile` mounted, on the `fieldproof` network of the compose stack by default; `--namespace` and `--frame-prefix` go into the environment and, through the launch's `namespace_arg` and `frame_prefix_arg`, onto its command line; extra arguments replace the entrypoint |
| `vehicle wait MANIFEST [--name N] [--timeout S] [--interval S] [--quiet] [--dry-run]` | `docker exec` of the readiness probe in the running container (named after the vehicle by default): exit code 0 once every `runtime.ready` condition holds, 1 when the timeout (the manifest's `ready.timeout_s` by default) passes first, with the pending conditions listed |

### The generated Dockerfile

Two stages on the manifest's base image:

1. **`build`**: the ROS build tools (`build-essential`, `cmake`, `git`, colcon, rosdep, vcstool)
   from the base image's apt snapshots; rosdep's source list and `ROSDISTRO_INDEX_URL` pointed at
   the pinned `ros/rosdistro` commit, `rosdep update`; `vcs import` of `vehicle.repos` and
   `COPY --from=vehicle-source` of the local sources into `/opt/vehicle_ws/src`; `rosdep install
   --simulate` resolves the keys of every package in `src/` (a key that needs an installer other
   than apt fails the build: vendor it or skip it) and `fieldproof-apt-install` fetches the apt
   packages in parallel; the exec-only resolution is kept as a list; `colcon build
   --merge-install --install-base /opt/vehicle` in release mode with the manifest's
   `colcon_args`.
2. **`runtime`**: the base image, `/opt/vehicle` copied from the build stage, the exec
   dependencies of that list and the manifest's `apt` extras installed, the entry point (sources
   `/opt/ros/<distro>/setup.bash` and `/opt/vehicle/setup.bash`, then `exec "$@"`), the resolved
   manifest, `FIELDPROOF_VEHICLE` and `FIELDPROOF_MANIFEST` in the environment plus `runtime.env`,
   the non-root user `fieldproof`, `tini` as PID 1, the entrypoint's command line as `CMD`, and the
   OCI labels (`title` `fieldproof-vehicle-<name>`, `source`, `revision`, `created`, `version`,
   `base.name`) with `io.fieldproof.vehicle`, `io.fieldproof.tier`, `io.fieldproof.manifest-api`
   and `io.fieldproof.rosdistro-commit`.

No compiler, colcon or rosdep is left in the runtime stage. The Dockerfile passes hadolint with
the repository's configuration (`tests/vehicles/test_vehicles.sh` checks it).

### The readiness probe

`fieldproof-vehicle-ready` (`engine/src/fieldproof/vehicle_ready.py`, copied unchanged into every
image at `/usr/local/bin`) evaluates the manifest's `runtime.ready` conditions against the ROS
graph as seen from inside the container, with rclpy from `ros-base` and nothing else: it polls
every `--interval` (1 s) until every condition holds (exit 0) or `--timeout` passes (exit 1; the
manifest's `ready.timeout_s` by default), printing a line each time the set of pending conditions
changes and a final `ready` or `not ready` line (`--json` adds the evaluation as JSON, `--quiet`
drops the progress):

```text
waiting (1/10 ready): /bt_navigator lifecycle inactive, /controller_server lifecycle unresponsive, ...
waiting (8/10 ready): map->base_link tf no frames yet, /navigate_to_pose action no server
ready: 10 conditions hold after 42.3 s
```

| Condition | Holds when | Pending detail |
| --- | --- | --- |
| `lifecycle: <node>` (`state: active` by default) | the node's `get_state` service answers with the state | the state it reported; `unavailable` (no service: the node is not in the graph); `unresponsive` (no answer in 2 s, what a Nav2 server blocked in its activation looks like while it waits for TF) |
| `topic: <name>` (`type` optional) | a publisher exists, of the type when given | `no publisher`, or the types published |
| `service: <name>` (`type` optional) | the service is offered, of the type when given | `no server`, or the types offered |
| `action: <name>` (`type` optional) | an action server is offered (its `_action/send_goal` service), of the type when given | as for services |
| `node: <name>` | the node is in the graph | `absent` |
| `tf: <parent>` with `child: <frame>` | a chain of transforms seen on `/tf` or `/tf_static` (0.5 s of listening per evaluation) links the two frames, in either direction; `tf: map`, `child: base_link` holds once SLAM publishes `map` → `odom` on top of the bridge's `odom` → `base_link`, which is when Nav2 can plan (a goal sent before it is aborted) | `no frames yet`, or `no transform (N frames seen)` |

Relative names resolve against the probe's namespace: `--namespace`, or `FIELDPROOF_NAMESPACE`,
which the orchestrator sets to the vehicle's namespace in a fleet, so `lifecycle: bt_navigator`
means `/ugv2/bt_navigator` inside `ugv2`; the `tf` condition listens on the namespace's `tf`
and `tf_static` and on the global ones. With `runtime.ready` the generated
Dockerfile also declares the probe as the image's `HEALTHCHECK` (`--interval=15s --timeout=30s
--start-period=60s --retries=3`, each check a 10 s wait), so `docker inspect` reports `starting`,
then `healthy` as soon as one check passes or `unhealthy` after three failed ones past the start
period, and `docker compose up --wait` or the orchestrator can gate on it. `fieldproof vehicle
wait` runs the same probe through `docker exec` with the manifest's timeout and shows what is
still pending, which is the readable form of the same answer. A manifest without `ready` carries
the probe (it reports `ready` at once) and no `HEALTHCHECK`.

### The example vehicle `ugv_nav2`

`examples/vehicles/ugv_nav2/` is the stand-in for a real vehicle codebase: the reference UGV
running Nav2 (navigation2 1.3.13) with SLAM Toolbox (2.8.5) as installed from the Jazzy apt
snapshot, driven by the bringup package `src/ugv_nav2_bringup`:

| File | Contents |
| --- | --- |
| `robot.yaml` | extends the reference UGV's manifest; `runtime`: T1, image `fieldproof-vehicle-ugv-nav2:local` built from `src`, entrypoint `bringup.launch.py use_sim_time:=true drivers:=false` with `namespace_arg: namespace`, the two parameter files, six drivers in the swap map (base through ros2_control, description, lidar, IMU, camera, GNSS), readiness on Nav2's and SLAM Toolbox's lifecycle nodes, the `map` topic, the `map` → `base_link` transform and the `navigate_to_pose` action (relative names, resolved under the vehicle's namespace), the Fast DDS profile `config/fastdds.xml`, a budget of 2 CPUs and 2 GiB, and the radio link (`runtime.link`: the heartbeat on `gcs/heartbeat` at 2 Hz and the loss-of-link behaviour of `link_monitor`) |
| `launch/bringup.launch.py` | the field bringup: `drivers.launch.py` when `drivers:=true` (the field default), SLAM Toolbox's `online_async_launch.py` with `config/slam.yaml`, the package's `navigation.launch.py` with `config/nav2.yaml`; `use_sim_time` defaults to `false` and is passed to every node; `namespace` puts every node under it and remaps `/tf` and `/tf_static` to the namespace's (Nav2's multi-robot bringup does the same; SLAM Toolbox's own launch knows no namespace, so this file wraps it); nothing else differs between the vehicle and the simulation |
| `config/fastdds.xml` | the vehicle's DDS profile as deployed: one UDPv4 transport with 8 MB socket buffers for the camera's images and no shared-memory transport; mounted into the container unchanged (`runtime.dds_profile`) |
| `launch/navigation.launch.py` | the Nav2 servers (controller, smoother, planner, behaviors, bt_navigator, waypoint follower, velocity smoother, collision monitor) under `nav2_lifecycle_manager`, one process each, with Nav2's `cmd_vel_nav` → `cmd_vel_smoothed` → `cmd_vel_safe` remaps, then the loss-of-link failsafe `link_monitor` (`cmd_vel_safe` → `cmd_vel`), the bringup watchdog `bringup_watchdog` and, under a namespace, `config/nav2.yaml` rewritten with the namespace as its root key (`nav2_common`'s `RewrittenYaml`, what `nav2_bringup` does: a parameter file names its nodes, and `controller_server:` reaches `/controller_server` only); the vehicle's own launch rather than `nav2_bringup`'s, whose package pulls Gazebo, the TurtleBot models and the rviz plugins into the image |
| `launch/drivers.launch.py` | the hardware drivers: `robot_state_publisher`, ros2_control (`controller_manager` with `diff_drive_controller` and `joint_state_broadcaster`), `rplidar_ros`, `microstrain_inertial_driver`, `nmea_navsat_driver`, `realsense2_camera`; `config/drivers.yaml` holds their ports and frames; not started and not installed in the simulation image |
| `config/nav2.yaml`, `config/slam.yaml` | the vehicle's parameters, from the Jazzy defaults of `nav2_bringup` (without the route and docking servers) and `slam_toolbox` adapted to the platform: 0.70 m × 0.44 m footprint with `base_link` at the axle, `scan` from the 25 m lidar, `odom` from the drive, MPPI controller limited to 0.8 m/s and 1.5 rad/s, NavFn planner, SLAM Toolbox mapping with `base_link` and 0.2 m / 0.2 rad travel thresholds, its map on the relative `map` (`/ugv1/map` in a fleet, `/map` alone; slam_toolbox's default is the absolute `/map`); both costmaps wait 0.5 s for a transform, SLAM stamps `map` → `odom` 0.5 s ahead and the behaviour tree waits 1 s for a server to acknowledge a goal, so a CPU-starved simulation (software rendering in CI) does not make Nav2 abort a goal on a stale `map` → `odom` or a slow `follow_path` acknowledgement |
| `scripts/link_monitor` | the loss-of-link failsafe (rclpy, installed to `lib/ugv_nav2_bringup`): armed by the ground station's first heartbeat on `gcs/heartbeat`; after `loss_timeout_s` (2 s) without one it publishes `link_state` false and holds the vehicle with zero `cmd_vel` at 20 Hz, dropping Nav2's commands; at the next heartbeat it publishes `link_state` true and passes them through again (parameters in `config/nav2.yaml` under `link_monitor`) |
| `scripts/bringup_watchdog` | the bringup retry (rclpy, installed next to it): `nav2_lifecycle_manager` brings the servers up once and, when one transition call is lost under load, logs `Aborting bringup` and leaves them `inactive` and `unconfigured` for good (issue #32); the watchdog sees that line on `/rosout` (or, as a fallback, servers not active and unchanged for 30 s after a 90 s grace), takes every server back to `unconfigured` through its `change_state` service and asks the manager for `STARTUP` again, at most three times |
| `package.xml` | `exec_depend` on the Nav2 servers it launches, `rclpy`, `std_msgs`, `geometry_msgs` (the failsafe), `nav2_mppi_controller`, `nav2_navfn_planner`, `nav2_msgs`, `nav2_common` and `slam_toolbox`; the driver packages under `condition="$UGV_TARGET == field"` |

`make test-vehicles` runs `tests/vehicles/test_vehicles.sh`: every `robot.yaml` validates
(engine and `check-jsonschema`), the committed schema is current, the generated Dockerfile and
entry point lint, the image builds (`--no-build` to use a present one, `--static` to skip the
image), runs as `fieldproof` with the manifest, the install space, the readiness probe and its
`HEALTHCHECK`, Nav2 and SLAM Toolbox but no compiler, no `nav2_bringup`, no rviz and no driver
package, declares the launch arguments, carries the labels and the entrypoint command, and,
started with `fieldproof vehicle run` on the `fieldproof` network with the manifest's budget,
joins the ROS graph: from a second container on the same network its nodes are listed,
`slam_toolbox` is `active` and Nav2's servers answer their lifecycle services. Without the bridge
they stay `inactive`: the controller server's costmap waits in its activation for the `odom` →
`base_link` transform, so `fieldproof vehicle wait` exits 1 listing them and Docker reports the
container `unhealthy`, which is why the manifest's `ready` conditions need the whole stack.

### The integration test

`make test-integration` runs `tests/integration/test_ugv_nav2.py` on the host from the engine's
environment, driving Docker (the P1-T04 verification; also the image job's last step in CI):

1. the compose service `sim` with the depot world ([containers.md](../setup/containers.md));
2. the reference UGV spawned at the depot's `spawn_dock` frame by a one-shot container of the
   bridge image (`docker compose run bridge ros2 launch fieldproof_robots spawn_ugv_ref.launch.py
   ... robot_state_publisher:=false`);
3. the compose service `bridge` on the example vehicle's manifest
   ([bridge.md](bridge.md#the-bridge-container));
4. `fieldproof vehicle run` of the example image, then `fieldproof vehicle wait` until Nav2's
   servers and SLAM Toolbox are `active`, `/map` has a publisher and `navigate_to_pose` a server,
   and Docker reports the container `healthy`;
5. `ros2 action send_goal /navigate_to_pose` from inside the vehicle container to a point 4 m
   ahead in the `map` frame: the goal `SUCCEEDED`, and the ground-truth pose the simulator
   publishes is within 0.5 m of that point in the world (the map frame starts at the spawn pose);
6. no node of the vehicle or the bridge died.

The three containers share `ROS_DOMAIN_ID` (default 0, as with `make up`); the test stops what it
started and prints their logs on a failure (`FIELDPROOF_IT_KEEP=1` leaves them running for a
look). It needs `make images` and `make vehicle-image` first. `fieldproof run` performs the same
steps from a scenario file ([scenarios.md](scenarios.md)), and `tests/integration/test_run.py`
checks that it does.

## Known limits

- Only tier T1 images are generated; T2 (ARM64 under QEMU) and T3 (HIL) are Phase 6.
- `rosdep` keys that resolve to pip are refused rather than installed, so the image stays
  reproducible from the apt snapshots.
- `extends` rewrites the relative paths of `description` only; other relative paths (`sources`,
  `params`, `dds_profile`) are the extending manifest's own.
- One manifest describes one kind of vehicle; a fleet runs it several times under different
  namespaces from a scenario ([scenarios.md](scenarios.md#fleets)). A launch without a
  `namespace_arg` can only run at the root, hence alone.
- `dds_profile` knows the profile variable of Fast DDS, Cyclone DDS and Connext; another RMW
  needs its variable in `env` and the file mounted by hand (`--docker-arg`).
