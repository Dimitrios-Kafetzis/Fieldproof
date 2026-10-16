# Fieldproof reference robots (tasks P0-T07, P1-T01): installs the vehicle descriptions, manifests
# and launch files, renders each xacro description to URDF and SDF at build time, and checks the
# results (check_urdf, gz sdf --check, the manifest against the SDF, a spawn-and-drive launch
# test).
cmake_minimum_required(VERSION 3.22)
project(fieldproof_robots NONE)

find_package(ament_cmake REQUIRED)
find_package(Python3 REQUIRED COMPONENTS Interpreter)

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  find_package(ament_cmake_xmllint REQUIRED)
  find_package(launch_testing_ament_cmake REQUIRED)
  find_program(GZ_EXECUTABLE gz REQUIRED)
  find_program(CHECK_URDF_EXECUTABLE check_urdf REQUIRED)
  ament_xmllint()
endif()

# Renders models/<name>/urdf/<name>.urdf.xacro into <name>.urdf and model.sdf in the build tree
# (scripts/generate_model.py: xacro, then `gz sdf -p`), installs both next to the model sources so
# `model://<name>` resolves, and registers check_urdf and `gz sdf --check` as tests.
function(fieldproof_add_robot_model name)
  set(source_dir ${CMAKE_CURRENT_SOURCE_DIR}/models/${name})
  set(gen_dir ${CMAKE_CURRENT_BINARY_DIR}/models/${name})
  set(generator ${CMAKE_CURRENT_SOURCE_DIR}/scripts/generate_model.py)
  file(GLOB xacro_sources CONFIGURE_DEPENDS ${source_dir}/urdf/*.xacro)
  add_custom_command(
    OUTPUT ${gen_dir}/${name}.urdf ${gen_dir}/model.sdf
    COMMAND ${Python3_EXECUTABLE} ${generator} ${source_dir}/urdf/${name}.urdf.xacro --urdf
            ${gen_dir}/${name}.urdf --sdf ${gen_dir}/model.sdf
    DEPENDS ${xacro_sources} ${generator}
    COMMENT "Rendering ${name}: xacro -> URDF -> SDF")
  add_custom_target(${name}_model ALL DEPENDS ${gen_dir}/${name}.urdf ${gen_dir}/model.sdf)
  install(FILES ${gen_dir}/${name}.urdf ${gen_dir}/model.sdf
          DESTINATION share/${PROJECT_NAME}/models/${name})
  if(BUILD_TESTING)
    add_test(NAME urdf_check_${name} COMMAND ${CHECK_URDF_EXECUTABLE} ${gen_dir}/${name}.urdf)
    add_test(NAME sdf_check_${name} COMMAND ${GZ_EXECUTABLE} sdf --check ${gen_dir}/model.sdf)
  endif()
endfunction()

fieldproof_add_robot_model(ugv_ref)

install(DIRECTORY models launch DESTINATION share/${PROJECT_NAME})
install(PROGRAMS scripts/generate_model.py DESTINATION lib/${PROJECT_NAME})

if(BUILD_TESTING)
  ament_add_pytest_test(test_fieldproof_robots test/test_fieldproof_robots.py)
  ament_add_pytest_test(test_ugv_ref test/test_ugv_ref.py)
  # Spawns the UGV into a headless Gazebo server with fieldproof_worlds' empty world, checks the
  # sensor rates and drives it one metre (the P1-T01 verification). Software rendering on a CI
  # runner needs minutes, hence the timeout.
  add_launch_test(test/test_ugv_ref_launch.py TARGET ugv_ref_launch TIMEOUT 900)
endif()

ament_environment_hooks(${CMAKE_CURRENT_SOURCE_DIR}/env-hooks/${PROJECT_NAME}.dsv.in)
ament_package()
