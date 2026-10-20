# Events

The event injection framework of task P2-T03 (session 1 of 2): how a scenario's `timeline`
says *when* something happens (the timeline DSL: `at`, `after`, `when`, `every`), *what*
happens (the events: built-in actions and plugins found through Python entry points), how every
occurrence is recorded (`run.json`, `engine.log`, `/fieldproof/events`), and how to write an
event plugin. The scenario format around the timeline is in [scenarios.md](scenarios.md); the
radio link events are detailed in [network.md](network.md#timeline-events).

```yaml
timeline:
  - at: 0s                                # the drive starts with the timeline
    name: drive_ahead
    vehicle: ugv1
    action: navigate_to_pose
    goal: { x: 10.0, y: -1.0, yaw: -1.5708 }
  - name: say_hello                       # at 2 s, then every 5 s, three times
    vehicle: ugv1
    at: 2s
    every: 5s
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
  - name: freeze_stack                    # one second after the drive ends, for 3 s
    vehicle: ugv1
    after: drive_ahead
    at: 1s
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
| `after` | the `name` of another entry: this one is triggered when that entry ends `ok` (a repeated entry cannot be named, nor the entry itself; chains are allowed, cycles are not) |
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

A `within_time` assertion ([assertions.md](assertions.md#the-primitives)) on an entry is how a
scenario requires it: it fails when the entry is skipped, when it fails, and when it takes
longer than `max_s`; on a repeated entry it judges the first occurrence.

### Conditions

A `when` is a mapping with a `condition`, an expression over `value` in the sandboxed language
of the `topic_value` assertion (comparisons, arithmetic, `and`, `or`, `not`, `abs`, `min`,
`max`, `sqrt`, `round`; [assertions.md](assertions.md#the-primitives)), applied to one of:

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
parameters. The built-ins of this session:

| Action | Fields | Effect |
| --- | --- | --- |
| `navigate_to_pose` | `vehicle`, `goal` (a placement in the world), `timeout_s` (300 s) | send a Nav2 `NavigateToPose` goal in the vehicle's `map` frame (which starts at its spawn pose) with `ros2 action send_goal` inside the vehicle's container and wait for its result: `ok` on `SUCCEEDED`, `failed` on any other status, `timeout` after `timeout_s` ([scenarios.md](scenarios.md#placements)) |
| `link.set`, `link.drop`, `link.restore` | `vehicle`, `profile` / `duration_s` | the radio link events of task P2-T02, unchanged ([network.md](network.md#timeline-events)); a scenario without a `network` section is refused |
| `container.pause` | `vehicle`, `duration_s` (optional) | pause the vehicle's container (`docker pause`: every process of the stack under test freezes while the simulator and the bridge go on, so the vehicle's last command stands in the world until the stack resumes; a container that is paused answers no `exec`); with `duration_s` the entry resumes the container after that long and ends then, without it the container stays paused until a `container.resume` (the teardown resumes it before stopping it) |
| `container.resume` | `vehicle` | resume the paused container |
| `container.kill` | `vehicle`, `signal` (`SIGKILL` by default; `SIGTERM`, `SIGINT`, `SIGHUP`) | send the signal to the vehicle's container; the run then expects the container gone (its exit does not end the run, its log is still kept), and the assertions decide what the exit meant for the fleet |
| `param.set` | `vehicle`, `node` (relative to the vehicle's namespace, or absolute), `parameter`, `value` (a boolean, a number or a string, which stays a string however it looks), `timeout_s` (30 s) | `ros2 param set` inside the vehicle's container; `ok` when the node accepted the value, `failed` when it refused it (a parameter callback) or is not found within `timeout_s` (the CLI's graph lags behind the stack for a few seconds, so the event asks again once a second), `timeout` when the node does not answer. Whether the node *uses* a value changed at run time is the node's business, as in the field |

A `param.set` or a `navigate_to_pose` aimed at a killed or paused container fails (`ros2`
cannot run there), so a scenario orders such entries with `after`. The world and sensor events
of the backlog (`spawn_object`, `remove_object`, `teleport`, `set_wind`, `gps.deny`,
`gps.offset`, `imu.bias`, `sensor.dropout`, `sensor.freeze`, `battery.drain`,
`actuator.limit`) arrive with the second session of the task, on a Gazebo-side plugin and
fault-injector nodes between the bridge and the vehicle ([Known limits](#known-limits)).

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
  `container.killed`, `link.applied`, and the commands' output as `action.output` at `debug`);
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
  124), `exec(container, argv, on_line)` (any command in any container of the run), `backend`
  (the container runtime: `pause`, `unpause`, `kill`, `state`, ...), `log(event, message,
  **fields)` (a line of `engine.log`, relayed to `/fieldproof/events`), `clock()`,
  `hold(seconds)` (wait in slices, `False` when the run is ending), `tearing_down`,
  `expect_exit(container)` (a container the event is about to kill), `mark_paused(container,
  paused)` (so the teardown resumes it), `apply_link(vehicle, profile, cause)` and
  `link_profile(vehicle)` (the radio links). The hook sets `record.status` (`ok`, `failed`,
  `timeout`) and `record.detail`; a record left `running` ends `ok`, an exception ends it
  `failed` with the exception's text.
- Registration: the distribution is installed in the engine's environment (`uv add --project
  engine --dev --editable <path>` for a plugin in the workspace, or `pip install` into that
  environment). The engine loads every entry point of the group when it starts; a plugin that
  fails to import or is malformed (no `action`, an `Entry` that is not a `TimelineBase`, an
  `action` literal that disagrees, an `action` another event has) is reported by
  `fieldproof events` (exit code 2) and by `fieldproof validate` (a warning) and its action
  stays unknown to the schema. The built-ins are registered the same way by the engine's own
  `pyproject.toml`, and loaded directly as well.

`examples/plugins/example_events/` is this example in full, with its tests
([README](https://github.com/Dimitrios-Kafetzis/Fieldproof/tree/main/examples/plugins/example_events));
the engine's development environment installs it, so `fieldproof events` lists
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
has moved a metre, `freeze_stack` pauses the container for 3 s after the drive and `stop_stack`
kills it. Seven assertions check the drive and, through `within_time`, that each event ran.

```bash
uv run --project engine fieldproof events
uv run --project engine fieldproof validate examples/scenarios/ugv_events.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_events.yaml
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
  tests). `make test-engine` runs them all.
