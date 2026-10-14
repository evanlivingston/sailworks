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
from gi.repository import GObject

from gweatherrouting.rendering.windpainter import WindPainter  # noqa: F401
from gweatherrouting.rendering.windpainter import wind_on_ground  # noqa: F401
from .windrenderer import WindOverlayRenderer


class GribMapLayer(GObject.GObject):
    def __init__(self, grib_manager, time_control, settings_manager):
        GObject.GObject.__init__(self)
        self.visible = True
        self.grib_manager = grib_manager
        self.time_control = time_control
        self.settings_manager = settings_manager
        self._renderer = WindOverlayRenderer(WindPainter)
        self._maps = set()
        self.time_control.connect("time_change", self.on_time_change)

    def set_visible(self, visible):
        self.visible = visible
        if not visible:
            for canvas in self._maps:
                self._renderer.forget(canvas)
        for canvas in self._maps:
            canvas.queue_draw()

    def on_time_change(self, *args):
        for canvas in self._maps:
            canvas.queue_draw()

    def _forget(self, canvas):
        self._renderer.forget(canvas)
        self._maps.discard(canvas)

    def do_draw(self, gpsmap, cr):
        if not self.visible:
            return
        if gpsmap not in self._maps:
            self._maps.add(gpsmap)
            gpsmap.connect("destroy", self._forget)
            gpsmap.connect("unmap", lambda canvas: self._renderer.forget(canvas))
        # All values are captured on the UI thread; workers have private GDAL
        # handles and never consult live widgets, settings, or the routing GRIB.
        style = (
            self.time_control.time,
            self.settings_manager.gribArrowOpacity,
            self.settings_manager.gribArrowOnGround,
            getattr(self.settings_manager, "gribIsobarsVisible", True),
        )
        sources = tuple((grib,) for grib in self.grib_manager.gribs)
        self._renderer.draw(gpsmap, cr, sources, style)

    def do_render(self, gpsmap):
        pass

    def do_busy(self):
        return False

    def do_button_press(self, gpsmap, gdkeventbutton):
        return False
