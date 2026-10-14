# 03 — Task backlog

One task equals one dedicated working session (tasks marked ×2 are planned for two sessions). Each
task has a goal, dependencies, inputs the maintainer must supply, deliverables, a definition of done
(the contract the session must satisfy), verification, related ADRs, and the exact line to paste to
open the session. Task state lives in [STATUS.md](STATUS.md), not here.

Conventions: `P<phase>-T<nn>`. Branch name `task/p0-t01-repo-bootstrap`. PR title is the task title
prefixed with its ID.

---

## Phase 0 — Foundations (Milestone M0)

### P0-T01 — Repository bootstrap and GitHub setup
- **Goal:** Turn this directory into the public `fieldproof` repository with licence, governance files and protections.
- **Depends on:** nothing.
- **Inputs from maintainer:** GitHub account or organisation name; public or private; confirm the name Fieldproof; confirm Apache-2.0.
- **Deliverables:** `git init` on `main`; `LICENSE` (Apache-2.0); `NOTICE`; `CONTRIBUTING.md`; `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1); `SECURITY.md`; `.github/CODEOWNERS`, issue templates (bug, feature, scenario request), PR template with task-ID checklist; `.editorconfig`; first commit containing the planning docs and ADRs; remote created with `gh repo create`; branch protection on `main` (PR required, status checks required, linear history); labels for phases and task types.
- **Definition of done:** Repo visible on GitHub, `main` protected, planning docs rendered correctly on GitHub, STATUS.md updated with links.
- **Verification:** `gh repo view`, `gh api repos/:owner/:repo/branches/main/protection`.
- **ADRs:** 0008.
- **Session opener:** `Work on task P0-T01 (repository bootstrap). My GitHub owner is <owner>, visibility <public|private>.`

### P0-T02 — Host environment setup
- **Goal:** Install and verify the host toolchain on the maintainer's Ubuntu 24.04 workstation without installing ROS or Gazebo on the host.
- **Depends on:** nothing (can run before P0-T01).
- **Inputs from maintainer:** sudo approval during the session.
- **Deliverables:** Docker Engine + Compose plugin + Buildx, user in `docker` group; `qemu-user-static` with binfmt registered for arm64; Node 22 LTS via `nvm` and `pnpm` via corepack; `uv` for Python environments; `pre-commit`; `gh` CLI authenticated; `tools/host-check.sh` that verifies every prerequisite and prints versions; `docs/setup/host.md`.
- **Definition of done:** `tools/host-check.sh` passes; `docker run --rm --device /dev/dri ubuntu:24.04 ls /dev/dri` lists the render node; `docker run --rm --platform linux/arm64 ubuntu:24.04 uname -m` prints `aarch64`.
- **Verification:** the check script, run twice (idempotent).
- **ADRs:** 0004.
- **Session opener:** `Work on task P0-T02 (host environment setup). I will approve sudo commands.`

### P0-T03 — Base container images
- **Goal:** Reproducible images for simulation, ROS 2 runtime and development.
- **Depends on:** P0-T02.
- **Inputs from maintainer:** none.
- **Deliverables:** `docker/base-ros2/Dockerfile` (Ubuntu 24.04 + ROS 2 Jazzy ros-base, non-root user, pinned apt snapshot date); `docker/sim/Dockerfile` (adds Gazebo Harmonic, ros_gz, Mesa drivers, headless rendering support); `docker/dev/Dockerfile` + `.devcontainer/devcontainer.json` (adds dev tools, colcon, mixins, clang tooling); `docker/compose.yaml` with profiles; image labels (OCI annotations, git SHA); `Makefile` targets `images`, `up`, `down`, `shell`, `prune`.
- **Definition of done:** `make up` starts `gz sim -s -r empty.sdf` and `ros2 topic list` works from the dev container; a camera sensor renders an image headless with `/dev/dri` and also with software rendering (`LIBGL_ALWAYS_SOFTWARE=1`); images build with BuildKit cache under 15 minutes cold.
- **Verification:** `tests/images/test_images.sh` runs the three checks.
- **ADRs:** 0001, 0002, 0004.
- **Session opener:** `Work on task P0-T03 (base container images).`

### P0-T04 — Developer tooling and quality gates
- **Goal:** Make the standards in 04-engineering-standards.md enforceable automatically.
- **Depends on:** P0-T01.
- **Inputs from maintainer:** none.
- **Deliverables:** `.pre-commit-config.yaml` with ruff (lint+format), mypy, clang-format, clang-tidy config, cmake-format, eslint + prettier, yamllint, markdownlint, shellcheck, hadolint, commitlint (Conventional Commits), gitleaks; `pyproject.toml` root tool config; `.clang-format`, `.clang-tidy`; `justfile` or `Makefile` with `lint`, `test`, `fmt`; `CONTRIBUTING.md` updated with the workflow.
- **Definition of done:** `pre-commit run --all-files` passes on the repo; a deliberately bad commit message is rejected locally.
- **Verification:** run hooks; attempt a non-conventional commit.
- **ADRs:** 0009.
- **Session opener:** `Work on task P0-T04 (developer tooling and quality gates).`

### P0-T05 — CI/CD pipeline
- **Goal:** GitHub Actions enforcing lint, build, test on every PR, publishing images and releases.
- **Depends on:** P0-T03, P0-T04.
- **Inputs from maintainer:** confirm GHCR for images; enable GitHub Pages.
- **Deliverables:** workflows `ci.yml` (lint, unit tests, image build with registry cache, image tests), `images.yml` (push to GHCR on main, SBOM via syft, Trivy scan), `release.yml` (release-please, semantic versions, changelog), `docs.yml` (placeholder for P0-T06); Dependabot for actions, pip, npm, docker; required status checks wired into branch protection; CI badge in README.
- **Definition of done:** a PR with a lint error is blocked; main build pushes `ghcr.io/dimitrios-kafetzis/fieldproof-sim:main`; release-please opens a release PR after a `feat:` commit.
- **Verification:** two throwaway PRs (one failing, one passing).
- **ADRs:** 0008.
- **Session opener:** `Work on task P0-T05 (CI/CD pipeline).`

### P0-T06 — Documentation site
- **Goal:** Publish the docs as a browsable site generated from the repo.
- **Depends on:** P0-T05.
- **Inputs from maintainer:** none.
- **Deliverables:** MkDocs Material with `mkdocs.yml`, nav for plan, ADRs, setup, reference; ADR index auto-generated; Mermaid support; `docs.yml` deploys to GitHub Pages on main; docs linting in CI; `docs/index.md`.
- **Definition of done:** site live at the Pages URL with all planning docs and ADRs.
- **Verification:** curl the site; broken-link check passes.
- **Session opener:** `Work on task P0-T06 (documentation site).`

### P0-T07 — Monorepo layout and package skeletons
- **Goal:** Create the directory structure and buildable, testable empty packages for every layer.
- **Depends on:** P0-T04, P0-T05.
- **Inputs from maintainer:** none.
- **Deliverables:** layout per 04-engineering-standards.md: `sim/` (colcon ws: `fieldproof_worlds`, `fieldproof_gz_plugins`), `bridge/` (`fieldproof_bridge`), `robots/` (`fieldproof_robots`), `engine/` (Python package `fieldproof` with `pyproject.toml`, CLI stub `fieldproof --version`), `cockpit/` (pnpm workspace, Vite app stub), `examples/`, `assets/`, `tools/`, `tests/`; each with a trivial unit test; CI runs all of them; `ARCHITECTURE.md` at root pointing to docs.
- **Definition of done:** `make test` runs Python, C++ (ctest), and TS tests and all pass; CI green.
- **Verification:** CI run.
- **ADRs:** 0008, 0009.
- **Session opener:** `Work on task P0-T07 (monorepo layout and package skeletons).`

---

## Phase 1 — Vertical slice (Milestone M1)

### P1-T01 — Reference UGV model
- **Goal:** A differential-drive robot with the sensor suite typical of indoor/outdoor UGVs.
- **Depends on:** P0-T07.
- **Inputs from maintainer:** optional: a real robot URDF to use instead of the reference.
- **Deliverables:** `robots/fieldproof_robots/ugv_ref/` with xacro URDF (base, wheels, casters, 2D lidar, IMU, RGB-D camera, GPS optional), SDF generation at build time, Gazebo plugins (DiffDrive, JointStatePublisher, sensors with noise params), `robot.yaml` manifest, meshes with licences; `ros2 launch fieldproof_robots spawn_ugv_ref.launch.py`.
- **Definition of done:** robot spawns, `gz topic -e /cmd_vel` drives it, all sensors publish on gz topics at declared rates; model passes `gz sdf --check`.
- **Verification:** `launch_testing` test spawning and driving 1 m, asserting odometry delta.
- **Session opener:** `Work on task P1-T01 (reference UGV model).`

### P1-T02 — Reference world and asset pipeline
- **Goal:** A world that exercises navigation: rooms, corridors, obstacles, outdoor area with slope.
- **Depends on:** P0-T07.
- **Inputs from maintainer:** none.
- **Deliverables:** `sim/fieldproof_worlds/worlds/depot.sdf`; `assets/` vendoring convention with `LICENSES/`; Fuel lockfile `assets/fuel.lock` + fetch script; world README with map image; physics step and real-time settings documented.
- **Definition of done:** world loads in under 10 s, runs at RTF ≥ 1.0 headless with the UGV and sensors on the maintainer's machine; SDF validated.
- **Verification:** `tests/worlds/test_depot.py` loads world, checks RTF over 30 sim seconds.
- **Session opener:** `Work on task P1-T02 (reference world and asset pipeline).`

### P1-T03 — Bridge configuration generator and contract tests
- **Goal:** Generate the ros_gz bridge configuration from a vehicle manifest so the ROS 2 interface matches the real drivers exactly.
- **Depends on:** P1-T01.
- **Inputs from maintainer:** none.
- **Deliverables:** `bridge/fieldproof_bridge/`: manifest-driven generator (`fieldproof bridge gen robot.yaml`) producing ros_gz YAML + launch; adapter nodes for `camera_info`, GPS fix, IMU covariance; namespacing; `/clock` bridge; TF tree assembly (`robot_state_publisher` from the same URDF); contract test harness checking topic names, types, frame ids, QoS and rate tolerance.
- **Definition of done:** UGV in the depot world produces `/scan`, `/imu/data`, `/odom`, `/camera/*`, `/tf` with correct types, frames and rates; contract test passes.
- **Verification:** `launch_testing` contract test in CI.
- **ADRs:** 0002.
- **Session opener:** `Work on task P1-T03 (bridge configuration generator and contract tests).`

### P1-T04 — Robot runtime container and vehicle manifest (×2)
- **Goal:** Run a real navigation stack as the "vehicle codebase" in its own container from a manifest, with the driver-swap pattern.
- **Depends on:** P1-T03.
- **Inputs from maintainer:** none (Nav2 is the stand-in codebase).
- **Deliverables:** `robot.yaml` manifest schema v0 (image or build context, entrypoint launch, params, driver-swap map, ready conditions, resource budget); Dockerfile generator producing a multi-stage image from a ROS 2 repo at a pinned commit (`vcs` import, `rosdep`, `colcon build --merge-install`); `examples/vehicles/ugv_nav2/` with Nav2 + SLAM Toolbox or AMCL, parameters identical to a field config, launch wiring that only adds `use_sim_time` and remaps; readiness probe on Nav2 lifecycle states.
- **Definition of done:** `docker run` of the vehicle image joins the ROS graph, Nav2 reaches `active`, accepts a `NavigateToPose` goal and the robot moves in the sim.
- **Verification:** integration test sending a goal and asserting arrival within tolerance.
- **ADRs:** 0004.
- **Session opener:** `Work on task P1-T04 (robot runtime container and vehicle manifest), session 1 of 2.`

### P1-T05 — Scenario schema v0 and runner v0
- **Goal:** The first end-to-end `fieldproof run`.
- **Depends on:** P1-T04.
- **Inputs from maintainer:** none.
- **Deliverables:** Pydantic schema (`apiVersion`, `world`, `vehicles[]`, `time`, `timeline[]` stub, `assertions[]` stub, `outputs`), JSON Schema export, `fieldproof validate`; orchestrator v0 using the Docker SDK: start sim → bridge → vehicles with readiness gates, timeout, teardown, structured logs; `run_id` and `runs/<run_id>/` layout; `fieldproof.toml` workspace config.
- **Definition of done:** `fieldproof run examples/ugv_nav_to_goal.yaml` brings everything up, waits for completion or timeout, tears down cleanly even on Ctrl-C; unit tests for schema and orchestrator state machine ≥ 80 % coverage.
- **Verification:** pytest; a manual interrupted run leaves no containers behind.
- **ADRs:** 0005.
- **Session opener:** `Work on task P1-T05 (scenario schema v0 and runner v0).`

### P1-T06 — Assertions v0, recording and reports
- **Goal:** Turn a run into a test with evidence.
- **Depends on:** P1-T05.
- **Inputs from maintainer:** none.
- **Deliverables:** assertion primitives `reach_pose`, `within_time`, `topic_rate`, `no_collision`, `topic_value` (expression over a field); streaming evaluator; rosbag2 MCAP recorder container with topic allow-list; side-channel topics `/fieldproof/events`, `/fieldproof/assertions`, `/fieldproof/metrics`; report in JSON and Markdown; exit code semantics.
- **Definition of done:** the UGV scenario reports PASS with per-assertion evidence; forcing a short timeout yields FAIL with the right assertion named; MCAP opens in Foxglove Studio.
- **Verification:** pytest with recorded fixtures; one failing scenario in tests.
- **Session opener:** `Work on task P1-T06 (assertions v0, recording and reports).`

### P1-T07 — First scenario in CI and M1 demo
- **Goal:** Prove the chain is reproducible away from the maintainer's machine.
- **Depends on:** P1-T06.
- **Inputs from maintainer:** run the demo locally and sign off M1.
- **Deliverables:** `scenario-tests.yml` workflow running the UGV scenario headless with software rendering on `ubuntu-24.04` runners, uploading MCAP and report as artefacts, JUnit summary; flaky-run policy (one retry, flagged); README quick-start; demo script `examples/demo_m1.sh`.
- **Definition of done:** scenario job green on main three consecutive runs; quick-start followed from a fresh clone works.
- **Verification:** CI history; maintainer sign-off recorded in STATUS.md.
- **Session opener:** `Work on task P1-T07 (first scenario in CI and M1 demo).`

---

## Phase 2 — Scenario engine (Milestone M2)

### P2-T01 — Multi-vehicle orchestration and resource budgets
- **Goal:** N vehicles per scenario, isolated and budgeted.
- **Depends on:** P1-T07.
- **Deliverables:** per-vehicle namespaces and TF prefixes end-to-end; `ROS_DOMAIN_ID` per run and optional Fast DDS discovery server; mounting of the vehicle's own DDS XML profile; cgroup CPU quota, cpuset and memory limits from the manifest; parallel start with dependency ordering; fleet example with three UGVs.
- **Definition of done:** three UGVs navigate concurrently without topic cross-talk; `docker stats` shows limits applied; contract tests per vehicle pass.
- **Verification:** integration test with three vehicles.
- **Session opener:** `Work on task P2-T01 (multi-vehicle orchestration and resource budgets).`

### P2-T02 — Network emulation
- **Goal:** Realistic radio links between vehicles and ground.
- **Depends on:** P2-T01.
- **Deliverables:** per-vehicle network namespace topology with a virtual "ground station" network; `tc netem` profiles (latency, jitter, loss, reordering, rate) as named presets (`wifi_good`, `lte_rural`, `rf_900mhz_5km`); timeline events `link.set`, `link.drop`, `link.restore`; measurement node validating applied profile; documentation of DDS behaviour under loss and recommended QoS.
- **Definition of done:** a scenario drops the link for 20 s and the assertion verifies the vehicle's documented loss-of-link behaviour; measured latency within 10 % of the profile.
- **Verification:** integration test with ping/iperf containers.
- **Session opener:** `Work on task P2-T02 (network emulation).`

### P2-T03 — Event injection framework (×2)
- **Goal:** A general, extensible way to perturb a run.
- **Depends on:** P2-T01.
- **Deliverables:** timeline DSL: `at`, `after`, `when` (expression over topics/metrics), `every`; event plugin API (Python entry points); built-in events: `spawn_object`, `remove_object`, `teleport`, `set_wind`, `gps.deny`, `gps.offset`, `imu.bias`, `sensor.dropout`, `sensor.freeze`, `battery.drain`, `actuator.limit`, `container.pause`, `container.kill`, `param.set`; Gazebo-side C++ plugin exposing services for world mutation; ROS-side fault-injector nodes inserted between bridge and vehicle; events recorded to `/fieldproof/events`.
- **Definition of done:** UGV scenario where an obstacle spawns on the planned path and Nav2 replans, and a GPS-denial scenario, both with assertions; plugin example in `examples/plugins/`.
- **Verification:** integration tests per event; unit tests for the DSL.
- **ADRs:** 0005.
- **Session opener:** `Work on task P2-T03 (event injection framework), session 1 of 2.`

### P2-T04 — Clock management and determinism
- **Goal:** Make time a controlled variable.
- **Depends on:** P2-T01.
- **Deliverables:** clock manager: sim-time with target RTF, pause/resume/step services, real-time-locked mode (RTF pinned to 1.0 with drift monitoring and warnings), lockstep mode (engine steps only when all declared vehicles acknowledge); seeded RNG propagated to Gazebo noise, world randomisation and events; drift and RTF written to `/fieldproof/metrics` and the report; determinism test running the same scenario twice and comparing trajectories within tolerance.
- **Definition of done:** two lockstep runs produce identical odometry streams; real-time-locked run reports drift below a documented threshold.
- **Verification:** determinism test in CI (nightly if slow).
- **ADRs:** 0006.
- **Session opener:** `Work on task P2-T04 (clock management and determinism).`

### P2-T05 — Assertion library v1 and KPIs
- **Goal:** Express realistic acceptance criteria.
- **Depends on:** P1-T06.
- **Deliverables:** assertions: `geofence`, `min_distance` (to objects or vehicles), `path_efficiency`, `mission_time`, `message_latency`, `resource_usage`, `lifecycle_state`, `sequence` (ordered events), `always`/`eventually`/`never` temporal combinators over expressions; KPI collectors; Python plugin API with example; evidence excerpts in reports.
- **Definition of done:** UGV scenarios rewritten with v1 assertions; documentation page with every assertion, parameters and examples.
- **Verification:** unit tests per assertion with synthetic streams.
- **Session opener:** `Work on task P2-T05 (assertion library v1 and KPIs).`

### P2-T06 — Runner hardening, run matrix and run database
- **Goal:** Operate at CI scale.
- **Depends on:** P2-T04, P2-T05.
- **Deliverables:** `fieldproof matrix` with parameter sweeps and seeds; parallel runs with resource-aware scheduling; retries with flake flagging; SQLite run DB (schema versioned with migrations); JUnit and HTML outputs; structured JSON logs; `fieldproof runs list|show|diff`.
- **Definition of done:** matrix of 12 runs completes with a single JUnit; `runs diff` shows KPI deltas between two runs.
- **Verification:** pytest; CI matrix job.
- **Session opener:** `Work on task P2-T06 (runner hardening, run matrix and run database).`

### P2-T07 — Field log replay source
- **Goal:** Use real sensor data as scenario input.
- **Depends on:** P2-T06.
- **Inputs from maintainer:** a field MCAP or ROS bag if available (synthetic one otherwise).
- **Deliverables:** scenario `source: replay` with topic mapping, time scaling and clock derived from the log; vehicle runs against replayed inputs with actuators open-loop; assertions on outputs (localisation error vs ground truth, detections); documentation of what replay can and cannot validate.
- **Definition of done:** replay scenario of the UGV log with a localisation-error assertion passes.
- **Verification:** integration test with a recorded fixture.
- **Session opener:** `Work on task P2-T07 (field log replay source).`

### P2-T08 — Vehicle onboarding SDK and M2 demo
- **Goal:** An external team can plug their repository in without reading Fieldproof source.
- **Depends on:** P2-T03, P2-T06.
- **Inputs from maintainer:** optional: a real vehicle repository to onboard as the worked example; sign off M2.
- **Deliverables:** `fieldproof vehicle init` scaffolding `robot.yaml` and Dockerfile; onboarding guide (driver swap, QoS, sim time, readiness, budgets); a separate example repository `fieldproof-example-vehicle` onboarded end-to-end; troubleshooting page; M2 demo script.
- **Definition of done:** following the guide, the example repository runs a scenario with no Fieldproof source modifications.
- **Verification:** CI job cloning the example repo and running its scenario.
- **Session opener:** `Work on task P2-T08 (vehicle onboarding SDK and M2 demo).`

---

## Phase 3 — Cockpit (Milestone M3)

### P3-T01 — Cockpit backend
- **Goal:** One process exposing the ROS graph and the engine to browsers.
- **Depends on:** P2-T06.
- **Deliverables:** Foxglove WebSocket server (`foxglove_bridge` or Python implementation) attached per run; FastAPI control API: runs CRUD, start/pause/step/stop, inject event, assertion status stream, run DB queries; MCAP streaming for replay; OpenAPI spec; localhost binding.
- **Definition of done:** Foxglove Studio connects and shows topics; `curl` can start a run and inject an event; API tests.
- **Verification:** pytest + schemathesis against OpenAPI.
- **ADRs:** 0003.
- **Session opener:** `Work on task P3-T01 (cockpit backend).`

### P3-T02 — Frontend scaffold and design system
- **Goal:** A production-grade frontend foundation.
- **Depends on:** P0-T07.
- **Deliverables:** Vite + React + TypeScript strict; react-three-fiber + drei; Zustand; Foxglove WS client; design tokens, dark/light theme, layout shell (3D viewport, side panels, timeline bar); Vitest, Testing Library, Playwright with CI; Storybook for panels; ESLint/Prettier wired to pre-commit.
- **Definition of done:** app shell renders with a spinning placeholder, connects to the backend, CI runs unit and E2E tests.
- **Session opener:** `Work on task P3-T02 (frontend scaffold and design system).`

### P3-T03 — 3D world view (×2)
- **Goal:** A faithful twin of the simulated world.
- **Depends on:** P3-T01, P3-T02.
- **Deliverables:** SDF/URDF loader to Three.js (meshes via glTF conversion pipeline in assets, primitives, materials); TF-driven vehicle poses; orbit/fly/follow cameras; grid, axes, ground; LOD and instancing; performance budget (60 fps with three vehicles on the integrated GPU); picking and selection with info panel.
- **Definition of done:** depot world with three UGVs renders with correct poses and meets the frame budget on the maintainer's machine.
- **Verification:** Playwright visual regression on fixtures; perf test script.
- **Session opener:** `Work on task P3-T03 (3D world view), session 1 of 2.`

### P3-T04 — Sensor and navigation overlays
- **Goal:** See what the robot sees.
- **Depends on:** P3-T03.
- **Deliverables:** LaserScan and PointCloud2 rendering, image panels (raw and compressed), Path, OccupancyGrid/costmaps, MarkerArray, PoseWithCovariance ellipses, per-vehicle toggles, colour maps, decimation for large clouds.
- **Definition of done:** Nav2 scenario shows lidar, costmaps, planned path and footprint aligned with the 3D twin.
- **Verification:** visual regression fixtures from MCAP.
- **Session opener:** `Work on task P3-T04 (sensor and navigation overlays).`

### P3-T05 — Scenario control, timeline and replay
- **Goal:** Steer a run and scrub its history.
- **Depends on:** P3-T04.
- **Deliverables:** scenario picker and YAML viewer with schema validation feedback; run/pause/step/stop; RTF slider; event injection forms generated from event schemas; timeline with event markers and assertion transitions; replay mode over MCAP with scrubbing and speed; assertion board with live evidence.
- **Definition of done:** inject an obstacle mid-run from the UI and watch the replan; scrub a finished run.
- **Verification:** Playwright E2E against a recorded run.
- **Session opener:** `Work on task P3-T05 (scenario control, timeline and replay).`

### P3-T06 — Fleet dashboard and diagnostics
- **Goal:** Operate many vehicles at once.
- **Depends on:** P3-T05.
- **Deliverables:** fleet table (state, pose, battery, link profile, CPU/mem), per-vehicle telemetry widgets (plots, gauges), `/rosout` and container log viewer with filters, `/diagnostics` panel, alerts on assertion failures and resource breaches.
- **Definition of done:** three-UGV scenario operated from the dashboard with logs and plots.
- **Session opener:** `Work on task P3-T06 (fleet dashboard and diagnostics).`

### P3-T07 — Cockpit packaging, docs and M3 demo
- **Goal:** Ship the cockpit as part of the product.
- **Depends on:** P3-T06.
- **Inputs from maintainer:** sign off M3.
- **Deliverables:** `fieldproof cockpit` serves the built frontend; cockpit container image; E2E suite in CI; user guide with screenshots; keyboard shortcuts; accessibility pass; M3 demo script.
- **Definition of done:** fresh clone → `fieldproof cockpit` → run and inspect a scenario following the guide.
- **Session opener:** `Work on task P3-T07 (cockpit packaging, docs and M3 demo).`

---

## Phase 4 — Drones (Milestone M4)

### P4-T01 — PX4 SITL integration
- **Goal:** PX4 as a first-class vehicle type.
- **Depends on:** P2-T08.
- **Inputs from maintainer:** preferred PX4 release.
- **Deliverables:** `fieldproof/px4-sitl` image at pinned release with Gazebo Harmonic models; uXRCE-DDS agent and `px4_msgs` bridge; MAVLink router to companion and ground; vehicle manifest type `px4`; readiness on EKF health; multi-instance support.
- **Definition of done:** `x500` takes off, flies a mission from QGroundControl-style MAVLink commands and lands; ROS 2 topics visible.
- **Verification:** integration test with a scripted mission.
- **Session opener:** `Work on task P4-T01 (PX4 SITL integration).`

### P4-T02 — ArduPilot SITL integration
- **Goal:** ArduPilot (Copter, Plane, Rover marine) as a first-class vehicle type.
- **Depends on:** P2-T08.
- **Inputs from maintainer:** preferred ArduPilot release.
- **Deliverables:** `fieldproof/ardupilot-sitl` image with `ardupilot_gazebo` plugin for Harmonic; MAVROS and MAVSDK containers; manifest type `ardupilot` with frame selection; parameter file injection; readiness on `PREARM` ok.
- **Definition of done:** Copter flies the same mission as P4-T01; Rover frame drives the UGV world (preparation for USV).
- **Session opener:** `Work on task P4-T02 (ArduPilot SITL integration).`

### P4-T03 — Reference UAV models
- **Goal:** Realistic airframes with sensors.
- **Depends on:** P4-T01, P4-T02.
- **Deliverables:** quadrotor and VTOL fixed-wing SDF with GPS, baro, mag, IMU, rangefinder, gimbaled camera; propulsion and aero params documented; noise models; manifests for both autopilots.
- **Definition of done:** both airframes fly on both autopilots; hover power and cruise speed within documented ranges.
- **Session opener:** `Work on task P4-T03 (reference UAV models).`

### P4-T04 — Aerial events and assertions
- **Goal:** Test the failure modes that matter in flight.
- **Depends on:** P4-T03, P2-T03.
- **Deliverables:** wind field model (steady, gust, turbulence) as Gazebo plugin + events; GPS denial, spoofing offset, multipath noise; battery failsafe injection; geofence assertion for 3D; `rtl_executed`, `landed_within`, `altitude_envelope` assertions; airspace world with no-fly zones.
- **Definition of done:** `uav_mission_gps_denied.yaml` passes on both autopilots with RTL verified.
- **Session opener:** `Work on task P4-T04 (aerial events and assertions).`

### P4-T05 — Companion computer stacks and M4 demo
- **Goal:** Realistic companion-side autonomy as the "real codebase".
- **Depends on:** P4-T04.
- **Inputs from maintainer:** optional: a real companion repository; sign off M4.
- **Deliverables:** example companion container (MAVSDK mission node, precision-landing with the gimbal camera and a fiducial), cockpit aerial widgets (HUD, attitude, mode), CI scenario jobs, demo script.
- **Definition of done:** precision landing scenario passes with landing error KPI reported.
- **Session opener:** `Work on task P4-T05 (companion computer stacks and M4 demo).`

---

## Phase 5 — USVs (Milestone M5)

### P5-T01 — Hydrodynamics and environment plugins (×2)
- **Goal:** Credible water physics in Gazebo Harmonic.
- **Depends on:** P2-T08.
- **Deliverables:** VRX plugins (buoyancy, hydrodynamics, wave field, wind, thrusters, surface rendering) pinned and built in the sim image; sea-state presets (Beaufort / WMO sea state 0–4); current field plugin; parameters documented; unit tests on buoyancy equilibrium and wave spectra.
- **Definition of done:** a box floats at the analytically expected draft; waves follow the configured spectrum.
- **Session opener:** `Work on task P5-T01 (hydrodynamics and environment plugins), session 1 of 2.`

### P5-T02 — Reference USV model
- **Goal:** A catamaran with the common marine sensor suite.
- **Depends on:** P5-T01.
- **Deliverables:** twin-thruster catamaran SDF with GPS, IMU, lidar, camera, simulated AIS and depth sounder; manifest for ArduPilot Rover (marine frame) and for a custom ROS 2 controller; thruster allocation documented.
- **Definition of done:** USV reaches commanded speed and turn rate within documented ranges.
- **Session opener:** `Work on task P5-T02 (reference USV model).`

### P5-T03 — Marine worlds
- **Goal:** Harbour and open-water environments.
- **Depends on:** P5-T01.
- **Deliverables:** harbour world (quays, dock, buoys, moored vessels), open water world with islands; navigation marks; spawn points; world docs.
- **Definition of done:** both worlds run at RTF ≥ 1.0 with the USV.
- **Session opener:** `Work on task P5-T03 (marine worlds).`

### P5-T04 — Marine events, assertions and scenarios
- **Goal:** Station-keeping, docking and encounter testing.
- **Depends on:** P5-T02, P5-T03.
- **Deliverables:** events `sea_state.set`, `current.set`, `vessel.spawn_moving`; assertions `station_keeping_error`, `docked_within`, `cpa_tcpa` (closest point of approach), `colregs_encounter` (head-on, crossing, overtaking checks); scenarios `usv_station_keeping_ss3.yaml`, `usv_docking.yaml`, `usv_crossing_encounter.yaml`.
- **Definition of done:** station-keeping scenario passes with error KPI; encounter scenario produces a CPA report.
- **Session opener:** `Work on task P5-T04 (marine events, assertions and scenarios).`

### P5-T05 — Cockpit marine layer and M5 demo
- **Goal:** Operate the USV from the cockpit.
- **Depends on:** P5-T04, P3-T07.
- **Inputs from maintainer:** sign off M5.
- **Deliverables:** sea surface rendering synced to the wave plugin, nautical chart overlay (S-57 or raster tiles), heading/COG/SOG widgets, AIS targets, demo script.
- **Definition of done:** docking scenario operated and replayed from the cockpit.
- **Session opener:** `Work on task P5-T05 (cockpit marine layer and M5 demo).`

---

## Phase 6 — Fidelity (Milestone M6)

### P6-T01 — ARM64 target emulation (×2)
- **Goal:** Run the vehicle's real binaries.
- **Depends on:** P2-T08.
- **Inputs from maintainer:** a target image or board type (Raspberry Pi 4/5, Jetson for CPU-only nodes).
- **Deliverables:** `tier: target` in manifests; QEMU user-mode path for arm64 containers; QEMU system-mode with KVM path for full OS images with virtio networking into the run topology; benchmark report of overheads per node type; mixed-tier runs (perception SIL, control emulated).
- **Definition of done:** UGV Nav2 scenario passes at `--tier target` on an arm64 image; overhead documented.
- **ADRs:** 0007.
- **Session opener:** `Work on task P6-T01 (ARM64 target emulation), session 1 of 2.`

### P6-T02 — Compute budget profiles
- **Goal:** Make CPU starvation bugs appear before the field.
- **Depends on:** P6-T01.
- **Deliverables:** profiles for Pi 4, Pi 5, Jetson Orin Nano (CPU side), generic x86 SBC: cpuset, quota, memory, I/O; scheduler stress option; CPU-time per node in metrics; assertion `deadline_miss_rate`.
- **Definition of done:** a scenario demonstrates a timing failure under a Pi 4 profile that passes unconstrained, and the report explains it.
- **Session opener:** `Work on task P6-T02 (compute budget profiles).`

### P6-T03 — Sensor noise calibration from field logs
- **Goal:** Noise models from data, not guesses.
- **Depends on:** P2-T07.
- **Inputs from maintainer:** field logs with static and moving segments, if available.
- **Deliverables:** `fieldproof calibrate imu|gps|lidar|camera` tools: Allan variance for IMU, GPS error distribution and dropout statistics, lidar range noise and dropout vs distance/material, image noise; output SDF sensor noise blocks and a calibration report; provenance recorded in the manifest.
- **Definition of done:** calibrated UGV sensors reproduce field log statistics within documented tolerance.
- **Session opener:** `Work on task P6-T03 (sensor noise calibration from field logs).`

### P6-T04 — Hardware in the loop
- **Goal:** Real autopilot hardware on the bench, simulated world.
- **Depends on:** P4-T05.
- **Inputs from maintainer:** a Pixhawk-class flight controller and USB cable; optional companion board.
- **Deliverables:** `tier: hil` with serial/UDP passthrough from the sim container to the FC; PX4 and ArduPilot HIL/SIH wiring documented; companion over Ethernet joining the run network; safety checklist (props off, arming interlocks); HIL-specific readiness gates.
- **Definition of done:** the UAV mission scenario passes with the real FC; cockpit shows the real FC telemetry.
- **Session opener:** `Work on task P6-T04 (hardware in the loop).`

### P6-T05 — ROS 1 compatibility
- **Goal:** Legacy stacks still testable.
- **Depends on:** P2-T08.
- **Deliverables:** `fieldproof/ros1-noetic` image (Ubuntu 20.04), `ros1_bridge` container with parameterised topic mapping, manifest type `ros1`, example legacy stack (e.g. `move_base`) running the UGV scenario; limitations documented (EOL, QoS mapping).
- **Definition of done:** ROS 1 vehicle passes the UGV navigation scenario.
- **ADRs:** 0002.
- **Session opener:** `Work on task P6-T05 (ROS 1 compatibility).`

### P6-T06 — Sim-versus-field comparison and M6 demo
- **Goal:** Quantify the gap.
- **Depends on:** P6-T02, P6-T03.
- **Inputs from maintainer:** a field log of a mission that also exists as a scenario; sign off M6.
- **Deliverables:** `fieldproof compare run-id field.mcap`: time alignment, trajectory and KPI divergence metrics, per-sensor statistics comparison, HTML report; guidance on interpreting divergence; demo script.
- **Definition of done:** comparison report for the UGV mission with divergence metrics and a reproducible notebook.
- **Session opener:** `Work on task P6-T06 (sim-versus-field comparison and M6 demo).`

---

## Phase 7 — Product (Milestone M7, v1.0)

### P7-T01 — Scenario library and templates
- **Depends on:** P5-T05, P4-T05.
- **Deliverables:** curated scenarios per domain (≥ 5 each) with documentation, difficulty tags and expected KPIs; `fieldproof scenario new --template`; library index page.
- **Definition of done:** all library scenarios pass nightly.
- **Session opener:** `Work on task P7-T01 (scenario library and templates).`

### P7-T02 — World editor in the cockpit
- **Depends on:** P3-T07.
- **Deliverables:** place/move/delete assets, set spawn points, geofences and no-fly zones, save to SDF and scenario YAML; undo/redo; asset browser.
- **Definition of done:** build a new world in the editor and run a scenario in it without touching files.
- **Session opener:** `Work on task P7-T02 (world editor in the cockpit).`

### P7-T03 — Run history dashboard and trends
- **Depends on:** P2-T06, P3-T07.
- **Deliverables:** run history views, KPI trends per scenario over commits, flaky-scenario detection, compare two runs side by side, export CSV.
- **Definition of done:** dashboard shows trends over the nightly history.
- **Session opener:** `Work on task P7-T03 (run history dashboard and trends).`

### P7-T04 — Plugin SDK and versioned APIs
- **Depends on:** P2-T03, P2-T05.
- **Deliverables:** documented, semver-ed APIs for vehicle, event, assertion and cockpit-panel plugins; cookiecutter templates; compatibility tests; plugin discovery via entry points and a `plugins.lock`.
- **Definition of done:** an external plugin repo passes the compatibility suite.
- **Session opener:** `Work on task P7-T04 (plugin SDK and versioned APIs).`

### P7-T05 — Security hardening and supply chain
- **Depends on:** P3-T07.
- **Deliverables:** cockpit authentication (local accounts + OIDC option), TLS option, threat model document, image signing with cosign, SBOM publication, dependency review gate, `SECURITY.md` process tested with a dry run.
- **Definition of done:** security review checklist complete; signed images verified in CI.
- **Session opener:** `Work on task P7-T05 (security hardening and supply chain).`

### P7-T06 — Release v1.0
- **Depends on:** P7-T01 … P7-T05.
- **Inputs from maintainer:** release approval.
- **Deliverables:** tutorials (first scenario, onboarding a vehicle, writing an assertion, drone and USV walkthroughs), versioned docs, changelog, `v1.0.0` tag, GitHub release with images and SBOMs, announcement draft.
- **Definition of done:** fresh clone to first scenario in under 15 minutes by a person who has not seen the project.
- **Session opener:** `Work on task P7-T06 (release v1.0).`
