# Assertions, KPIs, recording and reports

Task P1-T06 turned a run into a test with evidence; task P2-T05 made the assertions a
library. A scenario's `assertions` are evaluated while the timeline runs, its `kpis` are
measured alongside, every topic the scenario asks for is recorded to MCAP, the engine's events,
verdicts, metrics, resource samples and KPIs travel on `/fieldproof/*` topics so the recording
holds the whole run, and the run ends with a report in JSON, Markdown and JUnit. The sixteen
assertion types below (the five primitives of v0 and the eleven of v1) are plugins of one
API, and a scenario may use types of its own.

```bash
uv run --project engine fieldproof run examples/scenarios/ugv_nav_to_goal.yaml      # PASS, exit 0
uv run --project engine fieldproof run examples/scenarios/ugv_nav_fails_on_time.yaml # FAIL, exit 1
uv run --project engine fieldproof report runs/<run_id>                             # the Markdown report
uv run --project engine fieldproof assertions                                       # the types known
```

## How a run is observed

Everything an assertion needs comes from two sources. The **recorder container** is a
container of the bridge image (ROS 2, rosbag2 with the MCAP storage, the Gazebo transport
bindings) that the runner starts after the bridges and before the vehicles, with the run
directory bind-mounted read-write at `/fieldproof/run` and the engine's observer script mounted
read-only. It runs as the user who started the run, so the files it writes belong to them. The
observer (`fieldproof.observer`) reads `observe.json`, which the runner writes from the
scenario:

- it subscribes to the ROS topics the assertions and KPIs name (discovering the message type
  from the graph unless `message_type` is given) and to the Gazebo ground truth:
  `/world/<name>/pose/info` for the vehicles' poses (and any other model a `min_distance`
  names) and each vehicle's contact sensor for `no_collision`;
- it appends one JSON line per observation to `samples.jsonl`: `pose` at 10 Hz per model,
  `rate` once a second per topic over a sliding window, `value` per message of a watched
  field, `contact` per contact message, `latency` once a second per stamped topic (the age of
  its messages over a sliding window, header stamp against the simulation clock at receipt),
  `lifecycle` once a second per managed node (its `get_state` answer, and every transition
  event as it happens), `clock` once a second with the real-time factor. The runner tails the
  file and feeds it to the **streaming evaluator** (`fieldproof.assertions.Evaluator`) every
  half second while the timeline runs;
- it runs `ros2 bag record` when `outputs.record` is on, and publishes the side channels.

The **resource monitor** (`fieldproof.resources`) runs in the engine: from the moment the
timeline starts it samples every container of the run (the vehicles, their bridges, the
simulator, the recorder) through the Docker daemon's statistics stream, the same numbers
`docker stats` shows, once a second, into `resources.jsonl` in the run directory (`resource`
lines: `container`, `role`, `vehicle`, `cpu_pct` in percent of one core, `memory_mb` the
working set in MiB, `memory_limit_mb`, `pids`). The runner feeds them to the evaluator like
the observer's samples; the observer relays them onto `/fieldproof/resources`.

Each assertion resolves once, as soon as the stream decides it: a `reach_pose` passes at the
first pose within tolerance, a `no_collision` fails at the first contact, a `within_time` fails
the moment its limit passes. Whatever is still pending when the timeline completes is settled
then. A run that ends before the timeline completes (a failed action, the budget, Ctrl-C)
marks the pending assertions `skipped`.

