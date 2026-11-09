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

from copy import deepcopy

import gpxpy
from weatherrouting import IsoPoint

from sailworks.core.geo.track import Track
from sailworks.core.isochrones import IsochroneContours, normalize_contours


class Routing(Track):
    def __init__(
        self,
        name,
        points=[],
        isochrones=[],
        visible=True,
        collection=None,
        provenance=None,
        contours=None,
    ):
        super().__init__(name, points, visible, collection)
        self.isochrones = isochrones
        # None explicitly means provenance is unknown, including legacy routes.
        self.provenance = deepcopy(provenance)
        # A missing sidecar means this is a legacy route; never fabricate one.
        self.contours = normalize_contours(contours)

    def to_gpx_object(self):
        gpx_route = gpxpy.gpx.GPXRoute()

        for x in self.points:
            gpx_route.points.append(gpxpy.gpx.GPXRoutePoint(x[0], x[1]))

        return gpx_route

    def to_json(self):
        c = super().to_json()
        c["isochrones"] = list(
            map(lambda x: list(map(lambda y: y.to_list(), x)), self.isochrones)
        )
        c["provenance"] = deepcopy(self.provenance)
        if self.contours is not None:
            c["isochrone_contours"] = self.contours.to_json()
        return c

    @staticmethod
    def from_json(j):
        d = Track.from_json(j)
        ic = list(map(lambda x: list(map(IsoPoint.from_list, x)), j["isochrones"]))
        try:
            raw_contours = j.get("isochrone_contours")
            contours = (
                IsochroneContours.from_json(raw_contours)
                if raw_contours is not None
                else None
            )
        except Exception:
            # A malformed or future sidecar must not prevent loading the route.
            contours = IsochroneContours((), version=0, representation="unavailable")
        return Routing(
            d.name,
            d.points,
            ic,
            d.visible,
            provenance=j.get("provenance"),
            contours=contours,
        )
