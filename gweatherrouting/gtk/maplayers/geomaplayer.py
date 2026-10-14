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

import numpy
import dateutil.parser  # noqa: F401 - preserve existing patch/import path

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import GObject

from gweatherrouting.core import Core, TimeControl
from gweatherrouting.core import (  # noqa: F401 - preserve existing import path
    utils,
)
from gweatherrouting.gtk.style import Style
from gweatherrouting.rendering.navigation import (  # noqa: F401
    NavigationRenderer,
    _route_datetime,
)  # noqa: F401


class GeoMapLayer(GObject.GObject, NavigationRenderer):
    def __init__(self, core: Core, time_control: TimeControl):
        GObject.GObject.__init__(self)

        NavigationRenderer.__init__(self, core)
        self.time_control = time_control
        self.hl_routing = None
        self.hl_poi = None
        self.hl_track_item = None

    def highlight_routing(self, name):
        self.hl_routing = name

    def highlight_poi(self, name):
        self.hl_poi = name

    def highlight_track_item(self, index):
        self.hl_track_item = index

    def do_draw(self, gpsmap, cr):  # noqa: C901
        self._draw_log(gpsmap, cr)
        self.draw_routes(gpsmap, cr, self.time_control.time.timestamp())

        self.draw_navigation(gpsmap, cr)

    def _draw_log(self, gpsmap, cr):
        points = self.core.logManager.log
        count = len(points)
        if count < 2:
            return
        # Build from current points each frame so appends, edits and source
        # changes cannot leave stale geometry in a cache.
        lats = numpy.fromiter((p[0] for p in points), dtype=float, count=count)
        lons = numpy.fromiter((p[1] for p in points), dtype=float, count=count)
        xs, ys = gpsmap.convert_geographic_to_screen_batch(lats, lons)
        style = Style.Track.LogTrack
        style.apply(cr)
        padding = (style.line_width or 1) / 2 + 2
        width, height = gpsmap.get_allocated_width(), gpsmap.get_allocated_height()
        # Reject only segments wholly beyond the same edge. Crossing segments
        # remain visible even when neither endpoint lies inside the viewport.
        visible = (
            (numpy.maximum(xs[:-1], xs[1:]) >= -padding)
            & (numpy.minimum(xs[:-1], xs[1:]) <= width + padding)
            & (numpy.maximum(ys[:-1], ys[1:]) >= -padding)
            & (numpy.minimum(ys[:-1], ys[1:]) <= height + padding)
        )
        for index in numpy.flatnonzero(visible):
            # Keep separate strokes, preserving alpha overlap, caps and dashes.
            cr.move_to(xs[index], ys[index])
            cr.line_to(xs[index + 1], ys[index + 1])
            cr.stroke()

    def do_render(self, gpsmap):
        pass

    def do_busy(self):
        return False

    def do_button_press(self, gpsmap, gdkeventbutton):
        return False
