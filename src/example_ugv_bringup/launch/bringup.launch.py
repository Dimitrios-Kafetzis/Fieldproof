"""Bring up the example UGV: the hardware drivers and the waypoint navigator, as on the vehicle.

    ros2 launch example_ugv_bringup bringup.launch.py [use_sim_time:=false] [drivers:=true] \\
        [namespace:=] [params_file:=...]

The field defaults are `use_sim_time:=false drivers:=true`. In Fieldproof the manifest's
entrypoint runs this file with `use_sim_time:=true drivers:=false`: the hardware drivers of
drivers.launch.py are not started because the bridge provides their topics (robot.yaml,
`runtime.drivers`), and every node follows the simulation clock. Nothing else differs: the
parameter file is the vehicle's.

`namespace` makes the vehicle one of a fleet: every node runs under it, and `/tf` and
`/tf_static` are remapped to the namespace's (one TF tree per vehicle with the usual frame
names). The orchestrator passes the argument from the scenario
(robot.yaml, `runtime.entrypoint.launch.namespace_arg`).
"""

from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node, PushRosNamespace, SetRemap
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription


def generate_launch_description() -> LaunchDescription:
    share = FindPackageShare("example_ugv_bringup")
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
            default_value=PathJoinSubstitution([share, "config", "example_ugv.yaml"]),
            description="the navigator's parameters",
        ),
    ]
    drivers = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(PathJoinSubstitution([share, "launch", "drivers.launch.py"])),
        condition=IfCondition(LaunchConfiguration("drivers")),
        launch_arguments={"namespace": namespace, "use_sim_time": use_sim_time}.items(),
    )
    stack = GroupAction(
        [
            PushRosNamespace(namespace),
            SetRemap("/tf", "tf"),
            SetRemap("/tf_static", "tf_static"),
            Node(
                package="example_ugv_bringup",
                executable="waypoint_navigator",
                name="waypoint_navigator",
                output="screen",
                parameters=[LaunchConfiguration("params_file"), {"use_sim_time": use_sim_time}],
            ),
        ]
    )
    return LaunchDescription([*arguments, drivers, stack])
