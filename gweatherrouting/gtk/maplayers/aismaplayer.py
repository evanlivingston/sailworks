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

from gi.repository import GLib, GObject

from gweatherrouting.rendering.ais import (  # noqa: F401
    AISRenderer,
    MARKER_SIZE,
    LABEL_FONT_SIZE,
    LABEL_MIN_DIST_SQ,
)  # noqa: F401


class AISMapLayer(GObject.GObject, AISRenderer):
    def __init__(self, core):
        GObject.GObject.__init__(self)
        self.visible = True
        self.ais_manager = core.ais_manager
        self._gpsmap = None
        GLib.timeout_add(3000, self._refresh)

    def set_visible(self, visible):
        self.visible = visible

    def _refresh(self):
        if self._gpsmap is not None and self.visible:
            self._gpsmap.queue_draw()
        return True

    def do_draw(self, gpsmap, cr):
        self._gpsmap = gpsmap
        if not self.visible:
            return

        targets = self.ais_manager.get_active_targets()
        self.draw_targets(gpsmap, cr, targets)

    def do_render(self, gpsmap):
        pass

    def do_busy(self):
        return False

    def do_button_press(self, gpsmap, gdkeventbutton):
        return False
