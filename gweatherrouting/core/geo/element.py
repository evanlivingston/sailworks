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

import gpxpy
import logging

logger = logging.getLogger("gweatherrouting")


class Element:
    def __init__(self, name="any", visible=True, collection=None):
        self.name = name
        self._revision = 0
        self.visible = visible
        self.collection = collection

    @property
    def visible(self):
        return self._visible

    @visible.setter
    def visible(self, value):
        self._visible = value
        self.touch()

    @property
    def revision(self):
        """Monotonic counter bumped on any edit that affects drawn geometry.

        Read in O(1) by cache keys; never derived from point data.
        """
        return self._revision

    def touch(self):
        """Mark this element as changed so cached geometry is invalidated."""
        self._revision = getattr(self, "_revision", 0) + 1

    def to_gpx_object(self):
        raise Exception("Not implemented")

    def to_json(self):
        return {"name": self.name, "visible": self.visible}

    @staticmethod
    def from_json(j):
        return Element(name=j["name"], visible=j["visible"])

    def export(self, dest, format="gpx"):
        if format == "gpx":
            gpx = gpxpy.gpx.GPX()

            ob = self.to_gpx_object()
            if isinstance(ob, gpxpy.gpx.GPXTrack):
                gpx.tracks.append(ob)
            elif isinstance(ob, gpxpy.gpx.GPXRoute):
                gpx.routes.append(ob)
            elif isinstance(ob, gpxpy.gpx.GPXWaypoint):
                gpx.waypoints.append(ob)

            try:
                f = open(dest, "w")
                f.write(gpx.to_xml())
                f.close()
                return True
            except Exception as e:
                logger.debug("Failed to export GPX: %s", e)

        return False

    def __len__(self):
        return 0
