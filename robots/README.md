# robots

Reference vehicle descriptions and manifests: the `fieldproof_robots` ROS 2 package with the
URDF/xacro models, generated SDF, meshes and `robot.yaml` manifests of the reference UGV
(P1-T01), UAVs (P4-T03) and USV (P5-T02). It builds inside the `dev` container with
`make test-ros`.

| Package | Type | Contents |
| --- | --- | --- |
| `fieldproof_robots` | `ament_cmake` | `models/<robot>/` descriptions and manifests, `launch/` spawn files; P0-T07 ships the skeleton |

See [docs/setup/workspaces.md](../docs/setup/workspaces.md).
