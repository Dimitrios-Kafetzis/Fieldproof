# Field log replay

Task P2-T07 adds a second **source** of inputs to a run. A simulated run feeds the vehicle's
stack what Gazebo computes; a **replay** run feeds it the sensor data of a recorded drive, a
**field log** (an MCAP file or a rosbag2 directory), and judges what the stack makes of it. No
simulator and no bridge start: a **player** container publishes the clock from the log's
timestamps and plays the mapped topics into the vehicle's namespace with `ros2 bag play`; the
vehicle runs exactly as it would on the real machine, with its actuators **open-loop**: what it
commands is recorded and judged, nothing moves for it. The ground truth of the run is the
log's own (a motion-capture pose, an RTK-INS, a survey), so every assertion over the ground
truth keeps working, and the `localisation_error` assertion judges the stack's own estimate
against it.

```bash
make replay-log                                                       # the synthetic UGV log
uv run --project engine fieldproof log info examples/logs/ugv_dock_drive.mcap
uv run --project engine fieldproof validate examples/scenarios/ugv_replay.yaml
uv run --project engine fieldproof run examples/scenarios/ugv_replay.yaml
```

## The `source` section

```yaml
source:
  kind: replay
  log: ../logs/ugv_dock_drive.mcap   # an MCAP file or a rosbag2 directory, relative to the scenario
  rate: 1.0                          # log seconds per wall-clock second (the clock's pace too)
  start_s: 0s                        # skip this far into the log
  duration_s: null                   # play this much of it (absent: to its end)
  lead_in_s: 10s                     # the clock runs this long before the log's first message
  vehicles:
    ugv1:
      topics:                        # log topic -> vehicle topic (relative to its namespace)
        /scan: scan
        /odom: odom
        /tf_static: tf_static        # played transient local, whatever the log says
      tf:
        topic: /tf                   # the log's TF topic ...
        frames: [odom -> base_link]  # ... filtered to these transforms (empty: every one)
      ground_truth:
        topic: /ground_truth/pose    # PoseStamped, PoseWithCovarianceStamped or Odometry
        type: geometry_msgs/msg/PoseStamped   # optional: the log's type must agree
```

| Field | Meaning |
| --- | --- |
| `log` | the field log, relative to the scenario file: a single `.mcap` file, or a rosbag2 directory with its `metadata.yaml` (MCAP or SQLite storage). The player mounts it read-only; `fieldproof validate` reads its summary (`fieldproof log info` prints it) and refuses a mapping the log cannot serve |
| `rate` | the playback pace, log seconds per wall-clock second (`ros2 bag play --rate`); the clock advances at the same pace, so a stack never sees a stamp ahead of its clock. `time.mode: realtime` requires 1.0 |
| `start_s`, `duration_s` | the span of the log played: from `start_s` into it (`--start-offset`) for `duration_s` (`--playback-duration`; absent: to the end; a span past the end is clipped) |
| `lead_in_s` | log seconds the clock runs **before** the log's first played message (default 20). The vehicles start on a moving clock and come up during the lead-in; a stack still starting when the data begins misses the beginning of the log, and a stack whose activation waits for the data needs it flowing first, so the example uses 10 (see [What the timeline sees](#what-the-timeline-sees)) |
| `vehicles.<name>.topics` | log topic to vehicle topic: what the swapped drivers would publish on the real vehicle ([manifest.md](manifest.md), `runtime.drivers`). A relative target lives under the vehicle's namespace (`scan` is `/ugv1/scan` for `namespace: ugv1`), an absolute one stays as written. A log topic feeds one vehicle; `/clock` is never played (the player publishes the clock from the log's timestamps instead, so a log recorded in simulation plays like one recorded in the field) |
| `vehicles.<name>.tf` | the log's TF topic and, with `frames`, which `parent -> child` transforms reach the vehicle's `tf`. A field log carries the vehicle's whole tree, its own localisation (`map -> odom`) included; filtered to the wheel odometry, the stack computes the rest itself and the log's answer never competes with it. Without `frames` the topic is remapped as it is |
| `vehicles.<name>.ground_truth` | where the vehicle's true pose is in the log, in the world frame the placements of the scenario refer to. The player publishes it on a hidden topic (`/fieldproof/replay/<vehicle>/ground_truth`) the observer reads and the vehicle never sees; the `pose` samples of the stream, the `reach_pose`, `geofence`, `min_distance`, `path_efficiency` assertions, the `path` and `distance` KPIs and the `when` metrics all read it as they read Gazebo's. Without it, nothing judges the vehicle's position |

With a `source`, the rest of the scenario changes in three places:

- `world` is optional: nothing is simulated, and without a world file the log's ground-truth
  frame is the world the placements refer to (coordinates only). A world file may still be
  named for its frames (`spawn_dock`) when the log was recorded in a place the world models.
