# Bringup package of the example UGV (task P1-T04): launch files, parameters and the
# loss-of-link failsafe (task P2-T02, scripts/link_monitor) and the bringup watchdog that
# retries a Nav2 bringup the lifecycle manager aborted (issue #32, scripts/bringup_watchdog).
cmake_minimum_required(VERSION 3.22)
project(ugv_nav2_bringup NONE)

find_package(ament_cmake REQUIRED)

install(DIRECTORY launch config DESTINATION share/${PROJECT_NAME})
install(PROGRAMS scripts/link_monitor scripts/bringup_watchdog DESTINATION lib/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