- `tests/integration/test_event_injection.py` (`make test-integration`): the example scenario
  against the real stack: exit code 0, every entry `ok` with its trigger and parameters, the
  seven assertions passed, the vehicle container seen paused and then exited by `docker
  inspect` while its log is kept, the `action.start`, `action.end` and `action.triggered` lines
  of `engine.log`, the three `/announce` messages in the recording, nothing left behind.

## Known limits

- Session 2 of the task adds the world events (`spawn_object`, `remove_object`, `teleport`,
  `set_wind`) on a Gazebo-side C++ plugin exposing world-mutation services, the sensor and
  vehicle fault events (`gps.deny`, `gps.offset`, `imu.bias`, `sensor.dropout`,
  `sensor.freeze`, `battery.drain`, `actuator.limit`) on ROS-side fault-injector nodes between
  the bridge and the vehicle, and the definition-of-done scenarios (an obstacle spawned on the
  planned path with Nav2 replanning, a GPS denial).
- Times are wall clock from the start of the timeline; `every` drifts with the host's load and
  a `when` fires on the first sample that satisfies it. Sim-time scheduling is task P2-T04's.
- A `when` reads what the observer streams: topic fields at the topic's rate, the ground truth
  at 10 Hz, the clock once a second. Expressions over several topics, or over a window of
  samples, are not supported; chain entries with `after` or use several entries instead.
- `every` does not combine with `when`, and `after` cannot name a repeated entry.
- A container's pause freezes the whole stack, not one node, and leaves its last command
  standing in the simulator (the reference UGV's drive has no command timeout); a kill ends the
  container for good (there is no restart event yet).
