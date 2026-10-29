"""``fieldproof vehicle init``: the scaffold of a vehicle repository (task P2-T08).

:func:`scaffold` writes into a directory everything a vehicle repository needs to run its
first scenario on Fieldproof without reading Fieldproof's source (docs/reference/onboarding.md):

- ``robot.yaml``, a manifest that ``extends: fieldproof:<model>`` (the reference model of the
  stack, so the repository need not know where the Fieldproof checkout is) and adds a
  ``runtime`` section: the image built from ``src/``, the entrypoint launch with
  ``use_sim_time:=true drivers:=false``, the driver-swap map derived from the model's
  interfaces (one driver per sensor, the base, the description) with placeholder packages,
  readiness, a budget;
- ``fieldproof.toml``, the workspace: runs under ``runs/``, the published stack images at the
  engine's release tag;
- ``scenarios/<name>_smoke.yaml``, the first scenario: the vehicle in the depot, a ``wait`` so
  the stack is measured, rate, latency, collision and budget assertions;
- ``src/<package>/``, a bringup package (``ament_python``) with the field launch
  (``bringup.launch.py``: ``drivers.launch.py`` when ``drivers:=true``, the stack's nodes
  otherwise) and a status node that proves the stack runs on the bridge's topics and the
  simulation clock, to be replaced by the vehicle's own nodes (``--no-package`` for a
  repository that has them);
- ``docker/``, the generated build context (``fieldproof vehicle dockerfile``), so the image
  also builds without the engine;
- a ``Makefile`` wrapping the engine's commands, a ``README.md`` with the next steps and a
  ``.gitignore``.

Every file is a template rendered from the name, the package, the model's manifest and the
engine's version; nothing is written over an existing file unless ``force`` says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from fieldproof import __version__
from fieldproof.config import ConfigError, load_config
from fieldproof.manifest import BASE_IMAGE_NAME, Name, RobotManifest, load_manifest
from fieldproof.vehicle import VehicleError, load_vehicle, write_context
from fieldproof_bridge.manifest import MANIFEST_FILE, REFERENCE_SCHEME, ManifestError

DEFAULT_MODEL = "ugv_ref"
DEFAULT_LAUNCH = "bringup.launch.py"
STATUS_NODE = "status_node"
STATUS_TOPIC = "status"
CONTEXT_DIR = "docker"
SCENARIOS_DIR = "scenarios"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
DRIVER_PLACEHOLDER = "replace_with_the_field_driver"
# The budget the scaffold's manifest declares and the smoke scenario checks against (nine
# tenths of the memory, one and a half cores against a quota of one: the daemon's samples
# overshoot a quota by a few percent).
CPUS = 1.0
MEMORY_MB = 1024
MAX_MEMORY_MB = 900
MAX_CPU_PCT = 150


class ScaffoldError(ValueError):
    """The scaffold cannot be written as asked."""


@dataclass
class Scaffold:
    """What :func:`scaffold` wrote."""

    directory: Path
    name: str
    package: str
    files: list[Path] = field(default_factory=list)


def vehicle_name(directory: Path, name: str | None) -> str:
    """The vehicle's name: `name`, or the directory's basename made into one."""
    if name is None:
        name = re.sub(r"[^a-z0-9_]+", "_", directory.resolve().name.lower()).strip("_")
        if not name or not name[0].isalpha():
            name = f"vehicle_{name}" if name else "vehicle"
    if not NAME_PATTERN.match(name) or len(name) > 63:
        raise ScaffoldError(
            f"{name!r} is not a vehicle name (lower case letters, digits and underscores, "
            "starting with a letter)"
        )
    return name


def check_name(value: str, what: str) -> str:
    """A package or node name: the same rule as a vehicle name."""
    if not NAME_PATTERN.match(value):
        raise ScaffoldError(f"{what} {value!r}: lower case letters, digits and underscores only")
    return value


def image_tag() -> str:
    """The published stack images of the engine's release (`v0.19.0`)."""
    return f"v{__version__}"


@dataclass(frozen=True)
class DriverEntry:
    """One entry of the scaffold's driver-swap map."""

    name: str
    package: str
    provides: list[str]
    note: str
    executable: str | None = None


