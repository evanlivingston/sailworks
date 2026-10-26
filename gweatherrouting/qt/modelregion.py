"""Capture a viewport's exact visible geographic region for model requests."""

import math

from gweatherrouting.rendering.weatherbarbs import _view_bounds


def region_from_camera(camera):
    if camera.width <= 0 or camera.height <= 0:
        raise ValueError("Current map viewport has no visible area")
    # Use the renderer's inverse projection with no symbol-padding margin.
    (south, west), (north, east) = _view_bounds(camera.view(), margin=0.0)
    values = (south, west, north, east)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Current map viewport has invalid geographic bounds")
    if south >= north or south < -90 or north > 90:
        raise ValueError("Current map viewport has empty or invalid latitude bounds")
    span = east - west
    if not math.isfinite(span) or span <= 0:
        raise ValueError("Current map viewport has empty or invalid longitude bounds")
    if span >= 360:
        west, east = -180.0, 180.0
    else:
        # _view_bounds returns unwrapped longitudes so a crossing interval
        # remains west > east after canonical endpoint normalization.
        west = (west + 180.0) % 360.0 - 180.0
        east = (east + 180.0) % 360.0 - 180.0
        if west == east:
            raise ValueError("Current map viewport has empty longitude bounds")
    return float(south), float(west), float(north), float(east)


def region_from_visible_workspace(view_index, charts_map, logs_map):
    if view_index == 0:
        canvas = charts_map
    elif view_index == 1:
        canvas = logs_map
    else:
        raise ValueError("Open Charts or Logs to capture map region")
    return region_from_camera(canvas.requested_viewport_camera())
