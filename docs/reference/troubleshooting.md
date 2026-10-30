# Troubleshooting

What goes wrong when a vehicle is onboarded or a scenario runs, what it looks like and what to
do (task P2-T08). The run directory is the first place to look: `runs/<run_id>/engine.log` is
the orchestrator's log, `logs/` holds every container's output, `run.json` the record with the
phase the run died in and its reason ([scenarios.md](scenarios.md#the-run-directory)).
`fieldproof run --keep` leaves the containers of a failed run for a look (`docker logs`,
`docker exec -it <container> bash`); `docker ps -a --filter label=io.fieldproof.run` lists them.

## The image

| Symptom | Cause | Fix |
| --- | --- | --- |
| `fieldproof vehicle build`: `rosdep: Could not resolve rosdep key <key>` | the key is not in the pinned `ros/rosdistro` commit for Jazzy, or the package name in `package.xml` is wrong | check the key in [rosdistro](https://github.com/ros/rosdistro/tree/master/rosdep); a package only you have is vendored under `runtime.build.sources`; a key that must stay is skipped with `rosdep_skip_keys` and installed through `apt` |
| the build fails with `dependencies need an installer other than apt` | a key resolves to pip, which the reproducible build refuses | vendor the Python package under `sources` (an `ament_python` package of your own) or install the apt packaging of it through `apt` |
| `E: Unable to locate package` or `Version '...' not found` during the build | the package is not in the ROS apt snapshot of the base image's release | the snapshot is the release's; a newer package means a newer Fieldproof release (`fieldproof.toml`'s tag) or vendoring |
| `failed to resolve source metadata for ghcr.io/.../fieldproof-base-ros2:<tag>` | the base image at the workspace's tag is not on the daemon and the registry has no such tag | `docker pull` the stack images at the tag of `fieldproof.toml` (a release, `main`), or `FIELDPROOF_IMAGE_TAG=local` with images built in the checkout; `--base-image` overrides the manifest and the workspace |
| the image is huge (4 GB and more) | a dependency on `nav2_bringup`, `rviz2` or a `desktop` package pulls Gazebo, the TurtleBot models and the GUI | depend on the servers the launch starts (`nav2_controller`, `nav2_planner`, ...), never on the demo metapackages ([manifest.md](manifest.md#the-example-vehicle-ugv_nav2)) |
| the field driver packages are in the image, or the build fails on one | `package.xml` depends on them unconditionally | put them under a condition (`<exec_depend condition="$MY_TARGET == field">`), see [onboarding.md](onboarding.md#step-2-the-driver-swap) |
| `runtime.build.sources: 'src' is not a directory` | the manifest's `sources` are relative to the manifest, and the directory is elsewhere or empty | point `sources` at the package directories, or use `repos` with pinned commits |

## The manifest

| Symptom | Cause | Fix |
| --- | --- | --- |
| `extends: fieldproof:ugv_ref needs the reference models' directory` | the engine found no workspace stack: no checkout holds the engine, or `[stack] root` is wrong | run the engine from a Fieldproof checkout (`uv run --project <checkout>/engine fieldproof`), or set `[stack] root`/`FIELDPROOF_STACK` to one ([onboarding.md](onboarding.md#how-it-fits-together)) |
| inside the bridge container: the same error, or `no reference model` | the bridge image predates the `fieldproof:` form (before v0.20.0) | pull the images of the release the checkout is at |
| `vehicles[ugv1].manifest: ... is outside the workspace` | the scenario's manifest is above the directory holding `fieldproof.toml` | keep manifests and scenarios under the workspace root (the bridge mounts the workspace at `/workspace`), or move `fieldproof.toml` up |
| `runtime.drivers[x].provides: 'points' is not an interface of this manifest` | the topic is not one the model declares | the model's interfaces are listed by `fieldproof manifest validate` of the reference manifest; a stack that reads another name remaps in its launch |
| `no driver swap for scan, imu/data` in the validation output | informational: nothing in the swap map provides those interfaces | fine for sensors the stack does not read; add a driver entry otherwise |
| `entrypoint launch names no namespace_arg` in a fleet scenario | the launch cannot be told its namespace | add the argument to the launch and `namespace_arg` to the manifest ([onboarding.md](onboarding.md#step-8-fleets-and-namespaces)) |

## The run

| Symptom | Cause | Fix |
| --- | --- | --- |
| `image ... not found on the daemon` before anything starts | a run never pulls; the stack images at the workspace's tag or the vehicle image are missing | `docker pull` the stack images at the tag (or `FIELDPROOF_IMAGE_TAG`), `make image` for the vehicle |
| `Fuel model(s) not fetched under .../assets/fuel/models` | the world's Gazebo Fuel models are not in the checkout | `make assets` in the checkout (`make assets` of the scaffold's Makefile does it) |
| `world.file: 'depot.sdf' is not a file in ...` | the stack root is not a checkout (the path is in the message) | see the manifest entry above: run from a checkout or name one |
| the vehicle never becomes ready: `waiting (2/4 ready): /bt_navigator lifecycle inactive, map->base_link tf no transform` for the whole `ready.timeout_s` | a Nav2 stack cannot activate without the bridge's `odom -> base_link` and cannot localise without a scan: the bridge is not up, or the topics do not connect | the bridge's log in `logs/`; `ros2 topic info -v /ugv1/scan` inside the vehicle container (QoS); the clock (below) |
| a replay ends `localised: ... no estimate was observed` with `replay.missed` near the log's length | the stack activated before the log's data flowed (Nav2's costmaps wait a couple of seconds for the odometry transform), its first bringup aborted and the retry came late (issue #103) | lower `source.lead_in_s` so the data flows before the stack activates (the example uses 10 s); vehicle images from v0.20.1 on retry an aborted bringup within seconds |
| `waiting ...: /controller_server lifecycle unresponsive` for minutes, then ready after a retry | Nav2's lifecycle manager lost a transition under load and left the servers inactive (issue #32) | the example's `bringup_watchdog` retries the bringup; a slower host (software rendering) is the usual cause; the run is flagged flaky in CI and retried |
| readiness holds for `node:` but the stack does nothing | the condition is too weak: the node is in the graph before its servers are up | condition on what a goal needs (an action, a transform, a lifecycle state), [onboarding.md](onboarding.md#step-5-readiness) |
| `navigate_to_pose`: `goal ABORTED` right after it is sent | the goal was sent before the stack could plan (the readiness let it through), or the goal is outside the costmap | readiness on `tf: map, child: base_link` and the planner's lifecycle state; a goal inside the world |
| `goal REJECTED` | the action server refused it: the frame is not `map`, or a goal is already running | the goal is sent in the vehicle's `map` frame; one goal at a time per vehicle, or `after:` |
| the timeline entry times out (`no result within 300 s`) while the vehicle sits still | no `cmd_vel` reaches the base: the stack's velocity chain ends elsewhere (a smoother, a failsafe topic) | remap the last node's output to the model's `cmd_vel` in the launch; `ros2 topic echo` inside the container |
| the vehicle drives but `reach_pose` fails by metres | the stack navigates in a frame that is not the world's: the map frame starts at the spawn pose, a wrong `map -> odom` or a drifting odometry moves it | goals and `reach_pose` targets are in the world; a stack without localisation keeps `map -> odom` the identity and tolerates its drift (`tolerance_m`) |
| `topic_rate` fails at 3 Hz on a 10 Hz lidar | software rendering on a small host: the GPU lidar renders below its rate | the reference scenarios ask for 2 Hz; a GPU host (`FIELDPROOF_RENDER=gpu`, a `/dev/dri` render node) achieves the nominal rate |
| `message_latency` reports hours, TF warns *extrapolation into the future* | a node runs on the wall clock (`use_sim_time` not set for it) | pass `use_sim_time` to every node and included launch ([onboarding.md](onboarding.md#step-4-simulation-time)) |
| a subscriber sees nothing although `ros2 topic list` shows the topic | QoS mismatch: a reliable subscriber on a best-effort topic, or a volatile one on a latched topic that was published before it started | `ros2 topic info -v`; `qos_profile_sensor_data` for sensors; transient local for `tf_static`; the manifest's `qos` is the publisher's ([onboarding.md](onboarding.md#step-3-qos)) |
| nodes of two vehicles see each other's topics | both at the root: no namespace in the scenario, or the launch ignores it | `vehicles[].namespace` and the launch's `namespace_arg`; every run has its own ROS domain already |
| `resource_usage` fails on the CPU a few percent over the quota | Docker's samples overshoot a quota | ask for a margin (`max_cpu_pct: 150` for one core) |
| the simulator runs at a fraction of real time | software rendering, a camera recording (`--video` halves the rate), a host short of cores | a GPU, `--no-video`, fewer vehicles; the assertions count simulation time, the budgets wall clock |
| `TIMEOUT` verdict (exit code 3) | `time.timeout_s` passed before the timeline ended | a longer budget for a slow host, or a shorter mission |
| `ERROR` verdict (exit code 4) with `phase: bridges` and `the bridge of ugv1 did not publish /ugv1/odom` | the bridge failed on the manifest: a path of the description that does not resolve inside the container, a model the simulator could not spawn | the bridge's log; the description's paths are relative to the manifest; the spawn container's log for the model |
| `link ugv1 does not match profile lte_rural` | `network.verify: fail` and the host's kernel lacks `sch_netem`/`ifb`, or another run shares the host's network | `sudo modprobe sch_netem ifb`; one network run at a time |
| `lockstep: ugv1 did not acknowledge step` | the vehicle's clock agent could not reach the observer (a paused container, a killed stack) | the lockstep mode holds the world until every vehicle answers: pause and kill events end the run in lockstep |
| `INTERRUPTED` (exit code 130), containers left | the run was interrupted during the teardown | `docker ps -a --filter label=io.fieldproof.run`; `docker rm -f` of what is listed |
| `permission denied while trying to connect to the Docker daemon socket` | the user is not in the `docker` group yet | log out and back in after `tools/host-setup.sh` |
| the first run fails with *no space left on device* | the stack images, a vehicle image and the build cache take 15 GB and more | `docker system df`; `docker builder prune`; on a CI runner `tools/ci/free-disk.sh` |

## The reports

| Symptom | Cause | Fix |
| --- | --- | --- |
| an assertion `skipped: the run ended error` | the run died before the assertion could decide | the phase and reason at the top of `report.md`; fix the run, not the assertion |
| `topic_value` or `never` fails with `no message` | the topic never published during the run: the assertion needs data to judge | a `value` KPI with `count_if` to show nothing was commanded; `mode: eventually` where a message is expected later |
| `within_time` fails although the drive succeeded | the action's result came after `max_s` of wall clock (a slow simulator) | a wall-clock budget sized for the slowest host the scenario runs on; the drive's own duration is in `run.json` |
| the JUnit document shows one failed case per assertion of a failed run | expected: every assertion, timeline entry and the run itself is a case | read the failing case's message; the run's `report.md` has the evidence |
| the run is not in `fieldproof runs list` | the run database is in another runs directory (`--runs-dir`, `outputs.directory`) | `fieldproof runs list --runs-dir <dir>`; `fieldproof runs index` rebuilds the database from the directories |
