# fieldproof-example-vehicle

The repository of a fictional field team, onboarded on
[Fieldproof](https://github.com/Dimitrios-Kafetzis/Fieldproof) by following
[the onboarding guide](https://dimitrios-kafetzis.github.io/Fieldproof/reference/onboarding/) and
nothing else: no Fieldproof source was read or changed. The vehicle is a yard UGV whose
autonomy is a few hundred lines of rclpy, a waypoint navigator on wheel odometry that holds
position behind obstacles; in Fieldproof it runs unmodified in a container, fed by simulated
sensors through the bridge, and every scenario is a test with pass/fail assertions.

A copy of this repository lives in Fieldproof under `examples/fieldproof-example-vehicle/`,
where Fieldproof's own CI runs its scenario on every change to the engine; the published
repository is what a team clones and copies from.

## Layout

| Path | Contents |
| --- | --- |
| `robot.yaml` | the vehicle manifest: the model inherited from Fieldproof's reference UGV (`extends: fieldproof:ugv_ref`), the image built from `src/`, the field launch as the entrypoint (`use_sim_time:=true drivers:=false`), the driver-swap map, readiness (the navigator's node, action, `map -> base_link` transform and state topic), the budget (one core, 1 GiB) |
| `fieldproof.toml` | the workspace: runs under `runs/`, the published stack images at `main` (a team pins a release) |
| `scenarios/dock_to_yard.yaml` | the first scenario: the vehicle drives 4 m from the depot's loading dock through the roller door into the yard; ten assertions (the goal reached, in time, the lidar streaming, no contact, the speed limit, a geofence, the odometry's latency, the budget, the mission time, the order of events) and four KPIs |
| `scenarios/obstacle_hold.yaml` | the same drive with a crate dropped in the path once the vehicle moves: the navigator holds, the crate is moved out of the lane, the drive resumes; a `sequence` tells the story, `min_distance` keeps the chassis off the crate |
| `src/example_ugv_bringup/` | the vehicle codebase (`ament_python`): `launch/bringup.launch.py`, the field launch (`drivers:=true` starts the hardware drivers of `launch/drivers.launch.py`; `namespace` makes it one of a fleet); `example_ugv_bringup/waypoint_navigator.py`, the stack: a `NavigateToPose` action server driving on `odom`, holding on `scan`, reporting on `mission/state`; `config/example_ugv.yaml`, its parameters; `package.xml`, the driver packages as conditional dependencies (`EXAMPLE_UGV_TARGET=field`) |
| `docker/` | the generated build context (`make dockerfile` refreshes it): `docker buildx build --build-context vehicle-source=. --load -f docker/Dockerfile docker` builds the image without the engine |
| `Makefile` | the commands below, wrapping the engine of a Fieldproof checkout next to this repository |
| `.github/workflows/scenario.yml` | the repository's own CI: Fieldproof checked out next to it, the published images pulled, the vehicle image built, the scenario run with software rendering, the run uploaded |

## Run it

On an Ubuntu 24.04 workstation with Docker and uv (`Fieldproof/tools/host-setup.sh` installs
them; nothing from ROS or Gazebo installs on the host):

```bash
git clone https://github.com/Dimitrios-Kafetzis/Fieldproof.git ../Fieldproof   # the engine and the stack
make assets        # the worlds' Gazebo Fuel models, fetched into the checkout (once)
make validate      # robot.yaml and the scenarios against the workspace
make image         # the vehicle image: nav2_msgs and tf2 from the pinned apt snapshots, the package built
make run           # fieldproof run scenarios/dock_to_yard.yaml
make run SCENARIO=scenarios/obstacle_hold.yaml
```

`make run` pulls nothing: pull the stack images first (`docker pull
ghcr.io/dimitrios-kafetzis/fieldproof-{base-ros2,sim,bridge}:main`, the tag of
`fieldproof.toml`), or build them in the checkout (`make -C ../Fieldproof images`) and run with
`FIELDPROOF_IMAGE_TAG=local`. `FIELDPROOF_ROOT=/path/to/Fieldproof` names a checkout elsewhere.
The run ends with `PASS`, one line per assertion, and `runs/<run_id>/` holds the report
(`report.md`, `report.json`, `junit.xml`), the containers' logs and the MCAP recording, which
opens in Foxglove Studio.

## What the stack does

`waypoint_navigator` offers the `navigate_to_pose` action (`nav2_msgs/action/NavigateToPose`,
the interface an operator's tablet speaks, and Fieldproof's `navigate_to_pose` event): a
proportional heading controller drives the differential base on the wheel odometry at up to
0.5 m/s, turning in place beyond a heading error of 0.5 rad and slowing within half a metre
of the goal; while the lidar sees a return closer than 0.7 m in the front sector it publishes
zero velocity and `mission/state: holding`, and resumes once the sector is clear beyond 0.9 m.
Its localisation is the identity `map -> odom` transform it broadcasts once: the map frame
starts where the vehicle started, which is also where Fieldproof's goals are expressed.

In the field, `bringup.launch.py` starts the hardware drivers first (`drivers.launch.py`:
ros2_control for the base, `robot_state_publisher`, the lidar, IMU, GNSS and camera drivers)
and the navigator reads their topics. In Fieldproof the same launch runs with
`drivers:=false` and the bridge publishes the same topics from the simulator; `robot.yaml`'s
driver-swap map says which driver each topic stands in for.

## Licence

Apache-2.0, as Fieldproof.
