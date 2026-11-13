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

"""Shared track and POI portrayal for the Qt CPU fallback, in logical map pixels."""

import bisect
import math
import random
from functools import lru_cache

import dateutil.parser
from sailworks.core import utils

from sailworks.core.mapviewport import MapPoint
from .style import Style, route_speed_color as _speed_color


def draw_current_exclusions(gpsmap, cr, samples):
    """Mark sampled rejection locations, without claiming a surveyed gap polygon."""
    if not samples:
        return
    cr.save()
    drawn = set()
    for sample in samples:
        x, y = gpsmap.convert_geographic_to_screen(
            MapPoint.new_degrees(sample["latitude"], sample["longitude"])
        )
        key = (round(x / 4), round(y / 4))
        if key in drawn:
            continue
        drawn.add(key)
        cr.set_source_rgba(0.9, 0.05, 0.05, 0.18)
        cr.arc(x, y, 7, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.85, 0.02, 0.02, 0.9)
        cr.set_line_width(1.5)
        cr.move_to(x - 3, y - 3)
        cr.line_to(x + 3, y + 3)
        cr.move_to(x - 3, y + 3)
        cr.line_to(x + 3, y - 3)
        cr.stroke()
    cr.restore()


@lru_cache(maxsize=16384)
def _route_datetime(value):
    # Cache parsing, keeping timestamp() conversion at draw time so legacy
    # naive route times still follow the process's local timezone.
    return dateutil.parser.parse(value)


def routing_detail_fields(r):
    """Ordered (label, value) figures for a routing, highest priority first.

    Distance, duration and average speed are the same display strings the
    routing summary uses, so the tooltip and the scene tree cannot disagree.
    A route with too few complete points has no fields.

    Saved routes may carry partial points (for example a two-value coordinate
    without time or speed). They are skipped rather than allowed to crash the
    navigation panel; a route with too few complete points has no summary.

    Average speed is ground speed over the whole route when usable timestamps
    exist, so it agrees with the displayed distance and duration. Only when
    they do not is it the mean of the per-leg speeds that are present; missing
    speeds are absent, not zero. That fallback is labelled because it can
    disagree with the distance and duration figures.
    """
    points = [point for point in r if len(point) >= 6]
    if len(points) < 2:
        return []
    total_dist = 0.0
    total_speed = 0.0
    speed_count = 0
    for j in range(1, len(points)):
        total_dist += utils.point_distance(
            points[j - 1][0], points[j - 1][1], points[j][0], points[j][1]
        )
        speed = points[j][5]
        if speed:
            total_speed += speed
            speed_count += 1
    total_dist_nm = total_dist
    dur_str = None
    avg_speed = None
    leg_mean = False
    try:
        t_start = dateutil.parser.parse(points[0][2])
        t_end = dateutil.parser.parse(points[-1][2])
        hours = (t_end - t_start).total_seconds() / 3600.0
        if hours > 0:
            minutes = int(round(hours * 60))
            days, minutes = divmod(minutes, 24 * 60)
            hours_part, minutes_part = divmod(minutes, 60)
            if days > 0:
                dur_str = f"{days}d {hours_part}h {minutes_part:02d}m"
            else:
                dur_str = f"{hours_part}h {minutes_part:02d}m"
            avg_speed = total_dist / hours
    except Exception:
        pass
    if avg_speed is None and speed_count:
        avg_speed = total_speed / speed_count
        leg_mean = True
    fields = [("distance", "%.0f nm" % total_dist_nm)]
    if dur_str:
        fields.append(("duration", dur_str))
    if avg_speed is not None:
        avg_str = "avg %.1f kts" % avg_speed
        if leg_mean:
            avg_str += " (leg mean)"
        fields.append(("speed", avg_str))
    return fields


def track_detail_fields(points):
    """Ordered (label, value) detail fields for a track row.

    Mark count first and total distance second, so a narrow scene tree keeps
    the count. Distance sums the same great-circle legs the routing figures
    use.
    """
    points = list(points)
    distance = 0.0
    for index in range(1, len(points)):
        distance += utils.point_distance(
            points[index - 1][0],
            points[index - 1][1],
            points[index][0],
            points[index][1],
        )
    count = len(points)
    return [
        ("marks", f"{count} mark" if count == 1 else f"{count} marks"),
        ("distance", "%.1f nm" % distance),
    ]


