# Assertions, recording and reports

Task P1-T06 turns a run into a test with evidence. A scenario's `assertions` are evaluated
while the timeline runs, every topic the scenario asks for is recorded to MCAP, the engine's
events, verdicts and metrics travel on `/fieldproof/*` topics so the recording holds the whole
run, and the run ends with a report in JSON and Markdown. The five primitives below are v0 of
the assertion library; the KPI collectors and the plugin API are task P2-T05's.

```bash
uv run --project engine fieldproof run examples/scenarios/ugv_nav_to_goal.yaml      # PASS, exit 0
uv run --project engine fieldproof run examples/scenarios/ugv_nav_fails_on_time.yaml # FAIL, exit 1
uv run --project engine fieldproof report runs/<run_id>                             # the Markdown report
```

## How a run is observed

Everything an assertion needs comes from the **recorder container**: a container of the bridge
image (ROS 2, rosbag2 with the MCAP storage, the Gazebo transport bindings) that the runner
starts after the bridges and before the vehicles, with the run directory bind-mounted
read-write at `/fieldproof/run` and the engine's observer script mounted read-only. It runs as
the user who started the run, so the files it writes belong to them. The observer
(`fieldproof.observer`) reads `observe.json`, which the runner writes from the scenario:

- it subscribes to the ROS topics named by `topic_rate` and `topic_value` assertions
  (discovering the message type from the graph unless `message_type` is given) and to the
  Gazebo ground truth: `/world/<name>/pose/info` for the vehicles' poses and each vehicle's
  contact sensor for `no_collision`;
- it appends one JSON line per observation to `samples.jsonl` (`pose` at 10 Hz per model,
  `rate` once a second per topic over a sliding window, `value` per message, `contact` per
  contact message, `clock` once a second with the real-time factor), which the runner tails and
  feeds to the **streaming evaluator** (`fieldproof.assertions.Evaluator`) every half second
  while the timeline runs;
- it runs `ros2 bag record` when `outputs.record` is on, and publishes the side channels.

Each assertion resolves once, as soon as the stream decides it: a `reach_pose` passes at the
first pose within tolerance, a `no_collision` fails at the first contact, a `within_time` fails
the moment its limit passes. Whatever is still pending when the timeline completes is settled
then (`topic_rate`, `no_collision` and `topic_value always` pass if nothing went wrong;
`reach_pose` and `topic_value eventually` fail). A run that ends before the timeline completes
(a failed action, the budget, Ctrl-C) marks the pending assertions `skipped`.

Times in verdicts are seconds after the timeline started, on the wall clock the engine and the
containers share (the timeline's `at` is on the same clock in runner v0, ADR-0006's sim-time
scheduling arriving with P2-T04); the evidence carries the simulation time of the deciding
sample as well where the stream has it. Topic rates are measured in **simulation time**, so a
sensor's nominal rate holds whatever the real-time factor of the host.

## The primitives

Every assertion has a unique `name` (lower case, digits, underscores) and a `type`; the other
keys depend on the type and unknown keys are errors. Topics are relative to the vehicle's
namespace when `vehicle` is given, absolute otherwise.

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

### `within_time`

A timeline action reports a successful result within a duration of its start.

| Key | Meaning |
| --- | --- |
| `action` | the `name` of a timeline entry (an unnamed entry is `<action>@<at>s`) |
| `max_s` | the result must arrive within this long after the action starts |

Passes when the action completes `ok` in time; fails the moment `max_s` passes without a result,
when the action ends with another status, or if it never starts. Evidence: `started_t_s`,
`duration_s` or `elapsed_s`, `action_status`.

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
`highest_hz`, `messages`, and the failing `window`. Rendering sensors do not always reach their
nominal rate in simulation time: with software rendering on a two-core CI runner the reference
UGV's 10 Hz lidar arrives at about 3 Hz of simulation time while a GPU host gives 9 to 10 Hz, so
the example asks for 2 Hz and a scenario meant for GPU hosts can ask for more.

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
`ignored_contacts` and `messages` seen.

### `topic_value`

A condition over a field of a topic's messages.

