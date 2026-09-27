cmake_minimum_required(VERSION 3.20)
project(SparkLite LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Let dependents compile while a static library is still failing.
set(CMAKE_OPTIMIZE_DEPENDENCIES ON)

# Engine core: a static library with a precompiled header.
add_library(core STATIC src/core/math.cpp src/core/entity.cpp)
target_include_directories(core PUBLIC src/core)
target_precompile_headers(core PRIVATE src/core/pch.h)

# Build-time tool: compiled from the tree and executed during the build.
add_executable(shadergen tools/shadergen.cpp)
set(SPARKLITE_GENERATED ${CMAKE_CURRENT_BINARY_DIR}/generated)
add_custom_command(
  OUTPUT ${SPARKLITE_GENERATED}/shaders.h
  COMMAND ${CMAKE_COMMAND} -E make_directory ${SPARKLITE_GENERATED}
  COMMAND shadergen ${CMAKE_CURRENT_SOURCE_DIR}/shaders/basic.shader ${SPARKLITE_GENERATED}/shaders.h
  DEPENDS shadergen shaders/basic.shader
  COMMENT "Generating shaders.h")
add_custom_target(shaders DEPENDS ${SPARKLITE_GENERATED}/shaders.h)

add_executable(game src/game/main.cpp)
add_dependencies(game shaders)
target_include_directories(game PRIVATE ${SPARKLITE_GENERATED})
target_link_libraries(game PRIVATE core)

add_executable(crash_test tests/crash_test.cpp)
enable_testing()
add_test(NAME crash_test COMMAND crash_test)

# Utility targets: never selectable by a model unless an operator allowlists them.
add_custom_target(deploy COMMAND ${CMAKE_COMMAND} -E echo deploy)
add_custom_target(slow COMMAND sleep 600)