def driver_map(model: RobotManifest) -> list[DriverEntry]:
    """The driver-swap map of the model: the base, the description, one driver per sensor."""
    drivers: list[DriverEntry] = []
    if model.drive is not None:
        drivers.append(
            DriverEntry(
                "base",
                DRIVER_PLACEHOLDER,
                [t.ros_topic for t in model.drive.interfaces()],
                "the motor controller: the command in, odometry, joint states and "
                "odom -> base_link out",
            )
        )
    drivers.append(
        DriverEntry(
            "description",
            "robot_state_publisher",
            ["tf_static", "robot_description"],
            "the bridge publishes the same description from the model's xacro",
            executable="robot_state_publisher",
        )
    )
    drivers.extend(
        DriverEntry(
            sensor.name,
            DRIVER_PLACEHOLDER,
            [o.ros_topic for o in sensor.outputs],
            f"the {sensor.type} driver",
        )
        for sensor in model.sensors
    )
    return drivers


def yaml_list(values: list[str]) -> str:
    """A flow-style YAML list of plain scalars."""
    return "[" + ", ".join(values) + "]"


def render_manifest(
    name: str,
    model: RobotManifest,
    model_name: str,
    package: str,
    launch: str,
    *,
    with_package: bool,
) -> str:
    """The scaffold's `robot.yaml`."""
    drivers_text = ""
    for driver in driver_map(model):
        drivers_text += f"    - name: {driver.name}\n      package: {driver.package}\n"
        if driver.executable is not None:
            drivers_text += f"      executable: {driver.executable}\n"
        note = driver.note.replace('"', '\\"')
        drivers_text += f'      provides: {yaml_list(driver.provides)}\n      note: "{note}"\n'
    if with_package:
        ready = (
            "    conditions:\n"
            f"      # The status node of src/{package} publishes once it hears the bridge's\n"
            "      # odometry and lidar; add your own stack's conditions (lifecycle nodes,\n"
            "      # topics, services, actions, transforms) and drop this one when it goes.\n"
            f"      - topic: {STATUS_TOPIC}\n"
            "        type: std_msgs/msg/String\n"
        )
    else:
        ready = (
            "    conditions:\n"
            "      # Replace with your stack's conditions: `lifecycle: <node>`, `topic: <name>`\n"
            "      # (with `type`), `service`, `action`, `node`, `tf: <parent>` with `child`.\n"
            "      # `odom` is the bridge's, so this holds as soon as the bridge is up.\n"
            "      - topic: odom\n"
        )
    description = (model.metadata.description or model_name).rstrip(".")
    return f"""\
# Vehicle manifest of {name}, written by `fieldproof vehicle init` (Fieldproof {__version__}).
# Schema: docs/reference/manifest.md (`fieldproof manifest schema`); the walk-through:
# docs/reference/onboarding.md.
#
#   fieldproof manifest validate robot.yaml
#   fieldproof vehicle build robot.yaml          # make image
#   fieldproof run scenarios/{name}_smoke.yaml    # make run
apiVersion: fieldproof.dev/v0alpha1
kind: Robot
# The model (frames, drive, sensors, topics, rates, QoS) is the stack's reference {model_name}
# ({description}); `fieldproof:<model>` finds it in the Fieldproof checkout the engine runs
# from, wherever this repository is. A vehicle of your own description extends a manifest of
# your own instead (a relative path).
extends: {REFERENCE_SCHEME}{model_name}
metadata:
  name: {name}
  description: {name} on the reference {model_name}
runtime:
  tier: T1 # SIL: the stack rebuilt for x86 (ADR-0007)
  image: fieldproof-vehicle-{name}:local
  build:
    # The vehicle's sources: local directories copied into the image's `src/` (relative to
    # this file), and/or repositories cloned at pinned commits:
    #   repos:
    #     - name: {package}
    #       url: https://example.com/{package}.git
    #       version: <40-digit commit SHA>
    sources: [src]
    # rosdep resolves the packages' dependencies from the pinned ros/rosdistro commit and the
    # base image's apt snapshots; `apt: [...]` adds run-time packages, `rosdep_skip_keys` and
    # `colcon_args` tune the build. The base image follows fieldproof.toml's image tag.
  entrypoint:
    # The field bringup, unchanged: the simulation only sets the clock and leaves the hardware
    # drivers out (the bridge provides their topics). In a fleet the orchestrator appends
    # `namespace:=<vehicle namespace>` through `namespace_arg`.
    launch:
      package: {package}
      file: {launch}
      args:
        use_sim_time: true
        drivers: false
      namespace_arg: namespace
  params: []
  # Parameter files the launch reads unchanged from the field, relative to this file:
  #   params: [src/{package}/config/{package}.yaml]
  drivers:
    # The driver-swap map: each hardware driver `drivers.launch.py` starts in the field and the
    # topics of the model's interface the bridge provides instead (docs/reference/bridge.md).
    # Name the real driver packages; a sensor the stack does not use may stay unswapped
    # (`fieldproof manifest validate` lists those).
{drivers_text}  ready:
    # When the stack is up: the orchestrator starts the scenario clock once every condition
    # holds (the readiness probe evaluates them inside the container; relative names resolve
    # under the vehicle's namespace in a fleet).
    timeout_s: 120
{ready}  resources:
    # cgroup limits of the container (`--cpus`, `--memory`; `cpuset` and `pids` too); the
    # smoke scenario checks the stack fits. A scenario may override them per vehicle.
    cpus: {CPUS:g}
    memory: {MEMORY_MB}m
  # The vehicle's own DDS profile, as deployed, mounted into the container and named to the
  # RMW (docs/reference/manifest.md#runtime):
  #   dds_profile: src/{package}/config/fastdds.xml
"""