| Key | Meaning |
| --- | --- |
| `topic`, `vehicle` | the topic, under the vehicle's namespace when `vehicle` is given |
| `field` | the field: `linear.x`, `pose.pose.position.x`, `ranges[0]`, `status.status` |
| `condition` | an expression over `value` (below) |
| `mode` | `always` (every message after `after_s` satisfies it; the default) or `eventually` (one does) |
| `after_s` | grace period after the timeline starts, 0 by default |
| `within_s` | deadline for `eventually`; the end of the timeline when absent |
| `message_type` | `geometry_msgs/msg/Twist`; discovered from the graph when absent |

`always` fails at the first message that violates the condition and passes at the end if at
least one message was seen; `eventually` passes at the first message that satisfies it and fails
at `within_s` or the end. A field that does not exist fails the assertion with the observer's
message. Evidence: `samples`, `lowest`, `highest`, `last`, and the `violation` or `satisfied`
sample.

The condition is a Python expression limited to what a test needs: comparisons (`<`, `<=`,
`==`, `!=`, `in`, `is`), arithmetic, `and`/`or`/`not`, a conditional expression, indexing,
the functions `abs`, `min`, `max`, `round`, `sqrt`, `len`, `float`, `int`, `isnan`, `isinf`,
the constants `pi`, `inf`, `nan`, and the name `value`. Anything else (attributes, other names,
lambdas, comprehensions) is rejected when the scenario is validated. Examples: `value <= 1.5`,
`abs(value) < 0.2`, `0 < value and value < 2`, `value == 'ACTIVE'`, `len(value) == 720`.

## Verdicts, status and exit codes

Verdicts are `passed`, `failed`, `skipped` (the run ended before the assertion could resolve)
or `pending` (only while the run is in progress). They are in `run.json` under `assertions`
(name, type, status, reason, `t_s`, `sim_s`, evidence), appended one per line to
`assertions.jsonl` as they resolve, logged as `assertion.<status>` events, printed by
`fieldproof run` after the status line, and published on `/fieldproof/assertions`.

