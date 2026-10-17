# Fieldproof worlds (tasks P0-T07, P1-T02): installs the SDF worlds, the package's own primitive
# models (models/) and the Gazebo Fuel models the worlds include, and checks each world with
# `gz sdf --check` when testing.
#
# The Fuel models are pinned in assets/fuel.lock at the repository root; the build runs
# tools/assets/fetch_fuel.py (network on the first build, then the cached archives) so that
# assets/fuel/models holds every pinned model, and installs that directory together with
# models/ as share/fieldproof_worlds/models, which the environment hook puts on
# GZ_SIM_RESOURCE_PATH next to the worlds. FIELDPROOF_ASSETS_DIR points a build outside the
# monorepo at another assets directory with the same layout.
cmake_minimum_required(VERSION 3.22)
project(fieldproof_worlds NONE)

find_package(ament_cmake REQUIRED)
find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(FIELDPROOF_ASSETS_DIR
    "${CMAKE_CURRENT_SOURCE_DIR}/../../assets"
    CACHE PATH "assets directory holding fuel.lock and the fuel/ cache")
set(FIELDPROOF_FETCH_FUEL
    "${CMAKE_CURRENT_SOURCE_DIR}/../../tools/assets/fetch_fuel.py"
    CACHE FILEPATH "the Fuel fetch script")
set(FUEL_MODELS_DIR "${FIELDPROOF_ASSETS_DIR}/fuel/models")

add_custom_target(
  fuel_models ALL
  COMMAND ${Python3_EXECUTABLE} ${FIELDPROOF_FETCH_FUEL} --quiet --lock
          ${FIELDPROOF_ASSETS_DIR}/fuel.lock --dest ${FIELDPROOF_ASSETS_DIR}/fuel
  COMMENT "Fetching the Gazebo Fuel models pinned in assets/fuel.lock")

install(DIRECTORY worlds models DESTINATION share/${PROJECT_NAME})
install(DIRECTORY ${FUEL_MODELS_DIR}/ DESTINATION share/${PROJECT_NAME}/models)

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  find_package(ament_cmake_xmllint REQUIRED)
  ament_xmllint()

  # `gz sdf --check` resolves model:// includes through SDF_PATH (the package models and the
  # fetched Fuel models); every world and every package model is checked.
  find_program(GZ_EXECUTABLE gz REQUIRED)
  set(SDF_CHECK_PATH "${CMAKE_CURRENT_SOURCE_DIR}/models:${FUEL_MODELS_DIR}")
  file(GLOB WORLD_FILES CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/worlds/*.sdf)
  foreach(world IN LISTS WORLD_FILES)
    get_filename_component(world_name ${world} NAME_WE)
    add_test(NAME sdf_check_${world_name} COMMAND ${GZ_EXECUTABLE} sdf --check ${world})
    set_tests_properties(sdf_check_${world_name} PROPERTIES ENVIRONMENT
                                                            "SDF_PATH=${SDF_CHECK_PATH}")
  endforeach()
  file(GLOB MODEL_FILES CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/models/*/model.sdf)
  foreach(model IN LISTS MODEL_FILES)
    get_filename_component(model_name ${model} DIRECTORY)
    get_filename_component(model_name ${model_name} NAME)
    add_test(NAME sdf_check_model_${model_name} COMMAND ${GZ_EXECUTABLE} sdf --check ${model})
  endforeach()

  # The worlds against the lock, the installed models and the generated map (test_worlds.py).
  ament_add_pytest_test(test_worlds test/test_worlds.py)
endif()

ament_environment_hooks(${CMAKE_CURRENT_SOURCE_DIR}/env-hooks/${PROJECT_NAME}.dsv.in)
ament_package()
