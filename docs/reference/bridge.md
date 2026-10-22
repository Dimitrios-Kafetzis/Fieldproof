# Bridge contract

The bridge (layer L2, [01-architecture.md](../plan/01-architecture.md#l2-bridge),
[ADR-0002](../adr/0002-ros2-jazzy-primary-ros1-via-bridge.md)) gives a simulated vehicle the ROS 2
interface its real drivers have: the same topics, types, frames, rates and QoS. Task P1-T03 built
it as a generator that reads the vehicle manifest (`robot.yaml`,
[robots.md](robots.md#manifest-robotyaml)) and writes a **bundle**: the configuration of the
ros_gz `parameter_bridge`, the parameters of the adapter nodes that reshape what the bridge cannot,
the **contract** the stack must honour, and a launch file. Everything lives in the
`fieldproof_bridge` package of the `bridge/` workspace
([workspaces.md](../setup/workspaces.md#the-ros-workspaces-sim-robots-bridge)).

## Generating a bundle

```bash
uv run --project engine fieldproof bridge gen robots/fieldproof_robots/models/ugv_ref/robot.yaml \
    --out /tmp/ugv_ref_bridge --namespace ugv1
# the same, inside the dev container, from the installed ROS package
ros2 run fieldproof_bridge bridge_gen robot.yaml -o ugv_ref_bridge -n ugv1
```

| Option | Default | Effect |
| --- | --- | --- |
| `--out`, `-o` | `./<robot>_bridge` | directory the bundle is written to |
| `--namespace`, `-n` | empty | ROS namespace of the vehicle; prefixes every topic on both sides (`/ugv1/scan`) |
| `--frame-prefix` | empty | prefix of every frame in the contract and the adapters (several vehicles in one TF tree) |
| `--no-clock` | bridge `/clock` | do not bridge the simulation clock |
| `--no-robot-state-publisher` | run it | do not run `robot_state_publisher` from the manifest's description |
| `--without SENSOR` | none | leave an optional sensor out (its xacro argument of the same name is set to false) |

Both commands run one generator, `fieldproof_bridge.generator`: the engine installs the ROS
package from `bridge/fieldproof_bridge` (a uv path dependency), so a bundle written on the host
and one written in the bridge container are identical. A bad manifest exits with code 2 and the
reason (`drive.odometry: missing rate_hz`).

### The bundle

| File | Contents |
| --- | --- |
| `ros_gz_bridge.yaml` | `parameter_bridge` configuration of the main instance: one entry per topic (`ros_topic_name`, `gz_topic_name`, both types, `direction`) |
| `ros_gz_bridge_<frame>.yaml` | one more instance per relabelled frame (`override_frame_id`), for the camera outputs of the UGV |
| `params.yaml` | ROS parameters keyed by node name: QoS overrides of every bridged topic, `override_frame_id`, the adapter settings |
| `bridge.yaml` | what the launch starts: namespace, nodes with their kind and configuration, the description (xacro and arguments) for `robot_state_publisher`, the hidden topics |
| `contract.yaml` | the ROS interface: for every topic its type, role, frame, rate and QoS, and the node that provides it |
| `bridge.launch.py` | `ros2 launch <bundle>/bridge.launch.py` starts the bundle through the package launch file |

Paths in `bridge.yaml` are absolute, so a bundle is launched where it was generated (or
regenerated at launch time, below).

### The rules

Applied to every interface of the manifest (`drive.command`, `drive.odometry`,
`drive.joint_states`, `drive.tf` and each `sensors[].outputs[]`):

- **Names** are fully qualified with the namespace on both sides. A ROS topic the manifest writes
  with a leading slash is global. The TF topics follow the drive's `tf` interface (task P2-T01):
  relative, as the reference vehicles write it, the bridge publishes `/<namespace>/tf` and
  `robot_state_publisher` is remapped to `/<namespace>/tf` and `/<namespace>/tf_static`, so each
  vehicle of a fleet has its own tree with the usual frame names (Nav2's multi-robot
  convention); `/tf` keeps one tree shared by every vehicle, which tell their frames apart with
  `frame_prefix` (REP 105). The bundle's `bridge.yaml` records the two topics under `tf`.
- **QoS** is the output's `qos` block (`reliability`, `durability`, `depth`; defaults reliable,
  volatile, 10, what the ros_gz bridge publishes with). Direct bridge topics get it through
  rclcpp's `qos_overrides.<topic>.publisher.*` parameters, adapter outputs through the adapter's
  `qos_*` parameters. The drive command's QoS applies to the bridge's subscription.
- **Relabelling**: an output whose `ros_frame_id` differs from the sensor's `frame_id` goes
  through one more `parameter_bridge` instance per frame with `override_frame_id`, so no message
  is copied in Python (the UGV's images and camera info move from `camera_link`, Gazebo's x-forward
  camera convention, to the optical frame `camera_optical_link`; the point cloud stays in
  `camera_link`).
- **Adapters**: an IMU output gets the `imu_adapter`, a GNSS fix the `navsat_adapter`, an
  interface with `gz_rate` the `throttle`, every other output of an injectable type (a laser
  scan, an odometry, joint states; not the camera topics) the `relay`. The bridge then publishes
  on a hidden topic, `<namespace>/_bridge/<topic>` (not shown by `ros2 topic list`), which the
  adapter reads; the adapter publishes the contract topic with the declared QoS and frame. Every
  adapter is a fault injector (task P2-T03, [Adapters](#adapters)).
- **The command relay**: the drive command (`geometry_msgs/msg/Twist`) goes through the
  `command_relay`: the vehicle publishes the contract topic, the relay republishes on the
  hidden topic the bridge forwards to Gazebo, and the `actuator.limit` event caps the speeds
  in between.
- **The battery**: a manifest with a `power.battery` section gets the `battery` node, which
  publishes the synthetic battery state the `battery.drain` event drains.
- **Clock**: `/clock` (`rosgraph_msgs/msg/Clock` from `gz.msgs.Clock`) is bridged by the main
  instance unless disabled; the adapters and `robot_state_publisher` run with `use_sim_time`.
- **TF assembly**: `robot_state_publisher` runs on the manifest's xacro rendered with the same
  arguments as the spawn (namespace, frame prefix, the manifest's `xacro_args`), publishing
  `/tf_static` for the fixed frames and `/tf` for the joints from the bridged `joint_states`; the
  drive system's `odom -> base_link` transform arrives through the bridge (`gz.msgs.Pose_V` to
  `tf2_msgs/msg/TFMessage`). The spawn launch of the UGV therefore takes
  `robot_state_publisher:=false` when the bridge runs.

## Adapters

rclpy nodes in `fieldproof_bridge/adapters/`, started by the launch file with the parameters the
generator derived from the manifest (`ros2 run fieldproof_bridge <name> --ros-args -p ...` runs
one by hand). All take `input`, `output`, `qos_reliability`, `qos_durability`, `qos_depth`, and
every one is a **fault injector** (task P2-T03): it carries dynamic parameters a scenario's
timeline sets at run time through the fault events ([events.md](events.md#the-fault-events)) and
applies them to every message it republishes. The faults every injector offers: `dropout` (publish
nothing while true: the sensor is silent) and `freeze` (repeat the last message as it was, stamp
included: the sensor hangs). The vehicle's stack sees the injector's output as the sensor's
topic, so a fault reaches it the way a failing driver would, and its own container is not touched.

| Node | What it changes | Parameters from the manifest | Fault parameters |
| --- | --- | --- | --- |
| `imu_adapter` | the covariance diagonals (`stddev²`) of angular velocity and linear acceleration, which the bridge leaves at zero ("unknown") and a localisation stack reads; the orientation covariance stays unknown unless an `orientation_stddev` is given; the frame, if relabelled | `noise.gyro_stddev_radps`, `noise.accel_stddev_mps2`, `noise.orientation_stddev_rad` | `gyro_bias_radps`, `accel_bias_mps2` (three components each, added to every message: `imu.bias`) |
| `navsat_adapter` | the frame (Gazebo Harmonic's NavSat system sets none, gz-sim issue 2236), the position covariance (`DIAGONAL_KNOWN` from the horizontal and vertical stddev), `status.status = STATUS_FIX`, `status.service` (GPS) | `noise.horizontal_stddev_m`, `noise.vertical_stddev_m` | `deny` (the receiver reports no fix: `STATUS_NO_FIX`, NaN position, unknown covariance: `gps.deny`), `offset_enu_m` (metres east, north and up added to every fix at its latitude: `gps.offset`) |
| `throttle` | passes one message per period judged by the header stamps (simulation time), for interfaces Gazebo publishes faster than a real driver: the JointStatePublisher system publishes every physics step (1 kHz) and has no rate setting, the UGV's manifest declares 50 Hz | `gz_rate`, `rate_hz`, `ros_type` | |
| `relay` | republishes the topic as it is (the frame relabelled if the manifest asks), for every other output of an injectable type (laser scans, odometry, joint states, ranges, magnetic field, pressure, temperature, stamped twists and poses): small messages at up to 100 Hz | `ros_type`, `ros_frame_id` | |
| `command_relay` | the drive command on its way to the simulator: the vehicle publishes the contract topic (`cmd_vel`), the relay republishes on the hidden topic the bridge forwards to Gazebo, clamping the linear and angular speeds to the limits | | `max_linear_mps`, `max_angular_radps` (0: no limit; `actuator.limit`) |
| `battery` | a synthetic `sensor_msgs/msg/BatteryState` from the manifest's `power.battery` ([manifest.md](manifest.md#model-sections)): 100 % and the full voltage until a drain makes the percentage fall, linearly in simulation time, to a target over a duration; the voltage follows between empty and full, the status is `DISCHARGING` while the drain runs, `FULL` at 100 % and `NOT_CHARGING` otherwise | `rate_hz`, `full_voltage_v`, `empty_voltage_v`, `capacity_ah`, `frame_id` | `percentage` (set the state at once), `drain_to` (0 to 1) then `drain_duration_s` (which starts the ramp: `battery.drain`) |

The Python adapters handle small messages at up to 1 kHz; the camera path stays in C++ through
the relabelling bridge instance, as [04-engineering-standards.md](../plan/04-engineering-standards.md#languages-and-versions)
asks for rates above 100 Hz, so the camera topics have no injector. `bridge.yaml` records the
contract `topic` and the manifest `sensor` (or drive interface, or `battery`) each injector
serves, which is how the engine finds the node a fault event sets (`Bundle.injector`).

## The launch file

```bash
ros2 launch fieldproof_bridge bridge.launch.py manifest:=/path/to/robot.yaml namespace:=ugv1
ros2 launch fieldproof_bridge bridge.launch.py bundle:=/tmp/ugv_ref_bridge
ros2 launch /tmp/ugv_ref_bridge/bridge.launch.py
```

| Argument | Default | Effect |
| --- | --- | --- |
| `manifest` | | generate the bundle at launch time (into a temporary directory, or `bundle_out`) from this manifest |
| `bundle` | | start this bundle as written; the other arguments are then taken from it |
| `bundle_out` | | write the bundle generated from `manifest` here, where a contract check can find it (`fieldproof run` passes `/tmp/fieldproof-bridge`) |
| `namespace`, `frame_prefix`, `clock`, `robot_state_publisher`, `without` | as the generator | generator options for `manifest:=` (`without` takes a comma-separated list; `fieldproof run` passes `clock:=false` to every bridge of a fleet but the first, so `/clock` has one publisher) |
| `use_sim_time` | `true` | clock of the adapters and `robot_state_publisher` |

The launch reads `bridge.yaml` and starts one `parameter_bridge` per configuration file (each with
`params.yaml` and its `config_file`), one adapter per hidden topic and `robot_state_publisher`,
all under the bundle's namespace.

### The bridge container

The bridge of a vehicle runs as its own container (task P1-T04, [ADR-0004](../adr/0004-one-container-per-vehicle.md)):
the image `fieldproof-bridge` (`docker/bridge/Dockerfile`) is the simulation image plus this
package and `fieldproof_robots` built into `/opt/fieldproof_bridge`, and its default command
`fieldproof-bridge` runs the launch above with `manifest:=$FIELDPROOF_MANIFEST` (extra arguments
go to the launch). The compose service `bridge` sets the manifest from
`FIELDPROOF_VEHICLE_MANIFEST` (default: the example vehicle) and bind-mounts the repository
read-only at `/workspace`, so the manifest's `extends` chain and its description resolve
([containers.md](../setup/containers.md#compose-profiles-and-networking)):

```bash
docker compose -f docker/compose.yaml --profile bridge up -d bridge          # the example vehicle
FIELDPROOF_VEHICLE_MANIFEST=/workspace/robots/fieldproof_robots/models/ugv_ref/robot.yaml \
    docker compose -f docker/compose.yaml --profile bridge up -d bridge      # the reference model
docker run --rm --network fieldproof -e GZ_PARTITION=fieldproof -v "$PWD:/workspace:ro" \
    -e FIELDPROOF_MANIFEST=/workspace/examples/vehicles/ugv_nav2/robot.yaml \
    ghcr.io/dimitrios-kafetzis/fieldproof-bridge:local                         # without compose
```

The same image spawns the vehicle model before the bridge starts (`docker compose run --rm
bridge ros2 launch fieldproof_robots spawn_ugv_ref.launch.py ... robot_state_publisher:=false`),
which is what the integration test does
([manifest.md](manifest.md#the-integration-test)); the scenario runner of P1-T05 drives both
through the Docker API ([scenarios.md](scenarios.md#running-a-scenario)).

## The contract of the reference UGV

`fieldproof bridge gen` on `ugv_ref/robot.yaml` with the default (empty) namespace yields this
contract; with `--namespace ugv1` every name except `/clock` gains the `/ugv1` prefix (`/tf`
and `/tf_static` included, since the reference manifest's `tf` interface is relative).

| Topic | Type | Role | Frame | Rate | QoS | Provided by |
| --- | --- | --- | --- | --- | --- | --- |
| `/cmd_vel` | `geometry_msgs/msg/Twist` | subscriber | | | reliable, 10 | `ros_gz_bridge` |
| `/odom` | `nav_msgs/msg/Odometry` | publisher | `odom` → `base_link` | 50 Hz | reliable, 10 | `ros_gz_bridge` |
| `/joint_states` | `sensor_msgs/msg/JointState` | publisher | | 50 Hz | reliable, 10 | `joint_states_throttle` |
| `/tf` (`/ugv1/tf` under a namespace) | `tf2_msgs/msg/TFMessage` | publisher | `odom` → `base_link` | 50 Hz | reliable, 100 | `ros_gz_bridge`, `robot_state_publisher` |
| `/scan` | `sensor_msgs/msg/LaserScan` | publisher | `lidar_link` | 10 Hz | reliable, 10 | `ros_gz_bridge` |
| `/imu/data` | `sensor_msgs/msg/Imu` | publisher | `imu_link` | 100 Hz | reliable, 10 | `imu_adapter` |
| `/camera/image` | `sensor_msgs/msg/Image` | publisher | `camera_optical_link` | 15 Hz | reliable, 5 | `ros_gz_bridge_camera_optical_link` |
| `/camera/camera_info` | `sensor_msgs/msg/CameraInfo` | publisher | `camera_optical_link` | 15 Hz | reliable, 5 | `ros_gz_bridge_camera_optical_link` |
| `/camera/depth/image` | `sensor_msgs/msg/Image` | publisher | `camera_optical_link` | 15 Hz | reliable, 5 | `ros_gz_bridge_camera_optical_link` |
| `/camera/points` | `sensor_msgs/msg/PointCloud2` | publisher | `camera_link` | 15 Hz | reliable, 5 | `ros_gz_bridge` |
| `/gps/fix` | `sensor_msgs/msg/NavSatFix` | publisher | `gps_link` | 5 Hz | reliable, 10 | `gps_adapter` |
| `/clock` | `rosgraph_msgs/msg/Clock` | publisher | | | reliable, 10 | `ros_gz_bridge` |
| `/tf_static` (`/ugv1/tf_static` under a namespace) | `tf2_msgs/msg/TFMessage` | publisher | children `base_footprint`, `lidar_link`, `imu_link`, `camera_link`, `camera_optical_link`, `gps_link` | | reliable, transient local, 1 | `robot_state_publisher` |
| `/robot_description` | `std_msgs/msg/String` | publisher | | | reliable, transient local, 1 | `robot_state_publisher` |

The hidden topics `/_bridge/joint_states`, `/_bridge/imu/data` and `/_bridge/gps/fix` carry the
bridge's raw output to the adapters. Nothing else is visible under the namespace. The camera
outputs are reliable with a queue of 5, the default profile of image_transport publishers: under
best effort the UDP fragments of these 1 to 5 MB messages get lost at the default socket buffers
(the contract test measured 13 Hz of 15 on the cloud locally and 9 Hz of 15 on the images on a
loaded CI runner before the change). The `qos` block of the manifest still allows best effort
where a driver uses it.

## Checking a running stack

```bash
ros2 run fieldproof_bridge contract_check /tmp/ugv_ref_bridge [--timeout 300] [--samples 10] \
    [--tolerance 0.1] [--render-tolerance 0.5]
```

`fieldproof_bridge.contract.ContractChecker` subscribes to every published topic of
`contract.yaml` and reports one finding per check; the command prints them and exits 1 on a
failure. `fieldproof run --check-contracts` runs it inside every bridge container of a run on
the bundle the bridge wrote (`bundle_out`), once the bridge is up and before the recorder
starts, and records the counts in `run.json` ([scenarios.md](scenarios.md#fleets)):

| Check | Passes when |
| --- | --- |
| `graph` | a publisher (or, for the drive command, a subscription) of the contract's type exists |
| `qos` | every endpoint has the contract's reliability and durability (the graph does not report the depth) |
| `frame` | every message carries the contract's `frame_id` (and `child_frame_id`); for a TF topic, every transform listed in `frames` arrives |
| `rate` | the rate over the header stamps (simulation time; the first stamp is discarded because Gazebo publishes it when the sensor is created, off its grid) is within the tolerance of `rate_hz`, per transform for TF; the rendering sensors (images, camera info, point clouds, laser scans) take `--render-tolerance` when given, since under software rendering on a loaded CI runner their thread falls behind the physics even in simulation time (the contract test reads `FIELDPROOF_CONTRACT_RENDER_TOLERANCE`, which the CI workflow sets to 0.5; issue #69) |
| `topics` | no topic other than the contract's (and `/parameter_events`, `/rosout`) is visible under the namespace |

### The contract test

`bridge/fieldproof_bridge/test/test_bridge_contract_launch.py` is the P1-T03 verification, run by
`colcon test` through the `launch_testing` pytest plugin (`make test-ros`, CI's image job). It
starts `gz sim -s -r --headless-rendering depot.sdf` ([worlds.md](worlds.md)), spawns the UGV at
the world's `spawn_dock` frame through `spawn_ugv_ref.launch.py` with
`robot_state_publisher:=false`, starts `bridge.launch.py` on a bundle generated from the
installed UGV manifest, and checks, from an rclpy node in the test process:

1. the contract checker reports no failure (every topic above: type, QoS, frame, rate, nothing
   extra);
2. `/imu/data` carries the covariance of the manifest's noise figures and `/gps/fix` the frame,
   covariance, status and service of a real receiver;
3. a `geometry_msgs/msg/Twist` on `/cmd_vel` drives the robot half a metre along +x (the ROS to
   Gazebo direction) as seen on `/odom`;
4. after shutdown, `create` exited 0 and the server resolved every resource.

The test runs in its own Gazebo partition and ROS domain; the bridge workspace is built after
`sim` and `robots`, whose install spaces it needs.

### Unit tests

`test_generator.py` checks the manifest model (every interface of the UGV, the validation errors
of a minimal manifest) and the generator (node list, bridge entries, QoS overrides, adapter
parameters, the contract, namespacing and frame prefixes, optional sensors, the written files and
the command line); `test_adapters.py` checks the message filling and the throttle with real ROS
messages. The engine's `tests/test_bridge_cli.py` runs `fieldproof bridge gen` on the UGV manifest
on the host.

## Known limits

- `override_frame_id` applies to a whole bridge instance, hence one instance per relabelled frame.
- The `qos` block covers reliability, durability and depth; deadline, lifespan and liveliness stay
  at the ROS defaults.
- The camera topics (images, camera info, point clouds) stay on the C++ path and take no fault:
  `sensor.dropout` and `sensor.freeze` refuse the camera. The battery is synthetic (no power
  model in the simulation): its state is what the manifest's figures and a drain say.
- The bridge is one ros_gz bridge per vehicle; in a fleet the first vehicle's bridge publishes
  `/clock` and the others are started with `clock:=false`, so a run whose first vehicle's bridge
  dies loses the clock (the run fails at the next liveness check).
