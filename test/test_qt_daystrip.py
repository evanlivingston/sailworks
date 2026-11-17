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
        from sailworks.qt.forecasttimeline import DayStripWidget

        self.strip = DayStripWidget()
        self.strip.resize(401, 56)
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

    def test_clock_timer_runs_only_while_visible(self):
        self.assertFalse(self.strip._now_timer.isActive())
        self.strip.show()
        self.app.processEvents()
        self.assertTrue(self.strip._now_timer.isActive())
        self.strip.hide()
        self.app.processEvents()
        self.assertFalse(self.strip._now_timer.isActive())
        self.strip.show()
        self.app.processEvents()
        self.assertTrue(self.strip._now_timer.isActive())
        self.strip.stop_clock()
        self.assertFalse(self.strip._now_timer.isActive())

    def test_transport_button_is_compact_and_clickable(self):
        from PySide6.QtCore import QPoint, QSize, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QHBoxLayout, QWidget

        from sailworks.qt.forecasttimeline import ForecastTimeline

        container = QWidget()
        self.addCleanup(container.deleteLater)
        callback = Mock()
        button = ForecastTimeline._button(
            QHBoxLayout(container), "media-playback-start-symbolic", "Play", callback
        )
        container.show()
        self.app.processEvents()
        self.assertEqual(button.size(), QSize(24, 24))
        self.assertEqual(button.iconSize(), QSize(14, 14))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(18, 12))
        callback.assert_called_once()

    def test_strip_takes_keyboard_focus(self):
        from PySide6.QtCore import Qt

        self.assertEqual(self.strip.focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_arrow_keys_step_by_the_configured_interval(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.strip.set_bounds(0.0, 100000.0)
        self.strip.set_position(50000.0)
        QTest.keyClick(self.strip, Qt.Key.Key_Right)
        QTest.keyClick(self.strip, Qt.Key.Key_Left)
        self.assertEqual(self.stamps, [50900.0, 49100.0])
        QTest.keyClick(self.strip, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.stamps[-1], 53600.0)

    def test_home_end_and_bounds_clamping(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.strip.set_bounds(0.0, 100000.0)
        self.strip.set_position(50000.0)
        QTest.keyClick(self.strip, Qt.Key.Key_End)
        QTest.keyClick(self.strip, Qt.Key.Key_Home)
        self.assertEqual(self.stamps, [100000.0, 0.0])
        self.strip.set_step_seconds(40000)
        QTest.keyClick(self.strip, Qt.Key.Key_Right)
        QTest.keyClick(self.strip, Qt.Key.Key_Left)
        self.assertEqual(self.stamps[2:], [90000.0, 10000.0])
        self.strip.set_step_seconds(90000)
        QTest.keyClick(self.strip, Qt.Key.Key_Right)
        self.assertEqual(self.stamps[-1], 100000.0)

    def test_keyboard_is_inert_without_bounds_or_position(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.strip.set_position(1500.0)
        self.strip.set_bounds(None, None)
        QTest.keyClick(self.strip, Qt.Key.Key_Right)
        QTest.keyClick(self.strip, Qt.Key.Key_Home)
        self.assertEqual(self.stamps, [])
        self.strip.set_bounds(1000.0, 2000.0)
        self.strip.set_position(None)
        QTest.keyClick(self.strip, Qt.Key.Key_Right)
        QTest.keyClick(self.strip, Qt.Key.Key_End)
        self.assertEqual(self.stamps, [])

    def test_step_seconds_rejects_bad_values(self):
        self.strip.set_step_seconds(300)
        self.assertEqual(self.strip.step_seconds, 300)
        for bad in (None, 0, -60, "not-a-number"):
            with self.subTest(bad=bad):
                self.strip.set_step_seconds(bad)
                self.assertEqual(self.strip.step_seconds, 900)

    def test_hover_changes_cursor_without_emitting(self):
        from PySide6.QtCore import QEvent, Qt

        self.strip.show()
        self.app.processEvents()
        self.strip.repaint()
        before = self.strip.cursor().shape()
        self.assertNotEqual(before, Qt.CursorShape.SizeHorCursor)
        self.mouse("MouseMove", 100)
        self.assertEqual(self.strip.cursor().shape(), Qt.CursorShape.SizeHorCursor)
        self.assertEqual(self.strip._hover_index, 0)
        self.assertEqual(self.stamps, [])
        self.app.sendEvent(self.strip, QEvent(QEvent.Type.Leave))
        self.assertIsNone(self.strip._hover_index)
        self.assertEqual(self.strip.cursor().shape(), before)
        self.assertEqual(self.stamps, [])

    def test_paint_survives_empty_and_single_timestamp_ranges(self):
        self.strip.show()
        self.app.processEvents()
        with patch("sailworks.qt.forecasttimeline.logger") as log:
            self.strip.set_bounds(None, None)
            self.strip.repaint()
            self.strip.set_bounds(1000.0, 1000.0)
            self.strip.set_position(1000.0)
            self.strip.set_valid_times([1000.0])
            self.strip.repaint()
            log.warning.assert_not_called()

    def test_day_label_falls_back_to_the_day_number_then_to_nothing(self):
        from datetime import datetime, timezone

        from PySide6.QtGui import QFontMetrics

        from sailworks.qt.forecasttimeline import _day_label

        day = datetime(2026, 11, 16, tzinfo=timezone.utc)
        metrics = QFontMetrics(self.strip._label_font)
        weekday_width = metrics.horizontalAdvance("Mon 16")
        day_width = metrics.horizontalAdvance("16")
        # A cell wide enough for "Mon 16" keeps it, boundary included.
        self.assertEqual(_day_label(day, weekday_width, metrics), "Mon 16")
        self.assertEqual(_day_label(day, weekday_width - 1, metrics), "16")
        self.assertEqual(_day_label(day, day_width, metrics), "16")
        # Too narrow even for the number: draw nothing rather than overprint.
        self.assertIsNone(_day_label(day, day_width - 1, metrics))

    def test_accessibility_description_names_the_range_and_selection(self):
        self.strip.set_bounds(None, None)
        self.assertIn("no forecast range loaded", self.strip.accessibleDescription())
        self.strip.set_bounds(1000.0, 2000.0)
        described = self.strip.accessibleDescription()
        self.assertNotIn("no forecast range loaded", described)
        self.assertIn(" to ", described)
        self.assertIn("arrows step", described)
        self.assertNotIn("selected", described)
        self.strip.set_position(1500.0)
        self.assertIn("selected", self.strip.accessibleDescription())
