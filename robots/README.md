# robots

Reference vehicle descriptions and manifests: the `fieldproof_robots` ROS 2 package with the
URDF/xacro models, generated SDF, meshes and `robot.yaml` manifests of the reference UGV
(P1-T01), UAVs (P4-T03) and USV (P5-T02). It builds inside the `dev` container with
`make test-ros`, on top of the `sim` workspace (its launch test spawns into a `fieldproof_worlds`
world).

| Package | Type | Contents |
| --- | --- | --- |
| `fieldproof_robots` | `ament_cmake` | `models/<robot>/` descriptions (xacro, generated URDF and SDF, meshes, `robot.yaml`), `launch/` spawn files, `scripts/generate_model.py` |

`models/ugv_ref/` is the differential-drive UGV: 2D lidar, IMU, RGB-D camera and GNSS, Gazebo
DiffDrive and JointStatePublisher systems, spawned by
`ros2 launch fieldproof_robots spawn_ugv_ref.launch.py`. See
[docs/reference/robots.md](../docs/reference/robots.md) and
[docs/setup/workspaces.md](../docs/setup/workspaces.md).
