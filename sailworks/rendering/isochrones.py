"""Shared background portrayal of expanding routing isochrones."""

import math
from dataclasses import dataclass

import cairo
import numpy as np
from pyproj import Geod

from sailworks.core.mapviewport import MapPoint
from .canvas import Canvas, CanvasImage, JOIN_ROUND
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
        image = CanvasImage.new(job.viewport.width, job.viewport.height, job.factor)
        canvas = Canvas.for_image(image)
        render_isochrones(job.viewport, canvas, frame, job.cancel)
        canvas.finish()
        return image


def render_isochrones(view, canvas, frame, cancel):
    """Project and draw every point off the UI thread; keep the same 3 km joins."""
    geod = Geod(ellps="WGS84")
    canvas.set_line_join(JOIN_ROUND)
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
        canvas.set_source_rgba(0, 0, 0.8, 0.5)
        canvas.set_line_width(1.5)
        joined = np.flatnonzero(distance <= 3000)
        connected = np.zeros(len(frontier), dtype=bool)
        connected[joined] = connected[joined + 1] = True
        # Sparse/disconnected frontiers still show their actual search points.
        for index in np.flatnonzero(~connected):
            canvas.arc(xs[index], ys[index], 1.5, 0, 2 * math.pi)
            canvas.fill()
        for index in joined:
            canvas.move_to(xs[index], ys[index])
            canvas.line_to(xs[index + 1], ys[index + 1])
            canvas.stroke()
    previous = None
    for point in frame.path:
        if cancel.is_set():
            return
        latitude, longitude = point.pos if hasattr(point, "pos") else point[:2]
        x, y = view.convert_geographic_to_screen(
            MapPoint.new_degrees(latitude, longitude)
        )
        if previous is not None:
            Style.Track.RoutingTrack.apply_canvas(canvas)
            canvas.move_to(*previous)
            canvas.line_to(x, y)
            canvas.stroke()
        Style.Track.RoutingTrackCircle.apply_canvas(canvas)
        canvas.arc(x, y, 5, 0, 2 * math.pi)
        canvas.stroke()
        previous = x, y
    if not cancel.is_set():
        draw_current_exclusions(view, canvas, frame.exclusions)


def isochrone_overlay_data(frame):
    """Serialise isochrones for the QML overlay, preserving the 3 km joins."""
    geod = Geod(ellps="WGS84")
    frontiers = []
    for frontier in frame.isochrones:
        if not frontier:
            continue
        positions = np.array(
            [p.pos if hasattr(p, "pos") else p[:2] for p in frontier], dtype=float
        )
        if len(positions) < 2:
            frontiers.append({"segments": [], "points": positions.tolist()})
            continue
        _, _, distance = geod.inv(
            positions[:-1, 1],
            positions[:-1, 0],
            positions[1:, 1],
            positions[1:, 0],
        )
        joined = np.flatnonzero(distance <= 3000)
        connected = np.zeros(len(positions), dtype=bool)
        connected[joined] = connected[joined + 1] = True
        frontiers.append(
            {
                "segments": [
                    [positions[index].tolist(), positions[index + 1].tolist()]
                    for index in joined
                ],
                "points": positions[~connected].tolist(),
            }
        )
    path = [
        list(point.pos if hasattr(point, "pos") else point[:2]) for point in frame.path
    ]
    exclusions = [
        [float(sample["latitude"]), float(sample["longitude"])]
        for sample in frame.exclusions
    ]
    return {"frontiers": frontiers, "path": path, "exclusions": exclusions}
