# Reproducible tree identity for the vendored routing sources.
#
# Algorithm (mirrored by test/test_native_routing_import.py):
#   * consider every regular file below UPSTREAM_DIR (recursively);
#   * exclude paths with any component beginning with ".";
#   * sort the POSIX relative paths bytewise;
#   * manifest line = SHA-256(file bytes, lower-case hex) + "  " + path + "\n";
#   * identity = SHA-256(manifest bytes) as lower-case hex.
#
# The generated header lives outside UPSTREAM_DIR, so the identity never
# depends on itself. Symlinks are not followed.

if(NOT DEFINED UPSTREAM_DIR)
  message(FATAL_ERROR "UPSTREAM_DIR is required")
endif()
if(NOT DEFINED OUTPUT)
  message(FATAL_ERROR "OUTPUT is required")
endif()
if(NOT DEFINED BRIDGE_VERSION)
  message(FATAL_ERROR "BRIDGE_VERSION is required")
endif()

# Version facts come from the vendored tree, never from a caller override.
# The commit record, the engine VERSION file and the two CMake project()
# declarations are the inputs the identity reports.
set(_identity_commit_file "${UPSTREAM_DIR}/weather-routing-engine.version")
set(_identity_engine_version_file
  "${UPSTREAM_DIR}/weather-routing-engine/VERSION")
set(_identity_engine_cmake
  "${UPSTREAM_DIR}/weather-routing-engine/CMakeLists.txt")
set(_identity_original_cmake
  "${UPSTREAM_DIR}/original-routing-engine/CMakeLists.txt")
foreach(_identity_required_file
    "${_identity_commit_file}"
    "${_identity_engine_version_file}"
    "${_identity_engine_cmake}"
    "${_identity_original_cmake}")
  if(NOT EXISTS "${_identity_required_file}")
    message(FATAL_ERROR
      "Vendored identity input is missing: ${_identity_required_file}")
  endif()
endforeach()

file(STRINGS "${_identity_commit_file}" _identity_upstream_commit
  LIMIT_COUNT 1)
string(STRIP "${_identity_upstream_commit}" _identity_upstream_commit)
if(NOT _identity_upstream_commit MATCHES "^[0-9a-f]+$")
  message(FATAL_ERROR
    "weather-routing-engine.version is not a commit hash: "
    "'${_identity_upstream_commit}'")
endif()

file(READ "${_identity_engine_version_file}" _identity_engine_version)
string(STRIP "${_identity_engine_version}" _identity_engine_version)
if(NOT _identity_engine_version MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+$")
  message(FATAL_ERROR
    "Vendored engine VERSION is not a semantic version: "
    "'${_identity_engine_version}'")
endif()

# Extract the version from project(<name> ... VERSION x.y.z ...) without
# executing the vendored project.
function(_identity_project_version _file _output)
  file(READ "${_file}" _contents)
  # Normalise layout so a multi-line project() block parses the same way as
  # a single-line declaration.
  string(REGEX REPLACE "[ \t\r\n]+" " " _contents "${_contents}")
  string(REGEX MATCH
    "project\\([^)]*VERSION ([0-9]+\\.[0-9]+\\.[0-9]+)"
    _match "${_contents}")
  if(_match STREQUAL "" OR CMAKE_MATCH_1 STREQUAL "")
    message(FATAL_ERROR
      "No project() VERSION found in vendored file: ${_file}")
  endif()
  set(${_output} "${CMAKE_MATCH_1}" PARENT_SCOPE)
endfunction()

_identity_project_version(
  "${_identity_engine_cmake}" _identity_engine_cmake_version)
_identity_project_version(
  "${_identity_original_cmake}" _identity_original_cmake_version)
set(_identity_bridge_version "${BRIDGE_VERSION}")

file(GLOB_RECURSE _identity_paths
  LIST_DIRECTORIES false
  RELATIVE "${UPSTREAM_DIR}"
  "${UPSTREAM_DIR}/*")
list(FILTER _identity_paths EXCLUDE REGEX "(^|/)\\.")
list(SORT _identity_paths)

set(_identity_manifest "")
foreach(_identity_path IN LISTS _identity_paths)
  file(SHA256 "${UPSTREAM_DIR}/${_identity_path}" _identity_file_hash)
  string(APPEND _identity_manifest
    "${_identity_file_hash}  ${_identity_path}\n")
endforeach()
string(SHA256 _identity_sha256 "${_identity_manifest}")
list(LENGTH _identity_paths _identity_file_count)

configure_file(
  "${CMAKE_CURRENT_LIST_DIR}/tree_identity.h.in"
  "${OUTPUT}"
  @ONLY)
