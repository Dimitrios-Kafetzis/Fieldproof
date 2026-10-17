# Reference vehicles

The reference vehicles are the models Fieldproof ships for scenarios that need no real vehicle
description: a UGV (task P1-T01, this page), two UAVs (P4-T03) and a USV (P5-T02). They live in
the `fieldproof_robots` package of the `robots/` workspace
([workspaces.md](../setup/workspaces.md#the-ros-workspaces-sim-robots-bridge)), one directory
per vehicle under `models/`, and are described once, as xacro URDF: `robot_state_publisher`
reads the URDF for TF, and the SDF that Gazebo spawns is generated from it at build time
([01-architecture.md](../plan/01-architecture.md#l1-physics-and-sensor-engine)). A real robot's URDF
takes the same path; the maintainer's optional input for P1-T01 was not supplied, so the
reference model stands in.

## `ugv_ref`: differential-drive UGV

A mid-size indoor/outdoor platform in the class of a Clearpath Jackal: an aluminium chassis with
a sloped nose, two drive wheels on the lateral axis through `base_link`, frictionless caster
spheres front and rear, and a sensor suite of 2D lidar, IMU, RGB-D camera and GNSS receiver.

| Property | Value |
| --- | --- |
| Footprint | 0.70 m × 0.44 m, roof 0.235 m above the ground, 7 cm ground clearance |
| Mass | 21.6 kg (chassis 18 kg, wheels 1.5 kg each, casters, sensor housings) |
| Drive | differential, wheel radius 0.11 m, wheel separation 0.48 m |
| Limits | 1.5 m/s, 3.0 rad/s, 2.0 m/s², 4.0 rad/s² (enforced by the DiffDrive system) |
| Frames | `base_link` at the drive axle (x forward, y left, z up), `base_footprint` on the ground, `odom` |
| Physics | 1 ms step in `fieldproof_empty.sdf`; collisions are primitives, the chassis visual is `meshes/chassis.obj` |

### Files

```text
robots/fieldproof_robots/
├── models/ugv_ref/
│   ├── urdf/ugv_ref.urdf.xacro   the description; inertials.xacro and sensors.xacro are its macros
│   ├── robot.yaml                the vehicle manifest (frames, drive, topics, rates, noise)
│   ├── model.config              lets `model://ugv_ref` resolve on GZ_SIM_RESOURCE_PATH
│   ├── meshes/                   chassis.obj + chassis.mtl, LICENSE.txt (Apache-2.0, original work)
│   ├── ugv_ref.urdf              generated at build time (xacro)
│   └── model.sdf                 generated at build time (gz sdf -p), SDF 1.11
├── launch/spawn_ugv_ref.launch.py
├── scripts/generate_model.py     xacro → URDF → SDF, run by the CMake function fieldproof_add_robot_model
└── test/                         test_ugv_ref.py (manifest against the SDF), test_ugv_ref_launch.py
```

The generated files are installed next to the sources in `share/fieldproof_robots/models/ugv_ref/`,
so a world can `<include><uri>model://ugv_ref</uri></include>` it and a launch file can spawn it.

### Spawning

The server must already run a world with the Sensors, Imu and NavSat systems, with headless
rendering when there is no display; `fieldproof_worlds/fieldproof_empty.sdf` is such a world:

```bash
gz sim -s -r --headless-rendering fieldproof_empty.sdf &
ros2 launch fieldproof_robots spawn_ugv_ref.launch.py            # defaults below
gz topic -t /cmd_vel -m gz.msgs.Twist -p 'linear: {x: 0.5}'      # drives it
gz topic -e -t /odom                                             # watches the odometry
```

| Launch argument | Default | Effect |
| --- | --- | --- |
| `name` | `ugv_ref` | model name in Gazebo |
| `namespace` | empty | ROS namespace of the nodes and prefix of every Gazebo topic (`/<namespace>/scan`) |
| `frame_prefix` | empty | prefix of every link and joint name (several vehicles in one TF tree) |
| `world` | `fieldproof_empty` | name of the running world |
| `x`, `y`, `z`, `yaw` | `0`, `0`, `0.12`, `0` | spawn pose of `base_link` (the axle sits at 0.11 m) |
| `gps` | `true` | fit the GNSS receiver |
| `use_sim_time` | `true` | clock of `robot_state_publisher` |
| `robot_state_publisher` | `true` | run `robot_state_publisher`; `false` when the bridge launch assembles TF ([bridge.md](bridge.md)) |

The launch file renders the xacro with these arguments and spawns the result with
`ros_gz_sim create -string`, whose exit code tells whether the server accepted the model; by
default it also publishes the description as `robot_description` through `robot_state_publisher`
(TF of the fixed frames; the wheel joints follow the bridged joint states). Gazebo converts the
URDF with the same parser that generates `model.sdf` at build time, so both paths produce the same
model. The xacro arguments `lidar_rate`, `imu_rate`,
`camera_rate`, `gps_rate`, `camera_width` and `camera_height` are available to `xacro` directly;
their defaults are the rates declared in the manifest.

### Interfaces

Topic names are relative to the vehicle namespace (`scan` is `/scan` with the default empty
namespace, `/ugv1/scan` with `namespace:=ugv1`). The ROS side is the contract the bridge
implements ([bridge.md](bridge.md#the-contract-of-the-reference-ugv)); the Gazebo side is what
the model publishes.

| Sensor | Gazebo topic and type | Rate | `frame_id` | ROS topic and type |
| --- | --- | --- | --- | --- |
| drive command | `cmd_vel` `gz.msgs.Twist` (input) | | | `cmd_vel` `geometry_msgs/msg/Twist` |
| odometry (DiffDrive) | `odom` `gz.msgs.Odometry` | 50 Hz | `odom` → `base_link` | `odom` `nav_msgs/msg/Odometry` |
| transform (DiffDrive) | `tf` `gz.msgs.Pose_V` | 50 Hz | `odom` → `base_link` | `/tf` `tf2_msgs/msg/TFMessage` (global) |
| joint states | `joint_states` `gz.msgs.Model` | physics rate (1 kHz) | | `joint_states` `sensor_msgs/msg/JointState` at 50 Hz (throttled by the bridge) |
| `lidar` (`gpu_lidar`) | `scan` `gz.msgs.LaserScan` | 10 Hz | `lidar_link` | `scan` `sensor_msgs/msg/LaserScan` |
| `imu` (`imu`) | `imu` `gz.msgs.IMU` | 100 Hz | `imu_link` | `imu/data` `sensor_msgs/msg/Imu` |
| `camera` (`rgbd_camera`) | `camera/image`, `camera/depth_image` `gz.msgs.Image`; `camera/camera_info` `gz.msgs.CameraInfo`; `camera/points` `gz.msgs.PointCloudPacked` | 15 Hz | `camera_link` | `camera/image`, `camera/depth/image`, `camera/camera_info` in `camera_optical_link`; `camera/points` `sensor_msgs/msg/PointCloud2` in `camera_link` |
| `gps` (`navsat`, optional) | `gps/fix` `gz.msgs.NavSat` | 5 Hz | none in the header | `gps/fix` `sensor_msgs/msg/NavSatFix` in `gps_link` |

The chassis also carries a **ground-truth contact sensor** (`<sensor name="contact"
type="contact">` on the `base_link_collision`, Gazebo topic `contacts` under the namespace, 50 Hz,
task P1-T06). It is not an interface of the vehicle: nothing bridges it to ROS and the manifest's
`sensors` list leaves it out; it is the simulator's report of what the body hit, which the
scenario runner's `no_collision` assertion reads ([assertions.md](assertions.md#no_collision)).
The casters and wheels are separate links, so any contact of the chassis collision is a collision
with the world. The depot and `fieldproof_empty.sdf` load the Contact system it needs.

Gazebo's camera convention is x forward, so image and depth data are labelled with the physical
`camera_link`; the URDF adds the ROS optical frame `camera_optical_link` (z forward, x right,
y down) as its child, and the bridge relabels image and camera info with it. The point cloud is
expressed in `camera_link` and keeps that frame. Gazebo Harmonic's NavSat system publishes the
fix without a `frame_id`; the bridge's adapter sets `gps_link`. Every ROS output may carry a `qos`
block (the camera outputs are reliable with a queue of 5, like image_transport's default), and
`gz_rate` marks an interface Gazebo publishes faster than the declared `rate_hz`.

### Sensor models

| Sensor | Model | Noise (gaussian) |
| --- | --- | --- |
| 2D lidar | 720 beams over 360°, 0.10 to 25 m, 1 cm resolution (SICK TiM / RPLIDAR class) | range σ 0.01 m |
| IMU | 6-axis MEMS (BMI088 / ICM-42688 class), orientation enabled | gyro σ 0.009 rad/s, bias σ 0.00075 rad/s; accelerometer σ 0.021 m/s², bias σ 0.005 m/s²; slowly varying bias with a 400 s correlation time |
| RGB-D camera | 640 × 480, 69° horizontal field of view, RGB 0.1 to 10 m, depth 0.2 to 8 m (RealSense D435 class) | pixel σ 0.007 |
| GNSS | single band without corrections, position and velocity | horizontal σ 1.0 m, vertical σ 2.0 m, velocity σ 0.1 m/s |

The figures approximate mid-range commercial units; P6-T03 replaces them with values calibrated
from field logs. Every parameter is a xacro macro argument in `sensors.xacro`.

### Manifest (`robot.yaml`)

The manifest is the contract between the model and the rest of the stack and the input of the bridge
generator (`fieldproof bridge gen`, [bridge.md](bridge.md)). P1-T04 formalised the format as
manifest schema v0 ([manifest.md](manifest.md)): `fieldproof manifest validate` checks this file,
and a vehicle manifest such as `examples/vehicles/ugv_nav2/robot.yaml` `extends` it to add the
`runtime` section (image, entrypoint, driver-swap map, readiness, budget). The model sections, as
shipped with P1-T01 (`apiVersion: fieldproof.dev/v0alpha1`, `kind: Robot`):

| Section | Contents |
| --- | --- |
| `metadata` | `name`, `description`, `domain` (`ground`), `licence` |
| `description` | the xacro, generated URDF and SDF, `model_uri`, launch file, meshes, and the xacro argument defaults |
| `frames` | `base`, `footprint`, `odom` |
| `dimensions` | length, width, height, ground clearance, mass |
| `drive` | `type`, wheel radius and separation, `joints`, `limits`, and the `command`, `odometry`, `joint_states`, `tf` interfaces (Gazebo and ROS topic and type, rate, frames, `qos`; `gz_rate: physics` on the joint states) |
| `sensors[]` | `name`, `type`, `frame_id`, `rate_hz`, `gz_topic`, `noise`, `optional`, `outputs[]` (Gazebo topic and type, ROS topic, type and frame, `qos`; `gz_header_frame_id: false` where Gazebo leaves the header frame empty) |

`test/test_ugv_ref.py` checks the manifest against the generated SDF and URDF: every sensor,
topic, rate, frame, limit and xacro default declared here is what the model carries.

### Tests

`make test-ros` (CI's image job) builds the package and runs, through CTest:

| Test | Checks |
| --- | --- |
| `xmllint` | `package.xml` |
| `urdf_check_ugv_ref` | `check_urdf` parses the generated URDF (one tree rooted at `base_link`) |
| `sdf_check_ugv_ref` | `gz sdf --check` accepts the generated `model.sdf` |
| `test_ugv_ref` | pytest: installed files, mesh URIs and licence, manifest against SDF and URDF, noise on every interface sensor, the chassis contact sensor on the chassis collision only, xacro defaults, `gps:=false` and `namespace:=ugv1` renderings |
| `ugv_ref_launch` | `launch_testing`: starts `gz sim -s -r --headless-rendering fieldproof_empty.sdf`, spawns through the launch file, measures every output's rate in simulation time against the manifest (±10 %), checks the header frames and joint names, drives the robot one metre on `/cmd_vel` and compares the odometry delta with the ground-truth pose (within 10 cm, no heading change), checks `/tf_static` for every sensor frame, and after shutdown that `create` exited 0 and the server resolved every resource |

The launch test runs in its own Gazebo partition and ROS domain, so a stack started with
`make up` on the same host is neither disturbed nor observed. It takes about ten seconds with a
GPU render node and under a minute with software rendering.

### Known warnings

- `gz sdf -p` and the spawn print `XML Element[gz_frame_id] ... not defined in SDF. Copying`
  for each sensor: `gz_frame_id` is the element Gazebo Harmonic reads for the message frame,
  and it is not part of the SDF 1.11 specification, so sdformat copies it through with a warning.
- `robot_state_publisher` warns that the root link `base_link` has an inertia: KDL cannot use
  it, but nothing in Fieldproof relies on KDL dynamics. Keeping `base_link` as the root keeps
  the Gazebo link name, the odometry child frame and the TF root identical.
