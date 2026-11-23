"""Fallback coastlines retain interior water for either frontend."""

import unittest
from unittest.mock import Mock

from osgeo import ogr

from sailworks.core.mapviewport import MapPoint
from sailworks.rendering.canvas import Canvas, CanvasImage
from sailworks.rendering.style import CairoStyle
from sailworks.rendering.vectordrawer.simplechartdrawer import SimpleChartDrawer
from test.map_helpers import MapCanvas


class TestCoastlineHoles(unittest.TestCase):
    def test_coordinate_cache_reuses_arrays_but_distinguishes_clips(self):
        canvas = MapCanvas()
        drawer = SimpleChartDrawer.__new__(SimpleChartDrawer)
        ring = ogr.CreateGeometryFromWkt("LINESTRING(0 0,1 1,2 0)")
        drawer._geom_to_screen_batch(canvas, ring, "feature")
        arrays = next(iter(drawer._coordinate_cache.values()))
        drawer._geom_to_screen_batch(canvas, ring, "feature")
        self.assertIs(next(iter(drawer._coordinate_cache.values())), arrays)
        self.assertEqual(str(arrays[0].dtype), "float64")
        other = ogr.CreateGeometryFromWkt("LINESTRING(0 0,1.5 1,2 0)")
        drawer._geom_to_screen_batch(canvas, other, "feature")
        self.assertEqual(len(drawer._coordinate_cache), 2)
        for i in range(260):
            drawer._geom_to_screen_batch(canvas, ring, i)
        self.assertEqual(len(drawer._coordinate_cache), 256)

    def test_simplification_uses_device_scale_and_rejects_invalid(self):
        canvas = MapCanvas()
        ring = Mock()
        ring.GetPointCount.return_value = 12
        geom = Mock()
        geom.GetGeometryCount.return_value = 1
        geom.GetGeometryRef.return_value = ring
        geom.GetEnvelope.return_value = (0, 1, 0, 1)
        simplified = Mock()
        simplified.IsEmpty.return_value = False
        simplified.IsValid.return_value = True
        geom.SimplifyPreserveTopology.return_value = simplified
        image = CanvasImage.new(10, 10)
        target = Canvas.for_image(image)
        simplify = SimpleChartDrawer._simplify_polygon
        self.assertIs(simplify(canvas, target, geom), simplified)
        first = geom.SimplifyPreserveTopology.call_args.args[0]
        image.set_device_scale(2.0)
        self.assertIs(simplify(canvas, target, geom), simplified)
        self.assertAlmostEqual(
            geom.SimplifyPreserveTopology.call_args.args[0], first / 2
        )
        simplified.IsValid.return_value = False
        self.assertIs(simplify(canvas, target, geom), geom)
        ring.GetPointCount.return_value = 5
        geom.SimplifyPreserveTopology.reset_mock()
        self.assertIs(simplify(canvas, target, geom), geom)
        geom.SimplifyPreserveTopology.assert_not_called()

    def test_polygon_and_multipolygon_holes_keep_the_background(self):
        canvas = MapCanvas()
        canvas._zoom = 8
        polygon = ogr.CreateGeometryFromWkt(
            "POLYGON((-124 46,-120 46,-120 50,-124 50,-124 46),"
            "(-123 47,-122 47,-122 48,-123 48,-123 47))"
        )
        multiple = ogr.Geometry(ogr.wkbMultiPolygon)
        multiple.AddGeometry(polygon)
        for geometry in (polygon, multiple):
            with self.subTest(kind=geometry.GetGeometryName()):
                image = CanvasImage.new(400, 300)
                target = Canvas.for_image(image)
                target.set_source_rgb(0, 0, 1)
                target.paint()
                drawer = SimpleChartDrawer.__new__(SimpleChartDrawer)
                drawer.feature_render(
                    canvas,
                    target,
                    geometry,
                    None,
                    None,
                    CairoStyle(color=(1, 0, 0, 1)),
                    CairoStyle(color=(1, 0, 0, 1)),
                    None,
                )
                target.finish()
                # The shared test viewport is centered on 47.6N, 122.4W,
                # inside the hole. Outside the hole remains land.
                pixels = image.qimage
                self.assertEqual(pixels.pixelColor(200, 150).getRgb(), (0, 0, 255, 255))
                x, y = canvas.convert_geographic_to_screen(MapPoint(47.6, -121.8))
                self.assertEqual(
                    pixels.pixelColor(round(x), round(y)).getRgb(), (255, 0, 0, 255)
                )
