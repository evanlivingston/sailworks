"""Chart-depth lookup against real in-memory OGR geometries."""

import unittest
from types import SimpleNamespace

from osgeo import ogr

from gweatherrouting.rendering.chartcollection import ChartCollection
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer


class ChartDepthChecks(unittest.TestCase):
    def setUp(self):
        self.dataset = ogr.GetDriverByName("Memory").CreateDataSource("")
        self.soundings = self.dataset.CreateLayer(
            "SOUNDG", geom_type=ogr.wkbMultiPoint25D
        )
        self.areas = self.dataset.CreateLayer("DEPARE", geom_type=ogr.wkbPolygon)
        for name in ("DRVAL1", "DRVAL2"):
            self.areas.CreateField(ogr.FieldDefn(name, ogr.OFTReal))
        self.chart = GDALVectorChart.__new__(GDALVectorChart)
        self.chart.drawer = S57ChartDrawer.__new__(S57ChartDrawer)
        self.chart.vector_file = self.dataset
        self.chart.path = "harbor.000"
        self.chart.enabled = True
        self.chart._native_scale = 12000
        self.chart._coverage = [
            ogr.CreateGeometryFromWkt(
                "POLYGON((-124 47,-122 47,-122 49,-124 49,-124 47))"
            )
        ]

    def add_soundings(self, points):
        geometry = ogr.Geometry(ogr.wkbMultiPoint25D)
        for lon, lat, depth in points:
            point = ogr.Geometry(ogr.wkbPoint25D)
            point.AddPoint(lon, lat, depth)
            geometry.AddGeometry(point)
        feature = ogr.Feature(self.soundings.GetLayerDefn())
        feature.SetGeometry(geometry)
        self.soundings.CreateFeature(feature)

    def add_area(self, low, high, wkt=None):
        feature = ogr.Feature(self.areas.GetLayerDefn())
        feature.SetGeometry(
            ogr.CreateGeometryFromWkt(
                wkt
                or "POLYGON((-123.01 47.99,-122.99 47.99,-122.99 48.01,-123.01 48.01,-123.01 47.99))"
            )
        )
        if low is not None:
            feature.SetField("DRVAL1", low)
        if high is not None:
            feature.SetField("DRVAL2", high)
        self.areas.CreateFeature(feature)

    def test_nearest_sounding_uses_latitude_and_preserves_zero(self):
        self.add_soundings([(-123, 48.0003, 10), (-122.9996, 48, 0), (-120, 48, 80)])
        result = self.chart.depth_at(48, -123)
        self.assertEqual(result["sounding"], 0)
        self.assertAlmostEqual(result["distance_m"], 29.8, delta=1)

    def test_far_soundings_are_not_returned(self):
        self.add_soundings([(-123.1, 48, 70)])
        self.assertIsNone(self.chart.depth_at(48, -123))

    def test_enclosing_depth_range_and_filter_restoration(self):
        self.add_area(9.1, 18.2)
        self.add_soundings([(-123, 48, -1.2)])
        previous = ogr.CreateGeometryFromWkt("POINT(-123 48)")
        for layer in (self.areas, self.soundings):
            layer.SetSpatialFilter(previous)
        result = self.chart.depth_at(48, -123)
        self.assertEqual(result["area"], (9.1, 18.2))
        self.assertEqual(result["sounding"], -1.2)
        for layer in (self.areas, self.soundings):
            self.assertTrue(layer.GetSpatialFilter().Equals(previous))

    def test_range_does_not_fill_polygon_holes(self):
        self.add_area(
            0,
            10,
            "POLYGON((-124 47,-122 47,-122 49,-124 49,-124 47),"
            "(-123.01 47.99,-123.01 48.01,-122.99 48.01,-122.99 47.99,-123.01 47.99))",
        )
        self.assertIsNone(self.chart.depth_at(48, -123))

    def test_unknown_or_inverted_range_is_not_reported(self):
        for low, high in ((None, 10), (10, 1)):
            with self.subTest(low=low, high=high):
                self.add_area(low, high)
                self.assertIsNone(self.chart.depth_at(48, -123))

    def test_collection_respects_visibility_coverage_and_scale(self):
        self.add_area(9.1, 18.2)
        manager = SimpleNamespace(charts=[self.chart], settings_manager={})
        self.assertIsNotNone(ChartCollection.depth_at(manager, 48, -123, 2))
        self.assertIsNone(ChartCollection.depth_at(manager, 50, -123, 2))
        self.assertIsNone(ChartCollection.depth_at(manager, 48, -123, 1000))
        self.chart.enabled = False
        self.assertIsNone(ChartCollection.depth_at(manager, 48, -123, 2))
        self.chart.enabled = True
        manager.settings_manager["chartENCVisible"] = False
        self.assertIsNone(ChartCollection.depth_at(manager, 48, -123, 2))

    def test_collection_prefers_detailed_chart_range_over_coarse_sounding(self):
        self.add_area(9.1, 18.2)
        coarse = GDALVectorChart.__new__(GDALVectorChart)
        coarse.__dict__.update(self.chart.__dict__)
        coarse._native_scale = 100000
        coarse.path = "coastal.000"
        coarse.depth_at = lambda *args: {"sounding": 12, "distance_m": 20}
        manager = SimpleNamespace(charts=[coarse, self.chart], settings_manager={})
        self.assertEqual(
            ChartCollection.depth_at(manager, 48, -123, 2), {"area": (9.1, 18.2)}
        )
