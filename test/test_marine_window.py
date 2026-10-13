"""GTK integration for offline selection and asynchronous download cancellation."""

import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib

from gweatherrouting.core.marine import MarineManager
from gweatherrouting.core.marine.fields import Area
from gweatherrouting.gtk.marinemanagerwindow import MarineManagerWindow
from test.test_marine import STATION, dataset
from test.test_marine_fields import field


class TestMarineWindow(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = MarineManager(self.directory.name)
        self.manager.store_stations((STATION,))
        self.provider = Mock()
        self.window = MarineManagerWindow(self.manager, provider=self.provider)
        self.window.station_view.get_selection().select_path(0)

    def tearDown(self):
        self.window.destroy()
        self.directory.cleanup()

    def pump_until(self, predicate):
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 3
        while not predicate():
            while context.pending():
                context.iteration(False)
            if time.monotonic() > deadline:
                self.fail("GTK operation did not finish")
            time.sleep(0.005)

    def test_download_display_enable_and_offline_restart(self):
        value = dataset()
        self.provider.download.return_value = value
        self.window.download_button.clicked()
        self.pump_until(lambda: not self.window._busy)
        self.assertIn(value.id, self.manager.datasets)
        self.assertFalse(self.manager.is_enabled(value.id))
        buffer = self.window.details.get_buffer()
        details = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.assertIn(value.id, details)
        self.assertEqual(self.window.sample_store[0][4], "2")
        self.assertIn("flow toward", details)
        renderer = self.window.dataset_view.get_column(0).get_cells()[0]
        renderer.emit("toggled", "0")
        restarted = MarineManager(self.directory.name)
        self.assertTrue(restarted.is_enabled(value.id))
        self.assertEqual(restarted.ordered_datasets(), (value,))

    def test_cancellation_discards_completed_response(self):
        started = threading.Event()
        release = threading.Event()

        def download(*args):
            started.set()
            release.wait(2)
            return dataset()

        self.provider.download.side_effect = download
        self.window.download_button.clicked()
        self.assertTrue(started.wait(1))
        self.window.cancel_button.clicked()
        release.set()
        self.pump_until(lambda: not self.window._busy)
        self.assertFalse(self.manager.datasets)
        self.assertIn("Cancelled", self.window.status.get_text())

    def test_bad_date_and_network_error_are_visible(self):
        self.window.start.set_text("bad date")
        self.window.download_button.clicked()
        self.provider.download.assert_not_called()
        self.assertIn("YYYY-MM-DD", self.window.status.get_text())
        self.window.start.set_text("2026-10-11")
        self.provider.download.side_effect = OSError("Network unavailable")
        self.window.download_button.clicked()
        self.pump_until(lambda: not self.window._busy)
        self.assertIn("Network unavailable", self.window.status.get_text())
        self.assertFalse(self.manager.datasets)

    def test_area_download_needs_no_station_selection(self):
        self.window.station_view.get_selection().unselect_all()
        panel = self.window.spatial_panel
        value = field()
        panel.planner = Mock()
        panel.planner.plan.return_value = ()
        panel.planner.download.return_value = ((value,), ())
        panel.area = Area(-126, 49, -125, 50)
        panel.start.set_text("2026-10-11 00:00")
        panel.download.clicked()
        self.pump_until(lambda: not self.window._busy)
        self.assertIn(value.id, self.manager.fields.fields)
        self.assertEqual(panel.sources[0][0], True)
        self.assertEqual(panel.sources[0][1], "Test currents")
        buffer = panel.preview.get_buffer()
        preview = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.assertIn("east 1.000, north 2.000", preview)
        restarted = MarineManager(self.directory.name)
        self.assertEqual(len(restarted.fields.snapshot().fields), 1)

    def test_area_progress_visible_on_default_tab_and_cancellable(self):
        panel = self.window.spatial_panel
        release = threading.Event()
        self.addCleanup(release.set)

        def download(area, start, end, cancel, progress):
            progress("CIOPS SalishSea: hour 6 of 30")
            release.wait(2)
            return ((field(),), ())

        panel.planner = Mock()
        panel.planner.download.side_effect = download
        self.window.show_all()
        self.assertFalse(self.window.progress_row.get_visible())
        panel.download.clicked()
        self.pump_until(lambda: "hour 6" in self.window.status.get_text())
        self.assertTrue(self.window.progress.get_mapped())
        self.assertTrue(self.window.progress_cancel.get_mapped())
        self.assertTrue(self.window._progress_timer)
        self.window.progress_cancel.clicked()
        self.assertIn("Cancelling", self.window.status.get_text())
        release.set()
        self.pump_until(lambda: not self.window._busy)
        self.assertIn("Cancelled", self.window.status.get_text())
        self.assertFalse(self.window.progress_row.get_visible())
        self.assertEqual(self.window._progress_timer, 0)
        self.assertFalse(self.manager.fields.fields)

    def test_area_error_stops_progress_and_keeps_error_visible(self):
        panel = self.window.spatial_panel
        panel.planner = Mock()
        panel.planner.download.side_effect = OSError("Network unavailable")
        self.window.show_all()
        panel.download.clicked()
        self.pump_until(lambda: not self.window._busy)
        self.assertTrue(self.window.status.get_mapped())
        self.assertIn("Network unavailable", self.window.status.get_text())
        self.assertFalse(self.window.progress_row.get_visible())
        self.assertEqual(self.window._progress_timer, 0)
        self.window._report_progress(self.window._cancel, "Late progress")
        self.assertIn("Network unavailable", self.window.status.get_text())

    def test_failed_refresh_labels_cached_preview_and_does_not_duplicate_errors(self):
        self.manager.fields.add(field())
        panel = self.window.spatial_panel
        panel.planner = Mock()
        panel.planner.download.return_value = ((), ("Regional forecast unavailable",))
        panel.download.clicked()
        self.pump_until(lambda: not self.window._busy)
        self.assertIn("previously saved", panel.status.get_text())
        self.assertIn("Regional forecast unavailable", self.window.status.get_text())
        self.assertNotIn("Regional forecast unavailable", panel.status.get_text())
        self.assertFalse(self.window.progress_row.get_visible())
        self.assertEqual(self.window._progress_timer, 0)
