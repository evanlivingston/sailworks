"""Static flow integration, geographic registration, masks and hard budgets."""

import math
import unittest
from unittest.mock import patch

import numpy as np

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.windgeometry import stroke_mesh
import cairo
from gweatherrouting.rendering.streamlines import (
    WindGrid,
    trace_streamlines,
    geometry_for_view,
    density_parameters,
)
from test.map_helpers import MapCanvas


class TestStreamlines(unittest.TestCase):
    def setUp(self):
        canvas = MapCanvas()
        canvas._center_lat = canvas._center_lon = 0
        canvas._zoom = 8
        self.view = MapViewport(canvas)

    def field(self, velocity=lambda lat, lon: (7, 0)):
        columns = []
        for lon in np.linspace(-2, 2, 17):
            column = []
            for lat in np.linspace(-2, 2, 17):
                u, v = velocity(lat, lon)
                column.append(
                    (
                        math.degrees(math.atan2(-u, -v)) % 360,
                        math.hypot(u, v),
                        (lat, lon),
                    )
                )
            columns.append(column)
        return columns

    def test_uniform_westerly_has_registered_eastbound_lines_and_direction_heads(self):
        flat = trace_streamlines(self.view, self.field(), lambda lat, lon: False)
        self.assertIsNotNone(flat)
        lat1, lon1, lat2, lon2, speeds, land, arrows = flat
        np.testing.assert_allclose(lat1, lat2, atol=1e-10)
        self.assertTrue(np.all(lon2 > lon1))
        np.testing.assert_allclose(speeds, 7)
        self.assertFalse(land.any())
        self.assertTrue(arrows.any())
        geometry = geometry_for_view(flat, 0, 0, 8, 400, 300, True)
        self.assertGreater(len(geometry[3][0]), len(lat1))
        expected_x, expected_y = self.view.convert_geographic_to_screen_batch(
            lat1, lon1
        )
        np.testing.assert_allclose(
            geometry[3][0][0][:2], (expected_x[0], expected_y[0])
        )

    def test_density_presets_add_paths_and_keep_hard_work_limits(self):
        counts = []
        for density in ("sparse", "standard", "dense", "very_dense", "all"):
            flat = trace_streamlines(
                self.view, self.field(), lambda lat, lon: False, density=density
            )
            counts.append(len(flat[0]))
            _spacing, seeds, segments = density_parameters(density)
            self.assertLessEqual(seeds, 2000)
            self.assertLessEqual(segments, 60000)
            self.assertLessEqual(len(flat[0]), segments)
        self.assertTrue(all(a < b for a, b in zip(counts, counts[1:])), counts)
        self.assertEqual(density_parameters("invalid"), density_parameters("standard"))

    def test_northerly_flows_south_not_toward_the_wind_source(self):
        flat = trace_streamlines(
            self.view, self.field(lambda lat, lon: (0, -8)), lambda lat, lon: False
        )
        self.assertTrue(np.all(flat[2] < flat[0]))
        np.testing.assert_allclose(flat[1], flat[3], atol=1e-10)

    def test_rotating_field_produces_curved_lines(self):
        flat = trace_streamlines(
            self.view,
            self.field(lambda lat, lon: (-lat * 5, lon * 5)),
            lambda lat, lon: False,
        )
        self.assertIsNotNone(flat)
        angles = np.arctan2(flat[2] - flat[0], flat[3] - flat[1])
        self.assertGreater(np.std(angles), 0.5)
        r1, r2 = np.hypot(flat[0], flat[1]), np.hypot(flat[2], flat[3])
        np.testing.assert_allclose(r1, r2, atol=0.003)

    def test_mask_only_removes_land_segments_and_does_not_retrace(self):
        flat = trace_streamlines(self.view, self.field(), lambda lat, lon: lon > 0)
        self.assertTrue(flat[5].any())
        self.assertTrue((~flat[5]).any())
        shown = geometry_for_view(flat, 0, 0, 8, 400, 300, True)
        hidden = geometry_for_view(flat, 0, 0, 8, 400, 300, False)
        self.assertTrue(set(map(tuple, hidden[3][0])) < set(map(tuple, shown[3][0])))

    def test_calm_missing_and_ragged_fields_do_not_create_fake_flow(self):
        self.assertIsNone(
            trace_streamlines(
                self.view, self.field(lambda lat, lon: (0, 0)), lambda lat, lon: False
            )
        )
        data = self.field()
        data[1].pop()
        self.assertIsNone(trace_streamlines(self.view, data, lambda lat, lon: False))
        grid = WindGrid(self.view, [self.field()[i] for i in (0, 4, 12, 16)])
        self.assertIsNone(grid.velocity(200, 150))  # Missing central columns.
        data = self.field()
        data[8][8] = (0, float("nan"), (0, 0))
        self.assertIsNone(WindGrid(self.view, data).velocity(200, 150))

    def test_work_is_bounded_and_cancellable(self):
        with patch("gweatherrouting.rendering.streamlines.MAX_SEGMENTS", 20):
            flat = trace_streamlines(self.view, self.field(), lambda lat, lon: False)
            self.assertLessEqual(len(flat[0]), 20)
            xs, ys = self.view.convert_geographic_to_screen_batch(flat[0], flat[1])
            self.assertLess(abs(xs[0] - 200), 22)
            self.assertLess(abs(ys[0] - 150), 22)
        self.assertIsNone(
            trace_streamlines(
                self.view, self.field(), lambda lat, lon: False, cancelled=lambda: True
            )
        )

    def test_zoom_reprojects_without_changing_geographic_paths(self):
        flat = trace_streamlines(self.view, self.field(), lambda lat, lon: False)
        before = flat[0].copy()
        a = geometry_for_view(flat, 0, 0, 8, 400, 300, True)
        b = geometry_for_view(flat, 0, 0, 8.5, 400, 300, True)
        self.assertNotEqual(a, b)
        np.testing.assert_array_equal(flat[0], before)

    def test_geometry_paints_with_cpu_and_builds_finite_gpu_strokes(self):
        flat = trace_streamlines(self.view, self.field(), lambda lat, lon: False)
        geometry = geometry_for_view(flat, 0, 0, 8, 400, 300, True)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
        WindPainter.paint_barb_geometry(cairo.Context(surface), geometry, 0.8)
        surface.flush()
        self.assertTrue(any(bytes(surface.get_data())))
        for lines, triangles, dots in geometry.values():
            self.assertFalse(triangles)
            vertices = stroke_mesh(lines, dots)
            self.assertTrue(np.isfinite(vertices).all())
            self.assertGreater(len(vertices), 0)
