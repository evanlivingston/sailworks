"""Shared background portrayal of expanding routing isochrones."""

import math
from dataclasses import dataclass

import cairo
import numpy as np
from sailworks.core.isochrones import (
    ContourRun,
    IsochroneContours,
    IsochroneSlice,
    hit_test_contours,
)
from sailworks.core.mapviewport import MapPoint
from .style import Style
from .overlayrenderer import OverlayRenderer
from .navigation import draw_current_exclusions


@dataclass(frozen=True, eq=False)
class IsochroneFrame:
    isochrones: tuple
    path: tuple
    exclusions: tuple
    contours: object = None
    selected: object = None


def _contours(frame):
    value = getattr(frame, "contours", None)
    if value is None:
        return None
    return IsochroneContours.from_json(value)


def _visible_contours(frame):
    """Show the same contour steps during routing and after saving a route."""
    contours = _contours(frame)
    if contours is None:
        return None
    return contours.displayed()


def _authoritative(frame):
    """Return the displayed sidecar, including an unavailable one."""
    return _visible_contours(frame)


def _draw_contour_run(cr, view, run, highlight=False):
    if not run.points:
        return
    positions = np.asarray(run.points, dtype=float)
    xs, ys = view.convert_geographic_to_screen_batch(positions[:, 0], positions[:, 1])
    if highlight:
        cr.set_source_rgba(0.95, 0.35, 0.1, 0.95)
        cr.set_line_width(3.0)
    else:
        cr.set_source_rgba(0, 0, 0.8, 0.5)
        cr.set_line_width(1.5)
    if len(xs) == 1:
        cr.arc(xs[0], ys[0], 2.0 if highlight else 1.5, 0, 2 * math.pi)
        cr.fill()
        return
    edges = len(xs) if run.closed else len(xs) - 1
    for index in range(edges):
        cr.move_to(xs[index], ys[index])
        cr.line_to(xs[(index + 1) % len(xs)], ys[(index + 1) % len(xs)])
    cr.stroke()


def _selected_key(selected):
    if selected is None:
        return None
    if isinstance(selected, dict):
        return selected.get("leg_id"), selected.get("step")
    return getattr(selected, "leg_id", None), getattr(selected, "step", None)


def hit_test_isochrone_frame(frame, latitude, longitude, tolerance_m=2500.0):
    """Hit-test exactly the edges a frame renders, never across a break."""
    contours = _visible_contours(frame)
    if contours is not None and any(run.points for run in contours.runs):
        return hit_test_contours(contours, latitude, longitude, tolerance_m)
    if contours is not None:
        # An unavailable sidecar is displayed as point samples, never joined.
        slices = []
        for step, frontier in enumerate(getattr(frame, "isochrones", ()) or ()):
            runs = tuple(
                ContourRun((tuple(point.pos if hasattr(point, "pos") else point[:2]),))
                for point in frontier
            )
            if runs:
                slices.append(IsochroneSlice(0, step, "", runs, "unavailable"))
        if not slices:
            return None
        return hit_test_contours(
            IsochroneContours(tuple(slices), representation="unavailable"),
            latitude,
            longitude,
            tolerance_m,
        )
    # Legacy routes are displayed as point samples only; test those points.
    slices = []
    for step, frontier in enumerate(getattr(frame, "isochrones", ()) or ()):
        runs = []
        for point in frontier or ():
            position = point.pos if hasattr(point, "pos") else point[:2]
            runs.append(ContourRun((tuple(position),)))
        if runs:
            slices.append(IsochroneSlice(0, step, "", tuple(runs), "legacy-frontier"))
    if not slices:
        return None
    return hit_test_contours(
        IsochroneContours(tuple(slices), representation="legacy-frontier"),
        latitude,
        longitude,
        tolerance_m,
    )


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
    """Draw authoritative contour runs, or legacy point samples as a fallback."""
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    contours = _authoritative(frame)
    if contours is not None:
        selected = _selected_key(getattr(frame, "selected", None))
        has_runs = any(run.points for run in contours.runs)
        for item in contours.slices:
            if cancel.is_set():
                return
            highlight_slice = selected == (item.leg_id, item.step)
            for run in item.runs:
                _draw_contour_run(cr, view, run, highlight=highlight_slice)
        if not has_runs:
            # Unavailable sidecars stay point samples: never reconstruct joins.
            cr.set_source_rgba(0, 0, 0.8, 0.5)
            cr.set_line_width(1.5)
            for frontier in frame.isochrones:
                if cancel.is_set():
                    return
                for point in frontier:
                    latitude, longitude = (
                        point.pos if hasattr(point, "pos") else point[:2]
                    )
                    x, y = view.convert_geographic_to_screen(
                        MapPoint.new_degrees(latitude, longitude)
                    )
                    cr.arc(x, y, 1.5, 0, 2 * math.pi)
                    cr.fill()
    else:
        # Legacy routes have no ordered contour evidence.  Keep their actual
        # search points and never join them with a distance heuristic.
        cr.set_source_rgba(0, 0, 0.8, 0.5)
        cr.set_line_width(1.5)
        for frontier in frame.isochrones:
            if cancel.is_set():
                return
            if not frontier:
                continue
            positions = np.array(
                [p.pos if hasattr(p, "pos") else p[:2] for p in frontier]
            )
            xs, ys = view.convert_geographic_to_screen_batch(
                positions[:, 0], positions[:, 1]
            )
            for x, y in zip(xs, ys):
                cr.arc(x, y, 1.5, 0, 2 * math.pi)
                cr.fill()
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


