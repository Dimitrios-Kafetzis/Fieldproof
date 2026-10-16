# Fieldproof reference robots (task P0-T07): installs the vehicle descriptions, manifests and
# launch files; the test checks the installed layout through the ament index.
cmake_minimum_required(VERSION 3.22)
project(fieldproof_robots NONE)

find_package(ament_cmake REQUIRED)

install(DIRECTORY models launch DESTINATION share/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  find_package(ament_cmake_xmllint REQUIRED)
  ament_xmllint()
  ament_add_pytest_test(test_fieldproof_robots test/test_fieldproof_robots.py)
endif()

ament_environment_hooks(${CMAKE_CURRENT_SOURCE_DIR}/env-hooks/${PROJECT_NAME}.dsv.in)
ament_package()
