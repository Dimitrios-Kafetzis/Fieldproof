"""Start the bridge of one vehicle: ros_gz bridge instances, adapters and TF (task P1-T03).

    ros2 launch fieldproof_bridge bridge.launch.py manifest:=/path/to/robot.yaml \\
        [namespace:=ugv1] [frame_prefix:=] [clock:=true] [robot_state_publisher:=true] \\
        [without:=gps] [use_sim_time:=true] [bundle_out:=/tmp/fieldproof-bridge]
    ros2 launch fieldproof_bridge bridge.launch.py bundle:=/path/to/<robot>_bridge

With `manifest`, the bundle is generated at launch time into a temporary directory, or into
`bundle_out` when given (the same output as `fieldproof bridge gen`, where a contract check can
find it); with `bundle`, a directory written by the generator is used as it is and the other
arguments are ignored (they are recorded in it). The nodes started are those of the bundle's
bridge.yaml: one `parameter_bridge` per configuration file (QoS overrides and
`override_frame_id` from params.yaml), one adapter node per hidden bridge topic, and
`robot_state_publisher` on the manifest's xacro rendered with the bundle's arguments, its `/tf`
and `/tf_static` remapped to the vehicle's TF topics (under the namespace when the manifest's
`tf` interface is relative, task P2-T01).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import Command, FindExecutable, LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue

from fieldproof_bridge.generator import generate
from launch import LaunchDescription

ADAPTERS = ("imu_adapter", "navsat_adapter", "throttle")


def xacro_argument(value: Any) -> str:
    """xacro reads booleans as ``true``/``false``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def launch_setup(context: Any) -> list[Any]:
    """Resolve the arguments and build the nodes of the bundle."""
    bundle = LaunchConfiguration("bundle").perform(context)
    manifest = LaunchConfiguration("manifest").perform(context)
    use_sim_time = LaunchConfiguration("use_sim_time")
    if bundle:
        bundle_dir = Path(bundle).resolve()
    elif manifest:
        bundle_out = LaunchConfiguration("bundle_out").perform(context)
        bundle_dir = (
            Path(bundle_out).resolve()
            if bundle_out
            else Path(tempfile.mkdtemp(prefix="fieldproof_bridge_"))
        )
        without = LaunchConfiguration("without").perform(context)
        generate(
            manifest,
            bundle_dir,
            namespace=LaunchConfiguration("namespace").perform(context),
            frame_prefix=LaunchConfiguration("frame_prefix").perform(context),
            clock=LaunchConfiguration("clock").perform(context).lower() == "true",
            robot_state_publisher=(
                LaunchConfiguration("robot_state_publisher").perform(context).lower() == "true"
            ),
            without=tuple(s for s in without.split(",") if s.strip()),
        )
    else:
        msg = "bridge.launch.py needs manifest:=<robot.yaml> or bundle:=<directory>"
        raise ValueError(msg)

    data = yaml.safe_load((bundle_dir / "bridge.yaml").read_text())
    namespace = str(data.get("namespace") or "") or "/"
    params_file = str(bundle_dir / "params.yaml")
    tf_topics = data.get("tf") or {"tf": "/tf", "tf_static": "/tf_static"}
    actions: list[Any] = []
    for node in data["nodes"]:
        name, kind = str(node["name"]), str(node["kind"])
        if kind == "ros_gz_bridge":
            actions.append(
                Node(
                    package="ros_gz_bridge",
                    executable="parameter_bridge",
                    name=name,
                    namespace=namespace,
                    output="screen",
                    parameters=[
                        params_file,
                        {
                            "config_file": str(bundle_dir / str(node["config"])),
                            "use_sim_time": use_sim_time,
                        },
                    ],
                )
            )
        elif kind in ADAPTERS:
            actions.append(
                Node(
                    package="fieldproof_bridge",
                    executable=kind,
                    name=name,
                    namespace=namespace,
                    output="screen",
                    parameters=[params_file, {"use_sim_time": use_sim_time}],
                )
            )
        elif kind == "robot_state_publisher":
            description = data["description"]
            command: list[Any] = [FindExecutable(name="xacro"), " ", str(description["xacro"])]
            for key, value in (description.get("args") or {}).items():
                command += [f" {key}:=", xacro_argument(value)]
            actions.append(
                Node(
                    package="robot_state_publisher",
                    executable="robot_state_publisher",
                    name=name,
                    namespace=namespace,
                    output="screen",
                    remappings=[
                        ("/tf", str(tf_topics["tf"])),
                        ("/tf_static", str(tf_topics["tf_static"])),
                    ],
                    parameters=[
                        {
                            "robot_description": ParameterValue(Command(command), value_type=str),
                            "use_sim_time": use_sim_time,
                        }
                    ],
                )
            )
        else:
            msg = f"{bundle_dir / 'bridge.yaml'}: unknown node kind {kind!r}"
            raise ValueError(msg)
    return actions


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "bundle", default_value="", description="a bundle written by fieldproof bridge gen"
            ),
            DeclareLaunchArgument(
                "manifest", default_value="", description="robot.yaml to generate the bundle from"
            ),
            DeclareLaunchArgument("namespace", default_value="", description="vehicle namespace"),
            DeclareLaunchArgument(
                "frame_prefix", default_value="", description="prefix of every frame"
            ),
            DeclareLaunchArgument(
                "clock",
                default_value="true",
                choices=["true", "false"],
                description="bridge /clock",
            ),
            DeclareLaunchArgument(
                "robot_state_publisher",
                default_value="true",
                choices=["true", "false"],
                description="run robot_state_publisher from the manifest's description",
            ),
            DeclareLaunchArgument(
                "without", default_value="", description="optional sensors to leave out (a,b)"
            ),
            DeclareLaunchArgument(
                "use_sim_time", default_value="true", choices=["true", "false"], description="clock"
            ),
            DeclareLaunchArgument(
                "bundle_out",
                default_value="",
                description="write the bundle generated from manifest here (a temporary "
                "directory otherwise)",
            ),
            OpaqueFunction(function=launch_setup),
        ]
    )
