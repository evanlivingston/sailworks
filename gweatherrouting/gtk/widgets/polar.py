# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""GTK adapter for the shared production polar renderer.

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
from gi.repository import Gdk, Gtk

from gweatherrouting.rendering.polar import PolarRenderer, POLAR_COLORS  # noqa: F401


class PolarWidget(Gtk.DrawingArea, PolarRenderer):
    def __init__(self, parent, core, full_circle=False):
        Gtk.DrawingArea.__init__(self)
        self.par = parent
        self.init_polar_renderer(core.polar_manager, full_circle)
        self.set_size_request(60, 180)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.connect("draw", self.draw)
        self.connect("motion-notify-event", self.on_mouse_move)
        self.connect("leave-notify-event", self.on_mouse_leave)
        self.connect("unmap", self.on_mouse_leave)
        self.add_events(
            Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )

    def draw(self, area, cr):
        allocation = self.get_allocation()
        self.draw_polar(cr, allocation.width, allocation.height)
