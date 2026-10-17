"""Spawn the reference UGV into a running Gazebo server and publish its TF tree (task P1-T01).

    ros2 launch fieldproof_robots spawn_ugv_ref.launch.py [name:=ugv_ref] [namespace:=] \\
        [world:=fieldproof_empty] [x:=0 y:=0 z:=0.12 yaw:=0] [gps:=true] [use_sim_time:=true] \\
        [robot_state_publisher:=true]

The description is rendered from urdf/ugv_ref.urdf.xacro with the launch arguments and handed to
`ros_gz_sim create`, which asks the server named by `world` to spawn it; Gazebo converts the URDF
with the same parser that renders model.sdf at build time. By default robot_state_publisher
publishes the same description as `robot_description` and the TF of the fixed sensor frames (the
wheel joints follow the Gazebo joint states once the bridge runs). The bridge launch of
fieldproof_bridge (task P1-T03) assembles the TF tree itself from the manifest, so a stack that
runs it spawns with `robot_state_publisher:=false`. Needs a server with the Sensors, IMU and
NavSat systems loaded (fieldproof_worlds/fieldproof_empty.sdf has them) and `--headless-rendering`
when no display is available.
"""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import Command, FindExecutable, LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue

from launch import LaunchDescription

XACRO_FILE = (
    Path(get_package_share_directory("fieldproof_robots"))
    / "models/ugv_ref/urdf/ugv_ref.urdf.xacro"
)


def generate_launch_description() -> LaunchDescription:
    arguments = [
        DeclareLaunchArgument("name", default_value="ugv_ref", description="model name in Gazebo"),
        DeclareLaunchArgument(
            "namespace",
            default_value="",
            description="ROS namespace of the nodes and prefix of every Gazebo topic",
        ),
        DeclareLaunchArgument(
            "frame_prefix", default_value="", description="prefix of every link and joint name"
        ),
        DeclareLaunchArgument(
            "world", default_value="fieldproof_empty", description="name of the running world"
        ),
        DeclareLaunchArgument("x", default_value="0.0", description="spawn position x [m]"),
        DeclareLaunchArgument("y", default_value="0.0", description="spawn position y [m]"),
        DeclareLaunchArgument(
            "z", default_value="0.12", description="spawn height of base_link [m] (axle: 0.11)"
        ),
        DeclareLaunchArgument("yaw", default_value="0.0", description="spawn heading [rad]"),
        DeclareLaunchArgument(
            "gps",
            default_value="true",
            choices=["true", "false"],
            description="fit the GNSS receiver",
        ),
        DeclareLaunchArgument(
            "use_sim_time", default_value="true", choices=["true", "false"], description="clock"
        ),
        DeclareLaunchArgument(
            "robot_state_publisher",
            default_value="true",
            choices=["true", "false"],
            description="run robot_state_publisher (false when the bridge launch assembles TF)",
        ),
    ]
    name = LaunchConfiguration("name")
    namespace = LaunchConfiguration("namespace")

    description = Command(
        [
            FindExecutable(name="xacro"),
            " ",
            str(XACRO_FILE),
            " namespace:=",
            namespace,
            " frame_prefix:=",
            LaunchConfiguration("frame_prefix"),
            " gps:=",
            LaunchConfiguration("gps"),
        ]
    )

    robot_state_publisher = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        namespace=namespace,
        output="screen",
        condition=IfCondition(LaunchConfiguration("robot_state_publisher")),
        parameters=[
            {
                "robot_description": ParameterValue(description, value_type=str),
                "use_sim_time": LaunchConfiguration("use_sim_time"),
            }
        ],
    )
    # `create` takes the rendered description as a string (so it does not depend on which node
    # publishes robot_description) and exits once the server confirmed the spawn, so its exit
    # code tells whether the spawn succeeded.
    spawn = Node(
        package="ros_gz_sim",
        executable="create",
        namespace=namespace,
        output="screen",
        arguments=[
            "-world",
            LaunchConfiguration("world"),
            "-name",
            name,
            "-string",
            description,
            "-x",
            LaunchConfiguration("x"),
            "-y",
            LaunchConfiguration("y"),
            "-z",
            LaunchConfiguration("z"),
            "-Y",
            LaunchConfiguration("yaw"),
        ],
    )
    return LaunchDescription([*arguments, robot_state_publisher, spawn])
