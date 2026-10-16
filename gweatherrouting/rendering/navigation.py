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

"""Shared GTK/Qt track and POI portrayal, in logical map pixels."""

import math
from functools import lru_cache

import dateutil.parser
from gweatherrouting.core import utils

from gweatherrouting.core.mapviewport import MapPoint
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


def routing_summary(r):
    """Build a short summary string for a routing."""
    points = list(r)
    if len(points) < 2:
        return ""
    total_dist = 0.0
    total_speed = 0.0
    for j in range(1, len(points)):
        total_dist += utils.point_distance(
            points[j - 1][0], points[j - 1][1], points[j][0], points[j][1]
        )
        total_speed += points[j][5] if points[j][5] else 0
    avg_speed = total_speed / (len(points) - 1)
    total_dist_nm = total_dist
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
    try:
        t_start = dateutil.parser.parse(points[0][2])
        t_end = dateutil.parser.parse(points[-1][2])
        hours = (t_end - t_start).total_seconds() / 3600.0
        days = int(hours // 24)
        rem_hours = hours % 24
        dur_str = f"{days}d {rem_hours:.0f}h" if days > 0 else f"{rem_hours:.1f}h"
        return (
            "%.0f nm | %s | avg %.1f kts" % (total_dist_nm, dur_str, avg_speed)
            + source_note
        )
    except Exception:
        return "%.0f nm | avg %.1f kts" % (total_dist_nm, avg_speed) + source_note


class NavigationRenderer:
    def __init__(self, core):
        self.core = core
        self.hl_routing = None
        self.hl_poi = None
        self.hl_track_item = None

    def draw_routes(self, gpsmap, cr, display_time):  # noqa: C901
        # Legacy route times and the map clock are local and naive; newer
        # routes carry UTC offsets. Compare instants without discarding offsets.

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
            points_list = list(tr)

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

                # Interpolated boat position on route
                if prevp is not None:
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

    def draw_navigation(self, gpsmap, cr):  # noqa: C901
        for tr in self.core.trackManager:
            if not tr.visible:
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

                cr.arc(x, y, 7 if item_hl else 5, 0, 2 * math.pi)
                cr.stroke()

                prevx = x
                prevy = y

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
