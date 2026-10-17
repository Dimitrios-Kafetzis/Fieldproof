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
  with a leading slash (`/tf`) is global: every vehicle shares one TF tree and tells its frames
  apart with `frame_prefix` (REP 105).
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
  interface with `gz_rate` the `throttle`. The bridge then publishes on a hidden topic,
  `<namespace>/_bridge/<topic>` (not shown by `ros2 topic list`), which the adapter reads; the
  adapter publishes the contract topic with the declared QoS and frame.
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
one by hand). All take `input`, `output`, `qos_reliability`, `qos_durability`, `qos_depth`.

| Node | What it changes | Parameters from the manifest |
| --- | --- | --- |
| `imu_adapter` | the covariance diagonals (`stddev²`) of angular velocity and linear acceleration, which the bridge leaves at zero ("unknown") and a localisation stack reads; the orientation covariance stays unknown unless an `orientation_stddev` is given; the frame, if relabelled | `noise.gyro_stddev_radps`, `noise.accel_stddev_mps2`, `noise.orientation_stddev_rad` |
| `navsat_adapter` | the frame (Gazebo Harmonic's NavSat system sets none, gz-sim issue 2236), the position covariance (`DIAGONAL_KNOWN` from the horizontal and vertical stddev), `status.status = STATUS_FIX`, `status.service` (GPS) | `noise.horizontal_stddev_m`, `noise.vertical_stddev_m` |
| `throttle` | passes one message per period judged by the header stamps (simulation time), for interfaces Gazebo publishes faster than a real driver: the JointStatePublisher system publishes every physics step (1 kHz) and has no rate setting, the UGV's manifest declares 50 Hz | `gz_rate`, `rate_hz`, `ros_type` |

The Python adapters handle small messages at up to 1 kHz; the camera path stays in C++ through
the relabelling bridge instance, as [04-engineering-standards.md](../plan/04-engineering-standards.md#languages-and-versions)
asks for rates above 100 Hz.

## The launch file

```bash
ros2 launch fieldproof_bridge bridge.launch.py manifest:=/path/to/robot.yaml namespace:=ugv1
ros2 launch fieldproof_bridge bridge.launch.py bundle:=/tmp/ugv_ref_bridge
ros2 launch /tmp/ugv_ref_bridge/bridge.launch.py
```

| Argument | Default | Effect |
| --- | --- | --- |
| `manifest` | | generate the bundle at launch time (into a temporary directory) from this manifest |
| `bundle` | | start this bundle as written; the other arguments are then taken from it |
| `namespace`, `frame_prefix`, `clock`, `robot_state_publisher`, `without` | as the generator | generator options for `manifest:=` (`without` takes a comma-separated list) |
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
through the Docker API.

## The contract of the reference UGV

`fieldproof bridge gen` on `ugv_ref/robot.yaml` with the default (empty) namespace yields this
contract; with `--namespace ugv1` every name except `/tf`, `/tf_static` and `/clock` gains the
`/ugv1` prefix.

| Topic | Type | Role | Frame | Rate | QoS | Provided by |
| --- | --- | --- | --- | --- | --- | --- |
| `/cmd_vel` | `geometry_msgs/msg/Twist` | subscriber | | | reliable, 10 | `ros_gz_bridge` |
| `/odom` | `nav_msgs/msg/Odometry` | publisher | `odom` → `base_link` | 50 Hz | reliable, 10 | `ros_gz_bridge` |
| `/joint_states` | `sensor_msgs/msg/JointState` | publisher | | 50 Hz | reliable, 10 | `joint_states_throttle` |
| `/tf` | `tf2_msgs/msg/TFMessage` | publisher | `odom` → `base_link` | 50 Hz | reliable, 100 | `ros_gz_bridge`, `robot_state_publisher` |
| `/scan` | `sensor_msgs/msg/LaserScan` | publisher | `lidar_link` | 10 Hz | reliable, 10 | `ros_gz_bridge` |
| `/imu/data` | `sensor_msgs/msg/Imu` | publisher | `imu_link` | 100 Hz | reliable, 10 | `imu_adapter` |
| `/camera/image` | `sensor_msgs/msg/Image` | publisher | `camera_optical_link` | 15 Hz | reliable, 5 | `ros_gz_bridge_camera_optical_link` |
| `/camera/camera_info` | `sensor_msgs/msg/CameraInfo` | publisher | `camera_optical_link` | 15 Hz | reliable, 5 | `ros_gz_bridge_camera_optical_link` |
| `/camera/depth/image` | `sensor_msgs/msg/Image` | publisher | `camera_optical_link` | 15 Hz | reliable, 5 | `ros_gz_bridge_camera_optical_link` |
| `/camera/points` | `sensor_msgs/msg/PointCloud2` | publisher | `camera_link` | 15 Hz | reliable, 5 | `ros_gz_bridge` |
| `/gps/fix` | `sensor_msgs/msg/NavSatFix` | publisher | `gps_link` | 5 Hz | reliable, 10 | `gps_adapter` |
| `/clock` | `rosgraph_msgs/msg/Clock` | publisher | | | reliable, 10 | `ros_gz_bridge` |
| `/tf_static` | `tf2_msgs/msg/TFMessage` | publisher | children `base_footprint`, `lidar_link`, `imu_link`, `camera_link`, `camera_optical_link`, `gps_link` | | reliable, transient local, 1 | `robot_state_publisher` |
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
ros2 run fieldproof_bridge contract_check /tmp/ugv_ref_bridge [--timeout 300] [--samples 10] [--tolerance 0.1]
```

`fieldproof_bridge.contract.ContractChecker` subscribes to every published topic of
`contract.yaml` and reports one finding per check; the command prints them and exits 1 on a
failure:

| Check | Passes when |
| --- | --- |
| `graph` | a publisher (or, for the drive command, a subscription) of the contract's type exists |
| `qos` | every endpoint has the contract's reliability and durability (the graph does not report the depth) |
| `frame` | every message carries the contract's `frame_id` (and `child_frame_id`); for a TF topic, every transform listed in `frames` arrives |
| `rate` | the rate over the header stamps (simulation time, so a slow renderer cannot fail it; the first stamp is discarded because Gazebo publishes it when the sensor is created, off its grid) is within the tolerance of `rate_hz`, per transform for TF |
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
- The bridge is one ros_gz bridge per vehicle; sharing `/clock` between several vehicles and
  the per-run topology are P2-T01's.
