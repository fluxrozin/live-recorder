cmake_minimum_required(VERSION 3.21)
project(LiveRecorderTray LANGUAGES CXX)

if(NOT WIN32)
  message(FATAL_ERROR "LiveRecorderTray builds on Windows")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)

find_package(Qt6 6.2 REQUIRED COMPONENTS Widgets Network Concurrent)

set(TRAY_ICON_DIR "" CACHE PATH "Directory containing idle.ico, recording.ico, stopped.ico, and busy.ico")
foreach(name IN ITEMS idle recording stopped busy)
  if(NOT EXISTS "${TRAY_ICON_DIR}/${name}.ico")
    message(FATAL_ERROR "tray icons are missing; run windows/tray/build.ps1")
  endif()
endforeach()

set(icon_dir "${CMAKE_CURRENT_BINARY_DIR}/icons")
file(MAKE_DIRECTORY "${icon_dir}")
foreach(name IN ITEMS idle recording stopped busy)
  configure_file("${TRAY_ICON_DIR}/${name}.ico" "${icon_dir}/${name}.ico" COPYONLY)
endforeach()

qt_add_executable(LiveRecorderTray WIN32
  src/main.cpp
  src/trayapp.cpp
  src/trayapp.h
  src/textutil.cpp
  src/textutil.h
)

qt_add_resources(LiveRecorderTray trayicons
  PREFIX "/icons"
  BASE "${icon_dir}"
  FILES
    "${icon_dir}/idle.ico"
    "${icon_dir}/recording.ico"
    "${icon_dir}/stopped.ico"
    "${icon_dir}/busy.ico"
)

set(app_icon "${CMAKE_CURRENT_BINARY_DIR}/appicon.ico")
configure_file("${TRAY_ICON_DIR}/idle.ico" "${app_icon}" COPYONLY)
file(TO_CMAKE_PATH "${app_icon}" app_icon_cmake)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/app.rc" "1 ICON \"${app_icon_cmake}\"\n")
set_source_files_properties("${CMAKE_CURRENT_BINARY_DIR}/app.rc" PROPERTIES
  OBJECT_DEPENDS "${app_icon}"
)
target_sources(LiveRecorderTray PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/app.rc")

target_compile_definitions(LiveRecorderTray PRIVATE
  NOMINMAX
  WIN32_LEAN_AND_MEAN
  UNICODE
  _UNICODE
)

if(MSVC)
  target_compile_options(LiveRecorderTray PRIVATE /utf-8 /W4)
else()
  target_compile_options(LiveRecorderTray PRIVATE -Wall -Wextra)
endif()

target_link_libraries(LiveRecorderTray PRIVATE
  Qt6::Widgets
  Qt6::Network
  Qt6::Concurrent
  advapi32
)
