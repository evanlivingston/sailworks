import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent

from sailworks.qt.mapinteraction import MapInteractionMixin
from sailworks.qt.viewportmap import ViewportMap


class GeographicPoint:
    def __init__(self, x, y):
        self.x, self.y = x, y

    def get_degrees(self):
        return self.x, self.y


class MeasureHost(MapInteractionMixin):
    def __init__(self):
        self._measure_mode = False
        self._measure_points = ()
        self.move_callback = None
        self.keys = []
        self.published = 0

    def publish_route_overlay(self):
        self.published += 1

    def _cancel_cursor(self):
        pass

    def setCursor(self, _):
        pass

    def setFocus(self):
        pass

    def update(self):
        pass

    def convert_screen_to_geographic(self, x, y):
        return GeographicPoint(x, y)


def key_event(key):
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


class TestQtMapMeasure(unittest.TestCase):
    def test_i_enters_measure_mode_and_two_clicks_record_geographic_points(self):
        host = MeasureHost()
        event = key_event(Qt.Key.Key_I)
        MapInteractionMixin.keyPressEvent(host, event)
        self.assertTrue(host._measure_mode)
        self.assertTrue(host._record_measurement_click(45, -123))
        self.assertEqual(host._measure_points, ((45, -123),))
        host._record_measurement_click(46, -122)
        self.assertEqual(host._measure_points, ((45, -123), (46, -122)))
        self.assertFalse(host._measure_mode)
        self.assertGreater(host._measurement_distance_km(), 0)
        self.assertEqual(host.published, 2)

    def test_escape_clears_partial_or_completed_measurement(self):
        host = MeasureHost()
        MapInteractionMixin.keyPressEvent(host, key_event(Qt.Key.Key_I))
        host._record_measurement_click(45, -123)
        event = key_event(Qt.Key.Key_Escape)
        MapInteractionMixin.keyPressEvent(host, event)
        self.assertFalse(host._measure_mode)
        self.assertEqual(host._measure_points, ())
        MapInteractionMixin.keyPressEvent(host, key_event(Qt.Key.Key_I))
        host._record_measurement_click(45, -123)
        host._record_measurement_click(46, -122)
        MapInteractionMixin.keyPressEvent(host, key_event(Qt.Key.Key_Escape))
        self.assertEqual(host._measure_points, ())

    def test_pointer_movement_updates_live_preview(self):
        host = MeasureHost()
        host._measure_mode = True
        host._measure_points = ((45, -123),)
        self.assertTrue(host._update_measurement_preview(46, -122))
        self.assertEqual(host._measure_preview_point, (46, -122))
        self.assertAlmostEqual(
            host._distance_between_km(
                host._measure_points[0], host._measure_preview_point
            ),
            135.8,
            delta=1,
        )

    def test_move_drag_commits_on_release_not_press(self):
        host = MeasureHost()
        received = []
        host.move_callback = received.append

        self.assertTrue(host._begin_move_drag(QPointF(1, 2)))
        self.assertEqual(received, [])
        self.assertTrue(host._finish_move_drag(QPointF(3, 4)))
        self.assertEqual(received, [(3.0, 4.0)])
        self.assertIsNone(host.move_callback)

    def test_move_preview_follows_cursor_and_cancel_restores(self):
        host = MeasureHost()
        previewed = []
        restored = []
        host.move_callback = lambda coordinates: None
        host._move_preview_callback = previewed.append
        host._move_restore_callback = lambda: restored.append(True)

        self.assertTrue(host._move_preview(QPointF(3, 4)))
        self.assertEqual(previewed, [(3.0, 4.0)])

        host.cancel_move()
        self.assertEqual(restored, [True])
        self.assertIsNone(host.move_callback)
        self.assertIsNone(host._move_preview_callback)

    def test_overlay_paints_line_and_distance_label(self):
        host = SimpleNamespace(
            _measure_points=((45, -123), (46, -122)),
            _center_lat=45,
            _center_lon=-123,
            _lat_to_y=lambda value: value,
            _lon_to_x=lambda value: value,
            width=lambda: 800,
            height=lambda: 600,
            _measurement_distance_km=lambda: 135.8,
            _weather_presenter=None,
        )
        host._paint_measurement_overlay = lambda painter: (
            ViewportMap._paint_measurement_overlay(host, painter)
        )
        painter = Mock()
        ViewportMap._paint_foreground(host, painter)
        painter.drawLine.assert_called_once()
        label = painter.drawText.call_args.args[1]
        self.assertEqual(label, "135.8 km")

    def test_overlay_paints_live_line_and_distance_label(self):
        host = SimpleNamespace(
            _measure_points=((45, -123),),
            _measure_preview_point=(46, -122),
            _center_lat=45,
            _center_lon=-123,
            _lat_to_y=lambda value: value,
            _lon_to_x=lambda value: value,
            width=lambda: 800,
            height=lambda: 600,
            _distance_between_km=MapInteractionMixin._distance_between_km,
            _weather_presenter=None,
        )
        host._paint_measurement_overlay = lambda painter: (
            ViewportMap._paint_measurement_overlay(host, painter)
        )
        painter = Mock()
        ViewportMap._paint_foreground(host, painter)
        painter.drawLine.assert_called_once()
        self.assertEqual(painter.drawText.call_args.args[1], "135.8 km")

    def test_overlay_publishes_measurement_to_the_qml_route_canvas(self):
        from pathlib import Path

        from sailworks.qt.viewportmap import _viewport_measurement

        host = SimpleNamespace(
            _measure_points=((45, -123), (46, -122)),
            _measure_preview_point=None,
            _measurement_distance_km=lambda: 135.8,
        )
        self.assertEqual(
            _viewport_measurement(host),
            ([[45, -123], [46, -122]], [], 135.8),
        )
        qml = (
            Path(__file__).parents[1]
            / "sailworks"
            / "qt"
            / "shaders"
            / "ViewportChartScene.qml"
        ).read_text()
        self.assertIn("property var measurePoints", qml)
        self.assertIn("property var measurePreview", qml)
        self.assertIn("function drawMeasurement(context)", qml)
        self.assertIn("drawMeasurement(context)", qml)


if __name__ == "__main__":
    unittest.main()
