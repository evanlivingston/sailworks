cmake_minimum_required(VERSION 3.20)

project(sailworks_native_routing
  VERSION 0.1.0
  DESCRIPTION "Sailworks pinned native weather-routing bridge"
  LANGUAGES CXX)

include(CTest)
include(GNUInstallDirs)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

set(_routing_upstream "${CMAKE_CURRENT_SOURCE_DIR}/upstream")

# The identity header is regenerated from these inputs, so it cannot report a
# commit or version that the vendored tree does not contain. New vendored
# files trigger a reconfigure and a fresh identity.
file(GLOB_RECURSE _routing_vendor_files CONFIGURE_DEPENDS
  LIST_DIRECTORIES false
  "${_routing_upstream}/*")

# The vendored engines are pinned source. Their own test and install targets
# stay disabled so this build produces only the bridge and its local selftest.
set(WEATHER_ROUTING_ENGINE_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(WEATHER_ROUTING_ENGINE_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
set(ORIGINAL_ROUTING_BUILD_TESTS OFF CACHE BOOL "" FORCE)

add_subdirectory(
  "${_routing_upstream}/weather-routing-engine"
  "${CMAKE_CURRENT_BINARY_DIR}/weather-routing-engine")
add_subdirectory(
  "${_routing_upstream}/original-routing-engine"
  "${CMAKE_CURRENT_BINARY_DIR}/original-routing-engine")

set(_routing_identity_header
  "${CMAKE_CURRENT_BINARY_DIR}/generated/native_routing_tree_identity.h")
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/generated")
add_custom_command(
  OUTPUT "${_routing_identity_header}"
  COMMAND ${CMAKE_COMMAND}
    "-DUPSTREAM_DIR=${_routing_upstream}"
    "-DOUTPUT=${_routing_identity_header}"
    "-DBRIDGE_VERSION=${PROJECT_VERSION}"
    -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/tree_identity.cmake"
  DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/cmake/tree_identity.cmake"
    "${CMAKE_CURRENT_SOURCE_DIR}/cmake/tree_identity.h.in"
    ${_routing_vendor_files}
  COMMENT "Computing vendored routing tree identity"
  VERBATIM)

add_library(sailworks_native_routing_core STATIC
  src/synthetic.cpp
  "${_routing_identity_header}")
target_include_directories(sailworks_native_routing_core
  PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_BINARY_DIR}/generated"
  PRIVATE
    "${_routing_upstream}/weather-routing-engine/src")
target_compile_features(sailworks_native_routing_core PUBLIC cxx_std_20)
target_link_libraries(sailworks_native_routing_core
  PUBLIC
    weather_routing_engine::weather_routing_engine
    hardened_original_engine)

if(MSVC)
  target_compile_options(sailworks_native_routing_core PRIVATE /W4 /permissive-)
else()
  target_compile_options(sailworks_native_routing_core PRIVATE
    -Wall -Wextra -Wpedantic)
endif()

# pybind11 is needed only for the Python module. A CMake selftest still builds
# without it, which keeps the native calculation independently checkable.
find_package(Python3 COMPONENTS Interpreter Development.Module)
find_package(pybind11 CONFIG QUIET)
if(pybind11_FOUND AND Python3_Development.Module_FOUND)
  pybind11_add_module(sailworks_native_routing
    src/bridge.cpp
    "${_routing_identity_header}")
  target_link_libraries(sailworks_native_routing
    PRIVATE sailworks_native_routing_core)
  set_target_properties(sailworks_native_routing PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/python"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/python"
    OUTPUT_NAME sailworks_native_routing)
  if(APPLE)
    set_target_properties(sailworks_native_routing PROPERTIES
      INSTALL_RPATH "@loader_path")
  endif()
  message(STATUS "sailworks_native_routing: Python module enabled")
else()
  message(WARNING
    "pybind11 or Python development headers were not found; "
    "building only the native selftest. Configure with "
    "-Dpybind11_DIR=<python -m pybind11 --cmakedir> to build the bridge.")
endif()

add_executable(native-routing-selftest
  tests/selftest.cpp
  "${_routing_identity_header}")
target_link_libraries(native-routing-selftest
  PRIVATE sailworks_native_routing_core)
add_test(NAME native-routing-selftest COMMAND native-routing-selftest)
