"""Native tool-panel lifecycle, independent of tool content."""

import unittest
import time
from types import SimpleNamespace
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk
from gweatherrouting.gtk.widgets.tooldock import ToolDock


class TestToolDock(unittest.TestCase):
    def setUp(self):
        self.window = Gtk.Window()
        self.window.set_default_size(1000, 800)
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

    def drain(self):
        context = GLib.MainContext.default()
        # Geometry and pointer targets must include frame-clock allocations,
        # which can be scheduled just after the immediate event queue drains.
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)

    def pointer_event(self, widget, kind, x, y):
        event = Gdk.Event.new(kind)
        event.window = widget.get_window()
        event.set_device(Gdk.Display.get_default().get_default_seat().get_pointer())
        event.x_root, event.y_root = x, y
        event.x, event.y = 5, 5
        if kind == Gdk.EventType.MOTION_NOTIFY:
            event.state = Gdk.ModifierType.BUTTON1_MASK
        else:
            event.button = 1
        return widget.event(event)

    def drag_widget(self, widget, x, y):
        self.drain()
        toplevel = widget.get_toplevel()
        origin = toplevel.get_window().get_origin()[-2:]
        position = widget.translate_coordinates(toplevel, 5, 5)
        sx, sy = origin[0] + position[0], origin[1] + position[1]
        self.assertTrue(self.pointer_event(widget, Gdk.EventType.BUTTON_PRESS, sx, sy))
        self.assertTrue(widget.has_grab())
        self.assertTrue(self.pointer_event(widget, Gdk.EventType.MOTION_NOTIFY, x, y))
        self.assertTrue(widget.get_mapped())
        self.assertTrue(self.pointer_event(widget, Gdk.EventType.BUTTON_RELEASE, x, y))
        self.assertFalse(widget.has_grab())
        self.assertIsNone(self.dock._drag)
        self.drain()

    def tab_drop(self, key, target, placement):
        panel = self.dock.panels[key]
        source = panel.get_parent()
        self.drain()
        point = self.root_point(target, 0.9 if placement == "below" else 0.1)
        self.drag_widget(source.get_tab_label(panel), *point)
        return panel.get_parent()

    def root_point(self, notebook, y_fraction):
        origin = self.window.get_window().get_origin()[-2:]
        offset = notebook.translate_coordinates(self.window, 0, 0)
        return (
            origin[0] + offset[0] + 100,
            origin[1] + offset[1] + notebook.get_allocated_height() * y_fraction,
        )

    def test_tab_pointer_drag_splits_below_and_resizable_groups_survive_hide_show(self):
        self.dock.show_group(("marine", "other"))
        self.dock.notebook.set_current_page(
            self.dock.notebook.page_num(self.dock.panels["other"])
        )
        below = self.tab_drop("marine", self.dock.notebook, "below")
        self.assertEqual(self.dock._dock_layout, (self.dock.notebook, below))
        self.assertFalse(self.dock.panels["marine"].tool_floating)
        self.assertTrue(self.entry.get_mapped())
        self.assertTrue(self.dock.panels["other"].get_mapped())
        divider = self.dock.sidebar.get_children()[0]
        self.assertIsInstance(divider, Gtk.Paned)
        self.assertEqual(divider.get_orientation(), Gtk.Orientation.VERTICAL)
        divider.set_position(300)
        self.drain()
        self.assertEqual(divider.get_position(), 300)
        self.dock.hide_panel("marine")
        self.assertEqual(self.dock._dock_layout, (self.dock.notebook,))
        self.dock.show_panel("marine")
        self.drain()
        self.assertEqual(self.dock._dock_layout, (self.dock.notebook, below))
        self.assertEqual(self.entry.get_text(), "Retained settings")

    def test_tab_pointer_drag_above_then_merge_does_not_duplicate_or_hide_panels(self):
        self.dock.show_group(("marine", "other"))
        above = self.tab_drop("marine", self.dock.notebook, "above")
        self.assertEqual(self.dock._dock_layout, (above, self.dock.notebook))
        self.dock._dock_notebook(above, self.dock.notebook)
        self.drain()
        self.assertEqual(self.dock._dock_layout, (self.dock.notebook,))
        self.assertEqual(self.dock.notebook.get_n_pages(), 2)
        self.assertTrue(self.dock.is_open("marine"))
        self.assertTrue(self.dock.is_open("other"))
        self.assertEqual(self.changes, [("other", True), ("marine", True)])

    def test_floating_grip_drop_below_docks_into_a_new_group(self):
        self.dock.show_group(("marine", "other"))
        self.dock.toggle_dock("marine")
        self.drain()
        floating = self.dock.panels["marine"].get_parent()
        self.dock._drag_start(
            None, SimpleNamespace(button=1, x_root=10, y_root=10), floating
        )
        self.dock._drag_move(None, SimpleNamespace(x_root=30, y_root=30))
        x, y = self.root_point(self.dock.notebook, 0.9)
        self.dock._drag_move(None, SimpleNamespace(x_root=x, y_root=y))
        self.assertTrue(
            self.dock.notebook.get_style_context().has_class("tool-stack-below")
        )
        self.dock._drag_end(None, SimpleNamespace(x_root=x, y_root=y))
        self.drain()
        below = self.dock.panels["marine"].get_parent()
        self.assertEqual(self.dock._dock_layout, (self.dock.notebook, below))
        self.assertFalse(self.dock.panels["marine"].tool_floating)
        self.assertFalse(floating.get_toplevel().get_visible())

    def test_click_and_tiny_motion_select_tab_without_starting_a_drag(self):
        self.dock.show_group(("marine", "other"))
        self.drain()
        tab = self.dock.notebook.get_tab_label(self.dock.panels["other"])
        origin = self.window.get_window().get_origin()[-2:]
        offset = tab.translate_coordinates(self.window, 5, 5)
        x, y = origin[0] + offset[0], origin[1] + offset[1]
        self.pointer_event(tab, Gdk.EventType.BUTTON_PRESS, x, y)
        self.pointer_event(tab, Gdk.EventType.MOTION_NOTIFY, x + 2, y + 2)
        self.pointer_event(tab, Gdk.EventType.BUTTON_RELEASE, x + 2, y + 2)
        self.assertEqual(self.dock.notebook.get_n_pages(), 2)
        self.assertIs(
            self.dock.notebook.get_nth_page(self.dock.notebook.get_current_page()),
            self.dock.panels["other"],
        )
        self.assertIsNone(self.dock._drag)
        self.assertFalse(tab.has_grab())

    def test_three_groups_can_reorder_merge_and_reopen_without_losing_components(self):
        self.dock.register("third", "Third", Gtk.Entry(text="Third component"))
        self.dock.show_group(("marine", "other", "third"))
        below = self.tab_drop("marine", self.dock.notebook, "below")
        bottom = self.tab_drop("third", below, "below")
        self.assertEqual(self.dock._dock_layout, (self.dock.notebook, below, bottom))
        self.dock._dock_notebook(bottom, self.dock.notebook, "above")
        self.drain()
        top = self.dock.panels["third"].get_parent()
        self.assertEqual(self.dock._dock_layout, (top, self.dock.notebook, below))
        self.dock._dock_notebook(below, top)
        self.assertEqual(top.get_n_pages(), 2)
        self.dock._hide_notebook(top)
        self.assertFalse(self.dock.is_open("marine"))
        self.assertFalse(self.dock.is_open("third"))
        self.assertTrue(self.dock.is_open("other"))
        self.dock.show_group(("marine", "third"))
        self.drain()
        self.assertEqual(self.dock._dock_layout, (top, self.dock.notebook))
        self.assertEqual(top.get_n_pages(), 2)
        self.assertEqual(self.entry.get_text(), "Retained settings")

    def test_tab_strip_drop_reorders_and_removes_drop_feedback(self):
        self.dock.show_group(("marine", "other"))
        self.drain()
        first = self.dock.notebook.get_tab_label(self.dock.panels["other"])
        position = first.translate_coordinates(self.window, 5, 5)
        origin = self.window.get_window().get_origin()[-2:]
        source = self.dock.notebook.get_tab_label(self.dock.panels["marine"])
        self.drag_widget(source, origin[0] + position[0], origin[1] + position[1])
        self.assertIs(self.dock.notebook.get_nth_page(0), self.dock.panels["marine"])
        self.assertFalse(self.dock.get_style_context().has_class("tool-dock-drop"))
        self.assertEqual(self.dock.notebook.get_n_pages(), 2)

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
        self.assertTrue(floating.get_toplevel().get_decorated())
        self.assertIsInstance(floating.get_toplevel().get_titlebar(), Gtk.HeaderBar)
        self.dock.hide_panel("marine")
        self.window.show_all()
        self.assertFalse(self.dock.notebook.get_visible())

    def test_dragging_a_tab_out_floats_only_that_component(self):
        self.dock.show_group(("marine", "other"))
        self.drain()
        panel = self.dock.panels["marine"]
        tab = self.dock.notebook.get_tab_label(panel)
        origin = self.window.get_window().get_origin()[-2:]
        self.drag_widget(tab, origin[0] + 100, origin[1] + 200)
        self.assertTrue(panel.tool_floating)
        self.assertFalse(self.dock.panels["other"].tool_floating)
        self.dock.hide_panel("marine")
        self.assertTrue(self.dock.notebook.get_visible())
        self.assertTrue(self.dock.is_open("other"))

    def test_grip_float_then_redock_finishes_both_gestures_and_preserves_tabs(self):
        self.dock.show_group(("marine", "other"))
        self.drain()
        origin = self.window.get_window().get_origin()[-2:]
        grip = self.dock.notebook.get_action_widget(Gtk.PackType.END)
        self.drag_widget(grip, origin[0] + 100, origin[1] + 200)
        floating = self.dock.panels["marine"].get_parent()
        self.assertIs(floating, self.dock.panels["other"].get_parent())
        self.assertIsNot(floating, self.dock.notebook)
        grip = floating.get_action_widget(Gtk.PackType.END)
        offset = self.dock.translate_coordinates(self.window, 0, 0)
        right = origin[0] + offset[0] + self.dock.get_allocated_width()
        top = origin[1] + offset[1]
        self.drag_widget(grip, right - 10, top + 20)
        self.assertIs(self.dock.panels["marine"].get_parent(), self.dock.notebook)
        self.assertIs(self.dock.panels["other"].get_parent(), self.dock.notebook)
        self.assertFalse(floating.get_toplevel().get_visible())
