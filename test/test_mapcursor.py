from types import SimpleNamespace
from unittest.mock import Mock

from gweatherrouting.qt.mapcursor import MapCursorMixin


class Position:
    def __init__(self, x, y):
        self._x = x
        self._y = y

    def x(self):
        return self._x

    def y(self):
        return self._y


class Readout:
    def __init__(self):
        self.text = ""

    def emit(self, text):
        self.text = text


class Cursor(MapCursorMixin):
    def __init__(self, sources=("forecast",)):
        self.time_control = SimpleNamespace(time="valid-time")
        self.controller = SimpleNamespace(
            display_forecast=SimpleNamespace(sources=lambda: sources)
        )
        self._center_lat = 0
        self._center_lon = 0
        self._zoom = 1
        self.wind_visible = False
        self.marine = SimpleNamespace(
            sources=(), currents_visible=False, cursor_text=lambda *_: None
        )
        self.charts = SimpleNamespace(depth_at=lambda *_: None)
        self._wind_cursor = Mock()
        self.cursor_readout = Readout()
        self.disposed = False
        self.move_callback = None
        self._cursor_context = None
        self._cursor_position = None
        self._cursor_parts = {"wind": "", "gust": "", "current": "", "depth": ""}
        self._cursor_coords = ""

    def _weather_context(self):
        return (
            self.time_control.time,
            self.controller.display_forecast.sources(),
            self._center_lat,
            self._center_lon,
            self._zoom,
            100,
            100,
            self.wind_visible,
            self.marine.sources,
            self.marine.currents_visible,
        )

    def convert_screen_to_geographic(self, x, y):
        return SimpleNamespace(get_degrees=lambda: (y, x))

    def is_moving(self):
        return False

    def isVisible(self):
        return True

    def get_scale(self):
        return 1


def test_cursor_wind_and_gust_are_shown_even_when_barbs_are_hidden():
    cursor = Cursor()
    cursor._hover(Position(2, 1))
    assert cursor.cursor_readout.text.startswith("Wind …")
    callback = cursor._wind_cursor.request.call_args.args[-1]
    callback(((180, 10), 5))
    assert "Wind 180.0°, 19.4 kts" in cursor.cursor_readout.text
    assert "Gust 9.7 kts" in cursor.cursor_readout.text


def test_old_cursor_result_cannot_replace_the_current_point():
    cursor = Cursor()
    cursor._hover(Position(2, 1))
    old_callback = cursor._wind_cursor.request.call_args.args[-1]
    cursor._hover(Position(8, 7))
    current_callback = cursor._wind_cursor.request.call_args.args[-1]

    old_callback(((180, 10), 5))
    assert cursor.cursor_readout.text.startswith("Wind …")
    current_callback(((90, 4), None))
    assert "Wind 90.0°, 7.8 kts" in cursor.cursor_readout.text
    assert "Gust" not in cursor.cursor_readout.text


def test_cursor_reports_unavailable_wind_when_forecast_has_no_sample():
    cursor = Cursor()
    cursor._hover(Position(2, 1))
    callback = cursor._wind_cursor.request.call_args.args[-1]
    callback((None, None))
    assert "Wind unavailable" in cursor.cursor_readout.text
    assert "Gust" not in cursor.cursor_readout.text


def test_cursor_reports_unavailable_without_a_forecast_source():
    cursor = Cursor(sources=())
    cursor._hover(Position(2, 1))
    assert "Wind unavailable" in cursor.cursor_readout.text
    cursor._wind_cursor.request.assert_not_called()
