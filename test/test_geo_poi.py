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

import gpxpy

from sailworks.core.geo.poi import POI


class TestPOI(unittest.TestCase):
    def test_init(self):
        poi = POI("Harbor", (42.5, 13.2), symbol="anchor")
        self.assertEqual(poi.name, "Harbor")
        self.assertEqual(poi.position, (42.5, 13.2))
        # NOTE: the constructor ignores the symbol arg and sets self.symbol = None
        self.assertIsNone(poi.symbol)
        self.assertTrue(poi.visible)

    def test_to_json_from_json_roundtrip(self):
        poi = POI("Harbor", (42.5, 13.2), symbol="anchor", visible=False)
        j = poi.to_json()
        self.assertIn("symbol", j)
        self.assertIn("position", j)
        self.assertIn("name", j)
        poi2 = POI.from_json(j)
        self.assertEqual(poi2.name, "Harbor")
        self.assertEqual(poi2.position[0], 42.5)
        self.assertEqual(poi2.position[1], 13.2)
        self.assertFalse(poi2.visible)

    def test_to_gpx_object(self):
        poi = POI("TestWP", (40.5, 9.8))
        gpx_obj = poi.to_gpx_object()
        self.assertIsInstance(gpx_obj, gpxpy.gpx.GPXWaypoint)
        self.assertAlmostEqual(gpx_obj.latitude, 40.5)
        self.assertAlmostEqual(gpx_obj.longitude, 9.8)
        self.assertEqual(gpx_obj.name, "TestWP")


if __name__ == "__main__":
    unittest.main()
