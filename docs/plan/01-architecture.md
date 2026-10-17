# 01 — Architecture

## Layer diagram

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  L5  Cockpit (browser)  React + Three.js  ── Foxglove WS protocol + REST ──┐ │
├─────────────────────────────────────────────────────────────────────────────┤
│  L4  Scenario Engine (Python)                                               │
│      schema · runner · orchestrator · event injector · assertions · reports │
│      run DB · MCAP recorder · CLI `fieldproof`                              │
├───────────────┬────────────────────────────────┬────────────────────────────┤
│  L3 Robot     │  L3 Robot runtime (container)  │  L3 Robot runtime …        │
│  runtime      │  real ROS 2 workspace, pinned  │                            │
│  (container)  │  cpu/mem budget, netns + netem │                            │
├───────────────┴────────────────────────────────┴────────────────────────────┤
│  L2  Bridge  ros_gz + adapter nodes · topic/type/rate contract per vehicle  │
├─────────────────────────────────────────────────────────────────────────────┤
│  L1  Physics & sensors  Gazebo Harmonic (headless server, ogre2 rendering)  │
│      plugins: diff-drive, PX4/ArduPilot SITL glue, VRX hydrodynamics        │
└─────────────────────────────────────────────────────────────────────────────┘
```

Dependencies point downward only. L1 and L2 have no knowledge of scenarios. L3 has no knowledge of
anything, which is the point. L4 drives L1 through gz-transport services and L3 through container
lifecycle and ROS 2 topics. L5 is a client of L4 and a read-only observer of the ROS 2 graph.

## L1 — Physics and sensor engine

- **Engine:** Gazebo Harmonic (ADR-0001), run as `gz sim -s` (server only) in the `fieldproof/sim`
  container with `/dev/dri` passed through for Mesa rendering of camera, depth and GPU lidar sensors.
  Software rendering fallback for CI runners without a GPU.
- **Worlds:** SDF 1.11. Assets vendored under `assets/` with licence files, or fetched from Gazebo Fuel
  at pinned versions by a lockfile.
- **Vehicle models:** SDF generated from URDF via xacro where the robot repository provides URDF, so
  the description used in simulation is the one used on the vehicle.
- **Plugins (C++):** reference vehicle plugins, VRX hydrodynamics (Phase 5), event hooks (spawn,
  teleport, set wind, set sea state, sensor fault), metrics publisher (collisions, contacts).
- **Determinism:** fixed physics step, seeded noise, lockstep mode via gz-transport `step` when the
  scenario requires it.

## L2 — Bridge

The bridge turns engine output into the exact ROS 2 interface the vehicle's driver nodes would
produce. It is configured, not programmed, per vehicle:

```yaml
# excerpt of robot.yaml driver-swap map
driver_swap:
  - replaces: velodyne_driver          # node name on the real vehicle
    gz_sensor: lidar_front             # sensor in the SDF
    ros_topic: /points                 # topic the real driver publishes
    ros_type: sensor_msgs/msg/PointCloud2
    frame_id: velodyne
    rate_hz: 10
