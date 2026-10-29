"""The hardware drivers of the example UGV (the field side of the driver-swap map).

    ros2 launch example_ugv_bringup drivers.launch.py [namespace:=] [use_sim_time:=false]

Started by bringup.launch.py when `drivers:=true` (the field default). Each node below publishes
one of the interfaces that the vehicle manifest declares and that, in Fieldproof, the bridge
provides from the simulated sensors instead (robot.yaml, `runtime.drivers`): the swap is this file
not being included, nothing in it changes. The driver packages are dependencies of the package
only when it is built for the vehicle (`EXAMPLE_UGV_TARGET=field`, see package.xml), so a
simulation image carries neither them nor their serial-port parameters.
"""

from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, FindExecutable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription


def generate_launch_description() -> LaunchDescription:
    namespace = LaunchConfiguration("namespace")
    use_sim_time = LaunchConfiguration("use_sim_time")
    arguments = [
        DeclareLaunchArgument("namespace", default_value="", description="top-level namespace"),
        DeclareLaunchArgument("use_sim_time", default_value="false", description="clock"),
        DeclareLaunchArgument(
            "description_file",
            default_value=PathJoinSubstitution(
                [
                    FindPackageShare("fieldproof_robots"),
                    "models",
                    "ugv_ref",
                    "urdf",
                    "ugv_ref.urdf.xacro",
                ]
            ),
            description="xacro of the vehicle (the reference UGV's; a real vehicle ships its own)",
        ),
    ]
    description = ParameterValue(
        Command([FindExecutable(name="xacro"), " ", LaunchConfiguration("description_file")]),
        value_type=str,
    )
    common = {"use_sim_time": use_sim_time}
    nodes = [
        # Description: robot_description and the TF of the fixed frames (/tf_static).
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            namespace=namespace,
            output="screen",
            parameters=[{"robot_description": description, **common}],
        ),
        # Base: ros2_control with the motor controller's hardware interface, diff_drive_controller
        # (cmd_vel in, odom and odom->base_link TF out) and joint_state_broadcaster.
        Node(
            package="controller_manager",
            executable="ros2_control_node",
            namespace=namespace,
            output="screen",
            parameters=[{"robot_description": description, **common}],
            remappings=[
                ("diff_drive_controller/cmd_vel_unstamped", "cmd_vel"),
                ("diff_drive_controller/odom", "odom"),
            ],
        ),
        Node(
            package="controller_manager",
            executable="spawner",
            namespace=namespace,
            arguments=["joint_state_broadcaster", "diff_drive_controller"],
        ),
        # Lidar: 360 degree 2D scan on `scan` in lidar_link.
        Node(
            package="rplidar_ros",
            executable="rplidar_composition",
            name="rplidar",
            namespace=namespace,
            output="screen",
            parameters=[common],
        ),
        # IMU on `imu/data` in imu_link.
        Node(
            package="microstrain_inertial_driver",
            executable="microstrain_inertial_driver_node",
            name="imu",
            namespace=namespace,
            output="screen",
            parameters=[common],
        ),
        # GNSS fix on `gps/fix` in gps_link.
        Node(
            package="nmea_navsat_driver",
            executable="nmea_serial_driver",
            name="gnss",
            namespace=namespace,
            output="screen",
            parameters=[common],
            remappings=[("fix", "gps/fix")],
        ),
    ]
    # RGB-D camera: colour, camera info, depth and points under `camera/`.
    camera = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([FindPackageShare("realsense2_camera"), "launch", "rs_launch.py"])
        ),
        launch_arguments={
            "camera_namespace": namespace,
            "camera_name": "camera",
            "pointcloud.enable": "true",
        }.items(),
    )
    return LaunchDescription([*arguments, *nodes, camera])
