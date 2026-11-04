from types import SimpleNamespace
from unittest.mock import Mock

from sailworks.qt.mapcursor import MapCursorMixin


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
        self.wave_visible = True
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
        self._cursor_parts = {
            "wind": "",
            "gust": "",
            "current": "",
            "wave": "",
            "depth": "",
        }
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
            self.wave_visible,
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
    cursor.charts = SimpleNamespace(
        depth_at=lambda *_: {"sounding": 12.0, "distance_m": 30.0}
    )
    cursor._hover(Position(2, 1))
    assert cursor.cursor_readout.text.startswith("Wind …")
    callback = cursor._wind_cursor.request.call_args.args[-1]
    callback(((180, 10), 5, (2.1, 8.2, 250.4)))
    assert "Wind 180.0°, 19.4 kts" in cursor.cursor_readout.text
    assert "Gust 9.7 kts" in cursor.cursor_readout.text
    assert "Wave 2.1 m · 8 s · 250°" in cursor.cursor_readout.text
    text = cursor.cursor_readout.text
    assert text.index("Wave") < text.index("Sounding") < text.index("Lat")


def test_cursor_hidden_wave_layer_reports_unavailable_without_sampling():
    cursor = Cursor()
    cursor.wave_visible = False
    cursor._hover(Position(2, 1))
    assert "Waves unavailable" in cursor.cursor_readout.text
    assert cursor._wind_cursor.request.call_args.kwargs["wave_visible"] is False


def test_cursor_reports_unavailable_waves_when_the_sample_is_missing():
    cursor = Cursor()
    cursor._hover(Position(2, 1))
    callback = cursor._wind_cursor.request.call_args.args[-1]
    callback(((180, 10), 5, None))
    assert "Wind 180.0°, 19.4 kts" in cursor.cursor_readout.text
    assert "Waves unavailable" in cursor.cursor_readout.text


def test_toggling_waves_invalidates_an_in_flight_cursor_sample():
    cursor = Cursor()
    cursor._hover(Position(2, 1))
    old_callback = cursor._wind_cursor.request.call_args.args[-1]
    cursor.wave_visible = False
    old_callback(((180, 10), 5, (2.1, 8, 250)))
    assert "Wave 2.1" not in cursor.cursor_readout.text


def test_old_cursor_result_cannot_replace_the_current_point():
    cursor = Cursor()
    cursor._hover(Position(2, 1))
    old_callback = cursor._wind_cursor.request.call_args.args[-1]
    cursor._hover(Position(8, 7))
    current_callback = cursor._wind_cursor.request.call_args.args[-1]

    old_callback(((180, 10), 5, (2.1, 8, 250)))
    assert cursor.cursor_readout.text.startswith("Wind …")
    current_callback(((90, 4), None, None))
    assert "Wind 90.0°, 7.8 kts" in cursor.cursor_readout.text
    assert "Gust" not in cursor.cursor_readout.text


def test_cursor_reports_unavailable_wind_when_forecast_has_no_sample():
    cursor = Cursor()
    cursor._hover(Position(2, 1))
    callback = cursor._wind_cursor.request.call_args.args[-1]
    callback((None, None, None))
    assert "Wind unavailable" in cursor.cursor_readout.text
    assert "Gust" not in cursor.cursor_readout.text


def test_cursor_reports_unavailable_without_a_forecast_source():
    cursor = Cursor(sources=())
    cursor._hover(Position(2, 1))
    assert "Wind unavailable" in cursor.cursor_readout.text
    cursor._wind_cursor.request.assert_not_called()
