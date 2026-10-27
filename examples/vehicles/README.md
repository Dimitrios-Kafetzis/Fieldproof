# Example vehicles

One directory per example vehicle with its `robot.yaml` manifest and the sources its image is
built from. The manifest format is schema v0 ([manifest.md](../../docs/reference/manifest.md)).

| Directory | Vehicle | Arrives with |
| --- | --- | --- |
| `ugv_nav2/` | the reference UGV running Nav2 and SLAM Toolbox unmodified as the stand-in for a real vehicle codebase: `robot.yaml` extends the reference model's manifest and adds the runtime (image built from `src/ugv_nav2_bringup`, the field bringup launch with `use_sim_time:=true drivers:=false` and `namespace_arg: namespace` so a fleet can run several, the driver-swap map, Nav2 readiness with relative names, the vehicle's Fast DDS profile `config/fastdds.xml`, a 2 CPU / 2 GiB budget, the radio link with its ground-station heartbeat and the loss-of-link failsafe `link_monitor`, which holds the vehicle while the heartbeat is missing, and the bringup watchdog `bringup_watchdog`, which retries a Nav2 bringup the lifecycle manager aborted) | P1-T04, P2-T01, P2-T02 |

```bash
uv run --project engine fieldproof manifest validate examples/vehicles/ugv_nav2/robot.yaml
make vehicle-image          # fieldproof vehicle build on the local base image
make test-vehicles          # tests/vehicles/test_vehicles.sh: build, contents, Nav2 active on the network
uv run --project engine fieldproof vehicle run examples/vehicles/ugv_nav2/robot.yaml
```
