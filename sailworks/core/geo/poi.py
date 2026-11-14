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

from typing import Tuple

import gpxpy

from sailworks.core.geo.elementpoint import ElementPoint


class POI(ElementPoint):
    def __init__(
        self,
        name,
        position: Tuple[float, float],
        symbol=None,
        visible=True,
        collection=None,
    ):
        super().__init__(name, position, visible, collection)
        self.symbol = None

    def to_json(self):
        c = super().to_json()
        c["symbol"] = self.symbol
        return c

    @staticmethod
    def from_json(j):
        d = ElementPoint.from_json(j)
        return POI(d.name, d.position, j["symbol"], d.visible)

    def to_gpx_object(self):
        # TODO: add symbol , sym=self.symb
        return gpxpy.gpx.GPXWaypoint(
            latitude=self.position[0], longitude=self.position[1], name=self.name
        )
