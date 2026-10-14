# 02 — Roadmap and milestones

Each phase ends in a milestone with a demonstrable, testable exit criterion. A milestone is reached
only when every exit criterion is met on `main` with green CI, and the maintainer has run the demo
on their machine. Session counts are planning estimates: one task is sized for one working session
of roughly three to four hours; tasks marked ×2 are expected to need two.

| Phase | Milestone | Exit criteria | Tasks | Sessions |
|---|---|---|---|---|
| 0 Foundations | **M0 Hello, CI** | Repo public on GitHub with licence, ADRs, standards. Docker, ROS 2 Jazzy and Gazebo Harmonic images build in CI and run headless locally. Pre-commit and CI gates enforced. Docs site deployed. | P0-T01 … P0-T07 | 8 |
| 1 Vertical slice | **M1 First scenario passes** | A differential-drive UGV runs real Nav2 in its own container, navigates to a goal in a Gazebo world, scenario runner records MCAP and reports PASS. Same scenario runs headless in CI. | P1-T01 … P1-T07 | 8 |
| 2 Scenario engine | **M2 Fleet, faults, two clocks** | Three UGVs with namespaces and resource budgets. Network degradation and events from the timeline. Sim-time and real-time-locked modes. Parameter sweep produces a JUnit report. Field MCAP replays into the stack. External repo onboards through a manifest. | P2-T01 … P2-T08 | 10 |
| 3 Cockpit | **M3 Watch and steer in the browser** | Cockpit shows the 3D twin, sensors, fleet telemetry, injects events, scrubs a replay, and displays assertion status live. E2E tests in CI. | P3-T01 … P3-T07 | 9 |
| 4 Drones | **M4 Mission under wind and GPS loss** | Quadrotor on PX4 and on ArduPilot flies a waypoint mission from a MAVSDK companion node, survives a wind gust, executes failsafe on GPS denial, assertions verify RTL. | P4-T01 … P4-T05 | 6 |
| 5 USVs | **M5 Station-keeping in sea state 3** | Catamaran USV holds station and docks in a harbour world with waves, wind and current; marine KPIs reported; cockpit renders sea surface and nautical overlays. | P5-T01 … P5-T05 | 6 |
| 6 Fidelity | **M6 Same binary, same timing, same radio** | ARM64 image of a vehicle runs under QEMU with a Pi-class compute budget; IMU/GPS noise calibrated from a field log; real flight controller runs HIL; ROS 1 stack bridged; sim-vs-field divergence report produced. | P6-T01 … P6-T06 | 8 |
| 7 Product | **M7 v1.0** | Scenario library, world editor, run dashboard with trends, plugin SDK with versioned API, cockpit auth, signed images, tutorials. Tagged `v1.0.0`. | P7-T01 … P7-T06 | 7 |
| | | | **51 tasks** | **62** |

## Phase dependencies

```
P0 ─▶ P1 ─▶ P2 ─┬─▶ P3 (cockpit) ─────────────┐
                 ├─▶ P4 (drones) ──┐           │
                 │                 ├─▶ P6 ─────┼─▶ P7
                 └─▶ P5 (USV) ─────┘           │
                                               │
P3 is independent of P4/P5 and can interleave with them. ┘
```

P4 and P5 only need P2. P3 only needs P2. After M2 the maintainer may reorder P3, P4 and P5 to
match their priorities (for example, drones before cockpit if a drone team is waiting).

## Milestone demos

- **M0:** `make up` starts the sim container, `gz topic -l` lists topics, CI badge green.
- **M1:** `fieldproof run examples/ugv_nav_to_goal.yaml` ends with `PASS` and a report; opening the
  MCAP in Foxglove Studio shows the robot path.
- **M2:** `fieldproof matrix examples/ugv_fleet_sweep.yaml` runs 3 robots × 4 loss profiles and
  produces JUnit consumed by CI; one run uses real-time lock and reports zero drift.
- **M3:** `fieldproof cockpit` opens the browser; inject an obstacle mid-run and watch Nav2 replan.
- **M4:** `fieldproof run examples/uav_mission_gps_denied.yaml --autopilot px4` and `--autopilot ardupilot` both PASS.
- **M5:** `fieldproof run examples/usv_station_keeping_ss3.yaml` PASS with position error KPI.
- **M6:** same UGV scenario at `--tier target` on an ARM64 image; divergence report vs field log.
- **M7:** fresh clone to first scenario in under 15 minutes following the tutorial.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Gazebo Harmonic rendering on AMD Mesa inside containers is flaky | Camera sensors unusable | Validate in P0-T03 with `/dev/dri`; software rendering fallback; GPU-less sensor set for CI |
| VRX plugins lag behind Harmonic | USV phase slips | Pin to the VRX release targeting Harmonic; budget P5-T01 ×2 |
| PX4/ArduPilot Gazebo glue changes between versions | Drone phase churn | Pin autopilot versions; contract tests on MAVLink/uXRCE topics |
| Real-time lock impossible with heavy worlds on 16 threads | T2 scenarios cannot run | Report RTF honestly; reduce sensor rates; multi-machine in Phase 7 |
| QEMU ARM64 too slow for perception nodes | T2 limited | Document limits; per-node tier mixing (perception SIL, control emulated) |
| Scope creep in the cockpit | Delays fidelity work | Cockpit MVP list in P3 is fixed; extras go to Phase 7 |
| Disk space (88 GB free) | Image builds fail | Prune policy in Makefile; CI builds in GHCR not locally |
