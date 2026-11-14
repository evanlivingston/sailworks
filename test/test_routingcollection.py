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

import unittest
from unittest.mock import patch

import gpxpy.gpx

from sailworks.core.geo.routingcollection import RoutingCollection


class TestRoutingCollection(unittest.TestCase):
    """Test the RoutingCollection class with Storage I/O patched out."""

    def _make_collection(self):
        with (
            patch(
                "sailworks.core.geo.collection.CollectionStorage.load_or_save_default"
            ),
            patch("sailworks.core.geo.collection.CollectionStorage.save"),
        ):
            return RoutingCollection()

    def test_import_from_gpx_route(self):
        c = self._make_collection()

        gpx = gpxpy.gpx.GPX()
        route = gpxpy.gpx.GPXRoute(name="MyRoute")
        for lat, lon in ((40.0, 10.0), (41.0, 11.0), (42.0, 12.0)):
            route.points.append(gpxpy.gpx.GPXRoutePoint(lat, lon))
        gpx.routes.append(route)

        c.import_from_gpx(gpx)

        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].name, "MyRoute")
        self.assertEqual(c[0].points, [[40.0, 10.0], [41.0, 11.0], [42.0, 12.0]])

    def test_import_from_gpx_single_point_and_empty_routes(self):
        c = self._make_collection()

        gpx = gpxpy.gpx.GPX()
        single = gpxpy.gpx.GPXRoute(name="Single")
        single.points.append(gpxpy.gpx.GPXRoutePoint(40.0, 10.0))
        gpx.routes.append(single)
        gpx.routes.append(gpxpy.gpx.GPXRoute(name="Empty"))

        c.import_from_gpx(gpx)

        # A single-point route is still a route; an empty one has nothing to show.
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].name, "Single")
        self.assertEqual(c[0].points, [[40.0, 10.0]])


if __name__ == "__main__":
    unittest.main()
