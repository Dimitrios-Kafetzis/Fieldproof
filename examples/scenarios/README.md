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
| `ugv_events.yaml` | the example vehicle drives 4 m ahead while the timeline exercises every trigger of the timeline DSL and every engine-side event (task P2-T03, session 1): `say_hello` publishes from the vehicle's container at 2 s and then every 5 s, three times (`example.announce`, the example plugin of `../plugins`); `tighten_failsafe` sets a parameter of the stack's link monitor the first time the vehicle's ground truth is more than 1 m from its spawn (`when`, `param.set`); `freeze_stack` pauses the container for 3 s one second after the drive ends (`after`, `container.pause`); `stop_stack` kills it once the pause is over (`container.kill`). Seven assertions check the drive and, through `within_time`, that each event ran ([events.md](../../docs/reference/events.md)) | P2-T03 |
| `ugv_link_loss.yaml` | the example vehicle behind a rural LTE radio link (`network.profile: lte_rural`) to the ground station drives 8 m along the hall; 5 s in, `link.drop` cuts the link for 30 s. Seven assertions check the vehicle's documented loss-of-link behaviour (`link_state` false, wheel odometry and `cmd_vel` at zero while the link is down, `link_state` true again, the goal reached) and `network.verify: fail` fails the run if the measured latency of the link is not within 10 % of the profile's ([network.md](../../docs/reference/network.md)) | P2-T02 |
| `ugv_obstacle_replan.yaml` | the example vehicle drives 4.5 m ahead through the roller door; half a metre into the drive (`when` on its distance to the spawn) `spawn_object` puts a 0.6 m crate on the straight line, 1.7 m ahead and in the lidar's view, through the `fieldproof::WorldEvents` plugin of the world; Nav2 replans around it and the vehicle reaches the goal without a contact; `remove_object` clears the crate afterwards. Six assertions ([events.md](../../docs/reference/events.md#the-world-events)) | P2-T03 |
| `ugv_gps_denied.yaml` | the same drive with `gps.deny` 5 s in for 20 s (the bridge's GNSS adapter reports no fix, as under jamming), then a drive back to the dock once the fix is back. Seven assertions: the fix present, lost through the window, back for the return leg, both drives made, no contact; the example stack navigates by SLAM and never reads the fix, so the denial changes nothing about the drive ([events.md](../../docs/reference/events.md#the-fault-events)) | P2-T03 |

```bash
uv run --project engine fieldproof validate examples/scenarios/ugv_nav_to_goal.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_nav_to_goal.yaml    # make run
uv run --project engine fieldproof report runs/<run_id>                           # the report again
examples/demo_m1.sh   # make demo: the images, the assets, the vehicle image, then the run
uv run --project engine fieldproof run examples/scenarios/ugv_fleet.yaml --check-contracts
uv run --project engine fieldproof run examples/scenarios/ugv_link_loss.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_events.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_obstacle_replan.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_gps_denied.yaml
```

`ugv_nav_to_goal.yaml` also runs on every push to `main`, nightly and on demand in the Scenario
tests workflow, against the published images with software rendering
([ci.md](../../docs/setup/ci.md#scenario-tests)).
