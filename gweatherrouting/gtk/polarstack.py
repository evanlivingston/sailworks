# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

import logging
import math
import os

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import Gtk

from .polarmanagerwindow import PolarManagerPanel
from .polarperformance import PerformanceControls
from .nmeacontrols import NMEAControls
from .widgets.polar import POLAR_COLORS, PolarWidget
from .widgets.tooldock import ToolDock

logger = logging.getLogger("gweatherrouting")


class PolarStack(Gtk.Box):
    def __init__(self, parent, core, settings_manager, tool_dock=None):
        Gtk.Widget.__init__(self)
        self.polar_manager = core.polar_manager
        self.parent = parent
        self.core = core

        self.builder = Gtk.Builder()
        self.builder.add_from_file(
            os.path.abspath(os.path.dirname(__file__)) + "/polarstack.glade"
        )
        self.builder.connect_signals(self)
        content = self.builder.get_object("polarcontent")
        body = self.builder.get_object("main-paned")
        content.remove(body)
        self.body = body
        self.tool_dock = (
            tool_dock if tool_dock is not None else ToolDock(body, parent, width=320)
        )
        mount = (
            tool_dock.add_view("Polars", body)
            if tool_dock is not None
            else self.tool_dock
        )
        content.pack_start(mount, True, True, 0)
        content.reorder_child(mount, 1)
        self.pack_start(content, True, True, 0)

        self.statusBar = self.builder.get_object("statusbar")
        self.polar_list_store = self.builder.get_object("polar-list-store")
        self.polar_list = self.builder.get_object("polar-list")

        self.polars = self.polar_manager.polars
        self.polar_manager.connect("polars-list-updated", self.polars_list_updated)
        self.connect("destroy", self._destroy)

        self.polarWidget = PolarWidget(self.parent, self.core, full_circle=True)
        self.performance = PerformanceControls(self.parent, self.core, self.polarWidget)
        self.nmea = NMEAControls.attach(parent, core, self.tool_dock)
        self.manager_panel = None
        self.tool_dock.register("nmea-performance", "Performance", self.performance)
        self.tool_dock.register(
            "polar-selector",
            "Polar selector",
            self.builder.get_object("polar-selector-content"),
        )
        self.tool_dock.connect("panel-visibility-changed", self._panel_visibility)
        self.table = None

        self._populate_polar_list()
        self.builder.get_object("performance-toggle").set_active(True)
        self.builder.get_object("selector-toggle").set_active(True)

    def _destroy(self, widget):
        self.polar_manager.disconnect("polars-list-updated", self.polars_list_updated)

    def on_toggle_nmea(self, widget):
        if widget.get_active():
            self.tool_dock.show_panel("nmea")
        else:
            self.tool_dock.hide_panel("nmea")

    def on_toggle_selector(self, widget):
        if widget.get_active():
            self.tool_dock.show_panel("polar-selector")
        else:
            self.tool_dock.hide_panel("polar-selector")

    def on_toggle_performance(self, widget):
        if widget.get_active():
            self.tool_dock.show_panel("nmea-performance")
        else:
            self.tool_dock.hide_panel("nmea-performance")

    def _panel_visibility(self, dock, key, visible):
        if key == "nmea":
            self.builder.get_object("nmea-toggle").set_active(visible)
        elif key == "nmea-performance":
            self.builder.get_object("performance-toggle").set_active(visible)
        elif key == "polar-selector":
            self.builder.get_object("selector-toggle").set_active(visible)

    def _populate_polar_list(self):
        store, tree_iter = self.polar_list.get_selection().get_selected()
        selected = store.get_value(tree_iter, 0) if tree_iter is not None else None
        self.polar_list_store.clear()
        for polar in self.polars:
            meta = self.polar_manager.get_metadata(polar)
            self.polar_list_store.append(
                [
                    polar,
                    meta.get("sail", ""),
                    meta.get("name", ""),
                    meta.get("type", ""),
                ]
            )
        for row in self.polar_list_store:
            if row[0] == selected:
                self.polar_list.set_cursor(row.path)
                return
        if len(self.polar_list_store):
            self.polar_list.set_cursor(0)

    def load_polar(self, polar_file):
        polarwidgetcontainer = self.builder.get_object("polarwidgetcontainer")
        for child in polarwidgetcontainer.get_children():
            polarwidgetcontainer.remove(child)
        self.polarWidget.load_polar(polar_file)
        self.performance.polar_changed()
        polarwidgetcontainer.pack_start(self.polarWidget, True, True, 0)

        cc = self.builder.get_object("polartablecontainer")
        if self.table:
            cc.remove(self.table)
            self.table = None

        p = self.polarWidget.polar

        if p is None:
            return

        twa_step = 1
        if len(p.twa) > 20:
            twa_step = int(len(p.twa) / 10)

        self.table = Gtk.Grid()
        self.table.set_column_spacing(10)
        self.table.set_row_spacing(4)
        self.table.set_margin_start(10)
        self.table.set_margin_end(10)
        self.table.set_margin_top(10)
        self.table.set_margin_bottom(10)
        cc.pack_start(self.table, False, False, 0)

        label = Gtk.Label()
        label.set_markup("<b>TWA/TWS</b>")
        label.set_xalign(1.0)
        self.table.attach(label, 0, 0, 1, 1)

        for i, x in enumerate(p.tws):
            color = POLAR_COLORS[i % len(POLAR_COLORS)]
            hex_color = "#{:02x}{:02x}{:02x}".format(
                int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
            )
            label = Gtk.Label()
            label.set_markup(f'<b><span foreground="{hex_color}">{int(x)}</span></b>')
            label.set_xalign(1.0)
            self.table.attach(label, i + 1, 0, 1, 1)

        for row, x in enumerate(p.twa[::twa_step]):
            label = Gtk.Label()
            label.set_markup("<b>" + str(int(math.degrees(x))) + "°</b>")
            label.set_xalign(1.0)
            self.table.attach(label, 0, row + 1, 1, 1)

        for i in range(0, len(p.tws), 1):
            for j in range(0, len(p.twa), twa_step):
                if len(p.speed_table[j]) <= i:
                    continue

                label = Gtk.Label(str(p.speed_table[j][i]))
                label.set_xalign(1.0)
                self.table.attach(label, i + 1, (j // twa_step) + 1, 1, 1)

        self.body.show_all()
        self.show_all()

    def on_polar_list_select(self, selection):
        store, tree_iter = selection.get_selected()
        if tree_iter is None:
            return
        polar_name = store.get_value(tree_iter, 0)
        try:
            self.load_polar(polar_name)
            self.statusBar.push(0, "Polar loaded: " + polar_name)
        except Exception:
            logger.error("Error loading polar: %s", polar_name)
            self.statusBar.push(
                0, "Please select add/enable a polar file by using the Polar Manager"
            )

    def ensure_manager(self):
        if self.manager_panel is None:
            self.manager_panel = PolarManagerPanel(self.polar_manager, self.parent)
            self.tool_dock.register(
                "polar-manager", "Polar Manager", self.manager_panel
            )
        return self.manager_panel

    def on_polar_manager(self, event):
        self.ensure_manager()
        self.tool_dock.show_panel("polar-manager")

    def polars_list_updated(self, event):
        self.polars = self.polar_manager.polars
        self._populate_polar_list()
        if not self.polars:
            self.polarWidget.set_polar(None)
            self.performance.polar_changed()
            if self.table is not None:
                self.builder.get_object("polartablecontainer").remove(self.table)
                self.table = None