def _routing_source_note(r):
    """Provenance note appended to a routing summary, or an empty string."""
    provenance = r.provenance or {}
    source_note = ""
    if provenance.get("routing") == "isochrone-with-currents":
        currents = provenance["currents"]
        selected = currents["selected_sources"]
        used = set(currents["actual_sources"])
        models = sorted({s["model"] for s in selected if s["dataset_id"] in used})
        source_note = " | currents: " + ", ".join(models) + " · coverage checked"
        resolutions = {s["dataset_id"]: s.get("resolution_m") for s in selected}
        finest = min(
            (r for r in (s.get("resolution_m") for s in selected) if r is not None),
            default=None,
        )
        if finest is not None and any(
            resolutions.get(i, finest) > finest for i in used
        ):
            source_note += " · incl. fallback"
    elif provenance.get("routing") == "wind-only":
        source_note = " | wind only"
    return source_note


def routing_summary(r):
    """Build a short summary string for a routing.

    The figures come from ``routing_detail_fields`` so the tooltip and the
    scene tree show the same distance, duration and average speed. A route
    with too few complete points has no summary.
    """
    fields = routing_detail_fields(r)
    if not fields:
        return ""
    return " | ".join(value for _, value in fields) + _routing_source_note(r)


class NavigationRenderer:
    def __init__(self, core):
        self.core = core
        self.hl_routing = None
        self.hl_poi = None
        self.hl_track_item = None
        self.edit_track = None
        self._route_geometry = {}

    def draw_routes(self, gpsmap, cr, display_time, draw_routes=True):  # noqa: C901
        # Legacy route times and the map clock are local and naive; newer
        # routes carry UTC offsets. Compare instants without discarding offsets.

        if draw_routes:
            for tr in self.core.routingManager:
                if not tr.visible:
                    continue

                highlight = tr.name == self.hl_routing
                if highlight:
                    draw_current_exclusions(
                        gpsmap,
                        cr,
                        (tr.provenance or {}).get("excluded_current_samples", ()),
                    )

                prevx = None
                prevy = None
                prevp = None
                points_list = [point for point in tr if len(point) >= 7]

                for i, p in enumerate(points_list):
                    x, y = gpsmap.convert_geographic_to_screen(
                        MapPoint.new_degrees(p[0], p[1])
                    )

                    # Route name label at start with background
                    if prevp is None and highlight:
                        cr.set_font_size(10)
                        ext = cr.text_extents(tr.name)
                        cr.set_source_rgba(0.0, 0.0, 0.0, 0.6)
                        cr.rectangle(x + 8, y - 12, ext.width + 8, 16)
                        cr.fill()
                        cr.set_source_rgba(1.0, 1.0, 1.0, 0.9)
                        cr.move_to(x + 12, y)
                        cr.show_text(tr.name)
                        cr.stroke()
                    elif prevp is None:
                        Style.Track.RoutingTrackFont.apply(cr)
                        cr.move_to(x + 10, y)
                        cr.show_text(tr.name)
                        cr.stroke()

                    # Interpolated boat position on route. A point without a
                    # recorded heading still draws, but cannot place the boat.
                    if prevp is not None and p[6] is not None:
                        tprev = _route_datetime(prevp[2]).timestamp()
                        tcurr = _route_datetime(p[2]).timestamp()

                        if tprev < display_time <= tcurr:
                            dt = tcurr - tprev
                            dl = (
                                utils.point_distance(prevp[0], prevp[1], p[0], p[1])
                                / dt
                                * (display_time - tprev)
                            )

                            rp = utils.routage_point_distance(
                                prevp[0], prevp[1], dl, math.radians(p[6])
                            )

                            xx, yy = gpsmap.convert_geographic_to_screen(
                                MapPoint.new_degrees(rp[0], rp[1])
                            )
                            # Boat marker: filled circle with border
                            cr.set_source_rgba(0.1, 0.6, 0.1, 1.0)
                            cr.arc(xx, yy, 8, 0, 2 * math.pi)
                            cr.fill()
                            cr.set_source_rgba(1.0, 1.0, 1.0, 0.9)
                            cr.set_line_width(1.5)
                            cr.arc(xx, yy, 8, 0, 2 * math.pi)
                            cr.stroke()

                    # Draw segment line colored by boat speed
                    if prevx is not None and prevy is not None:
                        speed = p[5] if p[5] is not None else 0
                        lw = 3.0 if highlight else 2.0

                        r, g, b, a = _speed_color(speed)
                        cr.set_source_rgba(r, g, b, a if highlight else a * 0.7)
                        cr.set_line_width(lw)
                        cr.move_to(prevx, prevy)
                        cr.line_to(x, y)
                        cr.stroke()

                    # Waypoint markers
                    speed = p[5] if p[5] is not None else 0
                    r, g, b, a = _speed_color(speed)
                    radius = 3 if highlight else 2

                    cr.set_source_rgba(r, g, b, 1.0)
                    cr.arc(x, y, radius, 0, 2 * math.pi)
                    cr.fill()

                    prevx = x
                    prevy = y
                    prevp = p

    def _route_overlay_geometry(self, route):
        """Static route geometry, cached until ``route.revision`` changes."""
        key = (id(route), route.revision)
        cached = self._route_geometry.get(key)
        if cached is not None and cached[0] is route:
            return cached[1]
        points = []
        rows = []
        for p in route:
            if len(p) < 7:
                continue
            lat, lon = float(p[0]), float(p[1])
            speed = float(p[5] if p[5] is not None else 0)
            stamp = _route_datetime(p[2]).timestamp()
            heading = None if p[6] is None else math.radians(p[6])
            rows.append((lat, lon, speed, stamp, heading))
            points.append([lat, lon, speed])
        geometry = (points, tuple(rows), tuple(row[3] for row in rows))
        self._route_geometry[key] = (route, geometry)
        return geometry

    def route_overlay_data(self, display_time):
        """Route geometry for a QML overlay, including the interpolated boat."""
        routes = []
        live = set()
        for tr in self.core.routingManager:
            if not tr.visible:
                continue
            key = (id(tr), tr.revision)
            live.add(key)
            points, rows, stamps = self._route_overlay_geometry(tr)
            boat = None
            index = bisect.bisect_left(stamps, display_time)
            if 0 < index < len(rows):
                prev = rows[index - 1]
                curr = rows[index]
                dt = curr[3] - prev[3]
                if dt > 0 and prev[4] is not None and curr[4] is not None:
                    dl = (
                        utils.point_distance(prev[0], prev[1], curr[0], curr[1])
                        / dt
                        * (display_time - prev[3])
                    )
                    rp = utils.routage_point_distance(prev[0], prev[1], dl, curr[4])
                    boat = [float(rp[0]), float(rp[1])]
            routes.append(
                {
                    "name": str(tr.name),
                    "points": points,
                    "highlight": tr.name == self.hl_routing,
                    "boat": boat,
                }
            )
        for stale in tuple(self._route_geometry):
            if stale not in live:
                del self._route_geometry[stale]
        return routes

    def draw_navigation(self, gpsmap, cr, draw_tracks=True):  # noqa: C901
        if draw_tracks:
            for tr in self.core.trackManager:
                if not tr.visible:
                    continue
                # Race tracks are drawn separately (and cached) so scrubbing does
                # not redraw their geometry; skip them here, including their
                # per-fix number labels.
                if getattr(tr, "race_track", False):
                    continue

                active = self.core.trackManager.is_active(tr)

                prevx = None
                prevy = None
                i = 0

                for p in tr:
                    i += 1
                    x, y = gpsmap.convert_geographic_to_screen(
                        MapPoint.new_degrees(p[0], p[1])
                    )

                    item_hl = active and self.hl_track_item == (i - 1)

                    if prevx is None:
                        if active:
                            Style.Track.TrackActiveFont.apply(cr)
                        else:
                            Style.Track.TrackInactiveFont.apply(cr)

                        cr.move_to(x - 10, y - 10)
                        cr.show_text(tr.name)
                        cr.stroke()

                    if item_hl:
                        Style.Track.TrackItemHLFont.apply(cr)
                    elif active:
                        Style.Track.TrackActivePoiFont.apply(cr)
                    else:
                        Style.Track.TrackInactivePoiFont.apply(cr)

                    cr.move_to(x - 4, y + 18)
                    cr.show_text(str(i))
                    cr.stroke()

                    if prevx is not None and prevy is not None:
                        if active:
                            Style.Track.TrackActive.apply(cr)
                        else:
                            Style.Track.TrackInactive.apply(cr)

                        cr.move_to(prevx, prevy)
                        cr.line_to(x, y)
                        cr.stroke()

                    if item_hl:
                        Style.Track.TrackItemHL.apply(cr)
                    elif active:
                        Style.Track.TrackActive.apply(cr)
                    else:
                        Style.Track.TrackInactive.apply(cr)

                    Style.reset_dash(cr)

                    if self.edit_track is tr:
                        cr.arc(x, y, 7 if item_hl else 5, 0, 2 * math.pi)
                        cr.stroke()

                    prevx = x
                    prevy = y

    def draw_race_tracks(self, gpsmap, cr, draw_pois=True):  # noqa: C901
        """Draw race-track geometry without per-fix number labels."""
        for tr in self.core.trackManager:
            if not tr.visible or not getattr(tr, "race_track", False):
                continue

            active = self.core.trackManager.is_active(tr)

            prevx = None
            prevy = None

            for p in tr:
                x, y = gpsmap.convert_geographic_to_screen(
                    MapPoint.new_degrees(p[0], p[1])
                )

                if prevx is None:
                    if active:
                        Style.Track.TrackActiveFont.apply(cr)
                    else:
                        Style.Track.TrackInactiveFont.apply(cr)

                    cr.move_to(x - 10, y - 10)
                    cr.show_text(tr.name)
                    cr.stroke()

                color = getattr(tr, "color", None)
                if color is None:
                    rng = random.Random(getattr(tr, "name", "") or "")
                    color = (rng.random(), rng.random(), rng.random())

                if prevx is not None and prevy is not None:
                    cr.set_source_rgba(
                        color[0], color[1], color[2], 1.0 if active else 0.6
                    )
                    cr.set_line_width(2.0 if active else 1.2)
                    Style.reset_dash(cr)
                    cr.move_to(prevx, prevy)
                    cr.line_to(x, y)
                    cr.stroke()

                prevx = x
                prevy = y

        if draw_pois:
            for tr in self.core.poiManager:
                if not tr.visible:
                    continue

                poi_hl = tr.name == self.hl_poi
                x, y = gpsmap.convert_geographic_to_screen(
                    MapPoint.new_degrees(tr.position[0], tr.position[1])
                )

                if poi_hl:
                    Style.Poi.QuadHL.apply(cr)
                else:
                    Style.Poi.Quad.apply(cr)
                cr.rectangle(x + 3, y - 5, len(tr.name) * 6.7, 12)
                cr.stroke_preserve()
                if poi_hl:
                    Style.Poi.QuadIntHL.apply(cr)
                else:
                    Style.Poi.QuadInt.apply(cr)
                cr.fill()

                if poi_hl:
                    Style.Poi.FontHL.apply(cr)
                else:
                    Style.Poi.Font.apply(cr)
                cr.move_to(x + 5, y + 5)
                cr.show_text(tr.name)
                cr.stroke()

                if poi_hl:
                    Style.Poi.DotHL.apply(cr)
                else:
                    Style.Poi.Dot.apply(cr)
                cr.arc(x, y, 4 if poi_hl else 2, 0, 2 * math.pi)
                cr.fill()

            # Triangle
            # cr.move_to(x-5, y-5)
            # cr.line_to(x,y+5)
            # cr.move_to(x+5, y-5)
            # cr.line_to(x,y+5)
            # cr.move_to(x-5, y-5)
            # cr.line_to(x+5,y-5)
            # cr.stroke()