def render_config(name: str) -> str:
    """The scaffold's `fieldproof.toml`."""
    return f"""\
# Fieldproof workspace of {name}, written by `fieldproof vehicle init` (Fieldproof {__version__};
# docs/reference/scenarios.md#the-workspace). This repository is the workspace: runs land under
# `runs/`, scenarios and manifests resolve against it. The simulator's worlds, models, Fuel
# assets and reference vehicles come from the Fieldproof checkout the engine runs from
# (`uv run --project <checkout>/engine fieldproof ...`); `[stack] root = "../Fieldproof"` or
# FIELDPROOF_STACK names another checkout.
[runs]
directory = "runs"

[docker]
# The published stack images of the Fieldproof release this file was written with; keep the
# tag at the checkout's release (`git -C <checkout> describe --tags`). FIELDPROOF_IMAGE_TAG
# overrides it (`local` for images built with `make images` in the checkout).
image_tag = "{image_tag()}"
render = "auto" # gpu when the host has a /dev/dri render node, software otherwise
ros_domain_id = "auto" # a domain per run, so runs on one host never share one
"""


def render_scenario(name: str, with_package: bool) -> str:
    """The scaffold's smoke scenario."""
    status = f"    - /{STATUS_TOPIC}\n" if with_package else ""
    return f"""\
# The first scenario of {name}, written by `fieldproof vehicle init`: the vehicle spawns at
# the depot's loading dock, the stack comes up on the bridge's topics and the simulation clock
# and runs on its own for twenty seconds while the assertions measure it. Schema:
# docs/reference/scenarios.md (`fieldproof schema`); assertions: docs/reference/assertions.md.
#
#   fieldproof validate scenarios/{name}_smoke.yaml
#   fieldproof run scenarios/{name}_smoke.yaml       # make run
apiVersion: fieldproof.dev/v0alpha1
kind: Scenario
metadata:
  name: {name}_smoke
  description: {name} comes up in the depot and its sensors stream for twenty seconds
  tags: [smoke, {name}]
world:
  file: depot.sdf
vehicles:
  - name: {name}
    manifest: ../robot.yaml
    spawn:
      frame: spawn_dock # (10, 3), facing the roller door
time:
  mode: sim
  timeout_s: 5m # wall-clock budget once the vehicle is ready
timeline:
  # A stack that runs on its own gets nothing from the timeline, and a timeline with nothing
  # to do ends the run as soon as the vehicle is ready: `wait` holds it so the assertions and
  # the KPIs measure the stack. Replace it with the mission (`navigate_to_pose` for a Nav2
  # stack, `param.set` for a node that takes a command, a plugin for anything else).
  - at: 0s
    name: settle
    action: wait
    duration_s: 20s
assertions:
  # The lidar keeps publishing (simulation time; 10 Hz nominal, a few Hz with software
  # rendering on a small CI runner).
  - name: lidar_streams
    type: topic_rate
    topic: scan
    vehicle: {name}
    min_hz: 2.0
    window_s: 5s
    after_s: 5s
  # The wheel odometry keeps publishing at (nearly) its declared rate.
  - name: odom_streams
    type: topic_rate
    topic: odom
    vehicle: {name}
    min_hz: 20.0
    window_s: 5s
    after_s: 5s
  # The odometry arrives fresh: the 95th percentile of its message age under a quarter of a
  # second of simulation time.
  - name: odom_fresh
    type: message_latency
    topic: odom
    vehicle: {name}
    max_ms: 250
    after_s: 2s
  # The chassis never touches the world (the model's ground-truth contact sensor).
  - name: no_collision
    type: no_collision
    vehicle: {name}
  # The stack fits its budget (robot.yaml, runtime.resources).
  - name: stack_fits
    type: resource_usage
    vehicle: {name}
    max_memory_mb: {MAX_MEMORY_MB}
    max_cpu_pct: {MAX_CPU_PCT}
    window_s: 10s
kpis: # measured, not judged: in run.json, the report and /fieldproof/kpis
  - {{ name: scan_age, type: latency, topic: scan, vehicle: {name} }}
  - {{ name: stack, type: resources, vehicle: {name} }}
outputs:
  logs: true
  record: true # recording/*.mcap in the run directory (opens in Foxglove Studio)
  topics: # the allow-list; the /fieldproof/* side channels are always recorded
    - /clock
    - /tf
    - /tf_static
    - /odom
    - /scan
{status}    - /rosout
"""


