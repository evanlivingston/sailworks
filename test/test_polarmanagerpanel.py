"""Polar management works in docked/floating panels without modal-close refreshes."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from gweatherrouting.core.utils import EventDispatcher
from gweatherrouting.gtk.polarstack import PolarStack
from gweatherrouting.gtk.routingwizarddialog import RoutingWizardDialog


class PolarManagerFixture(EventDispatcher):
    def __init__(self):
        super().__init__()
        self.polars_files = ["bavaria38.pol", "first317.pol"]
        self.polars = ["bavaria38.pol"]

    def get_metadata(self, name):
        return {"name": name.removesuffix(".pol")}

    def get_path(self, name):
        if name not in self.polars_files[:2]:
            name = "first317.pol"
        return str(
            Path(__file__).resolve().parents[1] / "gweatherrouting/data/polars" / name
        )

    def is_enabled(self, name):
        return name in self.polars

    def enable(self, name):
        self.polars.append(name)

    def disable(self, name):
        self.polars.remove(name)
        self.dispatch("polars-list-updated", self.polars)

    def delete_polar(self, name):
        self.polars_files.remove(name)
        if name in self.polars:
            self.polars.remove(name)
        self.dispatch("polars-list-updated", self.polars)

    def add_polar_file(self, path):
        self.polars_files.append(Path(path).name)
        self.polars.append(Path(path).name)
        self.dispatch("polars-list-updated", self.polars)

    def download_orc_polar(self, name, dispatch=True):
        self.add_polar_file(name.replace("/", "_") + ".pol")


class TestPolarManagerPanel(unittest.TestCase):
    def setUp(self):
        self.worker = patch("gweatherrouting.gtk.polarmanagerwindow.Thread").start()
        self.addCleanup(patch.stopall)
        self.manager = PolarManagerFixture()
        self.core = SimpleNamespace(
            polar_manager=self.manager,
            connectionManager=EventDispatcher(),
            poiManager=[],
            trackManager=[],
        )
        self.window = Gtk.Window()
        self.window.set_default_size(1220, 850)
        self.stack = PolarStack(self.window, self.core, None)
        self.window.add(self.stack)
        self.window.show_all()
        self.addCleanup(self.window.destroy)
        self.stack.on_polar_manager(None)
        self.panel = self.stack.manager_panel
        self.builder = self.panel.builder
        dock = self.stack.tool_dock
        self.assertIs(
            dock.notebook.get_nth_page(dock.notebook.get_current_page()),
            dock.panels["polar-manager"],
        )
        self.panel._populate_orc_list(
            [
                ["USA/123", "Test yacht", "J/120"],
                ["CAN/456", "Other yacht", "First 31.7"],
            ]
        )

    def drain(self):
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def test_selector_float_hide_and_refresh_preserve_selected_polar(self):
        dock = self.stack.tool_dock
        toggle = self.stack.builder.get_object("selector-toggle")
        self.assertTrue(dock.is_open("polar-selector"))
        self.manager.enable("first317.pol")
        self.manager.dispatch("polars-list-updated", self.manager.polars)
        self.stack.polar_list_store.set_sort_column_id(0, Gtk.SortType.DESCENDING)
        before = self.stack.polarWidget.polar
        self.stack.polar_list.set_cursor(0)
        self.assertEqual(self.stack.polar_list_store[0][0], "first317.pol")
        self.assertIsNot(self.stack.polarWidget.polar, before)
        self.assertIsNotNone(self.stack.table)
        dock.toggle_dock("polar-selector")
        panel = dock.panels["polar-selector"]
        dock._hide_notebook(panel.get_parent())
        self.assertFalse(toggle.get_active())
        self.manager.dispatch("polars-list-updated", self.manager.polars)
        model, tree_iter = self.stack.polar_list.get_selection().get_selected()
        self.assertEqual(model[tree_iter][0], "first317.pol")
        self.assertFalse(dock.is_open("polar-selector"))
        toggle.set_active(True)
        self.assertTrue(panel.tool_floating)
        self.assertIs(
            self.stack.polar_list.get_parent(),
            self.stack.builder.get_object("polar-selector-content"),
        )
        dock.toggle_dock("polar-selector")
        self.assertIs(panel.get_parent(), dock.notebook)
        self.assertFalse(self.stack.table.is_ancestor(panel))
        self.manager.delete_polar("first317.pol")
        model, tree_iter = self.stack.polar_list.get_selection().get_selected()
        self.assertEqual(model[tree_iter][0], "bavaria38.pol")
        self.assertIsNotNone(self.stack.polarWidget.polar)

    def test_reopen_float_and_redock_reuse_search_selection_and_catalog(self):
        search = self.builder.get_object("orc-search")
        search.set_text("test yacht")
        self.panel.on_orc_search_changed(search)
        self.builder.get_object("manager-tabs").set_current_page(1)
        self.builder.get_object("orc-treeview").get_selection().select_path(0)
        self.assertEqual(self.panel.selected_orc_polar, "USA/123")
        self.assertEqual(len(self.panel.orc_filter), 1)
        dock = self.stack.tool_dock
        dock.toggle_dock("polar-manager")
        dock.hide_panel("polar-manager")
        self.stack.on_polar_manager(None)
        self.assertIs(self.stack.manager_panel, self.panel)
        self.assertTrue(dock.panels["polar-manager"].tool_floating)
        self.assertEqual(search.get_text(), "test yacht")
        self.assertEqual(self.panel.selected_orc_polar, "USA/123")
        self.assertEqual(self.worker.call_count, 1)
        dock.toggle_dock("polar-manager")
        self.assertIs(dock.panels["polar-manager"].get_parent(), dock.notebook)

    def test_sorted_toggle_and_remove_update_the_main_polar_list_immediately(self):
        store = self.panel.polar_managerStore
        store.set_sort_column_id(0, Gtk.SortType.DESCENDING)
        self.assertEqual(store[0][0], "first317.pol")
        self.panel.on_polar_toggle(None, "0")
        self.assertIn("first317.pol", self.manager.polars)
        self.assertEqual(len(self.stack.polar_list_store), 2)
        self.builder.get_object("local-treeview").get_selection().select_path(0)
        self.builder.get_object("remove-polar-button").clicked()
        self.assertNotIn("first317.pol", self.manager.polars_files)
        self.assertEqual(len(self.stack.polar_list_store), 1)
        self.assertFalse(self.builder.get_object("remove-polar-button").get_sensitive())
        self.panel.on_polar_toggle(None, "0")
        self.assertEqual(len(self.stack.polar_list_store), 0)
        self.assertIsNone(self.stack.polarWidget.polar)
        self.assertIsNone(self.stack.table)

    def test_download_completion_updates_catalog_and_reenables_controls(self):
        self.builder.get_object("orc-treeview").get_selection().select_path(0)
        self.panel.on_orc_download(None)
        self.assertTrue(self.panel._downloading)
        self.assertFalse(self.builder.get_object("download-orc-button").get_sensitive())
        self.panel.download_orc("USA/123")
        self.drain()
        self.assertFalse(self.panel._downloading)
        self.assertIn("USA_123.pol", self.manager.polars_files)
        self.assertFalse(self.panel.orc_ListStore[0][3])
        self.assertEqual(len(self.stack.polar_list_store), 2)
        self.builder.get_object("orc-treeview").get_selection().select_path(1)
        self.assertTrue(self.builder.get_object("download-orc-button").get_sensitive())

    def test_import_dialog_uses_floating_parent_and_cancel_destroys_it(self):
        self.stack.tool_dock.toggle_dock("polar-manager")
        dialog = Mock()
        dialog.run.return_value = Gtk.ResponseType.CANCEL
        with patch(
            "gweatherrouting.gtk.polarmanagerwindow.Gtk.FileChooserDialog",
            return_value=dialog,
        ) as create:
            self.panel.on_open(None)
            self.assertIs(create.call_args.args[1], self.panel.get_toplevel())
        dialog.destroy.assert_called_once()
        self.assertEqual(len(self.manager.polars_files), 2)
        dialog.reset_mock()
        dialog.run.return_value = Gtk.ResponseType.OK
        dialog.get_filename.return_value = "/tmp/imported.pol"
        with patch(
            "gweatherrouting.gtk.polarmanagerwindow.Gtk.FileChooserDialog",
            return_value=dialog,
        ):
            self.panel.on_open(None)
        self.assertIn("imported.pol", self.manager.polars)
        self.assertEqual(len(self.stack.polar_list_store), 2)

    def test_destroyed_panel_skips_ui_callbacks_but_publishes_finished_download(self):
        callback = Mock()
        self.window.destroy()
        self.panel._deliver(callback)
        callback.assert_not_called()
        self.assertNotIn(
            self.panel.refresh_polars_tab, self.manager.handlers["polars-list-updated"]
        )
        observer = Mock()
        self.manager.connect("polars-list-updated", observer)
        self.panel._download_completed("USA/123")
        observer.assert_called_once()

    def test_routing_wizard_uses_same_component_and_refreshes_without_closing_it(self):
        with patch.object(RoutingWizardDialog, "on_boat_select"):
            wizard = RoutingWizardDialog(self.core, self.window)
            self.addCleanup(wizard.destroy)
            wizard.on_polar_manager(None)
            panel = wizard.manager_panel
            self.assertTrue(wizard.tool_dock.is_open("polar-manager"))
            panel.on_polar_toggle(None, "1")
            self.assertEqual(len(wizard.boat_store), 2)
            wizard.tool_dock.hide_panel("polar-manager")
            wizard.on_polar_manager(None)
            self.assertIs(wizard.manager_panel, panel)
            wizard.destroy()
            self.assertNotIn(
                wizard._refresh_boat_store, self.manager.handlers["polars-list-updated"]
            )
