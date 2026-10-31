# The M2 demo video

The video on the [home page](../index.md) and in the README shows milestone M2, *Fleet, faults,
two clocks*, as Fieldproof itself records it: every clip is a scenario of `examples/scenarios/`
(or of the [example vehicle repository](onboarding.md#the-worked-example)) run with
[`outputs.video`](scenarios.md#video) from one of the depot's three cameras
([worlds.md](worlds.md#the-cameras)), with its assertions passing. Nothing is staged: the
recorder encodes the simulator's own frames in simulation time, so a clip plays at the pace the
vehicle drove, and the verdict shown under each clip is the run's.
[The full-resolution MP4](https://github.com/Dimitrios-Kafetzis/Fieldproof/releases/download/v0.20.1/fieldproof-m2-demo.mp4)
(3 min 26 s, 1280 × 720) is attached to the v0.20.1 release as `fieldproof-m2-demo.mp4`; the GIF
under `docs/media/` is a short, low-resolution cut of it for the README.

## The clips

| Clip | Scenario | Camera | What to look for |
| --- | --- | --- | --- |
| The fleet | `ugv_fleet.yaml` | `overview` | three UGVs with namespaces and budgets, one in the yard, one at the dock, one in the hall, each driving its own 4 m |
| A radio link cut | `ugv_link_loss.yaml` | `hall` | the vehicle holds in place while the LTE link to its ground station is dropped for 30 s (the loss-of-link failsafe), then drives on |
| A crate in the path | `ugv_obstacle_replan.yaml` | `spectator` | a crate dropped by the timeline once the vehicle moves; Nav2 replans around it |
| The simulator paused, stepped, resumed | `ugv_sim_control.yaml` | `spectator` | the drive frozen mid-way, nudged by 500 physics steps, resumed; the timeline sees every state |
| Two lockstep runs, side by side | `ugv_lockstep.yaml` twice | `spectator` | the same scenario stepped in lockstep twice: identical trajectories, compared by `fieldproof compare` |
| GNSS denied | `ugv_gps_denied.yaml` | `spectator` | the fix lost for 20 s under a SLAM stack that never reads it; the drive out and back |
| Real-time locked | `ugv_realtime.yaml` | `spectator` | the drive at the wall clock's pace, the drift measured |
| The run matrix | `ugv_nav_matrix.yaml` | (a card) | two cases over seeds and budgets, one JUnit document |
| A field log replayed | `ugv_replay.yaml` | (a plot) | SLAM's pose against the log's ground truth, drawn by `tools/media/replay_plot.py` |
| An external vehicle repository | `fieldproof-example-vehicle`: `obstacle_hold.yaml`, `dock_to_yard.yaml` | `spectator` | the example team's own navigator holding behind a crate until it is moved aside, then the dock-to-yard drive, run through the repository's Makefile |

## How it was made

1. The published images of the release (`FIELDPROOF_IMAGE_TAG=main` the day after v0.20.1) and
   the vehicle images rebuilt on them. Each scenario was run once with `--video` (or
   `--set outputs.video.camera=overview` for the fleet and `camera=hall` for the link
   scenario) on a workstation with a render node: the videos land in
   `runs/<run_id>/video/<camera>.mp4` next to the reports.
2. The replay run has no simulator and no camera; `tools/media/replay_plot.py <run_dir>` draws
   its card from the MCAP recording (`/pose` against the ground truth the observer publishes on
   `/fieldproof/metrics`) and the `localised` verdict.
3. `tools/media/m2_demo_video.sh` assembles the clips: a title card, one caption card per clip,
   the clips with a caption bar and the run's verdict, the two lockstep recordings side by
   side, the matrix and replay cards, a closing card; `ffmpeg` runs in the
   `lscr.io/linuxserver/ffmpeg` container with the host's DejaVu font mounted, so nothing
   installs on the host. The same script cuts the README GIF (`fps=10`, 640 px wide, a palette
   per clip) from a selection of the clips.

Recording costs the simulator a render pass per frame: on the workstation the lidar fell from
9 Hz to about 4 Hz of simulation time while a camera recorded, still above the scenarios' 2 Hz
floors, which is why `outputs.video` stays off in the scenarios and the demo turns it on.
