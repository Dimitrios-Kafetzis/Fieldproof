# Example vehicles

One directory per example vehicle with its `robot.yaml` manifest and the sources its image is
built from. The manifest format is schema v0 ([manifest.md](../../docs/reference/manifest.md)).

| Directory | Vehicle | Arrives with |
| --- | --- | --- |
| `ugv_nav2/` | the reference UGV running Nav2 and SLAM Toolbox unmodified as the stand-in for a real vehicle codebase: `robot.yaml` extends the reference model's manifest and adds the runtime (image built from `src/ugv_nav2_bringup`, the field bringup launch with `use_sim_time:=true drivers:=false`, the driver-swap map, Nav2 readiness, a 2 CPU / 2 GiB budget) | P1-T04 |

```bash
uv run --project engine fieldproof manifest validate examples/vehicles/ugv_nav2/robot.yaml
make vehicle-image          # fieldproof vehicle build on the local base image
make test-vehicles          # tests/vehicles/test_vehicles.sh: build, contents, Nav2 active on the network
uv run --project engine fieldproof vehicle run examples/vehicles/ugv_nav2/robot.yaml
```
