"""Workspace preferences survive reconstruction without restoring a data session."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk

from gweatherrouting.core import Core
from gweatherrouting.core.storage import Storage
from gweatherrouting.core.connectionmanager import ConnectionManager
from gweatherrouting.core.nmeasession import NMEALog
from gweatherrouting.gtk.mainwindow import MainWindow
from gweatherrouting.gtk.charts import ChartManager
from gweatherrouting.gtk.polarmanagerwindow import PolarManagerPanel
from gweatherrouting.gtk.settings import SettingsManager


class TestWorkspacePreferences(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "workspace.json"
        self.apps = []
        self._providers = []
        self._dark = Gtk.Settings.get_default().get_property(
            "gtk-application-prefer-dark-theme"
        )
        add_provider = Gtk.StyleContext.add_provider_for_screen

        def track_provider(screen, provider, priority):
            add_provider(screen, provider, priority)
            self._providers.append((screen, provider))

        for target in (
            patch.object(
                Gtk.StyleContext, "add_provider_for_screen", side_effect=track_provider
            ),
            patch.object(Storage, "save"),
            patch.object(ConnectionManager, "start_polling"),
            patch.object(ConnectionManager, "plug_all"),
            patch.object(ChartManager, "load_base_chart"),
            patch.object(ChartManager, "load_vector_layer"),
            patch.object(ChartManager, "load_raster_layer"),
            patch.object(PolarManagerPanel, "download_orc_list"),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.addCleanup(self.cleanup_styles)
        self.addCleanup(self.cleanup_apps)

    def cleanup_styles(self):
        for screen, provider in self._providers:
            Gtk.StyleContext.remove_provider_for_screen(screen, provider)
        Gtk.Settings.get_default().set_property(
            "gtk-application-prefer-dark-theme", self._dark
        )
        self.drain()

    def cleanup_apps(self):
        for app in reversed(self.apps):
            app.workspace.close()
            app.nmea_clock.close()
            app.core.nmea_session.close()
            app.window.destroy()
        self.drain()

    @staticmethod
    def drain():
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)

    def build(self):
        core = Core()
        core.logManager.log.points.clear()
        core.routingManager.elements.clear()
        core.trackManager.elements.clear()
        core.poiManager.elements.clear()
        core.polar_manager.polars = ["first317.pol", "bavaria38.pol"]
        core.polar_manager.get_metadata = lambda _: {}
        core.polar_manager.get_path = lambda name: str(
            Path(__file__).resolve().parents[1] / "gweatherrouting/data/polars" / name
        )
        settings = SettingsManager()
        with patch(
            "gweatherrouting.gtk.mainwindow.SettingsManager", return_value=settings
        ):
            app = MainWindow(core, workspace_path=self.path)
        self.apps.append(app)
        self.drain()
        return app

    def test_full_view_roundtrip_excludes_samples_files_and_playback_position(self):
        app = self.build()
        app.chartStack.map.set_center_and_zoom(49.25, -123.97, 12.7)
        app.logsStack.map.set_center_and_zoom(48.4, -123.4, 9)
        app.chartStack.builder.get_object("wind-toggle").set_active(False)
        app.chartStack.builder.get_object("dashboard-toggle").set_active(False)
        app.logsStack.builder.get_object("depth-chart-toggle").set_active(True)
        app.chartStack.marine_panel.current_toggle.set_active(False)
        app.polarStack.polar_list.set_cursor(1)
        performance = app.polarStack.performance
        performance.basis.set_active(1)
        performance.wind.set_active(2)
        performance.show_points.set_active(False)
        manager = app.polarStack.ensure_manager()
        manager.builder.get_object("orc-search").set_text("J/120")
        manager.builder.get_object("manager-tabs").set_current_page(1)
        app.tool_dock.show_panel("nmea")
        below = app.tool_dock._split_group(app.tool_dock.notebook, "below")
        app.tool_dock._move_panel(app.tool_dock.panels["nmea"], below)
        app.tool_dock.toggle_dock("polar-manager")
        app.tool_dock.hide_panel("polar-selector")
        app.builder.get_object("stack1").set_visible_child_name("Logs")
        app.chartStack.nmea.live.set_active(False)
        app.chartStack.nmea.speed.set_active_id("5")
        session = app.core.nmea_session
        session.finish_load(
            session.begin_load(), NMEALog("do-not-restore-this.log", (), 0, False)
        )
        session.position = 1234
        self.drain()
        app.tool_dock.sidebar.get_children()[0].set_position(270)
        self.drain()
        saved = app.workspace.save_state()
        app.workspace.close()
        text = self.path.read_text()
        self.assertNotIn("do-not-restore", text)
        self.assertNotIn("1234", text)
        self.assertNotIn("samples", text)
        app.window.destroy()
        restored = self.build()
        state = restored.workspace.save_state()
        for section in ("charts", "logs", "polars", "nmea", "polar_manager"):
            self.assertEqual(state[section], saved[section], section)
        self.assertEqual(state["window"]["maximized"], saved["window"]["maximized"])
        self.assertEqual(state["active_tab"], "Logs")
        self.assertAlmostEqual(state["dock"]["width"], saved["dock"]["width"], delta=2)
        self.assertEqual(state["dock"]["visible"], saved["dock"]["visible"])
        self.assertEqual(
            state["dock"]["splits"],
            saved["dock"]["splits"],
            (saved["window"], state["window"]),
        )
        self.assertTrue(restored.tool_dock.panels["polar-manager"].tool_floating)
        self.assertEqual(restored.core.nmea_session.source, "live")
        self.assertIsNone(restored.core.nmea_session.log)
        self.assertFalse(restored.core.nmea_session.playing)
        self.assertEqual(restored.core.nmea_session.history.snapshot()[1], ())

    def test_changes_are_debounced_and_no_disk_writes_happen_during_motion(self):
        app = self.build()
        preferences = app.workspace
        with patch.object(preferences.store, "save") as save:
            for _ in range(20):
                preferences.changed()
            timer = preferences._timer
            self.assertIsNotNone(timer)
            save.assert_not_called()
            preferences._last_change = 0
            app.chartStack.map._dragging = True
            self.assertTrue(preferences._autosave())
            save.assert_not_called()
            app.chartStack.map._dragging = False
            GLib.source_remove(timer)
            self.assertFalse(preferences._autosave())
            save.assert_called_once()
            self.assertIsNone(preferences._timer)
        with patch.object(
            preferences.store, "save", side_effect=OSError("disk unavailable")
        ):
            preferences.save()  # A preference write failure must not stop the UI.

    def test_normal_window_size_and_hidden_polar_table_width_are_restored(self):
        app = self.build()
        app.window.unmaximize()
        self.drain()
        app.window.resize(1280, 768)
        app.polarStack.body.set_position(430)
        self.drain()
        saved = app.workspace.save_state()
        self.assertFalse(saved["window"]["maximized"])
        app.workspace.close()
        app.window.destroy()
        restored = self.build()
        self.assertFalse(restored.workspace._maximized())
        self.assertEqual(
            restored.window.get_size(),
            (saved["window"]["width"], saved["window"]["height"]),
        )
        self.assertEqual(restored.polarStack.body.get_position(), 430)