- `vehicles[].spawn` is optional: a replayed vehicle starts wherever the log starts. The spawn
  then says where the vehicle was in the ground truth's frame when the log started, which
  anchors its map frame: `navigate_to_pose` goals (in the world) and `align: spawn` of the
  localisation assertion need it; `align: first` (the default) does not.
- what needs the simulator is refused at validation: `time.mode: lockstep` (the engine cannot
  step a log; `sim` and `realtime` work, the latter at `rate: 1.0`), `time.rtf` (the pace is
  `source.rate`), `outputs.video`, the `network` section (the links sit between the bridges and
  the vehicles), the clock, world and fault events (`sim.*`, `spawn_object`, `remove_object`,
  `teleport`, `set_wind`, `gps.*`, `imu.bias`, `sensor.*`, `battery.drain`, `actuator.limit`,
  which act through the simulator or the bridge's injectors) and the `no_collision` assertion
  (the simulator's contact sensor). `navigate_to_pose` (with a spawn), `container.*`,
  `param.set` and every other assertion work as they do in a simulated run.

The record (`run.json`) and the reports carry the source: the log, its pace, the span played,
and when the playback started and ended on the timeline (`source.playing_t_s`,
`source.done_t_s`, `source.played_s`); `fieldproof run --dry-run` shows the resolved mapping
(`source.topics`, `remaps`, `tf_filters`, `ground_truth`).

## How a replay runs

The phases of a replay run are `preparing`, `replay`, `recorder`, `vehicles`, `running`,
`teardown`, `finished` ([scenarios.md](scenarios.md#running-a-scenario) has the simulated
ones). In `replay`, the runner starts the **player** container (`fieldproof-<short>-player`,
role `player`: the bridge image, which carries ROS 2 and rosbag2 with the MCAP and SQLite
storages, running the engine's `fieldproof/player.py` as the engine's user, with the log
mounted read-only under `/fieldproof/log/` and the run directory read-write) on `replay.json`,
which the runner writes from the plan. The player

1. starts the **clock**: `/clock` at 100 Hz from `lead_in_s` before the log's first played
   message, advancing at `rate` log seconds per wall-clock second, and keeps it going until the
   run ends (the stacks are on `use_sim_time`, as in a simulated run);
2. starts `ros2 bag play` **paused** (`--start-paused`, the mapped topics only, each remapped
   into its vehicle's namespace or onto a hidden topic, `--rate`, `--start-offset`,
   `--playback-duration`, and a QoS override file that makes a vehicle's `tf_static` transient
   local whatever the recording says, so a stack that starts late still gets the static
   transforms), and relays the log's TF topic through the **frame filter** where the plan asks
   for one;
3. reports `ready` in `replay.jsonl` of the run directory once the paused player offers its
   services (the log is open); the runner waits for it (`timeouts.replay_s`, 120 s by default)
   and goes on to the recorder and the vehicles, which start on the moving clock;
4. **resumes** the playback the moment the clock reaches the log's start (the player's
   `resume` service), so the first message is published at the clock time its header carries
   and every later one follows, to a few milliseconds: `playing` in the status file, with how
   late the resume was if the player was not ready in time;
5. reports `done` when the log is played (or `error` if `ros2 bag play` fails), and keeps the
   clock going.

The recorder's observer takes the ground truth from the hidden topic (`ground_truth` in
`observe.json`) instead of Gazebo (`--no-gazebo`), stamps the `pose` samples with the message's
header, and streams the stack's own estimates as `estimate` samples for the localisation
assertion; its clock manager reports the clock's pace and drift from `/clock` with
`source: log` and nothing ever paused. The engine's resource monitor samples the player with
the vehicles and the recorder. The teardown stops the vehicles, the recorder and the player,
in that order, and keeps their logs (`logs/player.log` holds the player's status lines and
rosbag2's own output).

## What the timeline sees

The timeline starts when every vehicle is ready, as always. A replayed stack cannot be ready
before the data flows (SLAM publishes no map and no `map -> base_link` transform without
scans), so the playback starts during the bring-up: `lead_in_s` after the player started, a
few seconds before the vehicle containers did. The stack misses the part of the log that
played while it was still starting (the example stack takes 20 to 40 s to bring Nav2 and SLAM
Toolbox up on a loaded host), and the timeline's `t = 0` lands somewhere into the log, after
the readiness gate: `source.playing_t_s` in the record is negative when the playback started
before the timeline, and a `replay.missed` warning in the log says how much of the log the
stacks missed; `source.done_t_s` says when the log ended on the timeline. The lead-in is not
simply "longer is safer": the example stack's Nav2 costmaps wait only a couple of seconds for
the odometry transform when they activate, and only the played log provides it, so a stack
that activates before the data flows aborts its first bringup and comes up on the watchdog's
retry (issue #103: with the 20 s default on a fast workstation the stack activated at 13 s,
the data started at 20 s, and the old watchdog retried after 120 s, when the log was spent;
the watchdog now retries within seconds). The data must therefore flow *before* the stack
activates: the example scenario uses `lead_in_s: 10s`, enough for the nodes to subscribe,
and its log starts with the vehicle at rest so nothing of the drive is missed. A stack that
comes up on its own, without the data, can take a long lead-in. Two consequences:

- a log whose first seconds matter should start with the vehicle at rest, as a field recording
  usually does (the operator starts recording, then drives); `lead_in_s` is then tuned to the
  stack: long enough for its nodes to subscribe, short enough that nothing in its activation
  gives up waiting for the data (the clock runs that long before the log; the stack's
  wall-clock watchdogs see a normal clock either way);
- the assertions' windows (`after_s`, `within_s`) and the timeline's `at` count from the
  timeline's start, not from the log's; the samples' `sim` field carries the log time for the
  evidence. A sliding-window check (`topic_rate`, `message_latency`) sees the data stop in
  its last window when the log ends before the timeline does: give it a floor the last window
  clears, or `within_s` where the type has one.

The **running** phase lasts until the log has been played **and** the timeline's entries are
done, within `time.timeout_s`; the pending assertions are settled when both hold. A timeline
may be empty (the example's is): the run is then the log, and the assertions are the test. A
`navigate_to_pose` entry sends its goal; the planner plans on the stack's map and the
controller commands velocities that nothing executes (`cmd_vel` is recorded; the example's
`commanded` KPI counts non-zero commands, zero of them without a goal, since no assertion
type judges a topic that never publishes).

## The `localisation_error` assertion

The stack's own pose estimate against the ground truth ([assertions.md](assertions.md#localisation_error)):

```yaml
- name: localised
  type: localisation_error
  vehicle: ugv1
  estimate: { topic: pose }      # SLAM Toolbox's pose; amcl_pose; odometry/filtered; odom
  max_m: 0.5                     # the position error never to exceed
  p95_m: 0.3                     # the 95th percentile, judged at the end (optional)
  mean_m: null                   # the mean, judged at the end (optional)
  max_yaw_rad: null              # the heading error never to exceed (optional)
  align: first                   # first | spawn: how the estimate's frame is placed in the world
  max_age_s: 0.5                 # how far an estimate's stamp may be from a ground-truth pose
  min_samples: 5                 # estimates that must have been judged by the end
```

The estimate lives in the stack's map (or odometry) frame, which starts wherever the stack
started; the ground truth in the world. `align: first` places the frame at the first estimate
that matches a ground-truth pose (the stack started there), `align: spawn` at the vehicle's
spawn pose. Each estimate is compared to the ground truth **interpolated to its stamp**; an
estimate with no ground-truth pose within `max_age_s` is counted `unmatched`, not judged. The
assertion fails at the first error over `max_m` (or heading error over `max_yaw_rad`) and, at
the end, when fewer than `min_samples` were judged or the mean or the 95th percentile is over
its limit; its metrics (`max_error_m`, `mean_error_m`, `p95_error_m`, `rms_error_m`,
`final_error_m`, `max_yaw_error_rad`, `samples`, `unmatched`) are the run's KPIs, and the
`localisation` KPI collector measures the same without judging (the example measures the raw
wheel odometry's drift that way, the number a localisation has to beat). The assertion works
in a simulated run too, against Gazebo's ground truth.

## What replay can and cannot validate

Replay answers one question well: **given exactly what the sensors saw, what does the stack
compute?** Perception and estimation (SLAM, localisation, detection, fusion), their rates and
latencies under the real data's timing, their resource footprint, their reaction to the real
data's gaps and glitches, and the decisions the stack makes from them (the plan it would
follow, the velocities it would command, a failsafe it would trigger) are all judged on the
same inputs every time, so a regression in any of them shows as a changed verdict or KPI, and
two versions of a stack can be compared on one log (`fieldproof runs diff`).

It cannot validate anything that closes the loop. The vehicle in the log drove where its
driver (or its old software) took it; the replayed stack's commands change nothing, so a
different plan is only a different plan, a collision it would have caused does not happen and
a collision the log holds is not avoided: `no_collision`, `reach_pose` on a goal the stack was
sent, `path_efficiency` of a drive the stack commanded and `min_distance` judge the **recorded
drive**, not the replayed stack. The world events, the fault injectors, the radio links and
lockstep belong to the simulator; a log is as complete as the drivers that were recorded
(the synthetic one has no IMU, no camera and no GNSS fix, so a stack that fuses them runs
without), its ground truth is as good as the instrument that produced it, and a log recorded
on one vehicle replays into a stack configured for that vehicle (the static transforms, the
frame names, the topics come from the log). A **divergence report** between a replayed and a
simulated run of the same mission is the subject of P6-T06.

## Logs

A field log for the replay source is what `ros2 bag record` writes on the vehicle, with the
drivers' topics (the ones the manifest's `runtime.drivers` map provides in simulation),
`/tf_static` and a ground-truth pose. The MCAP storage is the natural one (one file, the
schemas inside, opens in Foxglove Studio); a SQLite bag directory plays as well, and an MCAP
written by another tool plays as long as its schemas are `ros2msg` and its messages CDR (the
player gives rosbag2 the QoS it needs through the override file). `fieldproof log info <log>`
prints the topics, types, message counts, span and storage from the summary without decoding
a message: the mapping is written from it.

The maintainer has supplied no field log so far (STATUS.md, open question 2), so the
**synthetic log** of `fieldproof log synth` stands in (`examples/logs/README.md`):
the reference UGV in a 20 m by 12 m hall with four crates, at rest for 10 s, a driven rectangle
(8 m, a left turn, 4 m, a turn, 8 m, a turn, 4 m, a turn) at 0.5 m/s and 0.5 rad/s, at rest to
the end (120 s by default): `/scan` (720 beams, 10 Hz, 1 cm of noise, ray-cast from the lidar
on the mast), `/odom` and `/tf` (20 Hz: the wheel odometry integrated with a 2 % scale error, a
yaw-rate bias and noise, so its frame drifts the way wheels do, by some 0.4 m over the loop),
`/tf_static` (the reference UGV's frames) and `/ground_truth/pose` (10 Hz, exact, in the world
frame `--start-x/y/yaw` places the hall in). It is deterministic for its seed, written on
demand (`make replay-log`; recordings are never committed) and documented as synthetic: it
proves the chain, not the stack's field performance. A real log replaces it in a scenario by
its path and a mapping.

## Tests

- `engine/tests/test_replay.py`: the schema of the section and its refusals, the log
  inspection (an MCAP summary, a rosbag2 directory, the errors), the plan (remaps, the TF
  filter, the QoS override, the hidden ground truth, the refusals: a topic the log has not, a
  ground truth of the wrong type, lockstep, `time.rtf`, video, the network, the simulator's
  events and `no_collision`, a goal without a spawn), the run on the fake backend (the phases
  and containers of a replay, the status lines, a run that waits for the log to be played, a
  failing player, a player that exits or is never ready), the player's pure parts and its
  state machine on a fake ROS and process (the command line, the clock, ready, the resume at
  the log's start, done, a late start, an early exit, the stop), the synthetic log (deterministic,
  the geometry of the first scan, the drift of the odometry, the static frames), the
  localisation tracker (alignment, interpolation, the age limit) and assertion (every verdict,
  the window) and KPI collector, the observer's ground truth and estimates from pose
  topics, the `fieldproof log` commands, `fieldproof clock` on a replay run, the reports.
- `tests/integration/test_replay_source.py` (`make test-integration`): the example replays
  the synthetic log into the real `ugv_nav2` image: the vehicle comes up on the player's
  clock, SLAM Toolbox localises on the replayed scans and odometry, the five assertions pass
  (the localisation error against the log's ground truth first of all), the KPIs hold SLAM's
  error and the odometry's drift and count no actuator command, the record says when the playback
  started and ended, the MCAP holds the replayed inputs and the stack's outputs and no hidden
  topic, only a player, a recorder and a vehicle ran, nothing is left behind; `fieldproof
  clock` refuses the run and
  a mapping to a missing topic is refused.

## Known limits

- One player per run, one log per scenario: a fleet's vehicles share the log's span and pace
  (each with its own mapping). Several logs, or a log per vehicle, are not supported.
- The playback is resumed by a service call when the clock reaches the log's start, which
  lands within a few milliseconds on a workstation and within a few tens on a loaded runner;
  `late_s` in `replay.jsonl` says how late. The lead-in holds the clock back, never the data:
  a stack that is not listening by then misses the beginning. The player does not wait for
  the vehicles either: holding the data while the clock runs would publish every message in
  the past, and holding the clock would stall a stack whose activation waits for the data.
- The TF frame filter relays through a node of the player, one more hop for those transforms;
  `tf_static` and every other topic go straight from rosbag2's publishers.
- `start_s` and `duration_s` cut the log; the player does not loop it (`-l`) and does not
  pause it from the timeline (`sim.pause` is the simulator's). `fieldproof clock` refuses a
  replay run.
- Topics are remapped by name, frames are not: a log recorded with frame names the stack does
  not expect (`base_footprint` for `base_link`) needs the stack told, or the log rewritten.
