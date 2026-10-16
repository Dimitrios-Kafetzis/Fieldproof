# Fieldproof Gazebo system plugins (task P0-T07, ADR-0009): C++17 against the Gazebo Harmonic
# ABI from the ROS 2 Jazzy vendor packages. The version compiled into the library comes from
# package.xml. compile_commands.json is exported for clang-tidy (`make tidy`).
cmake_minimum_required(VERSION 3.22)
project(fieldproof_gz_plugins CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
  add_compile_options(-Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion)
endif()

find_package(ament_cmake REQUIRED)
find_package(gz_cmake_vendor REQUIRED)
find_package(gz_plugin_vendor REQUIRED)
find_package(gz_sim_vendor REQUIRED)
find_package(gz-plugin REQUIRED COMPONENTS register)
find_package(gz-sim REQUIRED)

# ament_package_xml() reads package.xml and sets ${PROJECT_NAME}_VERSION.
ament_package_xml()

# Version and name helpers shared by every plugin and by the tests.
add_library(fieldproof_gz_plugins_core SHARED src/version.cpp)
target_include_directories(
  fieldproof_gz_plugins_core PUBLIC $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
                                    $<INSTALL_INTERFACE:include/${PROJECT_NAME}>)
target_compile_definitions(fieldproof_gz_plugins_core
                           PRIVATE FIELDPROOF_GZ_PLUGINS_VERSION="${${PROJECT_NAME}_VERSION}")

# Hello system: the smallest loadable system plugin, logs the plugin library version on Configure.
add_library(fieldproof_hello_system SHARED src/hello_system.cpp)
target_link_libraries(fieldproof_hello_system PUBLIC fieldproof_gz_plugins_core gz-sim::core
                                                     gz-plugin::register)

install(DIRECTORY include/ DESTINATION include/${PROJECT_NAME})
install(
  TARGETS fieldproof_gz_plugins_core fieldproof_hello_system
  EXPORT export_${PROJECT_NAME}
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(ament_cmake_xmllint REQUIRED)
  ament_xmllint()
  ament_add_gtest(test_version test/test_version.cpp)
  target_link_libraries(test_version fieldproof_gz_plugins_core)
endif()

ament_export_include_directories(include/${PROJECT_NAME})
ament_export_targets(export_${PROJECT_NAME} HAS_LIBRARY_TARGET)
ament_export_dependencies(gz_sim_vendor gz_plugin_vendor gz-sim gz-plugin)
# Gazebo loads system plugins from the directories in GZ_SIM_SYSTEM_PLUGIN_PATH.
ament_environment_hooks(${CMAKE_CURRENT_SOURCE_DIR}/env-hooks/${PROJECT_NAME}.dsv.in)
ament_package()
