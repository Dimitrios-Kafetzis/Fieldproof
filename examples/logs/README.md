# Example field logs

Field logs the replay source (`source: {kind: replay}`, task P2-T07,
[replay.md](../../docs/reference/replay.md)) plays into a vehicle's stack instead of the
simulator. Recordings are never committed (`*.mcap` is ignored, like `runs/`); the files here
are written on demand.

| File | Contents | Written by |
| --- | --- | --- |
| `ugv_dock_drive.mcap` | the synthetic field log of the reference UGV: 120 s in a 20 m by 12 m hall with crates, the vehicle at rest for 10 s, a driven rectangle (8 m, a left turn, 4 m, a turn, 8 m, a turn, 4 m, a turn) at 0.5 m/s, at rest to the end; `/scan` (720 beams, 10 Hz, 1 cm of noise), `/odom` and `/tf` (`odom -> base_link`, 20 Hz, the wheel odometry with a 2 % scale error and a yaw-rate bias, so it drifts), `/tf_static` (the reference UGV's sensor frames) and `/ground_truth/pose` (10 Hz, the true pose in the world, as a motion-capture system would give it); deterministic for its seed (7), 2 to 3 MB | `make replay-log` (`fieldproof log synth examples/logs/ugv_dock_drive.mcap`) |

`examples/scenarios/ugv_replay.yaml` replays it through the example stack `ugv_nav2` and
judges SLAM Toolbox's pose against the ground truth. The maintainer has supplied no real field
log so far (STATUS.md, open question 2); a real one takes the synthetic one's place in a
scenario by its path and a `source.vehicles` mapping written from `fieldproof log info <log>`.
