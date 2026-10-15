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

from gweatherrouting.rendering.isochrones import (  # noqa: F401
    IsochroneFrame,
    IsochronesRenderer,
    render_isochrones,
)
from gweatherrouting.rendering.navigation import draw_current_exclusions  # noqa: F401


class IsochronesMapLayer(GObject.GObject):
    def __init__(self):
        GObject.GObject.__init__(self)
        self.renderer = IsochronesRenderer()
        self.set_isochrones([], [])

    def set_isochrones(self, isoc, path, excluded_current_samples=()):
        self.isochrones = tuple(tuple(frontier) for frontier in isoc)
        self.path = tuple(path)
        self.excluded_current_samples = tuple(dict(s) for s in excluded_current_samples)
        self.frame = IsochroneFrame(
            self.isochrones, self.path, self.excluded_current_samples
        )

    def do_draw(self, gpsmap, cr):
        charts = (
            ((self.frame,),)
            if self.isochrones or self.path or self.excluded_current_samples
            else ()
        )
        self.renderer.draw(gpsmap, cr, charts, "isochrones")

    def do_render(self, gpsmap):
        pass

    def do_busy(self):
        return False

    def do_button_press(self, gpsmap, gdkeventbutton):
        return False
