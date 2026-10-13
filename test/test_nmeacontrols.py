"""NMEA views share state through tab changes, floating and async loading."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from gweatherrouting.core.nmeasession import read_nmea_log
from gweatherrouting.core.utils import EventDispatcher
from gweatherrouting.gtk.nmeacontrols import NMEAControls
from gweatherrouting.gtk.widgets.tooldock import ToolDock
from test.test_nmeasession import rmc
from test.test_polarperformance import water, wind


class TestSharedNMEAControls(unittest.TestCase):
    def setUp(self):
        self.core = SimpleNamespace(connectionManager=EventDispatcher())
        self.window = Gtk.Window()
        self.tabs = Gtk.Notebook()
        self.window.add(self.tabs)
        self.docks = []
        self.views = []
        for name in ("Charts", "Logs", "Polars"):
            view = NMEAControls(self.window, self.core)
            dock = ToolDock(Gtk.Label(label=name), self.window)
            dock.register("nmea", "NMEA", view)
            dock.show_panel("nmea")
            self.tabs.append_page(dock, Gtk.Label(label=name))
            self.views.append(view)
            self.docks.append(dock)
        self.window.show_all()
        self.addCleanup(self.window.destroy)
        self.addCleanup(self.core.nmea_session.close)

    def test_tab_hide_float_and_recreate_do_not_reset_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.log"
            path.write_text(
                "\n".join(map(str, [rmc("120000"), water(), wind(), rmc("120010")]))
            )
            log = read_nmea_log(path)
        session = self.core.nmea_session
        session.finish_load(session.begin_load(), log)
        self.views[0].speed.set_active_id("2")
        self.views[1].play.set_active(True)
        session.advance(1)
        self.tabs.set_current_page(2)
        self.docks[2].toggle_dock("nmea")
        self.docks[0].hide_panel("nmea")
        for view in self.views:
            self.assertIs(view.session, session)
            self.assertEqual(view.source.get_active(), 1)
            self.assertEqual(view.speed.get_active_id(), "2")
            self.assertTrue(view.play.get_active())
            self.assertAlmostEqual(view.progress.get_fraction(), 0.75)
        self.assertEqual(session.position, 2)
        self.views[2].play.set_active(False)
        self.assertFalse(self.views[1].play.get_active())
        recreated = NMEAControls(self.window, self.core)
        self.addCleanup(recreated.destroy)
        self.assertIs(recreated.session, session)
        self.assertEqual(session.position, 2)
        self.assertEqual(recreated.source.get_active(), 1)
        self.views[0].source.set_active(0)
        self.views[1].live.set_active(False)
        self.assertTrue(all(not view.live.get_active() for view in self.views))

    def test_file_completion_survives_destroyed_initiating_view(self):
        from threading import Event

        release = Event()
        self.addCleanup(release.set)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.log"
            path.write_text(str(water()) + "\n" + str(wind()))
            log = read_nmea_log(path)

        def read(_):
            release.wait(5)
            return log

        with patch("gweatherrouting.gtk.nmeacontrols.read_nmea_log", read):
            self.views[0].load_path("race.log")
            self.assertTrue(
                all(not view.load_button.get_sensitive() for view in self.views)
            )
            self.views[0].destroy()
            release.set()
            context = GLib.MainContext.default()
            deadline = time.monotonic() + 5
            while self.core.nmea_session.loading and time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                time.sleep(0.01)
        self.assertFalse(self.core.nmea_session.loading)
        self.assertIs(self.core.nmea_session.log, log)
        self.assertEqual(self.views[1].source.get_active(), 1)
        self.assertTrue(self.views[2].load_button.get_sensitive())


class TestLogsSelectedSource(unittest.TestCase):
    def test_save_loaded_log_and_cropped_copy_preserve_raw_archive(self):
        from gweatherrouting.gtk.logsstack import LogsStack
        import datetime

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.log"
            path.write_text("\n".join([rmc("120000"), rmc("120010"), rmc("120020")]))
            raw = Path(str(path) + ".n2k.log")
            raw.write_text("original PGN archive")
            log = read_nmea_log(path)
            host = SimpleNamespace(
                session=SimpleNamespace(source="log", log=log), _export_crop=None
            )
            saved = Path(directory) / "saved.log"
            LogsStack.save_to_file(host, saved)
            self.assertEqual(len(read_nmea_log(saved).entries), 3)
            self.assertEqual(Path(str(saved) + ".n2k.log").read_text(), raw.read_text())
            host._export_crop = (
                datetime.datetime(2026, 1, 1, 12, 0, 5),
                datetime.datetime(2026, 1, 1, 12, 0, 15),
            )
            LogsStack.save_to_file(host, saved)
            cropped = read_nmea_log(saved)
            self.assertEqual(len(cropped.entries), 1)
            self.assertEqual(cropped.entries[0][1].data.timestamp.second, 10)
            self.assertEqual(len(read_nmea_log(path).entries), 3)
            self.assertEqual(Path(str(saved) + ".n2k.log").read_text(), raw.read_text())

    def test_queued_live_packets_are_discarded_after_source_reset(self):
        from gweatherrouting.gtk.logsstack import LogsStack

        host = SimpleNamespace(_alive=True, _stream_generation=2)
        # No widgets are touched for an outdated callback or destroyed view.
        self.assertFalse(LogsStack._consume(host, (object(),), 1))
        host._alive = False
        self.assertFalse(LogsStack._consume(host, (object(),), 2))
