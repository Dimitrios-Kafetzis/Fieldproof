# Time and determinism

Task P2-T04 makes time a controlled variable of a run ([ADR-0006](../adr/0006-sim-time-default-realtime-locked-mode.md)).
A scenario's `time` section chooses one of three clock modes, declares what the run tolerates,
and the run records what the clock did: the real-time factor the simulator held, the drift
between simulation time and the wall clock, and in lockstep the steps the world took. A `seed`
makes the run's random numbers repeatable, and `fieldproof compare` measures how far two runs
of a scenario are apart.

## The `time` section

```yaml
seed: 7                   # optional: the run's random numbers (Gazebo's default, 0, when absent)
time:
  mode: sim               # sim (default) | realtime | lockstep
  rtf: null               # target real-time factor (sim: the world's own when null;
                          # realtime: 1.0; lockstep: the pace cap of a step, 1.0 by default)
  tolerance: 0.1          # a metrics window whose RTF is outside target ± tolerance is warned about
  max_drift_s: 1.0        # realtime: the drift over the timeline that loses the lock
  on_drift: fail          # realtime: fail | warn when the lock is lost
  step_s: 0.1             # lockstep: simulation seconds per step (a multiple of the physics step)
  ack_timeout_s: 30       # lockstep: how long a vehicle may take to acknowledge a step
  timeout_s: 10m          # the wall-clock budget of the run once every vehicle is ready
```

