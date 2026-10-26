import pytest

from gweatherrouting.qt.gribviewport import ViewportCamera
from gweatherrouting.qt.modelregion import (
    region_from_camera,
    region_from_visible_workspace,
)
from gweatherrouting.rendering.weatherbarbs import _view_bounds


def test_region_helper_uses_actual_viewport_projection_and_wraps_longitude():
    camera = ViewportCamera(center_lat=18, center_lon=42, zoom=8, width=640, height=480)
    (south, west), (north, east) = _view_bounds(camera.view(), margin=0.0)
    region = region_from_camera(camera)
    assert region[0] == south and region[2] == north
    assert region[1] == (west + 180) % 360 - 180
    assert region[3] == (east + 180) % 360 - 180

    dateline = ViewportCamera(
        center_lat=0, center_lon=180, zoom=8, width=640, height=480
    )
    (_, raw_west), (_, raw_east) = _view_bounds(dateline.view(), margin=0.0)
    assert raw_west < 180 < raw_east
    assert region_from_camera(dateline)[1] > region_from_camera(dateline)[3]

    world = ViewportCamera(
        center_lat=0, center_lon=0, zoom=0, width=100_000, height=40_000
    )
    (_, raw_west), (_, raw_east) = _view_bounds(world.view(), margin=0.0)
    assert raw_east - raw_west >= 360
    assert region_from_camera(world)[1::2] == (-180.0, 180.0)


def test_capture_uses_only_visible_charts_or_logs_workspace():
    calls = []

    class Canvas:
        def __init__(self, name):
            self.name = name

        def requested_viewport_camera(self):
            calls.append(self.name)
            return ViewportCamera(center_lon=10, zoom=8, width=640, height=480)

    charts, logs = Canvas("charts"), Canvas("logs")
    region_from_visible_workspace(0, charts, logs)
    region_from_visible_workspace(1, charts, logs)
    assert calls == ["charts", "logs"]
    with pytest.raises(ValueError, match="Charts or Logs"):
        region_from_visible_workspace(2, charts, logs)
    assert calls == ["charts", "logs"]
