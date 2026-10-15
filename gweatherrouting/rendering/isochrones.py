"""Shared background portrayal of expanding routing isochrones."""

import math
from dataclasses import dataclass

import cairo
import numpy as np
from pyproj import Geod

from gweatherrouting.core.mapviewport import MapPoint
from .style import Style
from .overlayrenderer import OverlayRenderer
from .navigation import draw_current_exclusions


@dataclass(frozen=True, eq=False)
class IsochroneFrame:
    isochrones: tuple
    path: tuple
    exclusions: tuple


class IsochronesRenderer(OverlayRenderer):
    def _can_preview(self, job, key):
        return job.key[:2] == key[:2]

    def _can_complete(self, job, key):
        return self._can_preview(job, key)

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
    """Project and draw every point off the UI thread; keep the same 3 km joins."""
    geod = Geod(ellps="WGS84")
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for frontier in frame.isochrones:
        if cancel.is_set():
            return
        if not frontier:
            continue
        positions = np.array([p.pos if hasattr(p, "pos") else p[:2] for p in frontier])
        xs, ys = view.convert_geographic_to_screen_batch(
            positions[:, 0], positions[:, 1]
        )
        _, _, distance = geod.inv(
            positions[:-1, 1], positions[:-1, 0], positions[1:, 1], positions[1:, 0]
        )
        cr.set_source_rgba(0, 0, 0.8, 0.5)
        cr.set_line_width(1.5)
        joined = np.flatnonzero(distance <= 3000)
        connected = np.zeros(len(frontier), dtype=bool)
        connected[joined] = connected[joined + 1] = True
        # Sparse/disconnected frontiers still show their actual search points.
        for index in np.flatnonzero(~connected):
            cr.arc(xs[index], ys[index], 1.5, 0, 2 * math.pi)
            cr.fill()
        for index in joined:
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
