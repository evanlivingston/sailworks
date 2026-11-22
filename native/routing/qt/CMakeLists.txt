cmake_minimum_required(VERSION 3.21)
project(sailworks_routing_qt LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Quick Qml ShaderTools)

set(SAILWORKS_LINE_SHADERS
    shaders/line.vert
    shaders/line.frag
)

add_library(sailworks_routing_qt STATIC
    routingretaineditem.cpp
    routingretaineditem.h
    gpulineitem.cpp
    gpulineitem.h
    gpulinematerial.cpp
    gpulinematerial.h
)
target_link_libraries(sailworks_routing_qt PUBLIC Qt6::Quick)

set(SAILWORKS_ROUTING_QML_DIR "${CMAKE_BINARY_DIR}/qml/SailworksRouting"
    CACHE PATH "QML module output directory")
add_library(sailworks_routing_qmlplugin MODULE qmlplugin.cpp)
target_link_libraries(sailworks_routing_qmlplugin PRIVATE sailworks_routing_qt Qt6::Qml)
set_target_properties(sailworks_routing_qmlplugin PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${SAILWORKS_ROUTING_QML_DIR}")
add_custom_command(TARGET sailworks_routing_qmlplugin POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/qmldir"
        "${SAILWORKS_ROUTING_QML_DIR}/qmldir")

# Bake Metal (MSL 12) plus the portable GLSL set into one .qsb per stage. The
# shaders are embedded in the plugin (and in the check executable) so the
# module can be copied to the PySide6 runtime as a single library.
qt_add_shaders(sailworks_routing_qmlplugin "sailworks_routing_line_shaders"
    PREFIX "/sailworks"
    FILES ${SAILWORKS_LINE_SHADERS}
    GLSL 120 330
    MSL 12
)

add_executable(sailworks_routing_qsg_check qsgcheck.cpp)
target_link_libraries(sailworks_routing_qsg_check PRIVATE sailworks_routing_qt)
target_compile_definitions(sailworks_routing_qsg_check PRIVATE
    SAILWORKS_RETAINED_TEST_QML="${CMAKE_CURRENT_SOURCE_DIR}/test/retainedtest.qml"
    SAILWORKS_GPU_LINE_TEST_QML="${CMAKE_CURRENT_SOURCE_DIR}/test/gpulinetest.qml")
qt_add_shaders(sailworks_routing_qsg_check "sailworks_routing_line_shaders"
    PREFIX "/sailworks"
    FILES ${SAILWORKS_LINE_SHADERS}
    GLSL 120 330
    MSL 12
)
