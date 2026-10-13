"""Forecast timeline bounds, clock synchronization, playback and cleanup."""

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib

from gweatherrouting.core.marine.manager import MarineManager
from gweatherrouting.core.timecontrol import TimeControl
from gweatherrouting.gtk.widgets.timetravel import TimeTravelWidget
from test.test_marine import START, dataset
from test.test_marine_fields import field


class TestTimeScrubber(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.marine = MarineManager(temporary.name)
        self.gribs = SimpleNamespace(gribs=[])
        self.clock = TimeControl()
        self.clock.set_time(datetime.fromtimestamp(START.timestamp()))
        self.map = Mock()
        self.widget = TimeTravelWidget(
            None,
            self.clock,
            self.map,
            grib_manager=self.gribs,
            marine_manager=self.marine,
        )
        self.addCleanup(self.widget.destroy)
        self.slider = self.widget.scrubber

    def drain_until(self, predicate):
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 2
        while not predicate():
            context.iteration(False)
            if time.monotonic() > deadline:
                self.fail("Timeline update timed out")
            time.sleep(0.002)

    def test_empty_range_is_disabled_and_download_updates_it_immediately(self):
        self.assertFalse(self.slider.scale.get_sensitive())
        source = field()
        self.marine.fields.add(source)
        self.assertEqual(
            self.slider.bounds, (START.timestamp(), START.timestamp() + 3600)
        )
        self.assertTrue(self.slider.scale.get_sensitive())
        self.marine.fields.enable(source.id, False)
        self.assertFalse(self.slider.scale.get_sensitive())
        self.assertIsNone(self.slider.bounds)

    def test_range_unions_enabled_sources_and_tracks_grib_list_changes(self):
        self.marine.fields.add(field())
        station = dataset(start=START - timedelta(hours=1))
        self.marine.add(station)
        self.marine.enable(station.id)
        self.gribs.gribs.append(
            SimpleNamespace(
                start_time=datetime.fromtimestamp(START.timestamp()), last_forecast=24
            )
        )
        self.drain_until(lambda: self.slider.bounds[1] == START.timestamp() + 24 * 3600)
        self.assertEqual(
            self.slider.bounds[0], (START - timedelta(hours=1)).timestamp()
        )
        self.gribs.gribs.clear()
        self.slider.refresh()
        self.assertEqual(self.slider.bounds[1], START.timestamp() + 3600)

    def test_drag_coalesces_clock_updates_pauses_play_and_commits_release(self):
        self.marine.fields.add(field())
        changes = Mock()
        self.clock.connect("time_change", changes)
        self.widget.on_play_click(None)
        for seconds in (600, 900, 1800):
            self.slider.scale.set_value(START.timestamp() + seconds)
        self.assertFalse(self.widget.play)
        changes.assert_not_called()
        self.slider._release()
        self.assertEqual(self.clock.time.timestamp(), START.timestamp() + 1800)
        changes.assert_called_once()
        self.assertIsInstance(self.clock.time, datetime)
        self.assertEqual(self.slider._commit_timer, 0)

    def test_external_clock_updates_slider_without_clamping_manual_time(self):
        self.marine.fields.add(field())
        self.clock.increase(minutes=20)
        self.assertEqual(self.slider.scale.get_value(), self.clock.time.timestamp())
        self.clock.increase(hours=3)
        self.assertIn("outside", self.slider.caption.get_text())
        self.assertEqual(self.clock.time.timestamp(), START.timestamp() + 12000)
        self.assertEqual(self.slider.scale.get_value(), START.timestamp() + 3600)

    def test_play_stops_at_range_end_and_repeated_play_does_not_add_timers(self):
        self.marine.fields.add(field())
        self.clock.increase(minutes=55)
        self.widget.on_play_click(None)
        timer = self.widget._play_timer
        self.widget.on_play_click(None)
        self.assertEqual(self.widget._play_timer, timer)
        self.assertFalse(self.widget.on_play_step())
        self.assertEqual(self.clock.time.timestamp(), START.timestamp() + 3600)
        self.assertEqual(self.widget._play_timer, 0)

    def test_new_clock_selection_cancels_a_pending_drag(self):
        self.marine.fields.add(field())
        self.slider.scale.set_value(START.timestamp() + 1800)
        self.clock.increase(minutes=10)
        self.slider._release()
        self.assertEqual(self.clock.time.timestamp(), START.timestamp() + 600)
        self.assertEqual(self.slider._commit_timer, 0)

    def test_delayed_drag_update_and_playback_are_removed_on_destroy(self):
        self.marine.fields.add(field())
        self.slider.scale.set_value(START.timestamp() + 1800)
        self.assertTrue(self.slider._commit_timer)
        self.widget.on_play_click(None)
        self.widget.destroy()
        self.assertEqual(self.slider._commit_timer, 0)
        self.assertEqual(self.widget._play_timer, 0)
        self.assertFalse(self.clock.handlers["time_change"])
        self.assertFalse(self.marine.handlers["changed"])
