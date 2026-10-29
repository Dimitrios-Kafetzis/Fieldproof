# Onboarding a vehicle

How a team plugs its own vehicle repository into Fieldproof without reading Fieldproof's source
(task P2-T08). The repository keeps its code, its launch files and its parameters as they are
deployed on the vehicle; it gains a manifest (`robot.yaml`), a workspace file
(`fieldproof.toml`) and scenarios, and runs them with the engine of a Fieldproof checkout
cloned next to it. The worked example is the repository
[fieldproof-example-vehicle](https://github.com/Dimitrios-Kafetzis/fieldproof-example-vehicle),
a copy of which lives in `examples/fieldproof-example-vehicle/` and runs in Fieldproof's CI.

The principle ([00-overview.md](../plan/00-overview.md)): the robot code never knows it is in
Fieldproof. Only the hardware driver nodes are left out, the bridge publishes their topics from
the simulator, and the clock is simulated. Perception, localisation, planning, control and
mission logic run exactly as they do on the vehicle.

## In five commands

On an Ubuntu 24.04 workstation with Docker and uv
([host.md](../setup/host.md): `tools/host-setup.sh` of the checkout installs them; nothing
from ROS or Gazebo installs on the host):

```bash
git clone https://github.com/Dimitrios-Kafetzis/Fieldproof.git Fieldproof
git -C Fieldproof checkout v0.20.0                       # a release; the images carry the same tag
uv run --project Fieldproof/engine fieldproof vehicle init my-rover   # the scaffold, next to the checkout
cd my-rover && make assets validate image                # the Fuel models, the checks, the vehicle image
make run                                                 # fieldproof run scenarios/my_rover_smoke.yaml
```

`fieldproof vehicle init` writes a repository that runs as it is: the manifest of a vehicle on
the reference UGV, a bringup package whose only node reports the bridge's topics and the clock,
a smoke scenario that measures it for twenty seconds, the generated build context, a Makefile
and a README with the next steps ([what init writes](#what-init-writes)). From there the team
replaces the package with its own code, section by section, as the steps below describe; a
repository that has its code already starts with `fieldproof vehicle init --no-package` and
points the manifest at it.

The published stack images (`base-ros2`, `sim`, `bridge`) are pulled from the registry at the
tag `fieldproof.toml` names, the release of the checkout:

```bash
for image in base-ros2 sim bridge; do docker pull ghcr.io/dimitrios-kafetzis/fieldproof-$image:v0.20.0; done
```

`make run` pulls nothing (a run never does); `FIELDPROOF_IMAGE_TAG=local` runs images built in
the checkout with `make images` instead.

## How it fits together

```text
~/work/
├── Fieldproof/                 the checkout: the engine (engine/), the worlds, models, assets
│   └── robots/fieldproof_robots/models/ugv_ref/   the reference vehicles
└── my-rover/                   the team's repository, a Fieldproof workspace of its own
    ├── fieldproof.toml         runs/, the image tag; the stack comes from the checkout
    ├── robot.yaml              extends: fieldproof:ugv_ref, plus the runtime section
    ├── scenarios/*.yaml        manifest: ../robot.yaml
    ├── src/<package>/          the vehicle codebase, built into the image
    └── runs/<run_id>/          reports, logs, recordings
```

- **The engine runs from the checkout** (`uv run --project ../Fieldproof/engine fieldproof`,
  what the scaffold's Makefile does): it is not published on PyPI, and the checkout is also
  where the worlds, models, Fuel assets and reference vehicles the simulator mounts live.
- **The repository is the workspace** ([scenarios.md](scenarios.md#the-workspace)): the engine
  finds its `fieldproof.toml` by walking up from the scenario, runs land under its `runs/`, and
  its manifests and scenarios resolve against it. The stack's paths (`[stack]`) resolve
  against the checkout the engine runs from, since the workspace holds no `sim/`; `[stack]
  root = "../Fieldproof"` or `FIELDPROOF_STACK=/path` names another checkout. The run record
  (`run.json`) carries both roots.
- **The manifest inherits the model by name**: `extends: fieldproof:ugv_ref` is the reference
  UGV of the checkout, resolved by the engine on the host and by the bridge inside its
  container (the reference models are mounted at `/fieldproof/robots` next to the workspace at
  `/workspace`), so the repository holds no path into the checkout. A vehicle with its own
  description writes a manifest with its own model sections and extends it by a relative path
  ([manifest.md](manifest.md#extends)).
- **The images follow the release**: `fieldproof.toml`'s `image_tag` is the tag the stack
  images are pulled at, and the base image a vehicle build starts from
  (`ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:<tag>`, unless `runtime.build.base` names
  another), so one checkout tag is the whole version of the platform a repository is tested
  against. Pin it; move it deliberately.

## Step 1: the manifest

`robot.yaml` ([manifest.md](manifest.md)) describes the vehicle for the whole stack. The model
sections come from the reference vehicle; the `runtime` section is the team's:

```yaml
apiVersion: fieldproof.dev/v0alpha1
kind: Robot
extends: fieldproof:ugv_ref
metadata: { name: my_rover, description: ... }
runtime:
  tier: T1
  image: fieldproof-vehicle-my_rover:local
  build:
    sources: [src]                 # and/or repos: [{name, url, version: <commit>}]
  entrypoint:
    launch: { package: my_rover_bringup, file: bringup.launch.py,
              args: { use_sim_time: true, drivers: false }, namespace_arg: namespace }
  params: [src/my_rover_bringup/config/my_rover.yaml]
  drivers: [...]                   # step 2
  ready: { timeout_s: 120, conditions: [...] }   # step 5
  resources: { cpus: 1.0, memory: 1g }           # step 6
```

- `build` says how the image is made ([manifest.md](manifest.md#runtimebuild)): local
  directories under `sources` (copied from the repository) and repositories under `repos`
  (cloned at pinned commits); rosdep resolves the packages' dependencies against a pinned
  `ros/rosdistro` commit and installs them from the base image's apt snapshots, so the same
  manifest builds the same image a year later. A dependency rosdep cannot resolve to apt (a
  pip key, a vendored driver) is vendored under `sources` or skipped (`rosdep_skip_keys`).
- `entrypoint` is the field launch with the two arguments the simulation changes:
  `use_sim_time:=true` and whatever turns the hardware drivers off (`drivers:=false` in the
  examples). Nothing else. `namespace_arg` names the launch argument that takes the vehicle's
  namespace in a fleet ([step 8](#step-8-fleets-and-namespaces)).
- `params` lists the parameter files the launch reads, checked to exist and recorded with every
  run, so a report says which configuration was tested.

`fieldproof manifest validate robot.yaml` (`make validate`) checks every section and prints the
interfaces no driver of the swap map provides; `fieldproof vehicle dockerfile robot.yaml --out
docker` shows the Dockerfile the build runs (the scaffold commits it under `docker/`, so the
image also builds without the engine).

## Step 2: the driver swap

On the vehicle, hardware driver nodes publish the sensors and take the actuator commands; in
Fieldproof the bridge publishes the same topics, types, frames and rates from the simulator
([bridge.md](bridge.md)). The swap is the vehicle's business and takes two things:

1. **The launch leaves the drivers out.** The examples keep them in a `drivers.launch.py` that
   `bringup.launch.py` includes when `drivers:=true` (the field default) and not otherwise. A
   launch that starts drivers unconditionally needs an argument first; a driver that is a
   lifecycle node or a composable container is the same, as long as nothing in the stack
   depends on the driver *process* rather than its topics.
2. **The package does not depend on them in the simulation build.** `package.xml` declares the
   driver packages under a condition (`<exec_depend condition="$MY_TARGET == field">`), so
   rosdep installs them for the vehicle and not for the image, which stays small and has no
   serial-port parameters to fail on.

`runtime.drivers` records the swap, one entry per driver, naming the topics of the model's
interface the bridge provides in its place:

```yaml
drivers:
  - name: base
    package: my_motor_controller
    executable: controller_node
    provides: [cmd_vel, odom, joint_states, tf]     # odom -> base_link comes from the bridge
  - name: description
    package: robot_state_publisher
    provides: [tf_static, robot_description]        # the bridge renders the model's xacro
  - name: lidar
    package: rplidar_ros
    provides: [scan]
```

`provides` names topics of the manifest (the model's interfaces, plus the bridge's own
`tf_static`, `robot_description` and `/clock`), each for one driver at most. The validation
lists the interfaces no driver provides, which is fine for a sensor the stack does not read,
and a reminder otherwise. What matters to the stack is that the topics it subscribes to are
the ones the model declares: the scaffold's manifest shows every interface of the reference
UGV and its topic names (`scan`, `imu/data`, `odom`, `camera/image`, `gps/fix`, `cmd_vel`); a
stack that reads `/lidar/points` remaps it in its launch, as it would for another lidar driver.

Everything the bridge publishes is relative to the vehicle's namespace, with the TF tree of
the vehicle on its own `tf` and `tf_static` topics ([step 8](#step-8-fleets-and-namespaces)).

## Step 3: QoS

ROS 2 topics only connect when the publisher's and the subscriber's QoS are compatible
(reliable publishers serve best-effort subscribers, not the reverse; a transient-local
publisher serves volatile subscribers, not the reverse). The bridge publishes every topic with
the QoS the manifest declares (`qos` on each interface: reliable and volatile with a depth of
10 by default; the reference UGV's camera topics reliable with a depth of 5, its TF with a
depth of 100), so a subscriber of the stack sees the profile the model documents, which the
manifest's author chose to match the field driver's. Checks:

- a **sensor subscriber with `qos_profile_sensor_data`** (best effort, depth 5) connects to any
  publisher, reliable or not: the usual choice for lidar, IMU and camera subscribers;
- a **reliable subscriber** (the default of `create_subscription(..., 10)`) does not connect to
  a best-effort publisher: a stack that subscribes reliably to a topic the manifest declares
  best effort sees nothing, which the bridge contract check (`fieldproof run
  --check-contracts`, [bridge.md](bridge.md#checking-a-running-stack)) and `ros2 topic info -v`
  inside the vehicle container both show;
- `tf_static` and `robot_description` are transient local (latched), as `robot_state_publisher`
  publishes them, so a node that starts after the bridge still gets them;
- a stack that changes a profile (a lidar driver configured reliable) changes the manifest's
  `qos` for that interface rather than the subscriber, so the model documents the field.

The vehicle's own **DDS configuration** goes with it: `runtime.dds_profile` names the XML
profile deployed on the vehicle (a Fast DDS, Cyclone DDS or Connext file), mounted into the
container and named to the RMW, so socket buffers, transports and discovery settings are the
field's ([manifest.md](manifest.md#runtime)). Every run has its own ROS domain and Gazebo
partition; `fieldproof run --discovery server` adds a Fast DDS discovery server for stacks that
cannot multicast ([scenarios.md](scenarios.md#the-workspace)). Over a radio link, QoS decides
what a lossy link does to a topic
([network.md](network.md#recommended-qos-for-topics-that-cross-a-radio-link)).

## Step 4: simulation time

The simulator publishes `/clock` through the bridge, and every node of the stack follows it
once it runs with `use_sim_time: true` ([ADR-0006](../adr/0006-sim-time-default-realtime-locked-mode.md),
[time.md](time.md)). The entrypoint sets the argument once, for the whole launch, and the launch
passes it to every node (the examples put `use_sim_time` in each node's parameters, launch
included files with it, and let `nav2_common.RewrittenYaml` or a `/**:` wildcard carry it into
parameter files). What to look for:

- a node that does not get the parameter runs on the wall clock: its timers run, its stamps
  disagree with everybody else's, TF lookups fail with *extrapolation into the future*, and a
  scenario's `message_latency` reports hours;
- a node that creates timers before the first `/clock` arrives waits (correctly) until it does;
  the orchestrator starts the vehicle after the bridge, so the clock is there first;
- the stack's own rates are measured in simulation time by the assertions (`topic_rate`), so a
  simulator running at half real time (software rendering on a small runner) does not fail a
  rate assertion; wall-clock budgets (`within_time`, `time.timeout_s`) are generous for that
  reason;
- in `time.mode: realtime` the engine holds the simulator at real time and reports the drift;
  in `lockstep` it steps the world only once every vehicle's clock agent has acknowledged the
  previous step and the timeline counts simulation seconds, which makes two runs comparable
  (`fieldproof compare`); neither needs anything from the stack beyond `use_sim_time`.

## Step 5: readiness

The orchestrator starts the scenario clock once every vehicle is ready, and a stack is ready
when the conditions of `runtime.ready` hold, evaluated inside the container by the image's
readiness probe ([manifest.md](manifest.md#the-readiness-probe)):

```yaml
ready:
  timeout_s: 120
  conditions:
    - lifecycle: controller_server           # a lifecycle node active
    - topic: map                             # a publisher of a topic
      type: nav_msgs/msg/OccupancyGrid
    - action: navigate_to_pose               # an action server
      type: nav2_msgs/action/NavigateToPose
    - tf: map                                # a transform chain
      child: base_link
    - node: waypoint_navigator               # a node in the graph
```

Write the conditions for what a goal needs, not for what starts first: a Nav2 stack needs its
servers *active*, the map published and the `map -> base_link` chain (SLAM's first transform),
since a goal sent before is aborted; the example's navigator needs its action server and its
`map -> odom` transform on top of the bridge's `odom -> base_link`. Names are relative, so one
manifest serves every vehicle of a fleet. `fieldproof vehicle wait robot.yaml` runs the probe
against a container started with `fieldproof vehicle run` and lists what is pending; the same
probe is the image's `HEALTHCHECK`, so `docker inspect` reports the container `healthy`. A
stack whose readiness needs the bridge (Nav2's costmaps block in their activation until
`odom -> base_link` exists) is only ready inside a run, never alone; that is expected.

## Step 6: budgets

`runtime.resources` is the container's cgroup budget (`cpus`, `memory`, `cpuset`, `pids`,
[manifest.md](manifest.md#runtime)), applied with every run, so the stack is tested at the
compute it gets on the vehicle rather than on the workstation. A scenario checks it with the
`resource_usage` assertion and measures it with the `resources` KPI
([assertions.md](assertions.md)); a run matrix sweeps it (`vehicles[ugv1].resources.cpus:
[1, 2]`, [matrix.md](matrix.md)) to find where the stack stops meeting its deadlines. Start with
the vehicle's computer: two cores and 2 GiB for the Nav2 example, one core and 1 GiB for the
example repository's navigator.

## Step 7: the first scenario

A scenario ([scenarios.md](scenarios.md)) names the world, the vehicle (its manifest and spawn
frame), a timeline and the assertions. The scaffold's smoke scenario holds the timeline with a
`wait` (a stack that runs on its own gives the timeline nothing to send, and a timeline with
nothing to do ends the run as soon as the vehicle is ready) while rate, latency, collision and
budget assertions measure the stack ([events.md](events.md#built-in-events)). The first real
scenario gives the stack a mission:

- a stack that offers Nav2's `NavigateToPose` action (Nav2 itself, or a navigator of its own,
  as the example repository's) takes a `navigate_to_pose` entry: the goal in the world, sent
  in the vehicle's `map` frame, which starts at its spawn pose;
- a stack that starts a mission on a parameter or a service takes a `param.set` entry, or an
  event plugin of a few lines for the service ([events.md](events.md#writing-a-plugin)), which
  the repository ships next to its scenarios;
- a stack that starts its mission on its own (from its launch) takes a `wait` of the mission's
  length.

Then the assertions that prove it: `reach_pose` on the ground truth, `within_time` on the entry,
`geofence`, `no_collision`, `topic_value` on the commands, `sequence` for the order of events,
`resource_usage` for the budget ([assertions.md](assertions.md#every-assertion)). The example
repository's `dock_to_yard.yaml` is a complete first scenario; its `obstacle_hold.yaml` adds a
world event and proves a documented behaviour (the navigator holds behind a crate and resumes
once it is moved aside).
From there: faults (`gps.deny`, `imu.bias`, `sensor.dropout`), radio links (`network`), the
two clock modes, a matrix over seeds and budgets, a field log replayed into the stack
([replay.md](replay.md)).

## Step 8: fleets and namespaces

A scenario runs several vehicles of one manifest under namespaces (`/ugv1`, `/ugv2`,
[scenarios.md](scenarios.md#fleets)), each with its own TF tree on `/<ns>/tf` and
`/<ns>/tf_static` with the usual frame names (Nav2's multi-robot convention). The stack needs
three things, all of which the field launch of a fleet-capable robot has: a launch argument
for the namespace (`namespace_arg` in the manifest; the orchestrator passes `namespace:=ugv1`),
every node started under it (`PushRosNamespace`), and `/tf` and `/tf_static` remapped to the
namespace's (`SetRemap`). Relative topic names in the code (`odom`, not `/odom`) and in the
readiness conditions do the rest. A stack that keeps one shared `/tf` tells its frames apart
with a frame prefix instead (`frame_prefix_arg`).

## Your own CI

The repository proves itself on every push the way Fieldproof's own scenario workflow does:
check out Fieldproof at the release next to the repository, pull the published images, fetch
the Fuel models, build the vehicle image, run the scenario with software rendering, upload the
run. The example repository's `.github/workflows/scenario.yml` is the template: a GitHub-hosted
`ubuntu-24.04` runner (two cores, no GPU) runs the example's scenario in about ten minutes,
most of it the image pulls and the vehicle image build; the JUnit document of every run
(`runs/<run_id>/junit.xml`) is what a test reporter reads. A heavier stack (Nav2 with SLAM)
takes a few minutes more to build and runs below real time on the runner, which the examples'
wall-clock budgets allow for; `tools/ci/scenario-run.sh` of the checkout adds the one-retry
flaky-run policy ([ci.md](../setup/ci.md#scenario-tests)).

## The worked example

[fieldproof-example-vehicle](https://github.com/Dimitrios-Kafetzis/fieldproof-example-vehicle)
is the repository of a fictional field team: a yard UGV whose autonomy is a waypoint navigator
of a few hundred lines of rclpy (a `NavigateToPose` action server driving on wheel odometry,
holding position behind obstacles, reporting on `mission/state`), its field launch with the
hardware drivers behind `drivers:=true`, its parameters, a manifest that extends the reference
UGV, two scenarios and its own CI workflow. It was onboarded by following this page: no
Fieldproof source was read or modified, and its copy under
`examples/fieldproof-example-vehicle/` is what Fieldproof's CI runs to keep it that way
(`tests/integration/test_onboarding_repo.py` copies it next to the checkout, builds its image and
runs `dock_to_yard.yaml` through its own Makefile; the Scenario tests workflow's `onboarding`
job does the same against the published images, from the published repository on demand).

| Scenario | Proves |
| --- | --- |
| `dock_to_yard.yaml` | the navigator takes Fieldproof's `navigate_to_pose` goal and drives 4 m from the dock through the roller door into the yard: the goal reached within 0.5 m, in time, the lidar streaming, no contact, the speed limit held, the drive inside a geofence, the odometry fresh, the budget met, the events in order |
| `obstacle_hold.yaml` | a crate dropped on the straight line once the vehicle moves: the navigator holds (zero velocity), keeps its distance from the crate, resumes when the crate is moved out of the lane eight seconds later and reaches the goal, a `sequence` over the events and the commands |

## What init writes

`fieldproof vehicle init DIR [--name N] [--model ugv_ref] [--package P] [--launch F]
[--no-package] [--force]` ([cli.md](cli.md)):

| File | Contents |
| --- | --- |
| `robot.yaml` | `extends: fieldproof:<model>`; `runtime`: T1, image `fieldproof-vehicle-<name>:local` built from `src`, the entrypoint launch with `use_sim_time:=true drivers:=false` and `namespace_arg`, the driver-swap map derived from the model (the base, the description, one driver per sensor, placeholder packages to replace), readiness on the status node's topic, a budget of one core and 1 GiB |
| `fieldproof.toml` | runs under `runs/`, the stack images at the engine's release tag (`v0.20.0`), the stack from the checkout |
| `scenarios/<name>_smoke.yaml` | the depot, the vehicle at `spawn_dock`, a `wait` of 20 s, rate assertions on the lidar and the odometry, the odometry's latency, `no_collision`, `resource_usage`; two KPIs; an MCAP recording |
| `src/<package>/` | an `ament_python` package: `launch/bringup.launch.py` (the field launch: `drivers`, `use_sim_time`, `namespace`, `params_file`), `launch/drivers.launch.py` (where the hardware drivers go), `<package>/status_node.py` (reports the odometry and lidar rates and the clock on `status`), `config/<package>.yaml`, `package.xml` with the conditional driver dependencies commented, `setup.py` |
| `docker/` | the generated build context (`Dockerfile`, `entrypoint.sh`, `ready.py`, the resolved `robot.yaml`) on the workspace's base image |
| `Makefile`, `README.md`, `.gitignore` | `make assets validate image run dockerfile`, the next steps, the ignored run directories |

`--no-package` writes everything but `src/<package>/` for a repository that has its code,
with a readiness condition on the bridge's `odom` to replace. Existing files are never
overwritten without `--force`.

## Limits

- The engine runs from a checkout (`uv run --project <checkout>/engine`); a packaged
  distribution is a later task.
- A manifest's `extends: fieldproof:<model>` names a reference vehicle of the checkout
  (`ugv_ref` until the UAV and USV models of Phases 4 and 5 arrive); a vehicle with its own
  description still writes its model sections itself ([robots.md](robots.md)).
- The published images must carry the release the checkout is at: a `fieldproof:` manifest
  needs a bridge image of v0.20.0 or later, and a feature of the engine needs the images of
  its release.
- Tier T1 only (the stack rebuilt for x86, [ADR-0007](../adr/0007-fidelity-tiers.md)); ARM64
  images and hardware in the loop are Phase 6.
