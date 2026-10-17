"""The Nav2 servers of the example UGV, as the vehicle runs them.

    ros2 launch ugv_nav2_bringup navigation.launch.py [namespace:=] [use_sim_time:=false] \\
        [params_file:=...] [autostart:=true] [log_level:=info]

The vehicle's own navigation launch (what nav2_bringup's navigation_launch.py does for the
TurtleBot demos, without the demo's simulation and visualisation dependencies): one process per
server, all under the lifecycle manager, with the parameters of config/nav2.yaml. Topics are
remapped exactly as Nav2 expects: the controller and the behaviours publish `cmd_vel_nav`, the
velocity smoother reads it and publishes `cmd_vel_smoothed`, the collision monitor publishes the
vehicle's `cmd_vel`.
"""

from launch.actions import DeclareLaunchArgument, GroupAction, SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node, PushRosNamespace, SetParameter
from launch_ros.descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription

SERVERS = [
    # (package, executable, node name, extra remappings)
    ("nav2_controller", "controller_server", "controller_server", [("cmd_vel", "cmd_vel_nav")]),
    ("nav2_smoother", "smoother_server", "smoother_server", []),
    ("nav2_planner", "planner_server", "planner_server", []),
    ("nav2_behaviors", "behavior_server", "behavior_server", [("cmd_vel", "cmd_vel_nav")]),
    ("nav2_bt_navigator", "bt_navigator", "bt_navigator", []),
    ("nav2_waypoint_follower", "waypoint_follower", "waypoint_follower", []),
    (
        "nav2_velocity_smoother",
        "velocity_smoother",
        "velocity_smoother",
        [("cmd_vel", "cmd_vel_nav")],
    ),
    ("nav2_collision_monitor", "collision_monitor", "collision_monitor", []),
]


def generate_launch_description() -> LaunchDescription:
    namespace = LaunchConfiguration("namespace")
    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart = LaunchConfiguration("autostart")
    log_level = LaunchConfiguration("log_level")
    params = ParameterFile(LaunchConfiguration("params_file"), allow_substs=True)
    # Nav2 publishes TF through relative names so a namespace can be prepended.
    remappings = [("/tf", "tf"), ("/tf_static", "tf_static")]
    arguments = [
        DeclareLaunchArgument("namespace", default_value="", description="top-level namespace"),
        DeclareLaunchArgument("use_sim_time", default_value="false", description="clock"),
        DeclareLaunchArgument(
            "params_file",
            default_value=PathJoinSubstitution(
                [FindPackageShare("ugv_nav2_bringup"), "config", "nav2.yaml"]
            ),
            description="parameters of every server",
        ),
        DeclareLaunchArgument(
            "autostart", default_value="true", description="configure and activate the servers"
        ),
        DeclareLaunchArgument("log_level", default_value="info", description="log level"),
    ]
    nodes = [
        Node(
            package=package,
            executable=executable,
            name=name,
            output="screen",
            respawn_delay=2.0,
            parameters=[params],
            arguments=["--ros-args", "--log-level", log_level],
            remappings=remappings + extra,
        )
        for package, executable, name, extra in SERVERS
    ]
    lifecycle_manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_navigation",
        output="screen",
        arguments=["--ros-args", "--log-level", log_level],
        parameters=[
            {"autostart": autostart, "node_names": [name for _, _, name, _ in SERVERS]},
        ],
    )
    group = GroupAction(
        [
            PushRosNamespace(namespace),
            SetParameter("use_sim_time", use_sim_time),
            *nodes,
            lifecycle_manager,
        ]
    )
    return LaunchDescription(
        [
            SetEnvironmentVariable("RCUTILS_LOGGING_BUFFERED_STREAM", "1"),
            *arguments,
            group,
        ]
    )