Times in verdicts are seconds after the timeline started, on the wall clock the engine and the
containers share (as the timeline's `at` is), or simulation seconds from the origin in
lockstep mode ([time.md](time.md#lockstep)); the evidence carries the simulation time of the
deciding sample as well where the stream has it. Topic rates and message latencies are
measured in **simulation time**, so a sensor's nominal rate holds whatever the real-time factor
of the host.

## Every assertion

Every assertion has a unique `name` (lower case, digits, underscores), a `type`, an optional
`on_fail` (`continue` by default: the timeline completes and the evidence of the whole run is
kept; `abort`: the timeline is cancelled the moment the assertion fails, the running entries
cancelled, the pending ones skipped, and the run ends `failed` naming it) and the parameters
of its type; unknown keys are errors. Topics are relative to the vehicle's namespace when
`vehicle` is given, absolute otherwise (a leading `/`). Windows: `after_s` is a grace period
after the timeline starts, `within_s` the end of the window (or a deadline); absent, the window
ends with the timeline. A vehicle must be one of the scenario's, an action the `name` of a
timeline entry, a condition a valid expression (below); `fieldproof validate` checks it all.

`fieldproof assertions` lists the types known to the engine with their parameters and metrics;
`fieldproof assertions --json` is the same as data.

### `reach_pose`

The vehicle's ground-truth pose, as the simulator reports it, comes within a tolerance of a
target.

| Key | Meaning |
| --- | --- |
| `vehicle` | the vehicle whose pose is read (its model name in Gazebo) |
| `target` | a placement in the world: a `frame` of the world file, or `x`, `y`, optional `yaw` |
| `tolerance_m` | distance to the target, 0.5 m by default |
| `yaw_tolerance_rad` | heading tolerance; the heading is not checked when absent |
| `within_s` | deadline after the timeline starts; the end of the timeline when absent |

Passes at the first sample within tolerance (and heading tolerance); fails when `within_s`
passes first or at the end of the timeline. Evidence: `reached` (or `closest`) with the sample's
time, position, heading, distance and heading error; `final`, the last pose seen; `samples`.
Metrics: `closest_m`, `reached_at_s`, `final_distance_m`.

### `within_time`

A timeline action reports a successful result within a duration of its start.

| Key | Meaning |
| --- | --- |
| `action` | the `name` of a timeline entry (an unnamed entry is `<action>@<at>s`) |
| `max_s` | the result must arrive within this long after the action starts |

Passes when the action completes `ok` in time; fails the moment `max_s` passes without a result,
when the action ends with another status, or if it never starts. Evidence: `started_t_s`,
`duration_s` or `elapsed_s`, `action_status`. Metrics: `duration_s`.

### `topic_rate`

A ROS topic keeps publishing at a rate.

| Key | Meaning |
| --- | --- |
| `topic`, `vehicle` | the topic, under the vehicle's namespace when `vehicle` is given |
| `min_hz` | lowest acceptable rate over a window |
| `max_hz` | highest acceptable rate (optional) |
| `window_s` | the sliding window, 5 s of simulation time by default |
| `after_s` | grace period after the timeline starts before windows count, 0 by default |
| `message_type` | `sensor_msgs/msg/LaserScan`; discovered from the graph when absent |

The observer emits one rate per second once a full window has passed since it subscribed.
Fails at the first window below `min_hz` or above `max_hz` after `after_s`; passes at the end of
the timeline, or fails there if no window was measured. Evidence: `windows`, `lowest_hz`,
`highest_hz`, `messages`, and the failing `window`. Metrics: `mean_hz`, `lowest_hz`,
`highest_hz`, `messages`. Rendering sensors do not always reach their nominal rate in
simulation time: with software rendering on a two-core CI runner the reference UGV's 10 Hz
lidar arrives at about 3 Hz of simulation time while a GPU host gives 9 to 10 Hz, so the
example asks for 2 Hz and a scenario meant for GPU hosts can ask for more.

### `no_collision`

The vehicle's chassis touches nothing during the timeline.

| Key | Meaning |
| --- | --- |
| `vehicle` | the vehicle whose contact sensor is read |
| `topic` | the Gazebo topic of a `gz.msgs.Contacts` sensor; `<namespace>/contacts` by default |
| `ignore` | entities whose contacts do not count: a model name (`cardboard_box`) or a scoped collision name |

The reference vehicles carry a ground-truth contact sensor on the chassis collision
([robots.md](robots.md#interfaces)); the casters and wheels are separate links, so any contact
of the chassis is a collision with the world. Fails at the first contact not ignored; passes at
the end of the timeline. Evidence: the `contact` (both collision names, the count), the
`ignored_contacts` and `messages` seen. Metrics: `contacts`.

### `topic_value`

A condition over a field of a topic's messages.

| Key | Meaning |
| --- | --- |
| `topic`, `vehicle` | the topic, under the vehicle's namespace when `vehicle` is given |
| `field` | the field: `linear.x`, `pose.pose.position.x`, `ranges[0]`, `status.status` |
| `condition` | an expression over `value` (below) |
| `mode` | `always` (every message after `after_s` satisfies it; the default) or `eventually` (one does) |
| `after_s` | grace period after the timeline starts, 0 by default |
| `within_s` | end of the window: the deadline of `eventually`, the end of `always`; the end of the timeline when absent (later than `after_s`) |
| `message_type` | `geometry_msgs/msg/Twist`; discovered from the graph when absent |

`always` fails at the first message that violates the condition and passes at `within_s` (or
the end) if at least one message was seen; `eventually` passes at the first message that
satisfies it and fails at `within_s` or the end. `after_s` and `within_s` make a window, so "the
vehicle stands still while its link is down" is `mode: always`, `after_s: 13s`, `within_s: 24s`
over the odometry's speed ([network.md](network.md#a-vehicle-on-a-link)). A field that does not
exist fails the assertion with the observer's message. Evidence: `samples`, `lowest`, `highest`,
`last`, and the `violation` or `satisfied` sample. Metrics: `samples`, `lowest`, `highest`,
`last`. The temporal combinators below are the same over several signals.

### `geofence`

The vehicle's ground truth stays inside (or outside) a region of the world.

| Key | Meaning |
| --- | --- |
| `vehicle` | the vehicle whose pose is read |
| `region` | exactly one of `polygon` (a list of `[x, y]` vertices, three or more, in order), `circle` (`x`, `y`, `radius_m`) or `box` (`x_min`, `x_max`, `y_min`, `y_max`); optional `z_min`, `z_max` bound the height |
| `mode` | `inside` (the default) or `outside` |
| `after_s`, `within_s` | the window |

Fails at the first pose on the wrong side of the boundary (a pose on the boundary counts as
inside); passes when the window closes or the timeline ends, or fails then if no pose was seen.
Evidence: `min_clearance` (the pose closest to the boundary, with its signed clearance: how
far inside, or past it when negative), the `violation` pose, `samples`. Metrics:
`min_clearance_m`, `samples`.

```yaml
- name: stays_in_yard
  type: geofence
  vehicle: ugv1
  region: { box: { x_min: 5.0, x_max: 15.0, y_min: -5.0, y_max: 7.0 } }
```

### `min_distance`

The vehicle keeps a minimum distance to another vehicle, a model of the world or a point.

| Key | Meaning |
| --- | --- |
| `vehicle` | the vehicle whose pose is read |
| `to` | exactly one of `vehicle` (another vehicle of the scenario), `model` (a model of the world, by name; the observer streams its pose too) or `target` (a placement) |
| `min_m` | the distance never to go below, between the model origins |
| `after_s`, `within_s` | the window |

Measured at every pose sample of either model once both are known. Fails at the first sample
closer than `min_m`; passes when the window closes or the timeline ends, or fails then if no
distance was measured. Evidence: `closest` (when, how close, both positions), `samples`.
Metrics: `closest_m`, `closest_at_s`, `final_m`.

```yaml
- name: clears_the_crate
  type: min_distance
  vehicle: ugv1
  to: { model: crate }
  min_m: 0.5
```

### `path_efficiency`

The path the vehicle drove is close to the straight line, and short enough.

| Key | Meaning |
| --- | --- |
| `vehicle` | the vehicle whose pose is read |
| `action` | a timeline entry: the window is its run, from its start to its end |
| `after_s`, `within_s` | the window, when there is no `action` |
| `target` | the straight line's end (a placement); the last pose of the window when absent |
| `min_ratio` | the straight-line distance over the path length, 0.5 by default; 1 is a straight drive |
| `max_path_m` | the path length never to exceed (optional) |

The path length is the sum of the distances between consecutive ground-truth poses in the
window (10 Hz); the straight line runs from the first pose of the window to the target. Judged
when the action ends (whatever its status), when the window closes or when the timeline ends;
a path over `max_path_m` fails as soon as it is; fewer than two poses in the window fail. A
vehicle that did not move and has no target has efficiency 1. Evidence: `path_m`, `straight_m`,
`efficiency`, `max_speed_mps`, `mean_speed_mps`, `samples`. Metrics: the same.

```yaml
- name: drives_straight
  type: path_efficiency
  vehicle: ugv1
  action: drive_ahead
  target: { x: 10.0, y: -1.0 }
  min_ratio: 0.6
```

### `mission_time`

A span of the timeline completes within a duration.

| Key | Meaning |
| --- | --- |
| `max_s` | the span must complete within this long |
| `min_s` | and take at least this long (optional) |
| `start` | the timeline entry whose start opens the span; the start of the timeline when absent |
| `end` | the timeline entry whose successful end closes the span; the end of the timeline when absent |

`within_time` bounds one entry; `mission_time` bounds the whole timeline or a span of it.
Fails the moment `max_s` passes without the end, when the `end` entry ends with another status
than `ok`, when the span is shorter than `min_s`, or when an entry never starts or completes.
Evidence: `started_t_s`, `ended_t_s`, `duration_s`. Metrics: `duration_s`.

### `message_latency`

The messages of a stamped topic arrive fresh.

| Key | Meaning |
| --- | --- |
| `topic`, `vehicle` | the topic (its messages must carry a `header.stamp`) |
| `max_ms` | the latency never to exceed, in milliseconds |
| `statistic` | which statistic of a window is judged: `p95` (the default), `max` or `mean` |
| `window_s` | the sliding window, 2 s of simulation time by default |
| `after_s`, `within_s` | the window of time |
| `message_type` | discovered from the graph when absent |

The observer measures the age of every message, its header stamp against the simulation clock
when it is received, and reports `count`, `mean_ms`, `max_ms`, `min_ms` and `p95_ms` over the
sliding window once a second. Fails at the first window whose statistic is above `max_ms`,
or when a message carries no header stamp; passes when the window closes or the timeline ends,
or fails then if no window was measured. Ages are in simulation time: a stack that lags the
simulator by a second of wall clock at a real-time factor of 0.3 reports 300 ms. The observer's
clock is the bridged `/clock`, so a message can look a few milliseconds ahead of the clock
(a negative age) as well as behind it. Evidence: the failing `window`, `windows`, `p95_ms`,
`max_ms`, `mean_ms`, `messages`. Metrics: `p95_ms`, `max_ms`, `mean_ms`, `messages`.

```yaml
- name: odom_fresh
  type: message_latency
  topic: odom
  vehicle: ugv1
  max_ms: 250
  after_s: 2s
```

### `sequence`

Events happen in order.

| Key | Meaning |
| --- | --- |
| `steps` | the steps, in order (one or more): each an `action` with its `event` (`started`, or `finished` meaning ended `ok`, the default), a signal (`topic` and `field`, or `metric` with `target` for `distance_to`, with `vehicle` and `message_type` as a `when` has them) with a `condition` over `value`, or an `assertion` (another assertion of the scenario, which must pass); an optional `name` labels the step in the evidence |
| `vehicle` | the vehicle the signal steps default to |
| `within_s` | the last step must happen by then (optional) |
| `max_gap_s` | each step must happen within this long after the previous (optional) |

Each step is watched only once the previous one has happened: the first message after it that
satisfies a signal step's condition, the action's start or `ok` end, the other assertion's
verdict (a verdict given before the sequence reached that step counts; a failed or skipped one
fails the sequence, as does an action step whose entry ends with another status). Passes when
the last step happens; fails at `within_s`, at `max_gap_s` after the previous step, or at the
end of the timeline, naming the step that did not happen. Assertions a sequence waits for are
settled before it at the end of the timeline. Evidence: `steps` (each one that happened, when
and how), `steps_done`. Metrics: `steps_done`, `completed_at_s`, `duration_s`.

```yaml
- name: mission_order
  type: sequence
  vehicle: ugv1
  steps:
    - { action: drive_ahead, event: started }
    - { topic: cmd_vel, field: linear.x, condition: value > 0 }
    - { assertion: reached_goal }
```

### `resource_usage`

A container of the vehicle stays within a CPU and memory footprint.

| Key | Meaning |
| --- | --- |
| `vehicle` | the vehicle whose container is measured |
| `container` | `vehicle` (the stack's container, the default) or `bridge` |
| `max_cpu_pct` | CPU limit in percent of one core, as `docker stats` shows it (a two-core quota is 200), judged on the mean over `window_s` |
| `max_memory_mb` | memory limit in MiB (the working set, the page cache left out), judged on every sample |
| `window_s` | the CPU averaging window, 5 s by default (one sample a second) |
| `after_s`, `within_s` | the window of time |

One of the limits at least. Fails at the first sample over the memory limit or the first
covered window whose CPU mean is over the limit; passes when the window closes or the timeline
ends, or fails then if the container was not sampled. The samples come from the resource
monitor, which runs on every host that runs Docker; the budget the manifest sets
([manifest.md](manifest.md)) is enforced by the runtime, so a limit under the budget is what
makes the assertion say something. Evidence: the `violation`, `peak_cpu_pct`, `mean_cpu_pct`,
`peak_memory_mb`, `mean_memory_mb`, `samples`. Metrics: the same.

### `lifecycle_state`

A managed (lifecycle) node of the vehicle is in a state.

| Key | Meaning |
| --- | --- |
| `vehicle` | the vehicle the node belongs to |
| `node` | the node, relative to the vehicle's namespace (`bt_navigator`) or absolute |
| `state` | `unconfigured`, `inactive`, `active` or `finalized` |
| `mode` | `eventually` (the default) or `always` |
| `after_s`, `within_s` | the deadline of `eventually`, the window of `always` |

The observer asks the node's `get_state` service once a second (a node that offers no service
answers `unavailable`, one that does not answer before the next call `unresponsive`) and
subscribes to its transition events, so a change is seen as it happens. `eventually` passes at
the first sample in `state`; `always` fails at the first sample in another state (the two
answers above included) and passes when the window closes. Both fail if no state was observed.
Evidence: `states` (how often each was seen), `last`, the `violation`, `samples`. Metrics:
`samples`, `reached_at_s`.

### `always`, `never`, `eventually`

A condition over several named signals, the temporal combinators.

| Key | Meaning |
| --- | --- |
| `signals` | the named signals, one or more: each a topic field (`topic`, `field`, optional `message_type`) or a `metric` of the run (`sim_time`, `rtf`, or the vehicle's ground-truth `x`, `y`, `z`, `yaw`, `distance_to` a `target`), with an optional `vehicle` of its own |
| `condition` | an expression over the signals' names (every signal must be read) |
| `vehicle` | the vehicle the signals default to |
| `after_s`, `within_s` | the window: the deadline of `eventually`, the end of `always` and `never` |
| `for_s` | `eventually` only: the condition must keep holding this long once it first does |

The condition is evaluated every time one of the signals changes, once all of them have a
value. `always` fails at the first evaluation that does not hold and passes when the window
closes; `never` fails at the first that holds; `eventually` passes at the first that holds
(with `for_s`, once it has held that long without a break, judged on the samples and on the
clock) and fails at the deadline. All three fail if the condition was never evaluated (a signal
without a value) and when a signal's field does not exist or the condition cannot be evaluated
for the values seen. Evidence: `evaluations`, `last_values`, the `violation` or `satisfied`
snapshot of the values, `held_since_t_s`. Metrics: `evaluations`, `satisfied_at_s`,
`longest_hold_s`.

```yaml
- name: holds_while_lost_combined
  type: eventually
  vehicle: ugv1
  signals:
    lost: { topic: link_state, field: data }
    speed: { topic: odom, field: twist.twist.linear.x }
  condition: not lost and abs(speed) < 0.05
  after_s: 5s
  for_s: 10s
  within_s: 36s
```

### Conditions

A condition is a Python expression limited to what a test needs: comparisons (`<`, `<=`, `==`,
`!=`, `in`, `is`), arithmetic, `and`/`or`/`not`, a conditional expression, indexing, the
functions `abs`, `min`, `max`, `round`, `sqrt`, `len`, `float`, `int`, `isnan`, `isinf`, the
constants `pi`, `inf`, `nan`, and the names it may read: `value` (the message field) or the
signals' names. Anything else (attributes, other names, lambdas, comprehensions) is rejected
when the scenario is validated. A signal's name is an identifier that is not one of the
functions or constants. Examples: `value <= 1.5`, `abs(value) < 0.2`, `0 < value and value < 2`,
`value == 'ACTIVE'`, `len(value) == 720`, `not lost or abs(speed) < 0.05`.

## KPI collectors

The scenario's `kpis` section lists **collectors**: what to measure without judging. Each has
a unique `name` and a `type`; `fieldproof assertions` lists them under the assertion types.

| Type | Parameters | Metrics |
| --- | --- | --- |
| `path` | `vehicle`, optional `action` (its run is the window) or `after_s`/`within_s`, optional `target` | `path_m`, `straight_m`, `efficiency`, `max_speed_mps`, `mean_speed_mps` |
| `mission_time` | optional `start` and `end` entries | `duration_s` |
| `distance` | `vehicle`, `to` (`vehicle`, `model` or `target`), `after_s`, `within_s` | `closest_m`, `closest_at_s`, `final_m` |
| `rate` | `topic`, `vehicle`, `window_s`, `after_s`, `message_type` | `mean_hz`, `lowest_hz`, `highest_hz`, `messages` |
| `latency` | `topic`, `vehicle`, `window_s`, `after_s`, `message_type` | `p95_ms`, `max_ms`, `mean_ms`, `messages` |
| `resources` | `vehicle`, `container`, `after_s` | `peak_cpu_pct`, `mean_cpu_pct`, `peak_memory_mb`, `mean_memory_mb`, `samples` |
| `value` | `topic`, `vehicle`, `field`, optional `count_if` (a condition over `value`), `after_s`, `within_s`, `message_type` | `samples`, `min`, `max`, `mean`, `last`, `count` (how often `count_if` held) |

A collector ends the timeline `measured`, or `missing` when it had nothing to measure; neither
fails the run. Every assertion's checker measures as well (the metrics listed above), so the
**run's KPIs** are one flat map, `<name>.<metric>` to a value with its unit and source, in
`run.json` under `kpis` (the collectors' own results under `kpi_collectors`), in `report.json`,
as a table of the Markdown report, as `kpi.<name>` properties of the JUnit document, written
to `kpis.jsonl` at the end of the timeline and published on `/fieldproof/kpis`. `fieldproof
runs diff` (task P2-T06) compares these maps between runs.

```yaml
kpis:
  - { name: drive, type: path, vehicle: ugv1, action: drive_ahead, target: { x: 10.0, y: -1.0 } }
  - { name: mission, type: mission_time }
  - { name: scan_age, type: latency, topic: scan, vehicle: ugv1 }
  - { name: stack, type: resources, vehicle: ugv1 }
  - { name: commanded, type: value, topic: cmd_vel, vehicle: ugv1, field: linear.x, count_if: value > 0 }
```

## Verdicts, status and exit codes

Verdicts are `passed`, `failed`, `skipped` (the run ended before the assertion could resolve)
or `pending` (only while the run is in progress). They are in `run.json` under `assertions`
(name, type, status, reason, `t_s`, `sim_s`, evidence, metrics), appended one per line to
`assertions.jsonl` as they resolve, logged as `assertion.<status>` events, printed by
`fieldproof run` after the status line, and published on `/fieldproof/assertions`.

Every verdict's evidence carries an **excerpt**: the last six samples the assertion noted
before deciding (a pose and its distance, a value, a rate, a resource sample, a step of a
sequence), each with its timeline and simulation time. The Markdown report prints the
excerpt of every failed assertion under the table, so a failure is read with the stream that
led to it; `report.json` and the JUnit failure text hold every excerpt.

A run **passes** (exit code 0) when every gate held, the timeline completed and every assertion
passed. A failed assertion ends the run `failed` (exit code 1) once the timeline completes, with
the reason naming the first failed assertion (`assertion arrived_in_time failed: drive_ahead has
not completed 1 s after its start (and 1 more)`); the other assertions keep their verdicts, so the
report shows everything the run found. An assertion with `on_fail: abort` cancels the timeline
instead the moment it fails: the running entries are marked `cancelled`, the ones that never
ran `skipped` (a `within_time` on them fails), the pending assertions `skipped`, and the reason
says so (`assertion stays_in_yard failed: ... (on_fail: abort, the timeline was cancelled)`).
The other statuses ([scenarios.md](scenarios.md#status-and-exit-codes)) are unchanged:
`timeout` and `error` exit 1, `interrupted` 130, a scenario that does not validate 2.

## Writing a plugin

An assertion type is a Python class deriving from `fieldproof.assertions.AssertionType` with
three class attributes and two hooks, plus a checker, registered through an entry point of the
group `fieldproof.assertions`; `examples/plugins/example_assertions` is a complete one
(`example.visits_waypoints`: the vehicle visits every waypoint of a list, in any order):

```python
from typing import Any, ClassVar, Literal
from pydantic import Field
from fieldproof.assertions import (AssertionSpec, AssertionType, Checker, FAILED, PASSED,
                                   PlanContext, PlannedAssertion, Sample)
from fieldproof.dsl import AssertionBase, Placement
from fieldproof.manifest import Name

class VisitsWaypoints(AssertionBase):              # the spec: what the scenario writes
    type: Literal["example.visits_waypoints"]     # the discriminator, equal to AssertionType.type
    vehicle: Name = Field(description="The vehicle whose ground-truth pose is read.")
    waypoints: list[Placement] = Field(min_length=1)
    tolerance_m: float = 0.5

class VisitsWaypointsChecker(Checker):            # the streaming logic
    def on_sample(self, sample: Sample, t: float) -> None:
        if sample.kind != "pose" or sample.data.get("model") != self.planned.model:
            return
        ...                                       # tick off waypoints, self.note(t, sample, ...)
        if all_visited:
            self.resolve(PASSED, "all waypoints visited", t, sample.sim, visited=...)
    def on_finish(self, t: float) -> None:
        if not self.done:
            self.resolve(FAILED, "the timeline ended: ...", t, missing=...)
    def metrics(self) -> dict[str, tuple[Any, str]]:
        return {"visited": (len(self.visited), "")}

class VisitsWaypointsType(AssertionType):
    type: ClassVar[str] = "example.visits_waypoints"
    Spec: ClassVar[AssertionSpec] = VisitsWaypoints
    summary: ClassVar[str] = "The vehicle's ground truth visits every waypoint, in any order."
    metrics: ClassVar[dict[str, str]] = {"visited": "waypoints visited"}

    def plan(self, spec: VisitsWaypoints, context: PlanContext) -> PlannedAssertion:
        vehicle = context.vehicle(spec.vehicle)                 # a ScenarioError if unknown
        waypoints = [context.place(w) for w in spec.waypoints]  # frames resolved in the world
        return PlannedAssertion(spec, model=vehicle.name, resolved={"waypoints": waypoints})

    def checker(self, planned: PlannedAssertion) -> Checker:
        return VisitsWaypointsChecker(planned)
```

```toml
# pyproject.toml of the plugin's distribution
[project.entry-points."fieldproof.assertions"]
"example.visits_waypoints" = "fieldproof_example_assertions:VisitsWaypointsType"
```

- `Spec` derives from `fieldproof.dsl.AssertionBase`, which brings `name` and `on_fail`; its
  `type` field is a `Literal` equal to the type's `type`; its other fields, with their
  descriptions, are the parameters (`fieldproof assertions` prints them, `fieldproof schema`
  exports them; the committed `scenario.schema.json` includes the installed plugins). Unknown
  keys are errors, as in every section. Override `vehicle_names()` and `action_names()` when
  the spec refers to vehicles or timeline entries through other fields than `vehicle`, so
  `fieldproof validate` checks them before any plan is made.
- `plan(spec, context)` runs when the scenario is resolved (`fieldproof validate`, the dry
  run, before any container starts). The `PlanContext` has the `world`, the `vehicles` (name,
  namespace, `container_name`, `bridge_name`, `spawn`, `topic()`), the timeline's `actions`
  and the other `assertions` by name, and helpers that refuse with a `ScenarioError` naming
  `where`: `vehicle(name)`, `topic(vehicle, topic)` (absolute under the namespace, or with its
  leading slash), `place(placement)`, `action(name)`, `signal(signal, default_vehicle)` (a
  `fieldproof.dsl.Signal` resolved: a `PlannedSignal` whose `value_of(sample)` reads it from
  the stream). It returns a `PlannedAssertion`: the spec, the resolved `topic`, `target` and
  `model` the primitives use, anything else under `resolved` (shown by the dry run), and the
  `watch` entries the observer needs (`fieldproof.assertions.Watch`: `rate`, `value`,
  `contact`, `latency`, `lifecycle` or `model`); override `watches(planned)` to derive them
  instead.
- `checker(planned)` returns the `Checker`: `on_sample(sample, t)` for every line of the
  stream (`t` seconds into the timeline, negative before it starts), `on_tick(t)` as time
  passes (deadlines), `on_action(label, event, t, status)` when a timeline entry starts or
  finishes, `on_verdict(result, t)` when another assertion resolves (declare the names in
  `depends_on()` to be settled after them), `on_finish(t)` when the timeline completes;
  `resolve(status, reason, t, sim, **evidence)` gives the verdict once, `note(t, sample,
  **fields)` keeps a sample for the excerpt, `metrics()` reports what was measured (a value
  and a unit per name) for the run's KPIs. `WindowedChecker` adds `after_s`/`within_s`
  helpers for specs that have them.
- The engine loads the built-ins directly and every entry point of the group; a plugin that
  fails to import or is malformed (no `type`, a `Spec` that does not derive from
  `AssertionBase`, a `type` literal that does not match, a type another plugin took) is
  reported by `fieldproof assertions` (exit code 2) and as a warning by `fieldproof validate`,
  and its type stays unknown to the schema. What a plugin cannot do: run arbitrary code at
  validation time (its spec is data, its hooks run in the engine's process on the engine's
  terms) or read the stream another way than through the samples.

## Recording

`outputs.record: true` records with `ros2 bag record` into `recording/` in the run directory, as
MCAP (`recording_0.mcap` next to rosbag2's `metadata.yaml`), from the moment the recorder
starts (before the vehicles, so their bring-up is in the file) to the teardown, where the
recorder is stopped after the vehicles and before the bridges and given SIGINT so the file is
finalised. `outputs.topics` is the allow-list; empty, everything is recorded, which includes the
camera's images and point clouds and grows by tens of megabytes a second. The side channels
are always recorded; a hidden topic (a leading underscore in a name, such as an action's
`_action/status`) is recorded when listed, though rosbag2 may store it without a message
definition, which Foxglove then cannot decode. The file opens in Foxglove Studio (File > Open
local file) and with the `mcap` library, since every channel of a visible topic carries its
ROS 2 message definition.

The side channels are `std_msgs/msg/String` messages carrying JSON:

| Topic | Contents |
| --- | --- |
| `/fieldproof/events` | every line of `engine.log` as the engine writes it (the phases, containers, the readiness probe's lines, the verdicts, and every occurrence of a timeline entry as `action.start` and `action.end` lines carrying its record: action, vehicle, trigger, occurrence, parameters, status, detail, [events.md](events.md#the-record)) |
| `/fieldproof/assertions` | every verdict as it resolves: `run_id`, `name`, `type`, `status`, `reason`, `t_s`, `sim_s`, `evidence`, `metrics` |
| `/fieldproof/metrics` | once a second: `wall`, `sim` (simulation time), `rtf` (the real-time factor over the last second), `drift_s` (simulation seconds gained or lost against the wall clock at the target pace since the observer's first sample), `mode`, `target_rtf`, `paused` and `gz_rtf` (from the world's statistics) and in lockstep `lockstep` (the step, the hold, the acknowledgements) ([time.md](time.md#what-every-mode-records)), `rates_hz` per watched topic, `poses` of the vehicles, `lifecycle` (the last state of every watched node), `recording` |
| `/fieldproof/network` | the ground station's link measurements, one window a second, when the run has radio links ([network.md](network.md)) |
| `/fieldproof/resources` | the resource monitor's samples: per container and second, `container`, `role`, `vehicle`, `cpu_pct`, `memory_mb`, `memory_limit_mb`, `pids` |
| `/fieldproof/kpis` | at the end of the timeline: each collector's result (`name`, `type`, `status`, `reason`, `evidence`, `metrics`) and then the run's KPI map (`kind: kpis`) |

## Reports

At the end of every run, whatever its status, the runner writes `report.json`, `report.md` and
`junit.xml` into the run directory from the record: the verdict and exit code, the scenario,
world and vehicles, start, end and durations (wall clock, the timeline, the simulation time it
covered), every assertion with its reason and evidence (the excerpts of the failed ones under
the table), the KPIs (the collectors and the flat map), the timeline, the phases and the
artefacts (the MCAP recording first, with its size). `fieldproof report RUN [--format
md|json|junit] [--write]` renders the same from a run directory, its `run.json` or a run id
under the workspace's runs directory; `--write` rewrites the three files.

The JUnit document (task P1-T07) is the report for CI: one `testsuite` named after the scenario
with the run's facts as properties (`run_id`, `verdict`, `status`, `exit_code`, `reason`, the
scenario's path and digest, the world, the engine version, the durations, the MCAP path, and
`kpi.<name>` for every KPI) and one `testcase` per assertion (class `<scenario>.assertions`,
its `time` the verdict's `t_s`: a `failure` with the reason and the evidence when it failed,
`skipped` when it was skipped or still pending), per timeline entry (class
`<scenario>.timeline`: a `failure` unless its status is `ok`) and for the run itself (class
`<scenario>`, name `run`: a `failure` when the run failed, an `error` when it timed out, errored
or was interrupted). The suite's `tests`, `failures`, `errors` and `skipped` counts are what a
test reporter sums; the Scenario tests workflow uploads the document with the run
([ci.md](../setup/ci.md#scenario-tests)). HTML arrives with the run database of P2-T06.

## The run directory

In addition to the files of task P1-T05 ([scenarios.md](scenarios.md#the-run-directory)):

| File | Contents |
| --- | --- |
| `observe.json` | what the observer watched and recorded: the world and models, contact topics, rate, value, latency and lifecycle entries, the recorder's topic list and output, the files it relays |
| `samples.jsonl` | the observer's stream, one JSON object per line: `kind`, `wall` (epoch seconds), `sim` (simulation seconds or null) and the sample's fields; also `ready`, `subscribed`, `recording`, `error` and `stopped` lines |
| `resources.jsonl` | the resource monitor's stream, one `resource` sample per container and second from the start of the timeline to the teardown |
| `assertions.jsonl` | the verdicts in the order they resolved |
| `kpis.jsonl` | the collectors' results and the KPI map, written at the end of the timeline |
| `recording/` | the MCAP recording and rosbag2's `metadata.yaml`, when `outputs.record` is on |
| `report.json`, `report.md`, `junit.xml` | the report, as data, as Markdown and as a JUnit document |
| `video/<camera>.mp4` | the video of the run from the world's camera, when `outputs.video` is set ([scenarios.md](scenarios.md#video)) |
| `logs/recorder.log` | the observer's and `ros2 bag record`'s output |

`run.json` carries the `assertions`, the `kpis` and `kpi_collectors`, and `resources` (the
containers sampled, the sample count, the last sample of each).

## Writing a failing scenario

`examples/scenarios/ugv_nav_fails_on_time.yaml` is the example run with its `within_time`
assertion given one second: the drive still succeeds, `reached_goal` and `no_collision` pass,
`arrived_in_time` fails the moment the second passes, and the run ends `FAIL` with exit code 1.
It is what a regression looks like, and the integration test runs it.

## The examples

The UGV scenarios of `examples/scenarios` use the library: `ugv_nav_to_goal.yaml` adds a
`geofence` around the dock's yard, a `path_efficiency` of the drive, a `mission_time`, a
`message_latency` of the odometry, a `resource_usage` of the stack's container, a
`lifecycle_state` of `bt_navigator`, a `sequence` (the goal sent, a forward speed commanded,
the goal reached) and an `eventually` over the distance to the goal and the speed, with five
collectors; `ugv_fleet.yaml` keeps the vehicles 2 m apart with `min_distance` and bounds the
fleet's mission; `ugv_obstacle_replan.yaml` keeps the chassis 0.5 m from the spawned crate
and bounds the detour; `ugv_link_loss.yaml` combines the link state and the speed in an
`eventually ... for_s` and a `never`, and tells the outage's story as a `sequence` of its
other assertions' verdicts; `ugv_events.yaml` and `ugv_gps_denied.yaml` check the order of
their events and fixes. Each is run by its integration test.

## Tests

- `engine/tests/test_expression.py`: the conditions accepted and rejected, evaluation errors,
  named signals; `test_assertions.py`: every primitive on synthetic streams (passing, failing
  at a deadline, at the end, with evidence), the bookkeeping, the observation plan, and the
  stream of a real run of the example recorded in `engine/tests/fixtures/` (the five
  assertions pass; with a one-second `within_time`, exactly that one fails);
  `test_assertions_v1.py`: every type of the library v1 on synthetic streams (the regions,
  the trackers, the windows, the deadlines, the sequences' steps and verdict propagation,
  the combinators' holds, the plan hooks and their refusals, `on_fail: abort`, the excerpts,
  the observation plan with the new kinds); `test_kpi.py`: every collector, the KPI map, the
  `kpis` section's refusals; `test_assertion_registry.py`: the plugin API (well-formed and
  malformed types, the entry points, the schema union) and `fieldproof assertions`;
  `test_resources.py`: the daemon's statistics as a sample, the monitor's threads and errors;
  `test_observer.py`: the pure parts (field extraction, rate and latency windows, the file
  tail, the recorder process) and the observer's loop on fake ROS and Gazebo adapters (the
  latencies, the lifecycle polling and transition events, the relays); `test_report.py`: the
  report, its Markdown (the KPI table, the excerpts) and its JUnit document (the KPI
  properties); `test_runner.py`: the recorder container and phase, the assertions on the fake
  backend, the KPIs and the resource monitor in the record and the files, the abort; the
  example assertion plugin's own tests (`examples/plugins/example_assertions/tests`).
- `tests/integration/test_run.py` (`make test-integration`): the example passes against the
  real stack with its thirteen assertions and their evidence, the KPIs and the resource
  samples, the MCAP read back with the `mcap` library (the allow-listed topics and the side
  channels, ROS 2 schemas on every channel), the reports; the failing example exits 1 naming
  `arrived_in_time`. The other scenarios' tests check their new assertions the same way.
- `robots/fieldproof_robots/test/test_ugv_ref.py`: the chassis contact sensor watches the
  chassis collision only and follows the namespace.

## Known limits

- One observer subscription per topic with a best-effort, volatile QoS (compatible with every
  publisher); a topic that never appears is reported in the `recorder.ready` event and fails
  the assertions that need it at the end of the timeline.
- `no_collision` needs a contact sensor on the model; the reference vehicles have one, a real
  vehicle's description needs the same `<sensor type="contact">` on its chassis collision
  (or `topic` pointing at an existing one).
- Distances (`min_distance`, `geofence`) are between model origins and to the boundary from
  the origin, not from the chassis' footprint; allow for the vehicle's size.
- Message ages are measured against the bridged `/clock` the observer receives, with its own
  delivery jitter of a few milliseconds; they say how far a stack lags the simulator, not the
  one-way latency of a radio link, which the ground station measures
  ([network.md](network.md)).
- The resource monitor samples through the Docker daemon, about once a second with the
  daemon's own timing; a short spike between samples is not seen. The fidelity tiers of
  Phase 6 bring per-process accounting.
