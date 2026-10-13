"""Native tool-panel lifecycle, independent of tool content."""

import unittest
from types import SimpleNamespace
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
from gweatherrouting.gtk.widgets.tooldock import ToolDock


class TestToolDock(unittest.TestCase):
    def setUp(self):
        self.window = Gtk.Window()
        self.dock = ToolDock(Gtk.Label(label="Chart"), self.window)
        self.window.add(self.dock)
        self.entry = Gtk.Entry(text="Retained settings")
        self.dock.register("marine", "Marine", self.entry)
        self.dock.register("other", "Other tool", Gtk.Label(label="Another tool"))
        self.changes = []
        self.dock.connect(
            "panel-visibility-changed",
            lambda dock, key, visible: self.changes.append((key, visible)),
        )
        self.window.show_all()

    def tearDown(self):
        self.window.destroy()

    def test_hide_float_redock_preserves_content_and_visibility(self):
        self.assertFalse(self.dock.notebook.get_visible())
        self.dock.show_panel("marine")
        self.assertTrue(self.dock.notebook.get_visible())
        panel = self.dock.panels["marine"]
        self.dock.toggle_dock("marine")
        floating = panel.get_parent()
        self.assertIsNot(floating, self.dock.notebook)
        self.assertTrue(panel.tool_floating)
        self.assertFalse(self.dock.notebook.get_visible())
        self.assertEqual(self.changes, [("marine", True)])
        self.dock._close_float(floating.get_toplevel(), None, floating)
        self.assertFalse(self.dock.is_open("marine"))
        self.dock.show_panel("marine")
        self.assertTrue(panel.tool_floating)
        self.assertEqual(self.entry.get_text(), "Retained settings")
        self.dock.toggle_dock("marine")
        self.assertIs(panel.get_parent(), self.dock.notebook)
        self.assertFalse(panel.tool_floating)
        self.assertFalse(hasattr(panel, "dock_button"))
        self.assertFalse(floating.get_toplevel().get_decorated())
        self.dock.hide_panel("marine")
        self.window.show_all()
        self.assertFalse(self.dock.notebook.get_visible())

    def test_multiple_tools_share_dock_and_native_drag_targets(self):
        for key in ("marine", "other"):
            self.dock.show_panel(key)
        self.assertEqual(self.dock.notebook.get_n_pages(), 2)
        panel = self.dock.panels["marine"]
        target = self.dock._detach(self.dock.notebook, panel, 20, 20)
        self.assertEqual(target.get_group_name(), self.dock.notebook.get_group_name())
        self.dock.notebook.detach_tab(panel)
        self.dock._add(target, panel)
        self.assertTrue(panel.tool_floating)
        self.dock.hide_panel("marine")
        self.assertTrue(self.dock.notebook.get_visible())
        self.assertTrue(self.dock.is_open("other"))

    def test_drag_group_to_edge_redocks_both_tabs_without_native_titlebar(self):
        self.dock.show_group(("marine", "other"))
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        self.dock._drag_start(
            None, SimpleNamespace(button=1, x_root=100, y_root=100), self.dock.notebook
        )
        self.dock._drag_move(None, SimpleNamespace(x_root=120, y_root=120))
        floating = self.dock.panels["marine"].get_parent()
        self.assertIs(floating, self.dock.panels["other"].get_parent())
        self.assertFalse(floating.get_toplevel().get_decorated())
        origin = self.window.get_window().get_origin()[-2:]
        offset = self.dock.translate_coordinates(self.window, 0, 0)
        right = origin[0] + offset[0] + self.dock.get_allocated_width()
        top = origin[1] + offset[1]
        self.assertTrue(self.dock._at_dock_edge(right - 10, top + 20))
        self.dock._drag_end(None, SimpleNamespace(x_root=right - 10, y_root=top + 20))
        self.assertIs(self.dock.panels["marine"].get_parent(), self.dock.notebook)
        self.assertIs(self.dock.panels["other"].get_parent(), self.dock.notebook)
        self.assertFalse(floating.get_toplevel().get_visible())
