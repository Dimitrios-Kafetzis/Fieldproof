# Example scenarios

Scenario files (`apiVersion: fieldproof.dev/v0alpha1`, `kind: Scenario`,
[ADR-0005](../../docs/adr/0005-declarative-yaml-scenarios.md)) validated by the engine's schema v0
(`fieldproof validate`, the `check-jsonschema` hook on this directory) and run by
`fieldproof run` ([scenarios.md](../../docs/reference/scenarios.md)).

| File | Scenario | Arrives with |
| --- | --- | --- |
| `ugv_nav_to_goal.yaml` | the example vehicle `ugv_nav2` spawns at the depot's `spawn_dock` and drives to a goal 4 m ahead through a `navigate_to_pose` timeline entry; the five assertion primitives (`reach_pose` on the ground truth, `within_time` on the drive, `topic_rate` on the lidar, `no_collision` on the chassis, `topic_value` on the commanded speed) an MCAP recording of the allow-listed topics ([assertions.md](../../docs/reference/assertions.md)); with `fieldproof run --video` (what the M1 demo does) also a video of the drive from the depot's spectator camera (`video/spectator.mp4`, [scenarios.md](../../docs/reference/scenarios.md#video)) | P1-T05, P1-T06, #46 |
| `ugv_nav_fails_on_time.yaml` | the same run with one second for the drive: fails on purpose, naming `arrived_in_time`, to show what a FAIL report looks like and to test the exit code semantics | P1-T06 |
| `ugv_fleet.yaml` | three example UGVs at once, each under its own namespace (`/ugv1`, `/ugv2`, `/ugv3`), with its own TF tree, DDS profile and budget (one memory limit overridden by the scenario, one vehicle starting after another with `depends_on`), driving to three goals in the depot concurrently; twelve assertions, four per vehicle; the run gets a ROS domain of its own ([scenarios.md](../../docs/reference/scenarios.md#fleets)). Wants a workstation (six cores for the vehicles' quotas alone); `fieldproof run --check-contracts` adds each bridge's contract check | P2-T01 |

```bash
uv run --project engine fieldproof validate examples/scenarios/ugv_nav_to_goal.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_nav_to_goal.yaml    # make run
uv run --project engine fieldproof report runs/<run_id>                           # the report again
examples/demo_m1.sh   # make demo: the images, the assets, the vehicle image, then the run
uv run --project engine fieldproof run examples/scenarios/ugv_fleet.yaml --check-contracts
```

`ugv_nav_to_goal.yaml` also runs on every push to `main`, nightly and on demand in the Scenario
tests workflow, against the published images with software rendering
([ci.md](../../docs/setup/ci.md#scenario-tests)).
