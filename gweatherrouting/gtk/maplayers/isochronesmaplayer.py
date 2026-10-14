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

import math
from dataclasses import dataclass

import cairo
import gi
import numpy as np
from pyproj import Geod

gi.require_version("Gtk", "3.0")

from gi.repository import GObject

from gweatherrouting.gtk.style import Style
from gweatherrouting.gtk.widgets.mapwidget import MapPoint
from gweatherrouting.gtk.widgets.overlayrenderer import OverlayRenderer


from gweatherrouting.rendering.navigation import draw_current_exclusions  # noqa: F401


@dataclass(frozen=True, eq=False)
class IsochroneFrame:
    isochrones: tuple
    path: tuple
    exclusions: tuple


class IsochronesRenderer(OverlayRenderer):
    def _can_preview(self, job, key):
        return job.key[:2] == key[:2]

    def _render(self, job):
        frame = job.charts[0][0]
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            job.viewport.width * job.factor,
            job.viewport.height * job.factor,
        )
        surface.set_device_scale(job.factor, job.factor)
        render_isochrones(job.viewport, cairo.Context(surface), frame, job.cancel)
        surface.flush()
        return surface


def render_isochrones(view, cr, frame, cancel):
    """Project and draw every point off the GTK thread; keep the same 3 km joins."""
    geod = Geod(ellps="WGS84")
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for frontier in frame.isochrones:
        if cancel.is_set():
            return
        if len(frontier) < 2:
            continue
        positions = np.array([p.pos for p in frontier])
        xs, ys = view.convert_geographic_to_screen_batch(
            positions[:, 0], positions[:, 1]
        )
        _, _, distance = geod.inv(
            positions[:-1, 1], positions[:-1, 0], positions[1:, 1], positions[1:, 0]
        )
        cr.set_source_rgba(0, 0, 0.8, 0.5)
        cr.set_line_width(1.5)
        for index in np.flatnonzero(distance <= 3000):
            cr.move_to(xs[index], ys[index])
            cr.line_to(xs[index + 1], ys[index + 1])
            cr.stroke()
    previous = None
    for point in frame.path:
        if cancel.is_set():
            return
        latitude, longitude = point.pos if hasattr(point, "pos") else point[:2]
        x, y = view.convert_geographic_to_screen(
            MapPoint.new_degrees(latitude, longitude)
        )
        if previous is not None:
            Style.Track.RoutingTrack.apply(cr)
            cr.move_to(*previous)
            cr.line_to(x, y)
            cr.stroke()
        Style.Track.RoutingTrackCircle.apply(cr)
        cr.arc(x, y, 5, 0, 2 * math.pi)
        cr.stroke()
        previous = x, y
    if not cancel.is_set():
        draw_current_exclusions(view, cr, frame.exclusions)


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
