cmake_minimum_required(VERSION 3.21)
project(sailworks_routing_qt LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Quick)

add_library(sailworks_routing_qt STATIC
    routingretaineditem.cpp
    routingretaineditem.h
)
target_link_libraries(sailworks_routing_qt PUBLIC Qt6::Quick)

add_executable(sailworks_routing_qsg_check qsgcheck.cpp)
target_link_libraries(sailworks_routing_qsg_check PRIVATE sailworks_routing_qt)
target_compile_definitions(sailworks_routing_qsg_check PRIVATE
    SAILWORKS_RETAINED_TEST_QML="${CMAKE_CURRENT_SOURCE_DIR}/retainedtest.qml")
