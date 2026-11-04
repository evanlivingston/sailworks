"""Fallback coastlines retain interior water for either frontend."""

import unittest
from unittest.mock import Mock

import cairo
from osgeo import ogr

from sailworks.core.mapviewport import MapPoint
from sailworks.rendering.style import CairoStyle
from sailworks.rendering.vectordrawer.simplechartdrawer import SimpleChartDrawer
from test.map_helpers import MapCanvas


class TestCoastlineHoles(unittest.TestCase):
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
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10)
        cr = cairo.Context(surface)
        simplify = SimpleChartDrawer._simplify_polygon
        self.assertIs(simplify(canvas, cr, geom), simplified)
        first = geom.SimplifyPreserveTopology.call_args.args[0]
        surface.set_device_scale(2, 2)
        self.assertIs(simplify(canvas, cr, geom), simplified)
        self.assertAlmostEqual(
            geom.SimplifyPreserveTopology.call_args.args[0], first / 2
        )
        simplified.IsValid.return_value = False
        self.assertIs(simplify(canvas, cr, geom), geom)
        ring.GetPointCount.return_value = 5
        geom.SimplifyPreserveTopology.reset_mock()
        self.assertIs(simplify(canvas, cr, geom), geom)
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
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
                cr = cairo.Context(surface)
                cr.set_source_rgb(0, 0, 1)
                cr.paint()
                drawer = SimpleChartDrawer.__new__(SimpleChartDrawer)
                drawer.feature_render(
                    canvas,
                    cr,
                    geometry,
                    None,
                    None,
                    CairoStyle(color=(1, 0, 0, 1)),
                    CairoStyle(color=(1, 0, 0, 1)),
                    None,
                )
                surface.flush()
                # The shared test viewport is centered on 47.6N, 122.4W,
                # inside the hole. Outside the hole remains land.
                data = bytes(surface.get_data())
                center = 150 * surface.get_stride() + 200 * 4
                self.assertEqual(data[center : center + 4], bytes((255, 0, 0, 255)))
                x, y = canvas.convert_geographic_to_screen(MapPoint(47.6, -121.8))
                land = round(y) * surface.get_stride() + round(x) * 4
                self.assertEqual(data[land : land + 4], bytes((0, 0, 255, 255)))
