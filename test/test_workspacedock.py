"""The visible sidebar and its native layout follow Charts, Logs and Polars."""

import unittest
import time
from types import SimpleNamespace

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk

from gweatherrouting.core.utils import EventDispatcher
from gweatherrouting.gtk.nmeacontrols import NMEAControls
from gweatherrouting.gtk.widgets.workspacedock import WorkspaceDock


class TestWorkspaceDock(unittest.TestCase):
    def setUp(self):
        self.window = Gtk.Window()
        self.window.set_default_size(1100, 850)
        self.tabs = Gtk.Stack()
        self.window.add(self.tabs)
        self.dock = WorkspaceDock(self.window)
        self.toolbars = {}
        self.bodies = {}
        self.hidden_control = Gtk.Button(label="Only visible during routing")
        for name in ("Charts", "Logs", "Polars"):
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            toolbar = Gtk.Label(
                label=name + " toolbar",
                height_request={"Charts": 40, "Logs": 82, "Polars": 90}[name],
            )
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            body.pack_start(Gtk.Entry(text=name + " main content"), False, False, 0)
            if name == "Charts":
                body.pack_start(self.hidden_control, False, False, 0)
            page.pack_start(toolbar, False, False, 0)
            page.pack_start(self.dock.add_view(name, body), True, True, 0)
            page.pack_start(Gtk.Label(label="Status"), False, False, 0)
            body.show_all()
            self.toolbars[name], self.bodies[name] = toolbar, body
            self.tabs.add_named(page, name)
        self.entry = Gtk.Entry(text="NMEA settings")
        self.dock.register("nmea", "NMEA", self.entry)
        self.dock.register("performance", "Performance", Gtk.Label(label="Filters"))
        self.dock.register("currents", "Currents", Gtk.Label(label="Currents"))
        self.dock.bind(self.tabs)
        self.window.show_all()
        self.hidden_control.hide()
        self.addCleanup(self.window.destroy)
        self.drain()

    @staticmethod
    def drain():
        context = GLib.MainContext.default()
        # GTK frame-clock allocations may be scheduled after the immediate
        # event queue is drained; wait for native layout to settle as well.
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)

    def test_sidebar_groups_selection_width_and_dividers_follow_active_tab(self):
        dock = self.dock
        dock.show_group(("nmea", "performance", "currents"))
        below = dock._split_group(dock.notebook, "below")
        dock._move_panel(dock.panels["performance"], below)
        dock.notebook.set_current_page(dock.notebook.page_num(dock.panels["nmea"]))
        self.drain()
        dock.set_position(700)
        divider = dock.sidebar.get_children()[0]
        divider.set_position(260)
        self.drain()
        sidebar = dock.sidebar
        width = sidebar.get_allocated_width()
        selected = dock.notebook.get_current_page()
        self.entry.set_text("Retained across tabs")
        for name in ("Logs", "Polars", "Charts"):
            self.tabs.set_visible_child_name(name)
            self.drain()
            self.assertIs(dock.get_parent(), dock.mounts[name])
            self.assertIs(dock.views.get_visible_child(), self.bodies[name])
            self.assertIs(dock.sidebar, sidebar)
            self.assertTrue(sidebar.get_mapped())
            self.assertTrue(self.entry.get_mapped())
            self.assertTrue(dock.panels["performance"].get_mapped())
            self.assertEqual(dock._dock_layout, (dock.notebook, below))
            self.assertIs(dock.sidebar.get_children()[0], divider)
            self.assertEqual(divider.get_position(), 260)
            self.assertEqual(dock.notebook.get_current_page(), selected)
            self.assertAlmostEqual(sidebar.get_allocated_width(), width, delta=2)
            self.assertEqual(self.entry.get_text(), "Retained across tabs")
            toolbar = self.toolbars[name]
            bottom = toolbar.translate_coordinates(
                self.window, 0, toolbar.get_allocated_height()
            )[1]
            self.assertGreaterEqual(
                sidebar.translate_coordinates(self.window, 0, 0)[1], bottom
            )
            self.assertFalse(self.hidden_control.get_visible())

    def test_floating_group_and_hidden_components_stay_in_place_across_tabs(self):
        dock = self.dock
        dock.show_group(("nmea", "performance"))
        dock.toggle_dock("nmea")
        floating = dock.panels["nmea"].get_toplevel()
        dock.hide_panel("performance")
        self.drain()
        for name in ("Logs", "Polars", "Charts"):
            self.tabs.set_visible_child_name(name)
            self.drain()
            self.assertIs(dock.panels["nmea"].get_toplevel(), floating)
            self.assertTrue(floating.get_visible())
            self.assertTrue(self.entry.get_mapped())
            self.assertFalse(dock.is_open("performance"))
            self.assertFalse(dock.sidebar.get_visible())
        dock.show_panel("performance")
        dock.toggle_dock("nmea")
        self.drain()
        self.assertTrue(dock.sidebar.get_mapped())
        self.assertIs(dock.panels["nmea"].get_parent(), dock.notebook)

    def test_nmea_is_one_component_in_the_shared_dock(self):
        core = SimpleNamespace(connectionManager=EventDispatcher())
        # Use an unused dock, avoiding the dummy NMEA entry from setUp.
        dock = WorkspaceDock(self.window)
        self.addCleanup(dock.destroy)
        views = [NMEAControls.attach(self.window, core, dock) for _ in range(3)]
        self.addCleanup(core.nmea_session.close)
        self.assertTrue(all(view is views[0] for view in views))
        self.assertEqual(len(dock.panels), 1)
        views[0].live.set_active(False)
        self.assertFalse(core.nmea_session.streaming)

    def test_layout_roundtrip_restores_groups_hidden_floating_home_and_splits(self):
        import json
        from gweatherrouting.gtk.widgets.tooldock import ToolDock

        dock = self.dock
        dock.register("hidden", "Hidden", Gtk.Entry())
        dock.show_group(("nmea", "performance", "currents"))
        below = dock._split_group(dock.notebook, "below")
        dock._move_panel(dock.panels["performance"], below)
        dock.toggle_dock("currents")
        floating = dock.panels["currents"].get_parent()
        dock._move_panel(dock.panels["hidden"], floating)
        dock.hide_panel("hidden")
        self.drain()
        dock.sidebar.get_children()[0].set_position(260)
        self.drain()
        state = json.loads(json.dumps(dock.save_state()))
        state["groups"][0]["tabs"].append("removed-component")
        restored_window = Gtk.Window()
        restored_window.set_default_size(1100, 850)
        restored = ToolDock(Gtk.Label(label="Restored"), restored_window)
        restored_window.add(restored)
        self.addCleanup(restored_window.destroy)
        for key in ("nmea", "performance", "currents", "hidden"):
            restored.register(key, key, Gtk.Entry())
        restored.restore_state(state, [(0, 0, 3000, 2000)])
        restored_window.show_all()
        self.drain()
        self.assertEqual(len(restored._dock_layout), 2)
        self.assertEqual(restored.sidebar.get_children()[0].get_position(), 260)
        self.assertTrue(restored.is_open("nmea"))
        self.assertTrue(restored.is_open("performance"))
        self.assertTrue(restored.is_open("currents"))
        self.assertFalse(restored.is_open("hidden"))
        self.assertTrue(restored.panels["currents"].tool_floating)
        self.assertIs(
            restored.panels["hidden"].tool_home,
            restored.panels["currents"].get_parent(),
        )
        restored.show_panel("hidden")
        self.assertTrue(restored.panels["hidden"].tool_floating)
        self.assertEqual(len(restored.floating), 1)
