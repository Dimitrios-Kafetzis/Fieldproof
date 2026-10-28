# Events

The event injection framework of task P2-T03: how a scenario's `timeline` says *when*
something happens (the timeline DSL: `at`, `after`, `when`, `every`), *what* happens (the
events: built-in actions and plugins found through Python entry points; the world events on a
Gazebo plugin, the fault events on the bridge's injectors), how every occurrence is recorded
(`run.json`, `engine.log`, `/fieldproof/events`), and how to write an event plugin. The
scenario format around the timeline is in [scenarios.md](scenarios.md); the radio link events
are detailed in [network.md](network.md#timeline-events).

```yaml
timeline:
  - at: 0s                                # the drive starts with the timeline
    name: drive_ahead
    vehicle: ugv1
    action: navigate_to_pose
    goal: { x: 10.0, y: -1.0, yaw: -1.5708 }
  - name: say_hello                       # at 1 s, then every 3 s, three times
    vehicle: ugv1
    at: 1s
    every: 3s
    count: 3
    action: example.announce              # an event of a plugin (examples/plugins)
    text: fieldproof says hello
  - name: tighten_failsafe                # the first time the vehicle is 1 m from its spawn
    vehicle: ugv1
    when: { metric: distance_to, target: { frame: spawn_dock }, condition: value > 1.0 }
    within_s: 4m
    action: param.set
    node: link_monitor
    parameter: loss_timeout_s
    value: 5.0
  - name: freeze_stack                    # five seconds after both have ended, for 3 s
    vehicle: ugv1
    after: [drive_ahead, tighten_failsafe]
    at: 5s
    action: container.pause
    duration_s: 3s
  - name: stop_stack                      # once the pause is over
    vehicle: ugv1
    after: freeze_stack
    action: container.kill
```

## The timeline DSL

Every entry has an `action` (which selects the event and its parameters), an optional `name`
(its label in the record; `<action>@<at>s` otherwise) and, for most events, a `vehicle`. The
rest of the entry says when it runs:

| Field | Meaning |
| --- | --- |
| `at` | offset from the entry's trigger, as a duration (`30s`, `1m`; 0 by default): seconds after the timeline starts, after the `after` entry ends, or after the `when` condition first holds |
| `after` | the `name` of another entry, or a list of names: this one is triggered when that entry ends `ok` (when every one of the list has; it is skipped when any of them does not); a repeated entry cannot be named, nor the entry itself; chains are allowed, cycles are not |
| `when` | a [condition](#conditions) of the run: the entry is triggered by the first sample of the observer's stream that satisfies it (`after` and `when` are exclusive) |
| `within_s` | with `when`: how long after the timeline starts the condition may still trigger the entry; the entry is `skipped` once that passes (without it: as long as the rest of the timeline runs) |
| `every` | repeat the entry at this period from its first start (a positive duration); the record labels the occurrences `<name>`, `<name>#2`, `<name>#3`, ...; occurrences never overlap: one that overruns its period delays the next until it ends; `every` does not combine with `when` |
| `count` | with `every`: stop after this many occurrences |
| `until_s` | with `every`: stop repeating this long after the timeline starts |

The trigger of an entry without `after` or `when` is the start of the timeline, which is the
moment every vehicle is ready ([scenarios.md](scenarios.md#running-a-scenario)). Every time is
on the wall clock, counted from that moment, as the assertions' deadlines are in runner v0;
sim-time scheduling belongs to the clock manager of task P2-T04. The one trigger in simulation
time is `when: {metric: sim_time, ...}`, which reads the simulator's clock from the stream.

Occurrences run concurrently on the runner's workers (three vehicles can drive at once, a link
drop runs while a vehicle drives), the earliest due first. The timeline ends when nothing runs
and nothing can still start. An entry still counts as able to start while it is due, while a
`when` is within its `within_s`, while an `after` entry's predecessor can still run, and while a
repeated entry has a `count` or `until_s` left; a `when` without `within_s` and an `every`
without `count` or `until_s` are *passive*: they run as long as the rest of the timeline does
and do not keep it going by themselves. When the timeline ends, what never ran is settled:

| Status | When |
| --- | --- |
| `ok` | the occurrence ended successfully; also a repeated entry whose occurrences all ran |
| `failed` | the event reported a failure (a goal aborted, a parameter refused) or raised; the run fails (`status: failed`) at once and the other running occurrences are `cancelled` |
| `timeout` | the entry's own limit (`timeout_s`) or the scenario's budget ended it; the budget ends the run with `status: timeout` |
| `skipped` | the entry never ran: its `when` never held (within `within_s`, or before the timeline ended), or its `after` entry did not end `ok` |

A `within_time` assertion ([assertions.md](assertions.md#every-assertion)) on an entry is how a
scenario requires it: it fails when the entry is skipped, when it fails, and when it takes
longer than `max_s`; on a repeated entry it judges the first occurrence.

### Conditions

A `when` is a mapping with a `condition`, an expression over `value` in the sandboxed language
of the `topic_value` assertion (comparisons, arithmetic, `and`, `or`, `not`, `abs`, `min`,
`max`, `sqrt`, `round`; [assertions.md](assertions.md#every-assertion)), applied to one of:

| Fields | `value` |
| --- | --- |
| `topic`, `field`, optional `message_type` | the field of each message of the ROS topic (relative to the vehicle's namespace, or absolute with a leading `/`), as the observer extracts it (`twist.twist.linear.x`, `data`, `ranges[0]`) |
| `metric: sim_time` | the simulator's clock, in seconds, from the observer's `clock` samples (once a second) |
| `metric: rtf` | the real-time factor measured over the last second |
| `metric: x`, `y`, `z`, `yaw` | the vehicle's ground-truth pose in the world, from the simulator (`yaw` in radians), at the observer's pose rate (10 Hz) |
| `metric: distance_to` with `target` | the horizontal distance, in metres, from the vehicle's ground truth to a placement of the world (a frame or coordinates) |

`vehicle` inside the `when` names whose topic or ground truth is read; it defaults to the
entry's vehicle, and a topic or a vehicle metric needs one of the two. The condition is
evaluated on every sample the observer streams, so its resolution is the sample rate: a
`distance_to` trigger fires within 0.1 s of simulation time of the threshold, a topic trigger on
the message that crosses it. A sample the condition cannot be evaluated for (a non-numeric
field) does not trigger. The observer subscribes to the topics the conditions name as it does
for the assertions ([assertions.md](assertions.md#how-a-run-is-observed)).

## Built-in events

`fieldproof events` lists every action the engine knows, where it comes from and its
parameters. The engine-side built-ins (session 1 of the task):

| Action | Fields | Effect |
| --- | --- | --- |
| `navigate_to_pose` | `vehicle`, `goal` (a placement in the world), `timeout_s` (300 s) | send a Nav2 `NavigateToPose` goal in the vehicle's `map` frame (which starts at its spawn pose) with `ros2 action send_goal` inside the vehicle's container and wait for its result: `ok` on `SUCCEEDED`, `failed` on any other status, `timeout` after `timeout_s` ([scenarios.md](scenarios.md#placements)) |
| `link.set`, `link.drop`, `link.restore` | `vehicle`, `profile` / `duration_s` | the radio link events of task P2-T02, unchanged ([network.md](network.md#timeline-events)); a scenario without a `network` section is refused |
| `container.pause` | `vehicle`, `duration_s` (optional) | pause the vehicle's container (`docker pause`: every process of the stack under test freezes while the simulator and the bridge go on, so the vehicle's last command stands in the world until the stack resumes; a container that is paused answers no `exec`); with `duration_s` the entry resumes the container after that long and ends then, without it the container stays paused until a `container.resume` (the teardown resumes it before stopping it) |
| `container.resume` | `vehicle` | resume the paused container |
| `container.kill` | `vehicle`, `signal` (`SIGKILL` by default; `SIGTERM`, `SIGINT`, `SIGHUP`) | send the signal to the vehicle's container; the run then expects the container gone (its exit does not end the run, its log is still kept), and the assertions decide what the exit meant for the fleet |
| `sim.pause`, `sim.resume`, `sim.step` | `duration_s` (pause: optional, wall clock) / none / `steps` or `duration_s` | the simulator's clock, through the world's `/world/<name>/control` service ([time.md](time.md#pausing-resuming-and-stepping)): a pause stops the physics and every node on simulation time (the clock keeps publishing the time it stopped at); a step advances a paused world by physics steps or simulation seconds and pauses again; refused in lockstep mode, where the engine steps the clock |
| `param.set` | `vehicle`, `node` (relative to the vehicle's namespace, or absolute), `parameter`, `value` (a boolean, a number or a string, which stays a string however it looks), `timeout_s` (30 s) | `ros2 param set` inside the vehicle's container; `ok` when the node accepted the value, `failed` when it refused it (a parameter callback) or is not found within `timeout_s` (the CLI's graph lags behind the stack for a few seconds, so the event asks again once a second), `timeout` when the node does not answer. Whether the node *uses* a value changed at run time is the node's business, as in the field |

A `param.set` or a `navigate_to_pose` aimed at a killed or paused container fails (`ros2`
cannot run there), and a command still running in a container when it is paused freezes with
it, so a scenario orders such entries with `after` (a list waits for all of them).

### The world events

The world events change the simulated world while it runs, through the `fieldproof::WorldEvents`
system every Fieldproof world loads ([worlds.md](worlds.md#the-world-events-plugin)): a
gz-transport service per event under `/world/<name>/fieldproof/`, which the engine calls with
the `gz service` CLI inside the simulator's container. The plugin applies a request in the
simulator's next step and answers once it has, with a one-line report that the entry takes as
its detail (`spawned model crate at (10, 0.8, 0.25, yaw 0)`); a request the plugin refuses (a
name already taken, a model that does not exist, SDF that does not parse) ends the entry
`failed` with the reason. None of them needs a `vehicle` (a `when` names its own).

| Action | Fields | Effect |
| --- | --- | --- |
| `spawn_object` | `model` (its name in the world), `pose` (a placement: a frame or `x`, `y`, `z`, `yaw`), then either a `shape` (`box` with `size: [x, y, z]`, `cylinder` with `radius` and `length`, `sphere` with `radius`), `static` (true), `mass_kg` (1, for a dynamic shape), `color` (`[r, g, b]`), or a `uri` (`model://<name>` of the simulator's resource path: a Fuel model of the world, a package model) | spawn an obstacle. A shape stands on the placement (its `z` is the ground; the model's centre is half the height above it) and is one link with a box, cylinder or sphere collision and visual, which keeps the real-time factor (a mesh on the ground plane halves it, [worlds.md](worlds.md#physics-step-and-real-time)); a URI model is placed at the pose as it is. The lidar sees a spawned obstacle in its next scan, so a navigation stack reacts to it as to anything else. The name must not be a vehicle's |
| `remove_object` | `model` | remove a model of the world by name (one spawned by `spawn_object`, or any model the world file declares; not a vehicle, which `container.kill` ends) |
| `teleport` | `vehicle` or `model`, `pose` (a placement) | move the model to the pose at once, velocities kept. A vehicle keeps its spawn height above the pose's ground `z` (0.12 m for the reference UGV); its stack is not told, so its localisation has to recover (the kidnapped-robot problem, as in the field) |
| `set_wind` | `velocity: [x, y, z]` (m/s, world frame: x east, y north, z up), `enabled` (true) | set the world's wind: the wind entity's seed and velocity, and Gazebo's WindEffects system (which every Fieldproof world loads) applies the force to the links of the models that enable wind (`<enable_wind>true</enable_wind>`: the aerial and surface vehicles of later phases; the reference UGV does not, so the wind changes nothing it does); `wind_info` reports it |

```yaml
  - name: spawn_crate                   # half a metre into the drive, a crate ahead
    when: { vehicle: ugv1, metric: distance_to, target: { frame: spawn_dock }, condition: value > 0.5 }
    within_s: 3m
    action: spawn_object
    model: crate
    shape: box
    size: [0.6, 0.6, 0.5]
    pose: { x: 10.0, y: 0.8 }
  - name: gust
    at: 20s
    action: set_wind
    velocity: [8.0, 0.0, 0.0]
  - name: clear_crate
    after: drive_ahead
    action: remove_object
    model: crate
```

### The fault events

The fault events make a sensor, the drive or the battery misbehave as a failing driver would,
without touching the vehicle's container: every adapter the bridge bundle runs between the
simulator's bridge and the vehicle is a **fault injector** ([bridge.md](bridge.md#adapters)):
the IMU and GNSS adapters, the throttle, the relays of the other sensor topics (the laser scan,
the odometry) and of the drive command, and the battery node of a manifest with a `power`
section. A fault is a parameter of the injector, which the event sets with `ros2 param set`
inside the bridge container (asked again while the CLI's graph lags behind the node, as
`param.set` does); with `duration_s` the entry holds that long and sets the parameter back
(`fix denied for 20 s, lifted`), without it the fault stays for the rest of the run. Each event
resolves its injector when the scenario is validated, from the vehicle's bridge bundle (the
same generator the bridge container runs), and refuses what the bundle does not route through
one: the camera topics stay on the C++ path, a manifest without a `power.battery` has no
battery, a manifest without a GNSS no fix to deny.

| Action | Fields | Effect |
| --- | --- | --- |
| `gps.deny` | `vehicle`, `sensor` (the manifest's GNSS sensor; the only one by default), `duration_s` | the receiver reports no fix: `status.status` `STATUS_NO_FIX`, NaN latitude, longitude and altitude, unknown covariance (jamming, a tunnel, a roof) |
| `gps.offset` | `vehicle`, `sensor`, `east_m`, `north_m`, `up_m` (0), `duration_s` | the fix moved by metres east, north and up at its latitude (multipath, spoofing) |
| `imu.bias` | `vehicle`, `sensor` (the manifest's IMU), `gyro_radps: [x, y, z]`, `accel_mps2: [x, y, z]` (zeros), `duration_s` | a bias added to the angular velocity and the linear acceleration of every message |
| `sensor.dropout` | `vehicle`, `sensor`, `duration_s` | the sensor goes silent: its topic stops. `sensor` is a sensor of the manifest (`lidar`, `imu`, `gps`), a drive interface (`odometry`, `joint_states`, `command`: the drive command on its way to the simulator) or `battery`, or one of their topics (`scan`, `/ugv1/odom`) |
| `sensor.freeze` | `vehicle`, `sensor`, `duration_s` | the sensor hangs: its last message repeats, stamp included (a hung driver; a consumer sees a stale stamp) |
| `battery.drain` | `vehicle`, `to_pct` (0), `duration_s` (required) | the battery's percentage falls linearly, in simulation time, from where it is to `to_pct` over `duration_s`; the voltage follows between the manifest's empty and full voltage, the status is `DISCHARGING` meanwhile; the entry waits the duration out (`drained to 0 % over 60 s`), so an entry `after` it runs on a flat battery. The reference vehicles simulate no power system: the state is the manifest's figures and the drain, nothing else drains it |
| `actuator.limit` | `vehicle`, `max_linear_mps`, `max_angular_radps` (one or both), `duration_s` | the actuators deliver no more than these speeds: the drive command is clamped between the vehicle and the simulator (a derated motor controller, a slipping wheel); the vehicle's own `cmd_vel` is recorded as it commanded it, the limited command goes on the hidden `_bridge/cmd_vel` |

```yaml
  - name: deny_gps                      # 5 s in, no fix for 20 s
    vehicle: ugv1
    at: 5s
    action: gps.deny
    duration_s: 20s
  - name: tired_motors                  # after the denial, the drive capped at 0.3 m/s
    vehicle: ugv1
    after: deny_gps
    action: actuator.limit
    max_linear_mps: 0.3
  - name: flat_battery
    vehicle: ugv1
    at: 10s
    action: battery.drain
    to_pct: 5
    duration_s: 2m
```

A fault's effect on the stack under test is the stack's business, as in the field: the example
vehicle navigates by SLAM and odometry and never reads the fix, so a `gps.deny` changes nothing
about its drive (which the GNSS-denial example shows), while a `sensor.dropout` of its lidar
stops its costmaps and Nav2 holds. What the assertions can see: the sensor topics the vehicle
sees (`topic_value` on `gps/fix` `status.status`, `topic_rate` on `scan`), the ground truth and
the recording.

## The record

Every occurrence of a timeline entry is recorded three times over:

- `run.json` `timeline[]`, one object per occurrence (the entries that never ran included):
  `name` (`say_hello#2` for the second occurrence of `say_hello`), `action`, `vehicle`, `at_s`,
  `trigger` (`at_s`, and `after`, `when` with `within_s`, `every_s` with `count` and `until_s`
  as the entry has them), `occurrence`, `status`, `detail` (what the event reported:
  `goal SUCCEEDED`, `paused for 3 s, resumed`, `condition never held within 240 s`),
  `params` (the entry's own fields), `started_at` (UTC), `t_s` (seconds after the timeline
  started), `sim_s` (simulation time covered by the stream when it started) and `duration_s`;
- `engine.log`, one `action.start` line when the occurrence starts and one `action.end` line
  when it ends, both carrying the same record as fields (`action`, `event_action`, `vehicle`,
  `occurrence`, `status`, `t_s`, `sim_s`, `trigger`, `params`), plus `action.triggered` when an
  `after` or a `when` fires, `action.skipped` and `action.settled` when the timeline ends, and
  the events' own lines (`action.goal`, `container.paused`, `container.resumed`,
  `container.killed`, `link.applied`, `sim.paused`, `sim.stepped`, `sim.resumed`, and the
  commands' output as `action.output` at `debug`);
- `/fieldproof/events` in the recording, since the observer relays every line of `engine.log`
  onto that side channel as it is written ([assertions.md](assertions.md#recording)); the MCAP
  holds the run's events next to the topics they changed.

The Markdown and JUnit reports list every occurrence with its status, duration and detail
([assertions.md](assertions.md#reports)); the dry run (`fieldproof run --dry-run`) shows each
entry with its trigger and what its event resolved (a goal in the map frame, a node's absolute
name, a `when` with its topic made absolute or its target in the world).

## Writing a plugin

An event is a Python class deriving from `fieldproof.events.Event` with three class attributes
and two hooks, registered through an entry point of the group `fieldproof.events`:

```python
from typing import Any, ClassVar, Literal
from pydantic import Field
from fieldproof.dsl import TimelineBase
from fieldproof.events import OK, ActionRecord, Event, EventContext, PlanContext
from fieldproof.manifest import Name

class Announce(TimelineBase):                  # the entry: what the scenario writes
    vehicle: Name = Field(description="The vehicle whose container publishes.")
    action: Literal["example.announce"]       # the discriminator, equal to Event.action
    text: str = Field(description="The text of the message.")

class AnnounceEvent(Event):
    action: ClassVar[str] = "example.announce"
    Entry: ClassVar[type[TimelineBase]] = Announce
    summary: ClassVar[str] = "Publish a std_msgs/msg/String once on a topic of the vehicle."

    def plan(self, entry: Announce, context: PlanContext) -> dict[str, Any]:
        vehicle = context.vehicle(entry.vehicle)          # a ScenarioError if unknown
        return {"topic": vehicle.topic("announce")}       # shown by the dry run

    def apply(self, entry: Announce, resolved: dict[str, Any],
              context: EventContext, record: ActionRecord) -> None:
        result = context.ros2(context.vehicle(entry.vehicle),
                              ("topic", "pub", "--once", resolved["topic"],
                               "std_msgs/msg/String", f'{{"data": "{entry.text}"}}'),
                              timeout_s=resolved["limit_s"])
        record.status, record.detail = (OK, "published") if result.ok else ("failed", result.tail(3))
```

```toml
# pyproject.toml of the plugin's distribution
[project.entry-points."fieldproof.events"]
"example.announce" = "fieldproof_example_events:AnnounceEvent"
```

- `Entry` derives from `fieldproof.dsl.TimelineBase`, which brings the triggers (`at`, `after`,
  `when`, `within_s`, `every`, `count`, `until_s`), `name` and an optional `vehicle` (declare
  `vehicle: Name` to require it). Its `action` field is a `Literal` equal to the event's
  `action`; its other fields, with their descriptions, are the parameters (`fieldproof events`
  prints them, `fieldproof schema` exports them). Unknown keys are errors, as in every section.
- `plan(entry, context)` runs when the scenario is resolved (`fieldproof validate`, the dry
  run, before any container starts). The `PlanContext` has the `world` (its frames), the
  `vehicles` (name, namespace, spawn pose, `topic()` to make a name absolute under the
  namespace), whether the scenario has radio `links`, and `where` (`timeline[2]`) for error
  messages. It returns what the dry run shows and what `apply` gets back as `resolved`
  (dataclasses such as a `Pose` are turned into data for the record); it raises a
  `ScenarioError` to refuse the entry.
- `apply(entry, resolved, context, record)` runs on a worker thread when the occurrence is due.
  `resolved` is `plan`'s result plus `limit_s`, how long the occurrence may take (the entry's
  `timeout_s` under the scenario's budget). The `EventContext` offers `vehicle(name)`,
  `ros2(vehicle, argv, timeout_s=..., on_line=...)` (a `ros2` command inside the vehicle's
  container, through the image's entry point, interrupted after `timeout_s` with exit code
  124), `bridge_ros2(vehicle, argv, timeout_s=..., on_line=...)` (the same inside the
  vehicle's bridge container, where the fault injectors run; the bridge runs on whether the
  vehicle's container is paused, killed or behind a link), `gz_service(service,
  request_type=..., reply_type=..., request=..., timeout_s=...)` (`gz service -s` inside the
  simulator's container, the request in protobuf text format, the reply printed the same way:
  the world events), `exec(container, argv, on_line)` (any command in any container of the
  run), `backend` (the container runtime: `pause`, `unpause`, `kill`, `state`, ...),
  `log(event, message, **fields)` (a line of `engine.log`, relayed to `/fieldproof/events`),
  `clock()`, `hold(seconds)` (wait in slices, `False` when the run is ending), `tearing_down`,
  `expect_exit(container)` (a container the event is about to kill), `mark_paused(container,
  paused)` (so the teardown resumes it), `apply_link(vehicle, profile, cause)` and
  `link_profile(vehicle)` (the radio links). A vehicle handle has `name`, `namespace`,
  `container_name`, `bridge_name`, `spawn`, `topic(name)` and `bundle()`, the plan of its
  bridge bundle ([bridge.md](bridge.md#the-bundle)): `bundle().injector("lidar")` is the
  node a fault of the lidar sets, `bundle().fully_qualified(node.name)` its name. The hook
  sets `record.status` (`ok`, `failed`, `timeout`) and `record.detail`; a record left
  `running` ends `ok`, an exception ends it `failed` with the exception's text.
- Registration: the distribution is installed in the engine's environment (`uv add --project
  engine --dev --editable <path>` for a plugin in the workspace, or `pip install` into that
  environment). The engine loads every entry point of the group when it starts; a plugin that
  fails to import or is malformed (no `action`, an `Entry` that is not a `TimelineBase`, an
  `action` literal that disagrees, an `action` another event has) is reported by
  `fieldproof events` (exit code 2) and by `fieldproof validate` (a warning) and its action
  stays unknown to the schema. The built-ins are registered the same way by the engine's own
  `pyproject.toml`, and loaded directly as well.

`examples/plugins/example_events/` is this example in full, with its tests and a README; the
engine's development environment installs it, so `fieldproof events` lists
`example.announce` next to the built-ins and `examples/scenarios/ugv_events.yaml` validates.
The committed JSON Schema (`engine/schemas/scenario.schema.json`, `make scenario-schema`) is
exported from that environment, the example plugin included.

What a plugin cannot do: run arbitrary code at validation time (its `Entry` is data; its hooks
run in the engine's process on the engine's terms), name an action another event has, or
change the triggers (they are the DSL's).

## Example

`examples/scenarios/ugv_events.yaml` is the timeline above on the example vehicle `ugv_nav2`:
the vehicle drives 4 m ahead from the loading dock while `say_hello` publishes three times from
its container, `tighten_failsafe` sets a parameter of the stack's link monitor once the vehicle
has moved a metre, `freeze_stack` pauses the container for 3 s once both have ended and
`stop_stack` kills it. Seven assertions check the drive and, through `within_time`, that each
event ran.

`examples/scenarios/ugv_obstacle_replan.yaml` is the first definition-of-done scenario of the
task: the vehicle drives 4.5 m ahead through the roller door; half a metre into the drive a
`when` on its distance to the spawn fires `spawn_object` and a 0.6 m crate stands on the
straight line, 1.7 m ahead and in the lidar's view; Nav2's costmaps take it in, its planner
routes around it (the door is 4 m wide) and the vehicle reaches the goal without a contact;
`remove_object` clears the crate once the drive has ended. Six assertions: the crate spawned
(`within_time`), the goal reached, in time, the path kept being published (`topic_rate` on
`plan`: 1 Hz of simulation time on a GPU host, 0.2 Hz under software rendering on a four-core
CI runner, so the floor is 0.1 Hz over 10 s windows; the drive has 8 min, since a loaded runner has
needed 200 s of simulation time for it), no contact, the crate cleared. The
integration test adds what the simulator saw: the
vehicle's ground truth leaving the straight line to pass the crate.

`examples/scenarios/ugv_gps_denied.yaml` is the second: the same drive, with `gps.deny` 5 s in
for 20 s and a drive back to the dock once both the drive and the denial have ended. Seven
assertions: the fix present before the denial, absent through a window the denial is sure to
cover (13 s to 24 s: the parameter's round trip through the CLI and the margin software
rendering needs in CI), back for the return leg (`topic_value` on `gps/fix` `status.status`,
`always` and `eventually`), the event run (`within_time`), the goal reached, the return made,
no contact. The example stack never reads the fix, so the denial changes nothing about the
drive, which is the point: a vehicle that depended on it would show it here.

```bash
uv run --project engine fieldproof events
uv run --project engine fieldproof validate examples/scenarios/ugv_events.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_events.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_obstacle_replan.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_gps_denied.yaml
```

## Tests

- `engine/tests/test_dsl.py`: the triggers of an entry and their errors, the `when` forms and
  their errors, the example, the cross-entry checks (`after` names, self and cycles, repeated
  entries, unique names, unknown actions naming the known ones); `test_schedule.py`: the
  scheduler with numbers (due, live, wait, `after` chains, `when` samples and metrics,
  `within_s`, `every` with `count` and `until_s`, overruns, settling); `test_events.py`: the
  registry (the built-ins, the example plugin, malformed and broken entry points, collisions),
  the built-in events through a scripted context (the commands they run, every outcome), the
  timeline union, `fieldproof events`; `test_events_runner.py`: the example scenario on the
  fake backend (every trigger, the record and the log lines, a condition that never holds, a
  `within_s`, a skipped chain, a failing event, an event raising, a paused container at the
  teardown, a killed container, a sim-time condition, the budget while a condition waits);
  `examples/plugins/example_events/tests/test_announce.py`: the plugin (run with the engine's
  tests); `test_world_fault_events.py`: the world events (the entries' validation, the SDF of
  every shape and of a URI model, the poses and services `plan` resolves, the requests `apply`
  composes and every reply: applied, refused, no answer) and the fault events (the injector
  each resolves from the bridge bundle by sensor, interface or topic and the refusals, the
  parameters set and lifted in the bridge, the CLI's lag, a refused value, the budget ending a
  fault, the run ending during one, the battery's wait). `make test-engine` runs them all.
- `sim/fieldproof_gz_plugins/test/test_world_events.cpp` (`make test-ros`): the WorldEvents
  plugin on a headless server ([worlds.md](worlds.md#the-world-events-plugin));
  `bridge/fieldproof_bridge/test/test_faults.py`: the injectors' logic (the pass-through filter
  with dropout and freeze, the parameter callback, the GNSS denial and offset, the IMU bias,
  the command limit, the battery's ramp and state), and `test_generator.py` the routing of
  every injectable output and the command through them; the bridge contract test runs the
  real bundle, relays included, against the reference UGV.
- `tests/integration/test_event_injection.py` (`make test-integration`): the example scenario
  against the real stack: exit code 0, every entry `ok` with its trigger and parameters, the
  seven assertions passed, the vehicle container seen paused and then exited by `docker
  inspect` while its log is kept, the `action.start`, `action.end` and `action.triggered` lines
  of `engine.log`, the three `/announce` messages in the recording, nothing left behind.
- `tests/integration/test_world_events.py`: the plugin's services against a bare simulator
  (the compose `sim` service), with the requests the events compose (a crate spawned where
  asked and `gz model` reporting it, refused the second time, teleported, the wind set and
  reported by `wind_info`, the crate removed and a second removal refused); then
  `ugv_obstacle_replan.yaml` end to end: every entry `ok` with the plugin's reports, the six
  assertions passed, the crate spawned while the vehicle was still north of it, the ground
  truth showing the vehicle at least 0.57 m off the straight line beside the crate, the
  `world.changed` lines, nothing left behind.
- `tests/integration/test_fault_injection.py`: the injectors against the real bridge (the
  compose `sim` and `bridge` services with the spawned reference UGV, no vehicle stack): each
  fault set with `ros2 param set` in the bridge container as the events do and observed on the
  contract topic with `ros2 topic echo` (no fix then a fix, the fix moved 100 m east, the gyro
  biased, the scan silent then back, the odometry stamp frozen then moving, the battery falling
  to 50 % with its status, a 1 m/s command leaving the relay at 0.2 m/s); then
  `ugv_gps_denied.yaml` end to end: `deny_gps` `ok` with the fix denied for 20 s and lifted,
  both drives `ok`, the seven assertions passed, the `fault.injected` and `fault.lifted` lines,
  nothing left behind.

## Known limits

- In a replay run ([replay.md](replay.md)) the clock, world and fault events are refused at
  validation (no simulator, no bridge); `navigate_to_pose` (with a spawn), `container.*` and
  `param.set` work.

- The fault events reach what the bridge routes through an injector: the IMU, the GNSS fix,
  the laser scan, the odometry, the joint states, the drive command and the battery of the
  reference vehicles. The camera topics stay on the C++ path (images at 15 Hz are too heavy
  for a Python relay) and take no fault; a stack's own topics (what it computes from the
  sensors) are not the bridge's to fault. A fault applies to every message of the topic, not
  to one in a hundred: there is no probabilistic dropout yet.
- The battery is synthetic: the reference vehicles simulate no power system, so the state is
  the manifest's figures and what `battery.drain` says, and nothing else drains it. A Gazebo
  battery model (the LinearBatteryPlugin) could feed the same node later.
- `set_wind` acts on the links that enable wind; the reference UGV does not, so the wind
  changes nothing about it until the aerial and surface vehicles arrive (P4, P5).
- A spawned obstacle is a primitive shape or a model of the resource path; a `uri` model's
  collisions are whatever the model has (a mesh collision on the ground plane halves the
  real-time factor), and `spawn_object` cannot check that in advance.
- A fault's parameter takes a round trip through the `ros2` CLI inside the bridge container
  (one to a few seconds, more while the CLI's graph lags), so an assertion window on a fault
  starts a few seconds after the entry's `at`; the record's `t_s` and `duration_s` say when it
  was in place.
- Times are wall clock from the start of the timeline in `sim` and `realtime` mode, so `every`
  drifts with the host's load and a `when` fires on the first sample that satisfies it; in
  lockstep mode ([time.md](time.md#lockstep)) they are simulation seconds from the origin and
  the world waits at every due time, which is what makes a run repeatable.
- A `when` reads what the observer streams: topic fields at the topic's rate, the ground truth
  at 10 Hz, the clock once a second. Expressions over several topics, or over a window of
  samples, are not supported; chain entries with `after` or use several entries instead.
- `every` does not combine with `when`, and `after` cannot name a repeated entry.
- A container's pause freezes the whole stack, not one node, and leaves its last command
  standing in the simulator (the reference UGV's drive has no command timeout); a kill ends the
  container for good (there is no restart event yet).