def _contour_overlay_data(contours, selected=None):
    selected = _selected_key(selected)
    runs = []
    slices = []
    for item in contours.slices:
        slice_runs = []
        for run_index, run in enumerate(item.runs):
            if not run.points:
                continue
            entry = {
                "points": [[latitude, longitude] for latitude, longitude in run.points],
                "closed": bool(run.closed),
                "leg_id": item.leg_id,
                "step": item.step,
                "valid_time": item.valid_time,
                "highlight": selected == (item.leg_id, item.step),
            }
            runs.append(entry)
            slice_runs.append(entry)
        slices.append(
            {
                "leg_id": item.leg_id,
                "step": item.step,
                "valid_time": item.valid_time,
                "representation": item.representation,
                "runs": slice_runs,
            }
        )
    return {"runs": runs, "slices": slices}


def isochrone_overlay_data(frame):
    """Serialise isochrones for the QML overlay.

    Authoritative routes provide ordered runs directly.  Legacy routes keep
    their existing point samples, without a reconstructed topology.
    """
    contours = _authoritative(frame)
    frontiers = []
    if contours is not None and not any(run.points for run in contours.runs):
        # Unavailable sidecars are point samples, not approximate joins.
        for frontier in frame.isochrones:
            positions = np.array(
                [p.pos if hasattr(p, "pos") else p[:2] for p in frontier], dtype=float
            )
            if len(positions):
                frontiers.append({"segments": [], "points": positions.tolist()})
    elif contours is None:
        # Legacy routes have no ordered evidence: point samples only.
        for frontier in frame.isochrones:
            if not frontier:
                continue
            positions = np.array(
                [p.pos if hasattr(p, "pos") else p[:2] for p in frontier], dtype=float
            )
            frontiers.append({"segments": [], "points": positions.tolist()})
    path = [
        list(point.pos if hasattr(point, "pos") else point[:2]) for point in frame.path
    ]
    exclusions = [
        [float(sample["latitude"]), float(sample["longitude"])]
        for sample in frame.exclusions
    ]
    data = {"frontiers": frontiers, "path": path, "exclusions": exclusions}
    if contours is not None:
        data["contours"] = _contour_overlay_data(
            contours, getattr(frame, "selected", None)
        )
    return data