```

- `ros_gz_bridge` handles the standard types. Thin Python or C++ adapter nodes handle shape mismatches
  (e.g. GPS fix status fields, IMU covariance conventions, camera_info).
- A contract test per vehicle verifies topic names, types, frame ids, QoS and rates against the
  manifest using `launch_testing`.
- `/clock` is bridged when the scenario runs in sim time, and `use_sim_time` is injected into the
  vehicle's launch via parameter override, never by editing the vehicle's files.

## L3 — Robot runtime

- One container per vehicle built from the vehicle's repository at a pinned commit by a multi-stage
  Dockerfile that Fieldproof generates from the manifest, or a prebuilt image the team supplies.
- Each vehicle gets its own network namespace. Inter-vehicle and vehicle-to-ground links run through
  `tc netem` profiles (latency, jitter, loss, rate) controlled by the scenario engine.
- DDS isolation by `ROS_DOMAIN_ID` per run, or Fast DDS discovery server for large fleets. The same
  DDS XML profile the vehicle uses in the field is mounted in.
- cgroup CPU and memory budgets approximate the target computer (Phase 6 adds QEMU and pinning).
- Health checks: the engine waits for declared "ready" topics or lifecycle states before starting the
  scenario clock.

## L4 — Scenario engine

Python 3.12 package `fieldproof`, installed as a CLI.

```text
fieldproof validate scenario.yaml
fieldproof run scenario.yaml [--tier sil|target|hil] [--rtf 4] [--headless] [--record]
fieldproof replay run-id
fieldproof report run-id [--format md|html|junit]
fieldproof matrix sweep.yaml           # parameter sweeps, parallel runs
fieldproof cockpit                     # serve the GUI against the local engine
```

Components:

- **Schema** — Pydantic models, exported JSON Schema for editor autocompletion. Versioned
  (`apiVersion: fieldproof.dev/v0alpha1`, as the vehicle manifest and the Fuel lock).
- **Orchestrator** — builds the per-run topology (sim container, bridge, N vehicle containers,
  recorder, cockpit backend) and drives it via the Docker API. Compose files are an output, not the
  source of truth.
- **Clock manager** — sim-time default with RTF target, pause, step, real-time-locked mode, and drift
  reporting (ADR-0006).
- **Event injector** — timeline (`at: 30s`) and conditional (`when: topic.expr`) triggers dispatching
  to Gazebo services, ROS 2 service calls, netem changes, or container actions.
- **Assertion evaluator** — streaming evaluation over ROS 2 topics and engine metrics. Built-in
  primitives plus a Python plugin API. Produces pass/fail with evidence (timestamps, values).
- **Recorder** — rosbag2 to MCAP for all topics, plus engine events, assertions and resource metrics
  as side-channel topics so one file holds the whole run.
- **Run database** — SQLite (file per workspace) with run metadata, results, KPIs, artefact paths.
- **Reporter** — Markdown and HTML reports, JUnit XML for CI, JSON for the cockpit.

## L5 — Cockpit

- **Stack:** TypeScript, React, Three.js via react-three-fiber, Vite, Zustand for state, Vitest and
  Playwright for tests.
- **Transport:** Foxglove WebSocket protocol (ADR-0003) for topics, so Foxglove Studio works as a
  secondary client during development, plus a small REST/WebSocket control API for the scenario engine.
- **Views:** 3D digital twin (world meshes, vehicle poses from TF, sensor overlays), fleet dashboard,
  scenario control (load/run/pause/step/inject), timeline with scrubbing over MCAP, assertion board,
  run history.
- **Rendering split:** the cockpit renders a *twin* for humans. Sensor images that the robot stack
  consumes are rendered by Gazebo. The two never need to match pixel for pixel.

## Cross-cutting

- **Identity of a run:** `run_id = <scenario-hash>-<timestamp>-<short-uuid>`; everything written to
  `runs/<run_id>/`.
- **Configuration precedence:** scenario file > CLI flags > workspace `fieldproof.toml` > defaults.
- **Extensibility points:** vehicle plugins (SDF + manifest), event plugins, assertion plugins,
  cockpit panel plugins (Phase 7 SDK).
- **Security posture (v1):** local trust boundary. Cockpit binds to localhost. Containers run as
  non-root. Images pinned by digest, SBOM generated in CI. Phase 7 adds auth and signing.

## Data flow for one run

1. `fieldproof run` validates the scenario and resolves vehicle manifests and images.
2. Orchestrator starts the sim container with the world, waits for the gz server.
3. Bridge containers start per vehicle, contract checks pass.
4. Vehicle containers start under their netns, budget and DDS profile; readiness gates pass.
5. Recorder starts. Clock manager releases the clock.
6. Event injector and assertion evaluator run on the timeline until end condition or timeout.
7. Teardown in reverse order, report generation, run DB update, artefacts in `runs/<run_id>/`.
8. Cockpit, if attached, has been streaming throughout and can now replay from the MCAP.
