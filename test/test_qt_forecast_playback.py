"""Forecast playback cadence remains independent of manual step selection."""

import os
import unittest
from datetime import datetime
from importlib.util import find_spec
from unittest.mock import Mock

from gweatherrouting.core.timecontrol import TimeControl


@unittest.skipUnless(find_spec("PySide6"), "Requires PySide6")
class TestForecastPlayback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from gweatherrouting.qt.forecasttimeline import ForecastTimeline

        self.start = datetime(2026, 1, 1, 12).timestamp()
        self.clock = TimeControl()
        self.clock.set_time(datetime.fromtimestamp(self.start))
        controller = Mock()
        controller.time_control = self.clock
        controller.display_forecast.sources.return_value = []
        self.timeline = ForecastTimeline(controller)
        self.timeline.bounds = (self.start, self.start + 18000)
        self.addCleanup(self.timeline.deleteLater)
        self.addCleanup(self.timeline.dispose)

    def test_timer_cadence_is_about_two_forecast_hours_per_second(self):
        from PySide6.QtCore import Qt

        interval = self.timeline.play_timer.interval()
        self.assertAlmostEqual(1000 / interval, 60, delta=5)
        self.assertAlmostEqual(120 * 1000 / interval, 7200, delta=300)
        self.assertEqual(
            self.timeline.play_timer.timerType(), Qt.TimerType.PreciseTimer
        )

    def test_each_tick_advances_two_minutes_independent_of_manual_step(self):
        self.timeline.play()
        for label, elapsed in (("15m", 120), ("3h", 240), ("30s", 360)):
            with self.subTest(step=label):
                self.timeline.step_combo.setCurrentText(label)
                self.assertTrue(self.timeline.playing)
                self.timeline.play_timer.timeout.emit()
                self.assertEqual(self.clock.time.timestamp(), self.start + elapsed)

    def test_playback_clamps_to_end_and_stopped_ticks_do_not_update(self):
        self.timeline.bounds = (self.start, self.start + 180)
        self.timeline.play()
        self.timeline.play_timer.timeout.emit()
        self.assertEqual(self.clock.time.timestamp(), self.start + 120)
        self.assertTrue(self.timeline.playing)
        self.timeline.play_timer.timeout.emit()
        self.assertEqual(self.clock.time.timestamp(), self.start + 180)
        self.assertFalse(self.timeline.playing)
        with unittest.mock.patch.object(self.clock, "set_time") as set_time:
            self.timeline.play_timer.timeout.emit()
            set_time.assert_not_called()

    def test_paused_ticks_do_not_advance_time(self):
        self.timeline.play()
        self.timeline.pause()
        with unittest.mock.patch.object(self.clock, "set_time") as set_time:
            self.assertFalse(self.timeline.tick())
            set_time.assert_not_called()

    def test_manual_buttons_honor_selected_step_interval(self):
        for label, seconds in (("15m", 900), ("3h", 10800)):
            with self.subTest(step=label):
                self.timeline.step_combo.setCurrentText(label)
                self.timeline.forward_button.click()
                self.assertEqual(self.clock.time.timestamp(), self.start + seconds)
                self.timeline.backward_button.click()
                self.assertEqual(self.clock.time.timestamp(), self.start)

    def test_playback_updates_marine_arrows_without_rebuilding_sources(self):
        from gweatherrouting.qt.marinestate import MarineDisplayState
        from gweatherrouting.rendering.marinepresentation import chart_instant

        manager = Mock()
        manager.fields.snapshot.return_value.fields = (Mock(),)
        overlay = MarineDisplayState(manager, self.clock)
        self.addCleanup(overlay.close)
        canvas = Mock()
        overlay.watch(canvas)
        sources = overlay.sources
        manager.reset_mock()
        self.timeline.play()

        for elapsed in (120, 240, 360):
            with self.subTest(elapsed=elapsed):
                canvas.queue_draw.reset_mock()
                self.timeline.play_timer.timeout.emit()
                canvas.queue_draw.assert_called_once_with()
                self.assertEqual(
                    chart_instant(overlay.time_control.time),
                    chart_instant(datetime.fromtimestamp(self.start + elapsed)),
                )
                self.assertIs(overlay.sources, sources)

        manager.snapshot.assert_not_called()
        manager.fields.snapshot.assert_not_called()