| Field | Meaning |
| --- | --- |
| `mode` | `sim`: the simulator free-runs at the target real-time factor and the timeline is scheduled on the wall clock (runner v0's behaviour). `realtime`: the real-time-locked mode, the factor pinned to 1.0 and the drift judged. `lockstep`: the stepped mode below. |
| `rtf` | the real-time factor asked of the simulator's physics once the world is up (`set_physics`). Absent in `sim` mode, the world's own setting applies (`<real_time_factor>` of the world file, 1.0 for the depot). `realtime` refuses any value but 1.0. In `lockstep` it caps the pace of a step: 1.0 by default, so a vehicle's stack gets at least the wall-clock time its work would have in the field; a larger value lets a fast host step faster |
| `tolerance` | the allowed deviation of the real-time factor the observer measures over each metrics window (one second) from the target; a window outside it counts as a violation in the record in `sim` and `realtime` mode (lockstep's windows are paced by the acknowledgements) and is a `clock.drift` warning in the log in `realtime` mode, the one that promised the pace |
| `max_drift_s` | `realtime`: the budget of the drift, in seconds, over the timeline. The drift is simulation seconds minus wall-clock seconds since the timeline started, with the time the world was paused (a `sim.pause`, `fieldproof clock pause`) left out; a drift past the budget in either direction loses the lock |
| `on_drift` | `realtime`: a lost lock fails the run (`fail`, the default: `status: failed`, the reason starting with `real-time lock lost`) or is a `clock.drifted` warning (`warn`). The verdict is judged when the timeline completes, so the assertions get their verdicts either way |
| `step_s` | `lockstep`: the simulation time of one step, a whole number of the world's physics steps (`<max_step_size>`, 1 ms in the depot; a value that is not is refused at validation) |
| `ack_timeout_s` | `lockstep`: wall-clock seconds the clock manager waits for every vehicle's acknowledgement of a step before the run fails (`lockstep: step N ... not acknowledged by <vehicle>`) |
| `timeout_s` | the wall-clock budget of the run from the moment every vehicle is ready, in every mode |
| `seed` (top level) | an integer from 0 to 2^31 - 1 (Gazebo's launcher converts it to a C `int`). The simulator is started with `gz sim --seed <seed>`, which seeds Gazebo's random number generator (the sensors' Gaussian noise, the wind), every container of the run gets `FIELDPROOF_SEED`, and the record and the report carry it. Absent, the run uses Gazebo's own default seed, 0 (so unseeded runs keep the noise sequence they always had and two of them are alike) and records it; `fieldproof run --seed <seed>` or a scenario's `seed` varies it |

`fieldproof validate` prints a note on what the mode means for the run; `fieldproof run
--dry-run` shows the resolved `time` section, the seed and the world's physics step and
real-time factor. A replay run ([replay.md](replay.md), task P2-T07) has no simulator: its
clock is the player's, from the log's timestamps at `source.rate` (which stands in for the
world's real-time factor; `rtf` is refused), `sim` and `realtime` (at rate 1.0) apply as above
and `lockstep` is refused; the `clock` samples carry `source: log` and nothing is ever paused.

## What every mode records

The observer's clock manager ([assertions.md](assertions.md#how-a-run-is-observed)) subscribes
to the world's statistics (`/world/<name>/stats`: whether the world is paused, its own
real-time factor), makes its requests to the world's control service through a helper process
of its own (`observer.py --world-control`, a gz-transport node that subscribes to nothing, since
a request from a node that also delivers Python callbacks loses its reply now and then), and
adds to the `/fieldproof/metrics` side channel and the `clock` samples of
the stream, once a second: `mode`, `target_rtf`, `drift_s` (simulation seconds gained or lost
against the wall clock at the target pace since the observer's first sample), `paused` and
`gz_rtf`. The engine's clock monitor reads the samples of the timeline and writes `run.json`'s
`clock`:

| Field | Contents |
| --- | --- |
| `mode`, `target_rtf`, `tolerance` | the `time` section as applied (`target_rtf` is the world's own factor when `rtf` was left out) |
| `max_drift_s`, `on_drift` | the real-time-locked mode's settings (null otherwise) |
| `origin_sim_s` | the simulation time the timeline's clock starts from: the first clock sample after the timeline started, or in lockstep the time the world was locked at |
| `sim_elapsed_s`, `wall_elapsed_s`, `paused_s` | simulation and wall-clock seconds the timeline covered, and the wall-clock seconds the world was paused for meanwhile |
| `drift_s`, `max_abs_drift_s` | the drift at the end of the timeline and the largest seen (paused time left out) |
| `rtf` | `windows`, `mean`, `min`, `max` of the real-time factor the observer measured over the timeline's metrics windows |
| `violations` | windows outside `target ± tolerance` (each a `clock.drift` warning in the log) |
| `verdict` | `locked` or `drifted` in the real-time-locked mode, null otherwise |
| `lockstep` | in lockstep: `step_s`, `vehicles`, `origin_sim_s`, `steps` taken, `holds` (how often the stepping waited for the engine), `max_wait_s` (the longest wait for an acknowledgement), `commands` the engine sent, `timeout` (the step nobody acknowledged, if any) |

The Markdown and JSON reports carry the same as a *Clock* line (mode, target and measured
factor, drift, violations, verdict, the lockstep's steps, the seed); the log has `clock.mode`
at the start, `clock.drift` per violating window, `clock.locked` or `clock.drifted` with the
verdict, and in lockstep `clock.agent`, `clock.agents`, `clock.locked` (the origin) and
`clock.run_until` (at `debug`).

## The real-time-locked mode

`time.mode: realtime` is for the bugs that only show with real timing: wall-clock watchdogs,
timers, timeouts in the stack under test. The simulator's physics is asked for a real-time
factor of 1.0 once the world is up, every vehicle still follows `/clock`, and the drift between
the two clocks is measured over the timeline. Whether the host can hold the lock is the host's
business: the maintainer's GPU workstation holds the depot with one UGV within a few
hundredths of a second over a drive, a CI runner with software rendering cannot (the
simulator runs well below real time there, as [scenarios.md](scenarios.md#running-a-scenario)
notes for the sensors), which is why `examples/scenarios/ugv_realtime.yaml` sets
`on_drift: warn` and the fidelity tiers that require the lock (T2 and T3, Phase 6) will set
`fail`. `tests/integration/test_clock_modes.py` asserts the verdict only with
`FIELDPROOF_REALTIME_STRICT=1`.

## Lockstep

`time.mode: lockstep` makes the simulator wait for the vehicles instead of the other way
round. Once every vehicle is ready (the world runs free during the bring-up, since the stacks
need a moving clock to come up), the run:

1. starts a **clock agent** per vehicle (`fieldproof.lockstep`, a container of the bridge
   image named `fieldproof-<run>-clock-<vehicle>` with the role `clock`) in the vehicle
   container's network namespace, so it hears exactly the `/clock` the vehicle hears, through
   the same radio link when the scenario has one. The agent publishes an acknowledgement on
   `/fieldproof/lockstep` (`{"vehicle", "sim", "step"}` in a `std_msgs/msg/String`) every
   time the clock crosses a step boundary, and repeats its last one every second;
2. waits until every agent has acknowledged, then tells the observer's clock manager to
   **lock**: the manager asks the world to run to the next step boundary half a second ahead
   (`run_to_sim_time` of `gz.msgs.WorldControl`) and pause, and waits for every agent to
   acknowledge that time. That time is the **origin**: the timeline's `t = 0`, `clock.origin_sim_s`;
3. runs the timeline in **simulation time**: `at`, `every`, `within_s`, `until_s`, the
   assertions' deadlines (`within_s`, `max_s`, `after_s`), the record's `t_s` and the `t_s`
   of the verdicts all count simulation seconds from the origin. The engine tells the manager
   how far the world may step (`run_until` in `clock.jsonl` of the run directory: the next due
   time of the timeline, rounded up to the step grid; no limit while nothing is scheduled), the
   manager requests one step of `step_s` at a time and the next only once every agent has
   acknowledged the previous one, and the world is **held** at a due time while the engine
   starts the entries due there: an event that says its effect is in place
   (`context.started(record)`: `navigate_to_pose` when Nav2 accepted the goal, the clock
   events at once) releases the hold, as does its return, or ten seconds of wall clock
   (`clock.hold.expired`). A goal is therefore sent with the world paused at exactly its `at`;
4. **releases** the world when the timeline ends, before the teardown.

A step the agents do not acknowledge within `ack_timeout_s` (a vehicle container that died, a
link dropped for longer than that) ends the run `failed`. `sim.pause`, `sim.resume` and
`sim.step` do not combine with lockstep (validation refuses them), and `fieldproof clock`
should leave a lockstep run alone.

What two lockstep runs of a scenario share exactly: the seed, the stepped clock (every step is
the same simulation time in both), the moment every entry starts, and the moment Nav2 accepts
each goal. What still differs: what the stack computed in the wall-clock time each step gave
it. Nav2's planner and controller run free between steps on their own timers, the rendering
sensors render on their own thread (a scan may land a step later under load), and DDS delivers
in its own time, so two runs follow the same trajectory *within a tolerance* rather than to the
millimetre. `examples/scenarios/ugv_lockstep.yaml` twice on the maintainer's workstation
deviates by a few centimetres in the ground truth and the odometry; the determinism test
(`tests/integration/test_determinism.py`, `make test-determinism`, nightly in the scenario
workflow) asserts 0.25 m. A stack that wants more must itself run in lockstep with its clock
(process everything a tick brought before the next tick), which the acknowledgement protocol
leaves room for: an agent that acknowledges on the stack's behalf, after its work, is a future
`runtime.lockstep` option of the manifest.

The pace: with `rtf: 1.0` a 0.1 s step takes at least 0.1 s of wall clock plus the round trip
of the acknowledgement (a few milliseconds on one host), so a lockstep run is a little slower
than real time; on a host where the world itself cannot reach real time (software rendering)
the stepping is as slow as the world, and the stack gets more wall-clock time per simulation
second than it would free-running, which makes a lockstep run on a slow host closer to a fast
one than two free-running runs are.

## Pausing, resuming and stepping

Three built-in events ([events.md](events.md#built-in-events)) act on the world's clock
through its `/world/<name>/control` service, in `sim` and `realtime` mode:

| Event | Parameters | Effect |
| --- | --- | --- |
| `sim.pause` | `duration_s` (optional, wall clock) | the physics stops and every node on simulation time stands still with it; the clock keeps publishing the time it stopped at, so a stack sees time stand still, which a wall-clock watchdog notices and a sim-time timer does not. With `duration_s` the entry resumes the world after that long and ends then; without it the world stays paused until a `sim.resume` or a `sim.step` |
| `sim.resume` | | the world runs again |
| `sim.step` | `steps` (physics steps) or `duration_s` (simulation seconds, converted with the world's physics step) | advance a paused world by that much and pause it again (a running world is paused first) |

The drift monitor leaves a paused world out of the drift (`paused_s`), and a `when: {metric:
rtf, condition: value < 0.05}` trigger sees the pause in the metrics
(`examples/scenarios/ugv_sim_control.yaml` resumes the world that way). The same by hand, on a
run in progress or one kept with `--keep`:

```text
fieldproof clock runs/<run_id> status          # paused or running, simulation time, iteration, RTF
fieldproof clock runs/<run_id> pause
fieldproof clock runs/<run_id> step --steps 100  # or --seconds 0.5
fieldproof clock runs/<run_id> resume
```

## Comparing two runs

```text
fieldproof run examples/scenarios/ugv_lockstep.yaml --run-id lockstep-a
fieldproof run examples/scenarios/ugv_lockstep.yaml --run-id lockstep-b
fieldproof compare runs/lockstep-a runs/lockstep-b [--tolerance-m 0.25] [--json]
```

`fieldproof compare` reads both runs' ground-truth `pose` samples (`samples.jsonl`) in
timeline seconds from each run's `clock.origin_sim_s`, interpolates the second run's trajectory
at the first run's samples over the span both cover and prints, per vehicle, the largest
deviation and when, the mean and the final one; exit code 0 when every vehicle stays within
the tolerance, 1 otherwise. Two free-running runs can be compared the same way, with the
caveat that their timelines started at different simulation times and their commands landed
at different steps. `fieldproof runs diff` (P2-T06) will compare the rest of two runs.

## Known limits

- A pause is seen by every wall-clock watchdog of the stack under test, which is the point and
  a trap at once: the example stack's lifecycle manager shuts Nav2 down after 4 s without a
  heartbeat from a server, so a `sim.pause` or a `fieldproof clock pause` longer than that
  ends its drive with `goal ABORTED`. Keep pauses short, or make them long on purpose to see
  what the stack does.
- The acknowledgement says the clock reached the vehicle's network namespace, not that the
  stack finished the work the step gave it; see above for what that leaves to the stack.
- The world runs free during the bring-up, so the simulation time of the origin differs from
  run to run (the record has it); the timeline's times are relative to it.
- A lockstep run with radio links waits for the clock to cross every link and the
  acknowledgement to cross back, so a dropped link longer than `ack_timeout_s` fails the run
  rather than letting the vehicle run on a stale clock (which is the point).
- `--seed` seeds Gazebo and is recorded; the scenario's own randomisation (parameter sweeps,
  randomised placements) arrives with P2-T06's matrix, which will draw from it.
- The triggers `after` and `when` are noticed by the engine's half-second poll, so an entry
  triggered during the timeline starts up to a few steps after its trigger (its `t_s` says
  when); entries triggered at the start of the timeline start at their exact `at`.
