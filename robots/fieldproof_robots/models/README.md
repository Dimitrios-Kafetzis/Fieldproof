# Reference vehicle models

One directory per reference vehicle, each with its xacro URDF under `urdf/`, the `robot.yaml`
manifest, `model.config`, meshes with their licence file under `meshes/`, and, after a build, the
generated `<name>.urdf` and `model.sdf` (rendered by `scripts/generate_model.py` from the CMake
function `fieldproof_add_robot_model`). The directory is on `GZ_SIM_RESOURCE_PATH` once the
package is sourced, so `model://<name>` resolves.

| Model | Task | Vehicle |
| --- | --- | --- |
| `ugv_ref/` | P1-T01 | differential-drive UGV: 2D lidar, IMU, RGB-D camera, GNSS (optional) |
| `uav_quad_ref/`, `uav_vtol_ref/` | P4-T03 | multirotor and VTOL UAVs |
| `usv_ref/` | P5-T02 | surface vessel |

See [docs/reference/robots.md](../../../docs/reference/robots.md) for the UGV's frames, topics,
rates and noise figures.
