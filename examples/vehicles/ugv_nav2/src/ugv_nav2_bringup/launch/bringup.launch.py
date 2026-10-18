"""Bring up the example UGV: drivers, SLAM Toolbox and Nav2, as on the vehicle.

    ros2 launch ugv_nav2_bringup bringup.launch.py [use_sim_time:=false] [drivers:=true] \\
        [namespace:=] [params_file:=...] [slam_params_file:=...] [autostart:=true] \\
        [log_level:=info]

The field defaults are `use_sim_time:=false drivers:=true`. In Fieldproof the manifest's
entrypoint runs this file with `use_sim_time:=true drivers:=false`: the hardware drivers of
drivers.launch.py are not started because the bridge provides their topics (robot.yaml,
`runtime.drivers`), and every node follows the simulation clock. Nothing else differs: the
parameter files are the vehicle's, the Nav2 servers are started by navigation.launch.py and SLAM
Toolbox through its own online_async_launch.py.

`namespace` makes the stack one robot of a fleet, the way Nav2's multi-robot bringup does: every
node runs under it, `/tf` and `/tf_static` are remapped to `tf` and `tf_static` of the
namespace, so each robot has its own TF tree with the usual frame names (`map`, `odom`,
`base_link`), and the parameter files are rewritten with the namespace as their root key, since
a ROS 2 parameter file names its nodes and `slam_toolbox:` reaches `/slam_toolbox` only;
navigation.launch.py does all that for the Nav2 servers and this file does it for SLAM Toolbox,
whose own launch knows no namespace. The orchestrator passes the argument from the scenario
(robot.yaml, `runtime.entrypoint.launch.namespace_arg`).
"""

from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import PushRosNamespace, SetRemap
from launch_ros.substitutions import FindPackageShare
from nav2_common.launch import RewrittenYaml

from launch import LaunchDescription


def generate_launch_description() -> LaunchDescription:
    share = FindPackageShare("ugv_nav2_bringup")
    namespace = LaunchConfiguration("namespace")
    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart = LaunchConfiguration("autostart")
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
            default_value=PathJoinSubstitution([share, "config", "nav2.yaml"]),
            description="Nav2 parameters",
        ),
        DeclareLaunchArgument(
            "slam_params_file",
            default_value=PathJoinSubstitution([share, "config", "slam.yaml"]),
            description="SLAM Toolbox parameters",
        ),
        DeclareLaunchArgument(
            "autostart",
            default_value="true",
            choices=["true", "false"],
            description="configure and activate the lifecycle nodes",
        ),
        DeclareLaunchArgument("log_level", default_value="info", description="Nav2 log level"),
    ]
    drivers = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(PathJoinSubstitution([share, "launch", "drivers.launch.py"])),
        condition=IfCondition(LaunchConfiguration("drivers")),
        launch_arguments={"namespace": namespace, "use_sim_time": use_sim_time}.items(),
    )
    slam_params = RewrittenYaml(
        source_file=LaunchConfiguration("slam_params_file"),
        param_rewrites={},
        root_key=namespace,
        convert_types=True,
    )
    slam = GroupAction(
        [
            PushRosNamespace(namespace),
            SetRemap("/tf", "tf"),
            SetRemap("/tf_static", "tf_static"),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    PathJoinSubstitution(
                        [FindPackageShare("slam_toolbox"), "launch", "online_async_launch.py"]
                    )
                ),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "slam_params_file": slam_params,
                    "autostart": autostart,
                }.items(),
            ),
        ]
    )
    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([share, "launch", "navigation.launch.py"])
        ),
        launch_arguments={
            "namespace": namespace,
            "use_sim_time": use_sim_time,
            "params_file": LaunchConfiguration("params_file"),
            "autostart": autostart,
            "log_level": LaunchConfiguration("log_level"),
        }.items(),
    )
    return LaunchDescription([*arguments, drivers, slam, navigation])