HELP_AWK = (
    '@awk \'BEGIN { FS = ":.*## "; printf "Targets:\\n" } '
    '/^[a-zA-Z_-]+:.*## / { printf "  \\033[1m%-12s\\033[0m %s\\n", $$1, $$2 }\' '
    "$(MAKEFILE_LIST)"
)


def render_makefile(name: str) -> str:
    """The scaffold's Makefile."""
    lines = [
        f"# Fieldproof targets of {name}, written by `fieldproof vehicle init`. Run `make` for the",
        "# list. The engine runs from a Fieldproof checkout (FIELDPROOF_ROOT, ../Fieldproof by",
        "# default), which also supplies the simulator's worlds, models, Fuel assets and reference",
        "# vehicles; the stack images come from the registry at the tag of fieldproof.toml",
        "# (FIELDPROOF_IMAGE_TAG overrides it).",
        "SHELL := /usr/bin/env bash",
        ".SHELLFLAGS := -euo pipefail -c",
        ".DEFAULT_GOAL := help",
        "",
        "FIELDPROOF_ROOT ?= ../Fieldproof",
        "UV ?= uv",
        "FIELDPROOF := $(UV) run --locked --project $(FIELDPROOF_ROOT)/engine fieldproof",
        f"SCENARIO ?= {SCENARIOS_DIR}/{name}_smoke.yaml",
        "RUN_FLAGS ?=",
        "BUILD_FLAGS ?=",
        "",
        ".PHONY: help check assets validate image run dockerfile clean",
        "",
        "help: ## Show this help",
        f"\t{HELP_AWK}",
        "",
        "check: ## The Fieldproof checkout and the engine are where FIELDPROOF_ROOT says",
        '\t@test -f "$(FIELDPROOF_ROOT)/engine/pyproject.toml" || { echo "no Fieldproof checkout'
        ' at $(FIELDPROOF_ROOT) (FIELDPROOF_ROOT=...)" >&2; exit 1; }',
        "\t$(FIELDPROOF) --version",
        "",
        "assets: check ## Fetch the Gazebo Fuel models the worlds use into the checkout (cached)",
        "\t$(MAKE) -C $(FIELDPROOF_ROOT) assets",
        "",
        "validate: check ## Validate the manifest and the scenario against the workspace",
        "\t$(FIELDPROOF) manifest validate robot.yaml",
        "\t$(FIELDPROOF) validate $(SCENARIO)",
        "",
        "image: check ## Build the vehicle image from robot.yaml (BUILD_FLAGS=--no-cache)",
        "\t$(FIELDPROOF) vehicle build robot.yaml $(BUILD_FLAGS)",
        "",
        f"run: check ## Run a scenario (SCENARIO={SCENARIOS_DIR}/{name}_smoke.yaml, "
        "RUN_FLAGS=--keep|--video|--render software)",
        "\t$(FIELDPROOF) run $(SCENARIO) $(RUN_FLAGS)",
        "",
        f"dockerfile: check ## Refresh {CONTEXT_DIR}/, the generated build context (a build "
        "without the engine)",
        f"\t$(FIELDPROOF) vehicle dockerfile robot.yaml --out {CONTEXT_DIR}",
        "",
        "clean: ## Remove the runs",
        "\trm -rf runs",
    ]
    return "\n".join(lines) + "\n"


