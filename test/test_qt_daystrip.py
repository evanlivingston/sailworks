"""Focused mouse-event checks for continuous forecast scrubbing."""

import os
import unittest
from importlib.util import find_spec
from unittest.mock import Mock, patch


@unittest.skipUnless(find_spec("PySide6"), "Requires PySide6")
class TestDayStrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from gweatherrouting.qt.forecasttimeline import DayStripWidget

        self.strip = DayStripWidget()
        self.strip.resize(400, 56)
        self.strip.set_bounds(1000.0, 2000.0)
        self.stamps = []
        self.strip.time_selected.connect(self.stamps.append)
        self.addCleanup(self.strip.deleteLater)

    def mouse(self, kind, x, button="NoButton", held="NoButton"):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        event = QMouseEvent(
            getattr(QEvent.Type, kind),
            QPointF(x, 10),
            QPointF(x, 10),
            getattr(Qt.MouseButton, button),
            getattr(Qt.MouseButton, held),
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(self.strip, event)

    def test_drag_is_proportional_clamped_and_stops_on_release(self):
        # No paint pass is needed to begin scrubbing.
        self.mouse("MouseButtonPress", -20, "LeftButton", "LeftButton")
        for x in (100, 200, 300, 450):
            self.mouse("MouseMove", x, held="LeftButton")
        self.mouse("MouseButtonRelease", 500, "LeftButton")
        self.assertEqual(self.stamps, [1000, 1250, 1500, 1750, 2000, 2000])
        self.mouse("MouseMove", 100)
        self.mouse("MouseMove", 200, held="LeftButton")
        self.mouse("MouseButtonRelease", 100, "LeftButton")
        self.assertEqual(len(self.stamps), 6)

    def test_release_seeks_final_position_and_right_click_does_not_scrub(self):
        self.mouse("MouseButtonPress", 100, "RightButton", "RightButton")
        self.mouse("MouseMove", 200, held="RightButton")
        self.mouse("MouseButtonRelease", 200, "RightButton")
        self.assertEqual(self.stamps, [])
        self.mouse("MouseButtonPress", 100, "LeftButton", "LeftButton")
        self.mouse("MouseButtonRelease", 300, "LeftButton")
        self.assertEqual(self.stamps, [1250, 1750])

    def test_missing_invalid_and_zero_width_bounds_do_not_seek(self):
        for bounds in ((None, None), (2000, 1000), (float("nan"), 2000)):
            with self.subTest(bounds=bounds):
                self.strip.set_bounds(*bounds)
                self.mouse("MouseButtonPress", 200, "LeftButton", "LeftButton")
                self.mouse("MouseMove", 300, held="LeftButton")
                self.mouse("MouseButtonRelease", 300, "LeftButton")
        self.strip.set_bounds(1000, 2000)
        self.strip.resize(0, 56)
        self.mouse("MouseButtonPress", 0, "LeftButton", "LeftButton")
        self.assertEqual(self.stamps, [])

    def test_single_timestamp_range_is_supported(self):
        self.strip.set_bounds(1000, 1000)
        self.mouse("MouseButtonPress", 200, "LeftButton", "LeftButton")
        self.mouse("MouseButtonRelease", 300, "LeftButton")
        self.assertEqual(self.stamps, [1000, 1000])

    def test_drag_continues_when_move_events_omit_button_state(self):
        self.mouse("MouseButtonPress", 0, "LeftButton", "LeftButton")
        for x in (100, 200, 300):
            self.mouse("MouseMove", x)
        self.mouse("MouseButtonRelease", 400, "LeftButton")
        self.mouse("MouseMove", 200)
        self.assertEqual(self.stamps, [1000, 1250, 1500, 1750, 2000])
        self.assertFalse(self.strip._dragging)

    def test_mouse_grab_is_released_on_release_or_missing_bounds(self):
        from PySide6.QtWidgets import QWidget

        with (
            patch.object(self.strip, "grabMouse") as grab,
            patch.object(self.strip, "releaseMouse") as release,
            patch.object(QWidget, "mouseGrabber", return_value=self.strip),
        ):
            self.mouse("MouseButtonPress", 100, "LeftButton", "LeftButton")
            grab.assert_called_once_with()
            self.mouse("MouseButtonRelease", 200, "LeftButton")
            release.assert_called_once_with()
            self.assertEqual(self.stamps, [1250, 1500])

            self.mouse("MouseButtonPress", 100, "LeftButton", "LeftButton")
            self.strip.set_bounds(None, None)
            self.assertFalse(self.strip._dragging)
            self.assertEqual(release.call_count, 2)
            self.mouse("MouseMove", 300)
            self.mouse("MouseButtonRelease", 400, "LeftButton")
            self.assertEqual(self.stamps, [1250, 1500, 1250])

    def test_invalid_bounds_mid_drag_cancel_the_gesture(self):
        self.mouse("MouseButtonPress", 100, "LeftButton", "LeftButton")
        self.strip.set_bounds(2000, 1000)
        self.mouse("MouseMove", 200)
        self.assertFalse(self.strip._dragging)
        self.strip.set_bounds(1000, 2000)
        self.mouse("MouseMove", 300)
        self.mouse("MouseButtonRelease", 400, "LeftButton")
        self.assertEqual(self.stamps, [1250])

    def test_transport_button_is_larger_and_clickable(self):
        from PySide6.QtCore import QPoint, QSize, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QHBoxLayout, QWidget

        from gweatherrouting.qt.forecasttimeline import ForecastTimeline

        container = QWidget()
        self.addCleanup(container.deleteLater)
        callback = Mock()
        button = ForecastTimeline._button(
            QHBoxLayout(container), "media-playback-start-symbolic", "Play", callback
        )
        container.show()
        self.app.processEvents()
        self.assertEqual(button.size(), QSize(32, 32))
        self.assertEqual(button.iconSize(), QSize(20, 20))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(26, 16))
        callback.assert_called_once()
