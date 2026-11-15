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

from typing import Dict, List, Tuple

import gpxpy

from sailworks.core.geo.elementmultipoint import ElementMultiPoint


def _normalise_departure(value):
    """Return a whole-minute ISO departure string, or ``None`` when unset."""
    from datetime import datetime

    from sailworks.core.marine.routingrecord import SchemaError

    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise SchemaError("default_departure must be an ISO datetime string")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise SchemaError("default_departure must be an ISO datetime") from None
    return stamp.replace(second=0, microsecond=0).isoformat()


class Track(ElementMultiPoint):
    def __init__(
        self,
        name,
        points=None,
        visible=True,
        collection=None,
        color=None,
        default_departure=None,
    ):
        super().__init__(name, points, visible, collection)
        self.color = color
        self.default_departure = _normalise_departure(default_departure)

    def to_json(self):
        c = super().to_json()
        if self.color is not None:
            c["color"] = self.color
        if self.default_departure:
            c["default_departure"] = self.default_departure
        return c

    @staticmethod
    def from_json(j: Dict) -> "Track":
        d = ElementMultiPoint.from_json(j)
        track = Track(
            d.name,
            d.points,
            d.visible,
            color=j.get("color"),
            default_departure=j.get("default_departure"),
        )
        # Older versions persisted race boat tracks in the user collection
        # (both the original flag and the later race-key scheme). Flag them so
        # the race panel can purge them on startup.
        track._legacy_race_track = bool(
            j.get("race_track") or j.get("race_key") or j.get("race_boat_key")
        )
        return track

    def to_gpx_object(self):
        gpx_track = gpxpy.gpx.GPXTrack()
        gpx_segment = gpxpy.gpx.GPXTrackSegment()
        gpx_track.segments.append(gpx_segment)

        for x in self.points:
            gpx_point = gpxpy.gpx.GPXTrackPoint(x[0], x[1])
            name = getattr(x, "name", "") or ""
            if name:
                gpx_point.name = name
            gpx_segment.points.append(gpx_point)

        return gpx_track

    def to_list(self) -> List[Tuple[float, float]]:
        return [(x[0], x[1]) for x in self.points]