def render_readme(name: str, package: str, with_package: bool) -> str:
    """The scaffold's README."""
    if with_package:
        package_text = (
            f"- `src/{package}/`: the bringup package (`ament_python`): "
            "`launch/bringup.launch.py`, the field launch (`drivers:=true` starts the hardware\n"
            "  drivers of `launch/drivers.launch.py`, `drivers:=false` leaves them to the bridge;\n"
            f"  `use_sim_time`, `namespace`), and `{package}/{STATUS_NODE}.py`, a node that\n"
            f"  reports the bridge's odometry and lidar rates and the clock on `{STATUS_TOPIC}`\n"
            "  (the readiness condition of `robot.yaml`). Replace it with the vehicle's own nodes\n"
            "  and the readiness with theirs.\n"
        )
    else:
        package_text = (
            f"- `src/`: put the vehicle's packages here (the manifest builds `{package}` from\n"
            "  it), or list repositories under `runtime.build.repos` in `robot.yaml`.\n"
        )
    docs = "https://dimitrios-kafetzis.github.io/Fieldproof/reference/onboarding/"
    return f"""\
# {name}

A vehicle repository onboarded on [Fieldproof](https://github.com/Dimitrios-Kafetzis/Fieldproof),
written by `fieldproof vehicle init` (Fieldproof {__version__}). The stack runs unmodified in a
container, fed by simulated sensors through the bridge, and every scenario is a test with
pass/fail assertions. The walk-through is [the onboarding guide]({docs}).

## Layout

- `robot.yaml`: the vehicle manifest: the model (inherited from the reference `ugv_ref`), the
  image built from `src/`, the entrypoint launch, the driver-swap map, readiness, the budget.
- `fieldproof.toml`: the workspace (runs, the stack images' tag).
- `scenarios/{name}_smoke.yaml`: the first scenario.
{package_text}- `docker/`: the generated build context (`make dockerfile` refreshes it);
  `docker buildx build --build-context vehicle-source=. --load -f docker/Dockerfile docker`
  builds the image without the engine.
- `Makefile`: the commands below.

## Run it

```bash
git clone https://github.com/Dimitrios-Kafetzis/Fieldproof.git ../Fieldproof  # FIELDPROOF_ROOT
git -C ../Fieldproof checkout {image_tag()}        # the release fieldproof.toml names
../Fieldproof/tools/host-setup.sh                 # once: Docker, uv (asks for sudo)
make assets                                       # the worlds' Fuel models, into the checkout
make validate                                     # the manifest and the scenario
make image                                        # the vehicle image (minutes the first time)
make run                                          # fieldproof run scenarios/{name}_smoke.yaml
```

The run ends with `PASS`, one line per assertion, and `runs/<run_id>/` holds the report
(`report.md`, `report.json`, `junit.xml`), the containers' logs and the MCAP recording.
`FIELDPROOF_IMAGE_TAG=local make run` uses images built in the checkout with `make images`.

## Next

1. Name the real driver packages in `robot.yaml` (`runtime.drivers`) and put the drivers in
   `launch/drivers.launch.py`: in the field `drivers:=true` starts them, in Fieldproof the
   bridge provides their topics.
2. Replace the status node with the stack and its readiness conditions (`runtime.ready`).
3. Give the scenario a mission (a `navigate_to_pose` for a Nav2 stack, a `param.set`, a plugin)
   and the assertions that prove it (`reach_pose`, `within_time`, `geofence`, ...).
4. Add the repository's own CI: a job that clones the Fieldproof release next to the checkout,
   pulls the images and runs `make image run` (the example repository shows one).
"""


def render_gitignore() -> str:
    """The scaffold's .gitignore."""
    return (
        "# Fieldproof runs (reports, logs, recordings)\nruns/\n*.mcap\n"
        "# ROS build trees\nbuild/\ninstall/\nlog/\n__pycache__/\n"
    )


