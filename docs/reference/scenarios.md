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
([assertions.md](assertions.md)).

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
`timeout_s`, `max_s`, `within_s`, `window_s`, `after_s`) are a number of seconds or a string
such as `90s`, `1m30s` or `2h`.

| Section | Fields |
| --- | --- |
| `metadata` | `name`, `description`, `tags` |
| `world` | `file`: a world file of the workspace's worlds directory (`depot.sdf`, [worlds.md](worlds.md)); its `<frame name="...">` elements are the named poses vehicles spawn at and goals refer to; the `<world name>` is what the runner waits for |
| `vehicles[]` | `name` (unique), `manifest` (a `robot.yaml` with a `runtime` section, relative to the scenario, inside the workspace), `namespace` (unique; empty by default, since the example stack's topics are at the root), `spawn` (a placement), `image` (overrides the manifest's `runtime.image`), `env` (extra environment of the vehicle container) |
| `time` | [ADR-0006](../adr/0006-sim-time-default-realtime-locked-mode.md): `mode` (`sim` by default; `realtime` validates but is refused by the runner until task P2-T04), `rtf` (target real-time factor, applied to the world's physics after it loads; absent: the world's own setting), `tolerance` (allowed deviation, recorded, enforced in the real-time-locked mode), `timeout_s` (the wall-clock budget of the run from the moment every vehicle is ready; 600 s by default) |
| `timeline[]` | the actions of the run, in order of `at` (seconds after every vehicle is ready, wall clock in v0). One action in v0: `navigate_to_pose` with `vehicle`, `goal` (a placement in the world), `timeout_s` (300 s) and an optional `name` for the record; the event injection framework of task P2-T03 adds the rest |
| `assertions[]` | `name` (unique) and `type` (`reach_pose`, `within_time`, `topic_rate`, `no_collision`, `topic_value`) with the parameters of the type ([assertions.md](assertions.md#the-primitives)); a `vehicle` must be one of the scenario's, a `within_time.action` the `name` of a timeline entry, a `topic_value.condition` a valid expression |
| `outputs` | `directory` (where `runs/<run_id>` goes; the workspace's `runs` by default), `record` (record to MCAP, `false` by default) and `topics` (the allow-list; every topic when empty; the `/fieldproof/*` side channels always, [assertions.md](assertions.md#recording)), `logs` (keep the containers' logs in the run directory, `true` by default) |

### Placements

A `spawn` or a `goal` is either a named frame of the world (`frame: spawn_dock`) or coordinates
(`x`, `y`, optional `z` and `yaw` in radians, the world's frame). The two forms are exclusive. A
vehicle spawns at the frame's `x`, `y`, `yaw` with `z` raised by the model's base height (the
spawn launch's default, 0.12 m for the reference UGV). A `navigate_to_pose` goal is given in the
world and sent in the vehicle's `map` frame, which starts at the spawn pose: the runner expresses
the goal relative to the spawn pose, so a goal 4 m south of a vehicle spawned facing south is
`(4, 0)` in `map`.

## The workspace

`fieldproof.toml` at the root of a workspace tells the engine where runs go, how the stack's
containers are started and where the simulator's worlds, models and assets live. The engine finds
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
ros_domain_id = 0

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
teardown_s = 10.0          # grace period of a container stop
```

Precedence, highest first ([01-architecture.md](../plan/01-architecture.md#cross-cutting)): the
scenario file (`outputs.directory`), the command line (`--runs-dir`, `--render`, `--domain-id`,
`--network`, `--config` for a file elsewhere), the environment variables the compose stack also
reads (`FIELDPROOF_IMAGE_PREFIX`, `FIELDPROOF_IMAGE_TAG`, `FIELDPROOF_RENDER`, `ROS_DOMAIN_ID`),
`fieldproof.toml`, the defaults. `FIELDPROOF_IMAGE_TAG=main fieldproof run ...` therefore runs
the published images, as `FIELDPROOF_IMAGE_TAG=main make up` does.

## Running a scenario

`fieldproof run SCENARIO` validates and resolves the scenario as `validate` does, then drives the
Docker daemon through the Docker SDK ([ADR-0004](../adr/0004-one-container-per-vehicle.md)):
compose files are not involved, the containers are the same as the compose stack's
([containers.md](../setup/containers.md#compose-profiles-and-networking)). The run is a state
machine of phases, each a gate the next one waits for:

| Phase | What happens | Gate |
| --- | --- | --- |
| `preparing` | `time.mode: realtime` is refused (P2-T04); every image is present locally (the stack's `sim` and `bridge` at the configured prefix and tag, each vehicle's image); every Fuel model the world uses is fetched (`make assets`); the Docker network exists or is created | |
| `sim` | the simulator container: `gz sim -s -r --headless-rendering <world>` with the four read-only mounts of the compose stack, `GZ_PARTITION=fieldproof`, `ROS_DOMAIN_ID`, software rendering or `/dev/dri` with the render node's group; then `time.rtf`, if set, through the world's `set_physics` service | `gz service -l` lists `/world/<name>/create` (`timeouts.world_s`) |
| `spawn` | per vehicle, a one-shot container of the bridge image runs the manifest's spawn launch (`description.launch`: a file next to the manifest, or a launch file of `fieldproof_robots`) with `name`, `namespace`, `world`, the pose, `use_sim_time:=true` and `robot_state_publisher:=false` | the container exits 0 (`timeouts.spawn_s`) |
| `bridges` | per vehicle, the bridge container: `fieldproof-bridge` on the manifest at `/workspace/...` (`namespace:=` when set) | `ros2 topic list` inside it shows the vehicle's odometry topic (`timeouts.bridge_s`) |
| `recorder` | the recorder container (the bridge image as the engine's user, the run directory mounted read-write at `/fieldproof/run`, the observer script read-only): the observer on `observe.json`, `ros2 bag record` to `recording/` when `outputs.record` is on, the Gazebo and ROS subscriptions the assertions need, the `/fieldproof/*` side channels ([assertions.md](assertions.md#how-a-run-is-observed)) | the observer's `ready` line in `samples.jsonl` (`timeouts.recorder_s`) |
| `vehicles` | per vehicle, the vehicle container: the manifest's image, environment and budget (`runtime.resources`), hostname the vehicle's name | the image's readiness probe, run through `docker exec` with the manifest's `ready.timeout_s`, reports every `runtime.ready` condition ([manifest.md](manifest.md#the-readiness-probe)); its progress lines are shown |
| `running` | the timeline in order: each entry waits for its `at`, then `navigate_to_pose` runs `ros2 action send_goal` inside the vehicle container (in a thread) and reads the result; every half second meanwhile the observer's new samples feed the assertion evaluator, deadlines are checked and, every two seconds, the containers; each assertion resolves as soon as the stream decides it and the rest are settled when the timeline completes | the timeline completes within `time.timeout_s` and every assertion passed |
| `teardown` | vehicles, the recorder (SIGINT to `ros2 bag record`, so the MCAP is finalised), bridges and the simulator are stopped (grace `timeouts.teardown_s`) and removed in reverse order, their logs saved first; then every container labelled with the run id is removed, whatever the in-memory list says | |
| `finished` | the record and the report (`report.json`, `report.md`) are written | |

Every container carries the labels `io.fieldproof.run=<run_id>`, `io.fieldproof.role` (`sim`,
`spawn`, `bridge`, `recorder`, `vehicle`) and, for a vehicle's containers,
`io.fieldproof.vehicle=<name>`, and is named `fieldproof-<short>-sim`,
`fieldproof-<short>-spawn-<vehicle>`, `fieldproof-<short>-bridge-<vehicle>`,
`fieldproof-<short>-recorder` or `fieldproof-<short>-<vehicle>` after the run id's last part,
so `docker ps --filter label=io.fieldproof.run` shows what a run started. The teardown runs
whatever happens: a failed gate, the budget, an error of the daemon, Ctrl-C or SIGTERM (a second
signal during the teardown is ignored so it completes). `--keep` leaves the containers running
for a look (`docker rm -f $(docker ps -aq --filter label=io.fieldproof.run)` removes them),
`--dry-run` prints the resolved plan (containers, commands, mounts, poses) as JSON without
touching Docker, `--quiet` hides the output of the commands run in the containers.

### The run directory

`run_id = <scenario-hash>-<timestamp>-<short-uuid>`: the first eight hex digits of the SHA-256
of the scenario file, the UTC start time (`20261017T143005Z`) and six random hex digits.
Everything the run did is in `runs/<run_id>/` (the workspace's `runs` directory, `--runs-dir`
or `outputs.directory`; `runs/` is git-ignored):

| File | Contents |
| --- | --- |
| `scenario.yaml` | the scenario file as it was run |
| `run.json` | the record: `run_id`, the scenario's name, path and hash, the engine version, `started_at`, `finished_at`, the current `phase`, the `status` and `reason`, every phase with its start, end and duration, the `vehicles` (name, manifest, namespace, image), the containers with their images, the timeline entries with their status, duration and detail (`goal SUCCEEDED`, `goal ABORTED`, `no result within 300 s`), the `assertions` with their verdict, reason, time and evidence, the `observation` (samples, simulation time covered, problems), the `time` settings and the `stack` (network, render mode, domain id, world, images); rewritten at every phase change and verdict, so a run that died still has a record |
| `engine.log` | one JSON object per line (`ts`, `t` seconds since the start, `run_id`, `level`, `event`, `message` and the event's fields): the phases, every container started and stopped, the readiness probe's lines, the actions and their results, the verdicts (`assertion.passed`, `assertion.failed`), the teardown, the recording and report written; what the console shows, with the same events |
| `observe.json`, `samples.jsonl`, `assertions.jsonl`, `recording/`, `report.json`, `report.md` | the observer's plan and stream, the verdicts, the MCAP recording and the report ([assertions.md](assertions.md#the-run-directory)) |
| `logs/sim.log`, `logs/bridge-<vehicle>.log`, `logs/recorder.log`, `logs/vehicle-<vehicle>.log` | the containers' output, saved during the teardown when `outputs.logs` is true |

### Status and exit codes

| Status | Meaning | Exit code |
| --- | --- | --- |
| `passed` | every gate held, the timeline completed and every assertion passed | 0 |
| `failed` | the stack under test misbehaved: an assertion failed (the reason names the first: `assertion arrived_in_time failed: ...`), a vehicle did not reach its ready conditions in time, an action failed (`goal ABORTED`, no result within the action's timeout) or a vehicle container exited | 1 |
| `timeout` | `time.timeout_s` passed before or during an action | 1 |
| `error` | the run could not proceed: an image or Fuel model missing, the simulator, a bridge or the recorder not coming up or exiting, a spawn failing, `set_physics` refused, the Docker daemon unreachable | 1 (2 when the scenario or the configuration does not even validate) |
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
  `test_scenario_cli.py`: the three commands, the dry run, a run on the fake backend with its
  exit codes; `test_docker_backend.py`: the Docker backend against a stub of the SDK's client.
  `make test-engine` runs them with coverage.
- `tests/integration/test_run.py` (`make test-integration`, after the vertical-slice test):
  `fieldproof run` of the example scenario passes against the real stack with its five
  assertions, the MCAP recording and the reports, the run directory is complete and no container
  of the run is left; a second run is interrupted with SIGINT while the stack starts and exits
  130 with nothing left behind; the failing example exits 1 naming its assertion
  ([assertions.md](assertions.md#tests)).

## Known limits

- The timeline knows one action and runs it sequentially on the wall clock after readiness;
  conditional triggers, sim-time scheduling and the other actions are task P2-T03's (the clock
  manager, P2-T04's).
- The assertions' deadlines are on the wall clock like the timeline; the recorder does not
  sample container resources yet ([assertions.md](assertions.md#known-limits)).
- The schema allows several vehicles and the runner starts them in sequence, but the example
  stack's launch files expect the root namespace, so one vehicle per run until task P2-T01
  (which also decides the shared `/clock` and per-run networks).
- The runs of one workspace share the configured Docker network; two runs at once on the same
  network and `ROS_DOMAIN_ID` would see each other's topics.
- `fieldproof run` needs the images built locally (`make images`, `make vehicle-image`) or
  published (`FIELDPROOF_IMAGE_TAG=main`) and the Fuel models fetched (`make assets`); it does
  not build or fetch.
