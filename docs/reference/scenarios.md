# Scenarios

A scenario is a YAML file that says what a run is made of: the world the simulator loads, the
vehicles with their manifests and spawn poses, the clock, a timeline of actions, the assertions
that turn the run into a test, and what the run writes
([ADR-0005](../adr/0005-declarative-yaml-scenarios.md)). Task P1-T05 defined **schema v0** as
the Pydantic model `fieldproof.scenario.Scenario` in the engine, with
`engine/schemas/scenario.schema.json` as its JSON Schema export (used by the `check-jsonschema`
pre-commit hook on `examples/scenarios/`, [tooling.md](../setup/tooling.md)), and the **runner
v0**, `fieldproof run`, which brings the stack up from a scenario through the Docker API, runs
the timeline and tears everything down again. Task P1-T06 added the five assertion primitives,
the recorder (MCAP and the `/fieldproof/*` side channels) and the report
([assertions.md](assertions.md)); task P2-T01 made the vehicles a fleet: namespaces end to end,
a ROS domain per run, an optional discovery server, the vehicle's DDS profile, cgroup budgets,
a parallel start with dependencies and concurrent actions ([Fleets](#fleets)).

```bash
uv run --project engine fieldproof validate examples/scenarios/ugv_nav_to_goal.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_nav_to_goal.yaml      # make run
uv run --project engine fieldproof schema > engine/schemas/scenario.schema.json   # make scenario-schema
```

`validate` checks the file against the schema and resolves it against the workspace: the world
file exists and declares every frame the scenario names, every vehicle manifest loads with a
`runtime` section ([manifest.md](manifest.md)), the timeline's goals resolve. It prints one line
per scenario with the world, the vehicles and their images, the timeline and the assertions, and
exits with code 2 and one line per problem otherwise (`vehicles[ugv1].spawn: world depot has no
frame 'spawn_moon' (goal_aisle, ...)`). Nothing is started.

## Document

```yaml
apiVersion: fieldproof.dev/v0alpha1
kind: Scenario
metadata:
  name: ugv_nav_to_goal                  # lower case, digits, underscores
  description: The example UGV navigates from the loading dock to a goal 4 m ahead
  tags: [smoke, nav, ugv]
world:
  file: depot.sdf                        # in the workspace's worlds directory
vehicles:
  - name: ugv1                           # the model name in Gazebo and the container
    manifest: ../vehicles/ugv_nav2/robot.yaml   # relative to this file
    namespace: ""                        # ROS and Gazebo namespace (default: the root)
    spawn: { frame: spawn_dock }         # a frame of the world, or x, y, z, yaw
network:                                 # optional: radio links (network.md)
  profile: lte_rural                     # wifi_good | lte_rural | rf_900mhz_5km | profiles.<name>
  verify: warn                           # off | warn | fail: the measured latency vs the profile
time:
  mode: sim                              # sim (default) | realtime (task P2-T04)
  rtf: null                              # target real-time factor (null: the world's)
  timeout_s: 10m                         # wall-clock budget once every vehicle is ready
timeline:
  - at: 0s
    name: drive_ahead
    vehicle: ugv1
    action: navigate_to_pose
    goal: { x: 10.0, y: -1.0, yaw: -1.5708 }   # in the world
    timeout_s: 5m
  - at: 5s                               # with a network section: link.set, link.drop, link.restore
    vehicle: ugv1
    action: link.drop
    duration_s: 20s
assertions:                              # assertions.md
  - name: reached_goal
    type: reach_pose
    vehicle: ugv1
    target: { x: 10.0, y: -1.0 }
    tolerance_m: 0.5
  - name: arrived_in_time
    type: within_time
    action: drive_ahead
    max_s: 4m
  - name: lidar_streams
    type: topic_rate
    topic: scan
    vehicle: ugv1
    min_hz: 2.0                          # 10 Hz nominal; about 3 Hz under software rendering in CI
    window_s: 5s
    after_s: 5s
  - name: no_collision
    type: no_collision
    vehicle: ugv1
  - name: speed_limit
    type: topic_value
    topic: cmd_vel
    vehicle: ugv1
    field: linear.x
    condition: value <= 1.5
outputs:
  logs: true
  record: true                           # recording/*.mcap in the run directory
  topics: [/clock, /tf, /tf_static, /odom, /cmd_vel, /scan, /map, /plan]   # the allow-list
```

Unknown keys are errors in every section, so a typo never passes silently. Durations (`at`,
`timeout_s`, `max_s`, `within_s`, `window_s`, `after_s`, `duration_s`) are a number of seconds or
a string such as `90s`, `1m30s` or `2h`.

| Section | Fields |
| --- | --- |
| `metadata` | `name`, `description`, `tags` |
| `world` | `file`: a world file of the workspace's worlds directory (`depot.sdf`, [worlds.md](worlds.md)); its `<frame name="...">` elements are the named poses vehicles spawn at and goals refer to; the `<world name>` is what the runner waits for |
| `vehicles[]` | `name` (unique), `manifest` (a `robot.yaml` with a `runtime` section, relative to the scenario, inside the workspace), `namespace` (unique; empty by default, the root; in a fleet one per vehicle, see [Fleets](#fleets)), `frame_prefix` (for a stack that keeps one shared TF tree; empty by default), `spawn` (a placement), `image` (overrides the manifest's `runtime.image`), `env` (extra environment of the vehicle container), `resources` (fields that replace the manifest's `runtime.resources`: `cpus`, `cpuset`, `memory`, `pids`), `depends_on` (vehicles that must be ready before this one's container starts) |
| `network` | radio links between the vehicles and a ground station ([network.md](network.md)): `profile` (every vehicle's link: a preset `wifi_good`, `lte_rural`, `rf_900mhz_5km` or a name under `profiles`; `wifi_good` by default), `links` (vehicle name to the profile of its link instead), `profiles` (the scenario's own: `latency_ms`, `jitter_ms`, `loss_pct`, `reorder_pct`, `rate_kbit`, `description`), `verify` (`off`, `warn` by default, `fail`: what a link whose measured latency is not within 10 % of its profile does). Absent: one shared network, no shaping |
| `time` | [ADR-0006](../adr/0006-sim-time-default-realtime-locked-mode.md): `mode` (`sim` by default; `realtime` validates but is refused by the runner until task P2-T04), `rtf` (target real-time factor, applied to the world's physics after it loads; absent: the world's own setting), `tolerance` (allowed deviation, recorded, enforced in the real-time-locked mode), `timeout_s` (the wall-clock budget of the run from the moment every vehicle is ready; 600 s by default) |
| `timeline[]` | the actions of the run, each started at its `at` (seconds after every vehicle is ready, wall clock in v0) whether or not an earlier one has finished, so several vehicles drive at once, told apart by `action`, each with an optional `name` for the record: `navigate_to_pose` with `vehicle`, `goal` (a placement in the world) and `timeout_s` (300 s); with a `network` section the link events `link.set` (`vehicle`, `profile`), `link.drop` (`vehicle`, optional `duration_s` after which the configured profile comes back) and `link.restore` (`vehicle`) ([network.md](network.md#timeline-events)); the event injection framework of task P2-T03 adds the rest |
| `assertions[]` | `name` (unique) and `type` (`reach_pose`, `within_time`, `topic_rate`, `no_collision`, `topic_value`) with the parameters of the type ([assertions.md](assertions.md#the-primitives)); a `vehicle` must be one of the scenario's, a `within_time.action` the `name` of a timeline entry, a `topic_value.condition` a valid expression |
| `outputs` | `directory` (where `runs/<run_id>` goes; the workspace's `runs` by default), `record` (record to MCAP, `false` by default) and `topics` (the allow-list; every topic when empty; the `/fieldproof/*` side channels always, [assertions.md](assertions.md#recording)), `logs` (keep the containers' logs in the run directory, `true` by default), `video` (a video of the run from a camera of the world: `true` for the world's `spectator` camera, or `camera: <sensor name>` and `format: mp4`; `false` by default, see [Video](#video)) |

### Placements

A `spawn` or a `goal` is either a named frame of the world (`frame: spawn_dock`) or coordinates
(`x`, `y`, optional `z` and `yaw` in radians, the world's frame). The two forms are exclusive. A
vehicle spawns at the frame's `x`, `y`, `yaw` with `z` raised by the model's base height (the
spawn launch's default, 0.12 m for the reference UGV). A `navigate_to_pose` goal is given in the
world and sent in the vehicle's `map` frame, which starts at the spawn pose: the runner expresses
the goal relative to the spawn pose, so a goal 4 m south of a vehicle spawned facing south is
`(4, 0)` in `map`.

## Fleets

A scenario with several vehicles is a fleet (task P2-T01; `examples/scenarios/ugv_fleet.yaml`
runs three example UGVs at once). Each vehicle is isolated from the others and from every other
run on the host:

- **Namespaces end to end.** Every vehicle of a fleet has its own `namespace` (`ugv1`, `ugv2`,
  ...; the schema refuses two vehicles with the same one). It reaches the spawn launch (the
  model's Gazebo topics are `/ugv1/scan`, `/ugv1/cmd_vel`, ...), the bridge (the ROS topics
  of the contract, `/ugv1/scan`, `/ugv1/odom`, and its nodes), the readiness probe (the
  manifest's `ready` conditions are written relative, `lifecycle: bt_navigator`, and resolve
  under the namespace; `FIELDPROOF_NAMESPACE` carries it into the container for the
  `HEALTHCHECK` too), the recorder (an assertion's `topic` under the vehicle's namespace), the
  timeline (`/ugv1/navigate_to_pose`) and the vehicle's own launch: the manifest names the
  launch argument that takes it (`runtime.entrypoint.launch.namespace_arg`, `namespace` for
  the example) and the orchestrator appends `namespace:=ugv1` to the container's command.
  `fieldproof validate` refuses a namespaced vehicle whose launch declares no such argument,
  since its stack would run at the root while its bridge would not.
- **One TF tree per vehicle.** The reference vehicles' `drive.tf` interface is relative, so the
  bridge publishes `/ugv1/tf` and `/ugv1/tf_static` and the vehicle's stack reads them, Nav2's
  multi-robot convention: the frame names stay what the stack expects (`map`, `odom`,
  `base_link`), nothing is prefixed, and the goal of a `navigate_to_pose` is sent in the
  vehicle's own `map` frame as before. A stack that keeps one shared `/tf` instead writes the
  manifest's `tf` interface as `/tf` and tells its vehicles apart with `frame_prefix`
  (`ugv1/`), which the scenario passes to the spawn (link names), the bridge (frames in the
  contract) and the launch (`frame_prefix_arg`); `FIELDPROOF_FRAME_PREFIX` carries it into
  the container ([bridge.md](bridge.md#the-rules)).
- **One ROS domain per run.** Every container of a run carries the run's `ROS_DOMAIN_ID`
  (label `io.fieldproof.domain`): with `ros_domain_id = "auto"` (the default) it is derived
  from the run id, between 1 and 100, so two runs on one host never share a domain and never
  disturb a `make up` stack on domain 0; `fieldproof run --domain-id N`, `ROS_DOMAIN_ID=N`
  or a number in `fieldproof.toml` pin it. The simulator's `GZ_PARTITION` stays `fieldproof`,
  so the vehicles' Gazebo topics are told apart by their namespaces and model names.
- **Discovery.** By default DDS discovery is multicast on the Docker network. With
  `discovery = "server"` in `fieldproof.toml`, `FIELDPROOF_DISCOVERY=server` or
  `fieldproof run --discovery server`, the run starts a Fast DDS discovery server first (a
  container of the bridge image running `fastdds discovery` on port 11811, role `discovery`,
  waited for until it listens) and every ROS container points at it (`ROS_DISCOVERY_SERVER`,
  with `ROS_SUPER_CLIENT=TRUE` so the readiness probe, the observer and `ros2 topic list` still
  see the whole graph), which keeps discovery traffic off multicast for large fleets.
- **The vehicle's DDS profile.** A manifest's `runtime.dds_profile` (the vehicle's own XML, as
  deployed in the field) is bind-mounted read-only at `/opt/fieldproof/dds/profile.xml` and
  named to the container's RMW (`FASTRTPS_DEFAULT_PROFILES_FILE` for Fast DDS,
  `CYCLONEDDS_URI` for Cyclone DDS; [manifest.md](manifest.md#runtime)).
- **Budgets.** The vehicle container runs under the cgroup limits of `runtime.resources`
  (`cpus`, the CPU quota; `cpuset`, the cores it may run on; `memory`; `pids`), with the
  scenario's `vehicles[].resources` replacing any field per vehicle; the budget applied is in
  `run.json` (`vehicles[].resources`), the `budget` event of `engine.log` sums the fleet
  against the host's cores and memory and warns when it asks for more, and `docker inspect`
  or `docker stats` show the limits on the running containers.
- **Start order.** The spawns run at once, then the bridges (each waited for until its
  vehicle's odometry topic appears, one of them bridging `/clock` for the run), then the
  recorder, then the vehicles in parallel: a vehicle with `depends_on` starts once the named
  vehicles are ready (the `vehicle.dependency` event), the others as soon as the recorder is;
  a cycle or an unknown name is a validation error. A vehicle that fails its readiness gate
  fails the run and the vehicles depending on it are never started.
- **Concurrent actions.** Every timeline entry starts at its `at` on its own worker, so three
  goals at `0s` are sent at once; the run observes the stream and the containers between
  polls. The first entry that does not end `ok` fails the run at once and the entries still
  running are recorded `cancelled`; the budget ends whatever is running.
- **Contract checks.** `fieldproof run --check-contracts` runs each vehicle's bridge contract
  check inside its bridge container once the bridge is up and before the recorder starts
  (`ros2 run fieldproof_bridge contract_check` on the bundle the bridge wrote,
  [bridge.md](bridge.md#checking-a-running-stack)); the counts land in `run.json`
  (`vehicles[].contract`) and a failed check ends the run with status `error`, since the
  bridge is Fieldproof's promise, not the stack under test. It costs the bridges phase a
  minute or so (`timeouts.contract_s`), which is why it is opt-in.

The fleet example drives `ugv1` from the loading dock into the yard, `ugv2` along the hall and
`ugv3` across the yard, all at `0s`, with a `reach_pose`, a `within_time`, a `no_collision` and
a `topic_value` assertion per vehicle; nothing of one vehicle reaches another, which
`tests/integration/test_fleet.py` checks from inside `ugv1` (no node of `/ugv2` or `/ugv3`
subscribes to its scan or odometry). Three Nav2 stacks and a simulator rendering three lidars
want a workstation: the manifest's quotas alone ask for six cores, and on a two-core CI runner
with software rendering the readiness gates would time out.

## The workspace

`fieldproof.toml` at the root of a workspace tells the engine where runs go, how the stack's
containers are started, how a run is isolated from other runs and where the simulator's worlds,
models and assets live. The engine finds
it by walking up from the scenario's directory, then from the current directory, the way git
finds its repository; the directory holding it is the workspace root that relative paths resolve
against, and the bridge containers mount it read-only at `/workspace` (so a vehicle manifest and
everything its `extends` chain and description point at must be below it). Every value has a
default that matches this repository, and the repository's own `fieldproof.toml` lists them:

```toml
[runs]
directory = "runs"

[docker]
network = "fieldproof"
image_prefix = "ghcr.io/dimitrios-kafetzis/fieldproof-"
image_tag = "local"
render = "auto"            # gpu when the host has a /dev/dri render node, software otherwise
ros_domain_id = "auto"     # a number, or one derived from the run id (1..100) per run
discovery = "multicast"    # or "server": a Fast DDS discovery server container per run

[stack]
worlds = "sim/fieldproof_worlds/worlds"
models = "sim/fieldproof_worlds/models"
fuel = "assets/fuel/models"
fuel_lock = "assets/fuel.lock"
robots = "robots/fieldproof_robots/models"

[timeouts]
world_s = 300.0            # the world's services answering
spawn_s = 300.0            # a vehicle model spawned
bridge_s = 120.0           # a bridge publishing the vehicle's odometry
recorder_s = 60.0          # the recorder's observer reporting ready
discovery_s = 60.0         # the discovery server listening (discovery = "server")
contract_s = 180.0         # a vehicle's bridge contract check (--check-contracts)
ground_s = 60.0            # the ground station reporting ready (radio links)
teardown_s = 10.0          # grace period of a container stop
```

Precedence, highest first ([01-architecture.md](../plan/01-architecture.md#cross-cutting)): the
scenario file (`outputs.directory`), the command line (`--runs-dir`, `--render`, `--domain-id`,
`--network`, `--discovery`, `--config` for a file elsewhere), the environment variables the
compose stack also reads (`FIELDPROOF_IMAGE_PREFIX`, `FIELDPROOF_IMAGE_TAG`, `FIELDPROOF_RENDER`,
`ROS_DOMAIN_ID`, `FIELDPROOF_DISCOVERY`), `fieldproof.toml`, the defaults.
`FIELDPROOF_IMAGE_TAG=main fieldproof run ...` therefore runs the published images, as
`FIELDPROOF_IMAGE_TAG=main make up` does.

## Running a scenario

`fieldproof run SCENARIO` validates and resolves the scenario as `validate` does, then drives the
Docker daemon through the Docker SDK ([ADR-0004](../adr/0004-one-container-per-vehicle.md)):
compose files are not involved, the containers are the same as the compose stack's
([containers.md](../setup/containers.md#compose-profiles-and-networking)). The run is a state
machine of phases, each a gate the next one waits for:

| Phase | What happens | Gate |
| --- | --- | --- |
| `preparing` | `time.mode: realtime` is refused (P2-T04); every image is present locally (the stack's `sim` and `bridge` at the configured prefix and tag, each vehicle's image); every Fuel model the world uses is fetched (`make assets`); the vehicles' budgets are summed against the host's cores and memory (`budget`, a `budget.oversubscribed` warning); the Docker network exists or is created; the run's `ROS_DOMAIN_ID` and discovery mode are logged | |
| `sim` | with `discovery: server`, first the discovery server container (`fastdds discovery` on port 11811, waited for until it listens, `timeouts.discovery_s`); then the simulator container: `gz sim -s -r --headless-rendering <world>` with the four read-only mounts of the compose stack, `GZ_PARTITION=fieldproof`, `ROS_DOMAIN_ID`, software rendering or `/dev/dri` with the render node's group; then `time.rtf`, if set, through the world's `set_physics` service | `gz service -l` lists `/world/<name>/create` (`timeouts.world_s`) |
| `spawn` | every vehicle at once: a one-shot container of the bridge image runs the manifest's spawn launch (`description.launch`: a file next to the manifest, or a launch file of `fieldproof_robots`) with `name`, `namespace`, `frame_prefix`, `world`, the pose, `use_sim_time:=true` and `robot_state_publisher:=false` | each container exits 0 (`timeouts.spawn_s`) |
| `bridges` | every vehicle at once: the bridge container, `fieldproof-bridge` on the manifest at `/workspace/...` (`namespace:=` and `frame_prefix:=` when set, `clock:=false` for every bridge but the first, the bundle written to `/tmp/fieldproof-bridge`); with `--check-contracts`, the contract check follows | `ros2 topic list` inside it shows the vehicle's odometry topic (`timeouts.bridge_s`); the contract check exits 0 (`timeouts.contract_s`) |
| `recorder` | the recorder container (the bridge image as the engine's user, the run directory mounted read-write at `/fieldproof/run`, the observer script read-only): the observer on `observe.json`, `ros2 bag record` to `recording/` when `outputs.record` is on, the Gazebo and ROS subscriptions the assertions need, the `/fieldproof/*` side channels ([assertions.md](assertions.md#how-a-run-is-observed)) | the observer's `ready` line in `samples.jsonl` (`timeouts.recorder_s`) |
| `network` | only with a `network` section ([network.md](network.md#topology)): every vehicle's link holder (the bridge image as root with `NET_ADMIN`, on the vehicle's onboard network and the radio network), each link's configured profile put on it with `tc` (`link.applied`); then the ground station on the radio network (heartbeats and probes) | each holder's script exits 0; the ground station's `ready` line in `network.jsonl` (`timeouts.ground_s`) |
| `vehicles` | every vehicle in parallel, each once the vehicles it `depends_on` are ready: the vehicle container with the manifest's image, its command with `namespace_arg`/`frame_prefix_arg` when the vehicle has a namespace or prefix, the environment (`ROS_DOMAIN_ID`, `FIELDPROOF_NAMESPACE`, `FIELDPROOF_FRAME_PREFIX`, the DDS profile's variable, `runtime.env`, the scenario's `env`), the DDS profile mounted, the budget applied (`vehicle.budget`), hostname the vehicle's name; with radio links the container joins its link holder's network namespace instead of a network | the image's readiness probe, run through `docker exec` with the manifest's `ready.timeout_s`, reports every `runtime.ready` condition ([manifest.md](manifest.md#the-readiness-probe)); its progress lines are shown |
| `running` | the timeline: each entry starts at its `at` on a worker, `navigate_to_pose` running `ros2 action send_goal` inside the vehicle container and reading the result, a link event running the holder's `tc` script, several at once; every half second meanwhile the observer's new samples feed the assertion evaluator, deadlines are checked and, every two seconds, the containers; each assertion resolves as soon as the stream decides it and the rest are settled when the timeline completes | every entry ends `ok` within `time.timeout_s`, every assertion passed and, with `network.verify: fail`, every link measured as its profile |
| `teardown` | each link's traffic counters read from its holder (radio links); vehicles, the ground station and the link holders, the recorder (SIGINT to `ros2 bag record`, so the MCAP is finalised), bridges, the simulator and the discovery server are stopped (grace `timeouts.teardown_s`) and removed in reverse order, their logs saved first; then every container labelled with the run id is removed, whatever the in-memory list says; then the run's networks (radio links) | |
| `finished` | the record and the report (`report.json`, `report.md`) are written | |

Every container carries the labels `io.fieldproof.run=<run_id>`, `io.fieldproof.domain=<id>`,
`io.fieldproof.role` (`discovery`, `sim`, `spawn`, `bridge`, `recorder`, `link`, `ground`,
`vehicle`) and, for a vehicle's containers, `io.fieldproof.vehicle=<name>`, and is named
`fieldproof-<short>-sim`, `fieldproof-<short>-discovery`, `fieldproof-<short>-spawn-<vehicle>`,
`fieldproof-<short>-bridge-<vehicle>`, `fieldproof-<short>-recorder`,
`fieldproof-<short>-link-<vehicle>`, `fieldproof-<short>-ground` or
`fieldproof-<short>-<vehicle>` after the run id's last part (the networks of radio links carry
`io.fieldproof.run` too),
so `docker ps --filter label=io.fieldproof.run` shows what a run started. The teardown runs
whatever happens: a failed gate, the budget, an error of the daemon, Ctrl-C or SIGTERM (a second
signal during the teardown is ignored so it completes). `--keep` leaves the containers running
for a look (`docker rm -f $(docker ps -aq --filter label=io.fieldproof.run)` removes them),
`--dry-run` prints the resolved plan (containers, commands, mounts, poses, the domain, the
discovery mode, each vehicle's budget) as JSON without touching Docker, `--quiet` hides the
output of the commands run in the containers, `--check-contracts` and `--discovery` are
described under [Fleets](#fleets).

### Video

`outputs.video` records the run from a camera of the world (issue #46, the M1 demo video):
`true` uses the world's `spectator` camera, `camera: <name>` another one. The camera is a
`<sensor type="camera">` of the world file carrying Gazebo's `CameraVideoRecorder` system,
whose `<service>` is `/<camera>/record_video` by convention
([worlds.md](worlds.md#the-spectator-camera)); `fieldproof validate` and `fieldproof run`
refuse a scenario naming a camera the world does not have. The runner starts the recording
through that service when the timeline starts and stops it when the timeline ends, whatever
ended it, waits for the encoder to close the file (`VIDEO_SETTLE_S`, 15 s), copies it out of
the simulator container into `video/<camera>.mp4` in the run directory and reports it
(`run.json` `video`, the reports' artefacts, the `video` property of `junit.xml`,
`fieldproof run`'s last line). The video is best effort: a recorder that refuses to
start, a file that never appears or a copy that fails is a `video.error` warning in
`engine.log` and a `status: error` in the record, and the run's verdict is unchanged.
`fieldproof run --video` and `--no-video` override the scenario: the example scenario leaves
the video off and the M1 demo (`examples/demo_m1.sh`) turns it on with `--video`.

The recorder encodes in simulation time (`<use_sim_time>`), so the clip plays at the pace
the robot drove, however slowly the simulator ran. Rendering the camera costs the simulator a
render pass per frame on top of the GPU lidar's: on a GPU workstation the example's drive takes
about twice as long in wall-clock time with the video on (the real-time factor falls from about
0.9 to 0.45 and the GPU lidar, which shares the render queue, from 9 Hz to about 4 Hz of
simulation time); with software rendering on a two-core CI runner the lidar falls to about
1 Hz. A video therefore changes what a scenario measures, which is why the example keeps it
off and a scenario whose assertions watch sensor rates should not ask for one on a host without
a GPU. A camera with no recording in progress renders nothing, so the cost is only paid when a
video is asked for.

### The run directory

`run_id = <scenario-hash>-<timestamp>-<short-uuid>`: the first eight hex digits of the SHA-256
of the scenario file, the UTC start time (`20261017T143005Z`) and six random hex digits.
Everything the run did is in `runs/<run_id>/` (the workspace's `runs` directory, `--runs-dir`
or `outputs.directory`; `runs/` is git-ignored):

| File | Contents |
| --- | --- |
| `scenario.yaml` | the scenario file as it was run |
| `run.json` | the record: `run_id`, the scenario's name, path and hash, the engine version, `started_at`, `finished_at`, the current `phase`, the `status` and `reason`, every phase with its start, end and duration, the `vehicles` (name, manifest, namespace, frame prefix, image, the budget applied, `depends_on`, the DDS profile, the contract check's counts when one ran), the containers with their images, the timeline entries with their status (`ok`, `failed`, `timeout`, `cancelled`), duration and detail (`goal SUCCEEDED`, `goal ABORTED`, `no result within 300 s`), the `assertions` with their verdict, reason, time and evidence, the `observation` (samples, simulation time covered, problems), the `time` settings and the `stack` (network, render mode, domain id and where it came from, discovery mode, world, images); rewritten at every phase change and verdict, so a run that died still has a record |
| `engine.log` | one JSON object per line (`ts`, `t` seconds since the start, `run_id`, `level`, `event`, `message` and the event's fields): the phases, every container started and stopped, the readiness probe's lines, the actions and their results, the verdicts (`assertion.passed`, `assertion.failed`), the teardown, the recording and report written; what the console shows, with the same events |
| `observe.json`, `samples.jsonl`, `assertions.jsonl`, `recording/`, `report.json`, `report.md`, `junit.xml` | the observer's plan and stream, the verdicts, the MCAP recording and the reports ([assertions.md](assertions.md#the-run-directory)) |
| `video/<camera>.mp4` | the video of the run from the world's camera, when `outputs.video` is set ([Video](#video)) |
| `logs/sim.log`, `logs/bridge-<vehicle>.log`, `logs/recorder.log`, `logs/vehicle-<vehicle>.log`, `logs/discovery.log` | the containers' output, saved during the teardown when `outputs.logs` is true |

### Status and exit codes

| Status | Meaning | Exit code |
| --- | --- | --- |
| `passed` | every gate held, the timeline completed and every assertion passed | 0 |
| `failed` | the stack under test misbehaved: an assertion failed (the reason names the first: `assertion arrived_in_time failed: ...`), a vehicle did not reach its ready conditions in time, an action failed (`goal ABORTED`, no result within the action's timeout) or a vehicle container exited | 1 |
| `timeout` | `time.timeout_s` passed before or during an action | 1 |
| `error` | the run could not proceed: an image or Fuel model missing, the simulator, a bridge, the discovery server or the recorder not coming up or exiting, a spawn failing, `set_physics` refused, a bridge contract check failing, the Docker daemon unreachable | 1 (2 when the scenario or the configuration does not even validate) |
| `interrupted` | Ctrl-C or SIGTERM | 130 |

The console ends with the verdict (`PASS`, `FAIL`, `TIMEOUT`, `ERROR`, `INTERRUPTED`), the
scenario, the run id, the reason and the run directory, one line per assertion with its verdict
and reason, and the path of the Markdown report; a warning follows if any container of the run is
still present afterwards.

## Tests

- `engine/tests/test_scenario.py`: durations, the example, every error of the schema, the
  committed JSON Schema is current, the depot's frames, placements and the world-to-map
  transform; `test_config.py`: the file, its discovery, the environment, its errors;
  `test_runner.py`: the plan of the example (containers, commands, mounts, goals) and the state
  machine against a fake backend and a fake clock: the happy path and the record it writes,
  the real-time factor, every failing gate, a goal aborted or without result, the budget before
  and during an action, a vehicle dying, Ctrl-C and daemon errors during start, an unexpected
  exception, `--keep`, a teardown that continues past errors and sweeps by label;
  `test_fleet_runner.py` (task P2-T01): the plan of the fleet example (namespaces on every
  container and topic, one clock bridge, the budgets and the scenario's override, the
  automatic domain, the discovery server's container and environment, a frame prefix, the
  refusals: a launch without `namespace_arg`, a missing DDS profile, dependency cycles) and
  its runs on the fake backend (three vehicles started in parallel and torn down in reverse, a
  dependency that fails, the discovery server up or never listening, the contract checks
  passing or failing, three actions truly concurrent on a thread pool, one failing and
  cancelling the others, the budget during actions, the oversubscription warning);
  `test_scenario_cli.py`: the three commands, the dry run (with `--discovery server`), a run
  on the fake backend with its exit codes, the fleet with `--check-contracts`;
  `test_docker_backend.py`: the Docker backend against a stub of the SDK's client (the budget
  mapped, the host's capacity, one connection per thread). `make test-engine` runs them with
  coverage.
- `tests/integration/test_run.py` (`make test-integration`, after the vertical-slice test):
  `fieldproof run` of the example scenario passes against the real stack with its five
  assertions, the MCAP recording and the reports, the run directory is complete and no container
  of the run is left; a second run is interrupted with SIGINT while the stack starts and exits
  130 with nothing left behind; the failing example exits 1 naming its assertion
  ([assertions.md](assertions.md#tests)). `tests/integration/test_fleet.py` (task P2-T01) runs
  the fleet example with `--check-contracts` on a host with eight cores or more
  (`FIELDPROOF_IT_FLEET=1` insists): while the three vehicles run it reads their cgroup limits
  from `docker inspect` and `docker stats` (the CPU quotas, the memory limits with `ugv2`'s
  override), the run's domain, namespace and DDS profile on every container, and the graph
  from inside `ugv1` (no node of another vehicle subscribes to its scan or odometry, one TF
  topic per vehicle); afterwards exit code 0, every vehicle at its own goal, the twelve
  assertions and the three contract checks passed, the MCAP with the namespaced topics, and
  nothing left behind.
- `.github/workflows/scenario-tests.yml` (task P1-T07): the example against the published
  images on every push to `main`, nightly and on demand, with software rendering and the
  flaky-run policy of `tools/ci/scenario-run.sh`; the run is uploaded as an artefact
  ([ci.md](../setup/ci.md#scenario-tests)). `examples/demo_m1.sh` (`make demo`) is the same
  chain on a workstation.

## Known limits

- The timeline knows `navigate_to_pose` and the three link events, started at their `at` on
  the wall clock after readiness; conditional triggers, sim-time scheduling and the other
  actions are task P2-T03's (the clock manager, P2-T04's).
- The assertions' deadlines are on the wall clock like the timeline; the recorder does not
  sample container resources yet ([assertions.md](assertions.md#known-limits)).
- The runs of one workspace share the configured Docker network and their containers can reach
  each other's ports (with radio links a run's vehicles sit on its own onboard and radio
  networks, [network.md](network.md)). With `ros_domain_id = "auto"` two runs started at once
  collide on a domain with odds of one in a hundred; `--domain-id` settles it where that
  matters.
- A vehicle whose launch takes no namespace argument (`runtime.entrypoint.launch.namespace_arg`)
  can only run at the root namespace, hence alone; a `command` entrypoint gets the namespace from
  `FIELDPROOF_NAMESPACE` and is trusted to use it.
- The discovery server is one container per run, not replicated; a run with it needs the bridge
  image (which carries `fastdds`) as every run does.
- `fieldproof run` needs the images built locally (`make images`, `make vehicle-image`) or
  published (`FIELDPROOF_IMAGE_TAG=main`) and the Fuel models fetched (`make assets`); it does
  not build or fetch.