def render_package_xml(package: str, name: str) -> str:
    """The bringup package's package.xml."""
    return f"""\
<?xml version="1.0"?>
<?xml-model href="http://download.ros.org/schema/package_format3.xsd" schematypens="http://www.w3.org/2001/XMLSchema"?>
<package format="3">
  <name>{package}</name>
  <version>0.1.0</version>
  <description>Bringup of {name}: the field launch, its parameters and a status node</description>
  <maintainer email="maintainer@example.com">{name} maintainers</maintainer>
  <license>Apache-2.0</license>

  <exec_depend>launch</exec_depend>
  <exec_depend>launch_ros</exec_depend>
  <exec_depend>nav_msgs</exec_depend>
  <exec_depend>rclpy</exec_depend>
  <exec_depend>sensor_msgs</exec_depend>
  <exec_depend>std_msgs</exec_depend>

  <!-- Hardware drivers: only when the package is built for the vehicle. In Fieldproof the
       bridge provides their topics (robot.yaml, runtime.drivers), so a simulation build
       neither installs nor starts them; drivers.launch.py starts them in the field.
  <exec_depend condition="$VEHICLE_TARGET == field">robot_state_publisher</exec_depend>
  <exec_depend condition="$VEHICLE_TARGET == field">rplidar_ros</exec_depend>
  -->

  <test_depend>ament_copyright</test_depend>
  <test_depend>ament_flake8</test_depend>
  <test_depend>ament_pep257</test_depend>
  <test_depend>python3-pytest</test_depend>

  <export>
    <build_type>ament_python</build_type>
  </export>
</package>
"""


def render_setup_py(package: str, name: str) -> str:
    """The bringup package's setup.py."""
    return f"""\
from pathlib import Path

from setuptools import find_packages, setup

package_name = "{package}"
share = Path("share") / package_name

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{{package_name}}"]),
        (str(share), ["package.xml"]),
        (str(share / "launch"), [str(p) for p in Path("launch").glob("*.launch.py")]),
        (str(share / "config"), [str(p) for p in Path("config").glob("*.yaml")]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="{name} maintainers",
    maintainer_email="maintainer@example.com",
    description="Bringup of {name}: the field launch, its parameters and a status node",
    license="Apache-2.0",
    entry_points={{
        "console_scripts": [
            "{STATUS_NODE} = {package}.{STATUS_NODE}:main",
        ],
    }},
)
"""


def render_setup_cfg(package: str) -> str:
    """The bringup package's setup.cfg."""
    return (
        f"[develop]\nscript_dir=$base/lib/{package}\n"
        f"[install]\ninstall_scripts=$base/lib/{package}\n"
    )


def render_bringup_launch(package: str, name: str) -> str:
    """The field bringup launch."""
    return f'''\
"""Bring up {name}: the hardware drivers and the stack, as on the vehicle.

    ros2 launch {package} bringup.launch.py [use_sim_time:=false] [drivers:=true] \\\\
        [namespace:=] [params_file:=...]

The field defaults are `use_sim_time:=false drivers:=true`. In Fieldproof the manifest's
entrypoint runs this file with `use_sim_time:=true drivers:=false`: the hardware drivers of
drivers.launch.py are not started because the bridge provides their topics (robot.yaml,
`runtime.drivers`), and every node follows the simulation clock. Nothing else differs.

`namespace` makes the vehicle one of a fleet: every node runs under it, and `/tf` and
`/tf_static` are remapped to the namespace's (one TF tree per vehicle, the usual frame names).
The orchestrator passes the argument from the scenario (`runtime.entrypoint.launch.namespace_arg`).
"""

from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node, PushRosNamespace, SetRemap
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription


def generate_launch_description() -> LaunchDescription:
    share = FindPackageShare("{package}")
    namespace = LaunchConfiguration("namespace")
    use_sim_time = LaunchConfiguration("use_sim_time")
    arguments = [
        DeclareLaunchArgument("namespace", default_value="", description="top-level namespace"),
        DeclareLaunchArgument(
            "use_sim_time",
            default_value="false",
            choices=["true", "false"],
            description="follow /clock instead of the wall clock",
        ),
        DeclareLaunchArgument(
            "drivers",
            default_value="true",
            choices=["true", "false"],
            description="start the hardware drivers (false when a bridge provides their topics)",
        ),
        DeclareLaunchArgument(
            "params_file",
            default_value=PathJoinSubstitution([share, "config", "{package}.yaml"]),
            description="the stack's parameters",
        ),
    ]
    drivers = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(PathJoinSubstitution([share, "launch", "drivers.launch.py"])),
        condition=IfCondition(LaunchConfiguration("drivers")),
        launch_arguments={{"namespace": namespace, "use_sim_time": use_sim_time}}.items(),
    )
    stack = GroupAction(
        [
            PushRosNamespace(namespace),
            SetRemap("/tf", "tf"),
            SetRemap("/tf_static", "tf_static"),
            # The stack: replace the status node with the vehicle's own nodes.
            Node(
                package="{package}",
                executable="{STATUS_NODE}",
                name="{STATUS_NODE}",
                output="screen",
                parameters=[LaunchConfiguration("params_file"), {{"use_sim_time": use_sim_time}}],
            ),
        ]
    )
    return LaunchDescription([*arguments, drivers, stack])
'''


