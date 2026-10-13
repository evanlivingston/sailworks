"""Marine panel coverage, independent visibility and shared download jobs."""

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch
from types import SimpleNamespace
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
from gweatherrouting.core.marine import MarineManager
from gweatherrouting.core.marine.models import Sample, Series
from gweatherrouting.core.timecontrol import TimeControl
from gweatherrouting.gtk.maplayers.marinemaplayer import MarineMapLayer
from gweatherrouting.gtk.marinetoolpanel import MarineToolPanel, current_coverage
from gweatherrouting.gtk.widgets.tooldock import ToolDock
from gweatherrouting.gtk.tidetoolpanel import TideToolPanel
from gweatherrouting.gtk.settings.settingswindow import SettingsWindow
from gweatherrouting.gtk.settings.settingswindow_charts import SettingsWindowCharts
from gweatherrouting.gtk.settings.settingswindow_connections import (
    SettingsWindowConnections,
)
from gweatherrouting.gtk.settings.settingswindow_display import SettingsWindowDisplay
from test.map_helpers import MapCanvas
from test.test_marine import START, dataset
from test.test_marine_fields import field


class TestMarineToolPanel(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = MarineManager(self.directory.name)
        self.clock = TimeControl()
        self.clock.set_time(START.astimezone().replace(tzinfo=None))
        self.map = MapCanvas()
        self.map._center_lat, self.map._center_lon, self.map._zoom = 49.5, -125.5, 9
        self.layer = MarineMapLayer(self.manager, self.clock)
        self.panel = MarineToolPanel(self.manager, self.clock, self.map, self.layer)
        self.window = Gtk.Window()
        self.dock = ToolDock(Gtk.Label(label="Chart"), self.window)
        self.window.add(self.dock)
        self.dock.register("marine", "Tides & currents", self.panel)
        self.tides = TideToolPanel(
            self.manager,
            self.clock,
            self.map,
            self.layer,
            settings_callback=self.panel.open_settings,
        )
        self.dock.register("tides", "Tides", self.tides)
        self.dock.show_panel("marine")

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
                self.fail("GTK job did not finish")
            time.sleep(0.005)

    def test_live_coverage_and_visibility_are_independent_of_panel_visibility(self):
        self.assertIn("No enabled", self.panel.coverage.get_text())
        source = field()
        self.manager.fields.add(source)
        self.assertEqual(self.panel.coverage.get_text(), "Current data available")
        self.assertIn("Test currents", self.panel.coverage_detail.get_text())
        self.panel.current_toggle.set_active(False)
        self.assertFalse(self.layer.currents_visible)
        self.assertEqual(self.layer.sources.fields, ())
        self.assertEqual(self.layer.cursor_text(49.5, -125.5), "")
        self.assertEqual(self.panel.coverage.get_text(), "Current data available")
        self.dock.hide_panel("marine")
        self.dock.show_panel("marine")
        self.assertFalse(self.panel.current_toggle.get_active())
        self.panel.current_toggle.set_active(True)
        self.assertEqual(self.layer.sources.fields, (source,))
        self.clock.set_time(
            (START + timedelta(days=1)).astimezone().replace(tzinfo=None)
        )
        self.assertIn("Outside", self.panel.coverage.get_text())
        self.map._center_lon = 10
        self.panel.refresh()
        self.assertEqual(self.panel.coverage.get_text(), "No provider for current area")
        self.manager.fields.enable(source.id, False)
        self.assertEqual(self.panel.coverage.get_text(), "No provider for current area")
        self.manager.coverage_planner.worldwide.configure("sailor", "example-password")
        self.panel.refresh()
        self.assertIn("No enabled", self.panel.coverage.get_text())

    def test_source_tabs_show_predictions_and_update_shared_selection(self):
        self.manager.add(dataset())
        self.panel.settings_button.clicked()
        settings = self.panel._advanced
        self.assertEqual(settings.sections.get_current_page(), 0)
        self.tides.settings_button.clicked()
        self.assertIs(self.panel._advanced, settings)
        self.assertEqual(settings.sections.get_current_page(), 1)
        self.assertIs(settings.spatial_panel.planner, self.panel.spatial_panel.planner)
        self.assertEqual(len(settings.dataset_store), 1)
        settings.dataset_view.get_selection().select_path(0)
        self.assertGreater(len(settings.sample_store), 0)
        self.assertTrue(
            settings.dataset_view.is_ancestor(settings.sections.get_nth_page(1))
        )
        source = field()
        self.manager.fields.add(source)
        self.assertEqual(len(settings.spatial_panel.sources), 1)
        settings.spatial_panel._toggle(None, "0")
        self.assertEqual(self.layer.sources.fields, ())
        settings.destroy()
        self.assertIsNone(self.panel._advanced)

    def test_current_downloads_group_models_preserve_gaps_and_individual_controls(self):
        first = field()
        second = field(start=START + timedelta(hours=3))
        third = field(resolution=2000)
        for source in (first, second, third):
            self.manager.fields.add(source)
        panel = self.panel.spatial_panel
        self.assertEqual(len(panel.sources), 2)
        group = panel.sources[0]
        self.assertIn("2 downloads", group[1])
        self.assertEqual(group[3].count("→"), 2)
        self.assertEqual(panel.sources.iter_n_children(group.iter), 2)
        panel.source_view.expand_row(group.path, False)
        panel._toggle(None, "0:0")
        self.assertTrue(panel.sources[0][6])
        self.assertFalse(panel.sources[0][0])
        panel._toggle(None, "0")
        self.assertTrue(panel.sources[0][0])
        self.assertFalse(panel.sources[0][6])
        panel._toggle(None, "0")
        self.assertFalse(panel.sources[0][0])
        self.assertEqual(self.manager.fields.snapshot().fields, (third,))
        self.assertTrue(panel.source_view.row_expanded(panel.sources[0].path))
        panel.source_view.get_selection().select_path("0")
        self.assertFalse(panel.remove_button.get_sensitive())
        panel.source_view.get_selection().select_path("0:0")
        self.assertTrue(panel.remove_button.get_sensitive())
        identifier = panel.sources["0:0"][4]
        panel.remove_button.clicked()
        self.assertNotIn(identifier, self.manager.fields.fields)
        self.assertIn(third.id, self.manager.fields.fields)
        self.assertEqual(panel.sources.iter_n_children(panel.sources[0].iter), 1)

    def test_model_info_is_revealed_on_hover_and_only_icon_opens_description(self):
        self.manager.fields.add(field())
        self.window.set_default_size(650, 800)
        self.window.show_all()
        panel = self.panel.spatial_panel
        view, info = panel.source_view, panel.row_info
        self.pump_until(lambda: view.get_allocated_width() > 100)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        path = Gtk.TreePath.new_from_string("0")
        area = view.get_background_area(path, info.column)
        event = SimpleNamespace(
            window=view.get_bin_window(),
            x=area.x + area.width // 2,
            y=area.y + area.height // 2,
        )
        info._motion(view, event)
        self.assertEqual(info.hover_path, "0")
        info._render(
            info.column, info.renderer, panel.sources, panel.sources.get_iter(path)
        )
        self.assertIsNotNone(info.renderer.get_property("pixbuf"))
        x, y = view.convert_bin_window_to_widget_coords(int(event.x), int(event.y))
        tooltip = Mock()
        with patch.object(view, "set_tooltip_cell"):
            self.assertTrue(info._query_tooltip(view, x, y, False, tooltip))
            body = tooltip.set_custom.call_args.args[0].get_children()[1].get_text()
            self.assertIn("Grid spacing: 500 m", body)
            name_area = view.get_background_area(path, view.get_column(1))
            nx, ny = view.convert_bin_window_to_widget_coords(
                name_area.x + 10, int(event.y)
            )
            self.assertFalse(info._query_tooltip(view, nx, ny, False, tooltip))
        info._leave()
        info._render(
            info.column, info.renderer, panel.sources, panel.sources.get_iter(path)
        )
        self.assertIsNone(info.renderer.get_property("pixbuf"))

    def test_application_currents_settings_configure_all_downloaders(self):
        from gweatherrouting.core.marine.credentials import SystemCredentialStore
        from test.test_marine_credentials import MemoryKeyring

        self.manager.coverage_planner.worldwide.credential_store = (
            SystemCredentialStore(MemoryKeyring())
        )
        preferences = SimpleNamespace(gribArrowOnGround=False, gribArrowOpacity=0.4)
        core = SimpleNamespace(marine_manager=self.manager)
        with (
            patch.object(SettingsWindowCharts, "__init__", return_value=None),
            patch.object(SettingsWindowConnections, "__init__", return_value=None),
            patch.object(SettingsWindowDisplay, "__init__", return_value=None),
        ):
            settings = SettingsWindow(self.window, preferences, core)
        self.addCleanup(settings.window.destroy)
        settings.show()
        stack = settings.builder.get_object("stack1")
        account = stack.get_child_by_name("currents")
        self.assertIs(account, settings.currents_settings)
        account.username.set_text(" sailor ")
        account.password.set_text("example-password")
        account._apply_account(None)
        self.assertEqual(account.password.get_text(), "")
        self.assertIn("Account saved", account.account_status.get_text())
        self.assertEqual(
            self.panel.spatial_panel.planner.worldwide.credentials,
            ("sailor", "example-password"),
        )
        self.panel.settings_button.clicked()
        self.assertIs(self.panel._advanced.spatial_panel.planner, account.planner)
        settings.window.destroy()
        self.assertEqual(
            self.manager.coverage_planner.worldwide.credentials[0], "sailor"
        )

    def test_forget_saved_account_in_application_settings(self):
        from gweatherrouting.gtk.settings.settingswindow_currents import (
            CurrentsSettingsPanel,
        )
        from gweatherrouting.core.marine.credentials import SystemCredentialStore
        from test.test_marine_credentials import MemoryKeyring

        provider = self.manager.coverage_planner.worldwide
        provider.credential_store = SystemCredentialStore(MemoryKeyring())
        provider.configure("sailor", "example-password")
        account = CurrentsSettingsPanel(self.manager.coverage_planner)
        self.addCleanup(account.destroy)
        self.assertEqual(account.username.get_text(), "sailor")
        self.assertEqual(account.password.get_text(), "")
        self.assertIn("Saved account ready", account.account_status.get_text())
        account._forget_account(None)
        self.assertIsNone(provider.credentials)
        self.assertIsNone(provider.credential_store.load())
        self.assertEqual(account.username.get_text(), "")
        self.assertIn("removed", account.account_status.get_text())

    def test_tide_predictions_and_masked_current(self):
        original = dataset()
        value = replace(
            original,
            station=replace(original.station, latitude=49.5, longitude=-125.5),
            series=(Series("water_level", "m", (Sample(START, 2.5, "2"),)),),
        )
        self.manager.add(value)
        self.manager.enable(value.id)
        self.assertIn("1 of 1", self.tides.tide_coverage.get_text())
        self.tides.tide_toggle.set_active(False)
        self.assertEqual(self.layer.sources.levels.datasets, ())
        self.assertTrue(self.layer.currents_visible)
        self.manager.fields.add(field(masked=True))
        self.assertIn("No valid", self.panel.coverage.get_text())
        self.assertFalse(current_coverage(self.manager, 49.5, -125.5, START)[0])

    def test_download_uses_chart_and_survives_hide_and_redock(self):
        self.assertEqual(
            self.panel.spatial_panel.start.get_text(), START.strftime("%Y-%m-%d %H:%M")
        )
        started, release = threading.Event(), threading.Event()
        source = field()

        def download(area, start, end, cancel, progress):
            started.set()
            progress("Fetching test model")
            release.wait(2)
            return [source], []

        self.panel.spatial_panel.planner.download = Mock(side_effect=download)
        self.map._center_lon = -125.6
        self.panel.spatial_panel.download.clicked()
        self.assertTrue(started.wait(1))
        self.assertTrue(self.panel.progress_row.get_visible())
        self.dock.toggle_dock("marine")
        self.dock.hide_panel("marine")
        self.assertFalse(self.panel._cancel.is_set())
        release.set()
        self.pump_until(lambda: not self.panel._busy)
        self.dock.show_panel("marine")
        self.assertIn(source.id, self.manager.fields.fields)
        self.assertIn("Saved 1", self.panel.status.get_text())
        self.assertFalse(self.panel.progress_row.get_visible())
        self.assertEqual(self.panel.coverage.get_text(), "Current data available")
        area = self.panel.spatial_panel.planner.download.call_args.args[0]
        self.assertAlmostEqual((area.west + area.east) / 2, -125.6, places=4)

    def test_map_area_refreshes_and_selected_route_stays_independent_of_map(self):
        spatial = self.panel.spatial_panel
        self.map._center_lon = -124.5
        self.panel.refresh()
        area = spatial._area()
        self.assertAlmostEqual((area.west + area.east) / 2, -124.5, places=4)
        self.map._center_lon = -123.5
        self.panel.refresh()
        area = spatial._area()
        self.assertAlmostEqual((area.west + area.east) / 2, -123.5, places=4)
        route = SimpleNamespace(
            name="New route", to_list=lambda: [(49, -125), (50, -124)]
        )
        spatial.core = SimpleNamespace(trackManager=[route], routingManager=[])
        self.panel.refresh()
        self.assertEqual(spatial.routes, [route])
        spatial.route.set_active(1)
        self.assertEqual(spatial.route.get_active_text(), "New route")
        route_area = spatial._area()
        self.map._center_lon = -122
        self.panel.refresh()
        self.assertEqual(spatial._area(), route_area)

    def test_tide_route_download_freezes_bounds_across_date_line(self):
        original = dataset()
        station = replace(
            original.station, latitude=10, longitude=179.9, series_codes=("wlp",)
        )
        far = replace(station, code="FAR", longitude=0)
        value = replace(
            original,
            station=station,
            series=(Series("water_level", "m", (Sample(START, 2.5, "2"),)),),
        )
        points = [(10, 179.8), (10, -179.8)]
        route = SimpleNamespace(name="Date line route", to_list=lambda: list(points))
        self.tides.core = SimpleNamespace(trackManager=[route], routingManager=[])
        self.tides.refresh()
        self.tides.route.set_active(1)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def discover(cancel):
            started.set()
            release.wait(2)
            return station, far

        self.tides.provider = Mock()
        self.tides.provider.discover.side_effect = discover
        self.tides.provider.download.return_value = value
        self.tides.download.clicked()
        self.pump_until(started.is_set)
        points[:] = [(0, 0)]
        release.set()
        self.pump_until(lambda: not self.tides._busy)
        self.tides.provider.download.assert_called_once()
        self.assertEqual(self.tides.provider.download.call_args.args[0], station)
        self.assertTrue(self.manager.is_enabled(value.id))
        self.tides.hours.set_value(48)
        self.tides.start.emit("icon-release", Gtk.EntryIconPosition.SECONDARY, None)
        self.assertEqual(self.tides.route.get_active(), 1)
        self.assertEqual(self.tides.hours.get_value_as_int(), 48)

    def test_download_start_follows_chart_until_edited_or_now_selected(self):
        entries = (self.panel.spatial_panel.start, self.tides.start)
        later = START + timedelta(hours=3)
        self.clock.set_time(later.astimezone().replace(tzinfo=None))
        for entry in entries:
            self.assertEqual(entry.get_text(), later.strftime("%Y-%m-%d %H:%M"))
        entries[0].set_text("2026-01-01 00:00")
        with patch("gweatherrouting.gtk.widgets.downloadstart.datetime") as moment:
            moment.now.return_value = START
            entries[1].emit("icon-release", Gtk.EntryIconPosition.SECONDARY, None)
        self.assertEqual(entries[1].get_text(), START.strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(
            self.clock.time.astimezone().replace(tzinfo=None),
            later.astimezone().replace(tzinfo=None),
        )
        self.clock.set_time(
            (later + timedelta(hours=1)).astimezone().replace(tzinfo=None)
        )
        self.assertEqual(entries[0].get_text(), "2026-01-01 00:00")
        self.assertEqual(entries[1].get_text(), START.strftime("%Y-%m-%d %H:%M"))

    def test_tide_download_selects_stations_in_view_and_enables_predictions(self):
        original = dataset()
        station = replace(
            original.station, latitude=49.5, longitude=-125.5, series_codes=("wlp",)
        )
        far = replace(station, code="FAR", latitude=0, longitude=0)
        value = replace(
            original,
            station=station,
            series=(Series("water_level", "m", (Sample(START, 2.5, "2"),)),),
        )
        self.tides.provider = Mock()
        self.tides.provider.discover.return_value = (station, far)
        self.tides.provider.download.return_value = value
        self.tides.download.clicked()
        self.pump_until(lambda: not self.tides._busy)
        self.tides.provider.download.assert_called_once()
        self.assertEqual(self.tides.provider.download.call_args.args[0], station)
        self.assertTrue(self.manager.is_enabled(value.id))
        self.assertIn("1 of 1", self.tides.tide_coverage.get_text())
        self.assertEqual(self.tides.sources[0][2], "2.50 m")
        self.assertIn("Saved 1", self.tides.status.get_text())

    def test_tide_download_outside_chs_coverage_reports_no_station_data(self):
        self.tides.provider = Mock()
        self.tides.provider.discover.return_value = ()
        self.tides.download.clicked()
        self.pump_until(lambda: not self.tides._busy)
        self.tides.provider.download.assert_not_called()
        self.assertIn("No CHS tide stations", self.tides.status.get_text())
        self.assertFalse(self.manager.datasets)
