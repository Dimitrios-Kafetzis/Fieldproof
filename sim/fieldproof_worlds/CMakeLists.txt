# Fieldproof worlds (task P0-T07): installs the SDF worlds and checks each one with `gz sdf
# --check` when testing.
cmake_minimum_required(VERSION 3.22)
project(fieldproof_worlds NONE)

find_package(ament_cmake REQUIRED)

install(DIRECTORY worlds DESTINATION share/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_xmllint REQUIRED)
  ament_xmllint()

  find_program(GZ_EXECUTABLE gz REQUIRED)
  file(GLOB WORLD_FILES CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/worlds/*.sdf)
  foreach(world IN LISTS WORLD_FILES)
    get_filename_component(world_name ${world} NAME_WE)
    add_test(NAME sdf_check_${world_name} COMMAND ${GZ_EXECUTABLE} sdf --check ${world})
  endforeach()
endif()

ament_environment_hooks(${CMAKE_CURRENT_SOURCE_DIR}/env-hooks/${PROJECT_NAME}.dsv.in)
ament_package()