def render_drivers_launch(package: str, name: str) -> str:
    """The hardware drivers launch (the field side of the driver-swap map)."""
    return f'''\
"""The hardware drivers of {name} (the field side of the driver-swap map).

    ros2 launch {package} drivers.launch.py [namespace:=] [use_sim_time:=false]

Started by bringup.launch.py when `drivers:=true` (the field default). Each node here publishes
one of the interfaces the vehicle manifest declares and that, in Fieldproof, the bridge provides
from the simulated sensors instead (robot.yaml, `runtime.drivers`): the swap is this file not
being included; nothing in it changes. Keep the driver packages conditional dependencies of
package.xml (`condition="$VEHICLE_TARGET == field"`), so the simulation image carries neither
them nor their serial-port parameters.
"""

from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration

from launch import LaunchDescription


def generate_launch_description() -> LaunchDescription:
    namespace = LaunchConfiguration("namespace")
    use_sim_time = LaunchConfiguration("use_sim_time")
    arguments = [
        DeclareLaunchArgument("namespace", default_value="", description="top-level namespace"),
        DeclareLaunchArgument("use_sim_time", default_value="false", description="clock"),
    ]
    common = {{"use_sim_time": use_sim_time}}
    nodes = [
        # One Node(...) per hardware driver of robot.yaml's `runtime.drivers`, for example the
        # base (ros2_control with a diff_drive_controller publishing `odom`, `joint_states`
        # and odom -> base_link), robot_state_publisher (`tf_static`, `robot_description`),
        # the lidar (`scan`), the IMU (`imu/data`), the camera (`camera/*`), the GNSS
        # (`gps/fix`):
        #   Node(package="rplidar_ros", executable="rplidar_composition", name="lidar",
        #        namespace=namespace, output="screen", parameters=[common]),
    ]
    _ = (namespace, common)
    return LaunchDescription([*arguments, *nodes])
'''


def render_status_node(name: str) -> str:
    """The status node: the bridge's topics and the clock, reported once a second."""
    return f'''\
#!/usr/bin/env python3
"""Status node of {name} (written by fieldproof vehicle init): the stack's first node.

Subscribes to the bridge's odometry and lidar, counts their messages and reports their rates
and the clock on `{STATUS_TOPIC}` (`std_msgs/msg/String`) once a second: proof that the stack
runs on the swapped drivers' topics and follows the simulation clock (`use_sim_time`), and the
readiness condition of robot.yaml. Replace it with the vehicle's own nodes.
"""

from __future__ import annotations

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String


class StatusNode(Node):  # type: ignore[misc]
    """Count the odometry and lidar messages, report their rates and the clock."""

    def __init__(self) -> None:
        super().__init__("{STATUS_NODE}")
        period = float(self.declare_parameter("report_period_s", 1.0).value)
        self.counts = {{"odom": 0, "scan": 0}}
        self.reported = {{"odom": 0, "scan": 0}}
        self.heard: set[str] = set()
        # The odometry is reliable (a driver's default); a lidar driver publishes best effort,
        # and so does the bridge when the manifest says so: a sensor-data subscriber matches
        # either.
        self.create_subscription(Odometry, "odom", lambda m: self.on_message("odom"), 10)
        self.create_subscription(
            LaserScan, "scan", lambda m: self.on_message("scan"), qos_profile_sensor_data
        )
        self.status = self.create_publisher(String, "{STATUS_TOPIC}", 10)
        self.period = period
        self.create_timer(period, self.report)
        self.get_logger().info("waiting for the odometry and the lidar")

    def on_message(self, topic: str) -> None:
        """One message more on the topic."""
        self.counts[topic] += 1
        if topic not in self.heard:
            self.heard.add(topic)
            self.get_logger().info(f"first message on {{topic}}")

    def report(self) -> None:
        """Publish the rates and the clock once both topics have been heard."""
        if len(self.heard) < len(self.counts):
            return
        rates = {{t: (self.counts[t] - self.reported[t]) / self.period for t in self.counts}}
        self.reported = dict(self.counts)
        clock = self.get_clock().now().nanoseconds / 1e9
        rates_text = ", ".join(f"{{t}} {{r:.1f}} Hz" for t, r in rates.items())
        self.status.publish(String(data=f"{{rates_text}}, clock {{clock:.1f}} s"))


def main() -> None:
    """Spin until shut down."""
    rclpy.init()
    node = StatusNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
'''