A run **passes** (exit code 0) when every gate held, the timeline completed and every assertion
passed. A failed assertion ends the run `failed` (exit code 1) once the timeline completes, with
the reason naming the first failed assertion (`assertion arrived_in_time failed: drive_ahead has
not completed 1 s after its start (and 1 more)`); the other assertions keep their verdicts, so the
report shows everything the run found. The timeline is not cut short by a failed assertion in v0:
the evidence of the whole run is worth more than the seconds saved. The other statuses
([scenarios.md](scenarios.md#status-and-exit-codes)) are unchanged: `timeout` and `error` exit 1,
`interrupted` 130, a scenario that does not validate 2.

## Recording

`outputs.record: true` records with `ros2 bag record` into `recording/` in the run directory, as
MCAP (`recording_0.mcap` next to rosbag2's `metadata.yaml`), from the moment the recorder
starts (before the vehicles, so their bring-up is in the file) to the teardown, where the
recorder is stopped after the vehicles and before the bridges and given SIGINT so the file is
finalised. `outputs.topics` is the allow-list; empty, everything is recorded, which includes the
camera's images and point clouds and grows by tens of megabytes a second. The three side
channels are always recorded; a hidden topic (a leading underscore in a name, such as an
action's `_action/status`) is recorded when listed, though rosbag2 may store it without a
message definition, which Foxglove then cannot decode. The file opens in Foxglove Studio
(File > Open local file) and with the `mcap` library, since every channel of a visible topic
carries its ROS 2 message definition.

The side channels are `std_msgs/msg/String` messages carrying JSON:

| Topic | Contents |
| --- | --- |
| `/fieldproof/events` | every line of `engine.log` as the engine writes it (the phases, containers, the readiness probe's lines, the actions, the verdicts) |
| `/fieldproof/assertions` | every verdict as it resolves: `run_id`, `name`, `type`, `status`, `reason`, `t_s`, `sim_s`, `evidence` |
| `/fieldproof/metrics` | once a second: `wall`, `sim` (simulation time), `rtf` (the real-time factor over the last second), `rates_hz` per watched topic, `poses` of the vehicles, `recording` |

Container resource metrics (CPU, memory per container) are not sampled yet; they belong with the
compute budgets of Phase 6.

## Reports

At the end of every run, whatever its status, the runner writes `report.json` and `report.md`
into the run directory from the record: the verdict and exit code, the scenario, world and
vehicles, start, end and durations (wall clock, the timeline, the simulation time it covered),
every assertion with its reason and evidence, the timeline, the phases and the artefacts (the
MCAP recording first, with its size). `fieldproof report RUN [--format md|json] [--write]`
renders the same from a run directory, its `run.json` or a run id under the workspace's runs
directory; `--write` rewrites the two files. JUnit output for CI arrives with task P1-T07, HTML
with the run database of P2-T06.

## The run directory

In addition to the files of task P1-T05 ([scenarios.md](scenarios.md#the-run-directory)):

| File | Contents |
| --- | --- |
| `observe.json` | what the observer watched and recorded: the world and models, contact topics, rate and value topics, the recorder's topic list and output |
| `samples.jsonl` | the observer's stream, one JSON object per line: `kind`, `wall` (epoch seconds), `sim` (simulation seconds or null) and the sample's fields; also `ready`, `subscribed`, `recording`, `error` and `stopped` lines |
| `assertions.jsonl` | the verdicts in the order they resolved |
| `recording/` | the MCAP recording and rosbag2's `metadata.yaml`, when `outputs.record` is on |
| `report.json`, `report.md` | the report |
| `logs/recorder.log` | the observer's and `ros2 bag record`'s output |

## Writing a failing scenario

`examples/scenarios/ugv_nav_fails_on_time.yaml` is the example run with its `within_time`
assertion given one second: the drive still succeeds, `reached_goal` and `no_collision` pass,
`arrived_in_time` fails the moment the second passes, and the run ends `FAIL` with exit code 1.
It is what a regression looks like, and the integration test runs it.

## Tests

- `engine/tests/test_expression.py`: the conditions accepted and rejected, evaluation errors;
  `test_assertions.py`: every primitive on synthetic streams (passing, failing at a deadline, at
  the end, with evidence), the bookkeeping, the observation plan, and the stream of a real run
  of the example recorded in `engine/tests/fixtures/` (the five assertions pass; with a
  one-second `within_time`, exactly that one fails); `test_observer.py`: the pure parts (field
  extraction, rate windows, the file tail, the recorder process) and the observer's loop on fake
  ROS and Gazebo adapters; `test_report.py`: the report and its Markdown; `test_runner.py`: the
  recorder container and phase, the assertions on the fake backend (passing, failing, several
  failing, the failing example, skipped when the run ends early, observer errors, the observer
  never ready or exiting), the reports; `test_scenario_cli.py`: the verdicts printed, the exit
  code of a failed assertion, `fieldproof report`.
- `tests/integration/test_run.py` (`make test-integration`): the example passes against the
  real stack with the five assertions and their evidence, the MCAP read back with the `mcap`
  library (the allow-listed topics and the side channels, ROS 2 schemas on every channel), the
  reports; the failing example exits 1 naming `arrived_in_time`.
- `robots/fieldproof_robots/test/test_ugv_ref.py`: the chassis contact sensor watches the
  chassis collision only and follows the namespace.

## Known limits

- Deadlines (`within_s`, `max_s`, `after_s`) are on the wall clock, as the timeline is in
  runner v0; rates are in simulation time. P2-T04 brings the clock manager.
- One observer subscription per topic with a best-effort, volatile QoS (compatible with every
  publisher); a topic that never appears is reported in the `recorder.ready` event and fails
  the assertions that need it at the end of the timeline.
- `no_collision` needs a contact sensor on the model; the reference vehicles have one, a real
  vehicle's description needs the same `<sensor type="contact">` on its chassis collision
  (or `topic` pointing at an existing one).
- A failed assertion does not stop the timeline; `on_fail: abort` is a candidate for P2-T05.
- Resource metrics per container are not recorded yet.
