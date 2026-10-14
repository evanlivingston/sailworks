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

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GObject, Gtk

from gweatherrouting.common import resource_path
from gweatherrouting.rendering.chartcollection import ChartCollection
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from .gshhs import AskDownloadDialog, CombinedDownloadDialog


class ChartManager(GObject.GObject, ChartCollection):
    def __init__(self, settings_manager):
        GObject.GObject.__init__(self)
        ChartCollection.__init__(self, settings_manager)

    def add_map(self, m):
        ChartCollection.add_map(self, m)
        m.connect("destroy", lambda canvas: self._enc_renderer.forget(canvas))
        m.connect("destroy", lambda canvas: self._chs_renderer.forget(canvas))
        m.connect("unmap", lambda canvas: self._chs_renderer.forget(canvas))

    def load_base_chart(self, parent=None):
        self._load_base_gshhs()
        self._load_base_osm()

        gshhs_missing = self.gshhsLayer is None
        osm_missing = self.osmLayer is None

        if not gshhs_missing and not osm_missing:
            return

        def f():
            Gdk.threads_enter()
            ask_diag = AskDownloadDialog(parent, gshhs_missing, osm_missing)
            r = ask_diag.run()
            download_gshhs = ask_diag.download_gshhs
            download_osm = ask_diag.download_osm
            ask_diag.destroy()

            if r == Gtk.ResponseType.OK and (download_gshhs or download_osm):
                down_diag = CombinedDownloadDialog(parent, download_gshhs, download_osm)
                r = down_diag.run()
                down_diag.destroy()
                if r == Gtk.ResponseType.OK:
                    if download_gshhs:
                        self._load_base_gshhs()
                    if download_osm:
                        self._load_base_osm()

            if not self.gshhsLayer:
                self.charts = [
                    GDALVectorChart(
                        resource_path("gweatherrouting", "data/countries.geojson"),
                        self.settings_manager,
                    )
                ] + self.charts

            Gdk.threads_leave()

        GObject.timeout_add(10, f)