def render_params(package: str) -> str:
    """The stack's parameter file."""
    return f"""\
# Parameters of the {package} stack, read by bringup.launch.py unchanged from the field; list
# this file under `runtime.params` in robot.yaml once the stack reads it.
/**:
  ros__parameters:
    report_period_s: 1.0
"""


def scaffold(
    directory: Path | str,
    *,
    name: str | None = None,
    model: str = DEFAULT_MODEL,
    package: str | None = None,
    launch: str = DEFAULT_LAUNCH,
    robots: Path | str,
    base_image: str | None = None,
    with_package: bool = True,
    force: bool = False,
) -> Scaffold:
    """Write the vehicle repository scaffold into `directory` and return what was written.

    `robots` is the reference models' directory of the stack (the model's manifest is read
    from it); `base_image` the base image the generated Dockerfile defaults to (without it,
    the scaffolded workspace's own: the `base-ros2` image at the tag its `fieldproof.toml`
    names, `FIELDPROOF_IMAGE_TAG` included).
    """
    target = Path(directory).resolve()
    vehicle = vehicle_name(target, name)
    package_name = check_name(package or f"{vehicle}_bringup", "package")
    check_name(model, "model")
    model_path = Path(robots).resolve() / model / MANIFEST_FILE
    if not model_path.is_file():
        raise ScaffoldError(f"no reference model {model!r} under {Path(robots).resolve()}")
    try:
        reference = load_manifest(model_path)
    except ManifestError as error:
        raise ScaffoldError(f"{model_path}: {error}") from error
    files: dict[str, str] = {
        "robot.yaml": render_manifest(
            vehicle, reference, model, package_name, launch, with_package=with_package
        ),
        "fieldproof.toml": render_config(vehicle),
        f"{SCENARIOS_DIR}/{vehicle}_smoke.yaml": render_scenario(vehicle, with_package),
        "Makefile": render_makefile(vehicle),
        "README.md": render_readme(vehicle, package_name, with_package),
        ".gitignore": render_gitignore(),
    }
    if with_package:
        src = f"src/{package_name}"
        files.update(
            {
                f"{src}/package.xml": render_package_xml(package_name, vehicle),
                f"{src}/setup.py": render_setup_py(package_name, vehicle),
                f"{src}/setup.cfg": render_setup_cfg(package_name),
                f"{src}/resource/{package_name}": "",
                f"{src}/{package_name}/__init__.py": "",
                f"{src}/{package_name}/{STATUS_NODE}.py": render_status_node(vehicle),
                f"{src}/launch/{launch}": render_bringup_launch(package_name, vehicle),
                f"{src}/launch/drivers.launch.py": render_drivers_launch(package_name, vehicle),
                f"{src}/config/{package_name}.yaml": render_params(package_name),
            }
        )
    existing = [rel for rel in files if (target / rel).exists()]
    if existing and not force:
        raise ScaffoldError(
            f"{target}: {', '.join(existing)} exist(s) already (--force overwrites)"
        )
    written: list[Path] = []
    for rel, text in files.items():
        path = target / rel
        path.mkdir(parents=True, exist_ok=True) if rel.endswith("/") else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)
    (target / "src").mkdir(exist_ok=True)
    # The generated build context: the manifest as written, resolved against the stack, on
    # the base image of the workspace just written.
    try:
        if base_image is None:
            base_image = load_config(start=target).image(BASE_IMAGE_NAME)
        manifest = load_vehicle(target / "robot.yaml", robots=robots)
        context = write_context(manifest, target / CONTEXT_DIR, base_image=base_image)
    except (ManifestError, VehicleError, ConfigError) as error:
        raise ScaffoldError(f"the scaffold's manifest does not generate: {error}") from error
    written.extend(context.files)
    return Scaffold(target, vehicle, package_name, written)


__all__ = ["BASE_IMAGE_NAME", "Name", "Scaffold", "ScaffoldError", "scaffold"]
