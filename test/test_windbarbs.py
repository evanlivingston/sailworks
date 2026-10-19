"""Fixed-size foreground weather symbols and dense native-grid selection."""

import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cairo
import numpy as np

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.rendering.windpainter import (
    BARB_MAX_COUNT,
    BARB_DENSITIES,
    WindPainter,
    barb_density_parameters,
    flatten_wind_columns,
    mercator_project_nodes,
)
from gweatherrouting.rendering.windrenderer import WindOverlayRenderer


class TestLiveWindBarbs(unittest.TestCase):
    def test_land_toggle_only_removes_symbols_after_thinning_and_count_cap(self):
        lons, lats = np.meshgrid(np.linspace(-1, 1, 50), np.linspace(-1, 1, 50))
        flat = (np.zeros(2500), np.full(2500, 1.0), lats.ravel(), lons.ravel())
        land = np.arange(2500) % 3 == 0
        for step, maximum in ((44, 900), (16, 20), (0, None)):
            shown, count = WindPainter.live_geometry_for_view(
                flat, 0, 0, 9, 1000, 1000, step=step, max_count=maximum
            )
            hidden, hidden_count = WindPainter.live_geometry_for_view(
                flat,
                0,
                0,
                9,
                1000,
                1000,
                step=step,
                max_count=maximum,
                land_mask=land,
            )
            shown_lines = set(shown[0][0])  # <2.5 kt: shafts only
            hidden_lines = set(hidden[0][0])
            self.assertTrue(hidden_lines < shown_lines)
            self.assertLess(hidden_count, count)
            # Legacy callable masking obeys the same post-selection ordering.
            lookup = dict(zip(zip(flat[2], flat[3]), land))
            legacy, legacy_count = WindPainter.live_geometry_for_view(
                flat,
                0,
                0,
                9,
                1000,
                1000,
                step=step,
                max_count=maximum,
                hide_on_ground=True,
                on_ground=lambda lat, lon: lookup[lat, lon],
            )
            self.assertEqual(legacy, hidden)
            self.assertEqual(legacy_count, hidden_count)

    def test_density_presets_add_detail_and_all_points_has_no_count_cap(self):
        side = 100
        lons, lats = np.meshgrid(np.linspace(-1, 1, side), np.linspace(-1, 1, side))
        flat = (
            np.zeros(side * side),
            np.full(side * side, 8.0),
            lats.ravel(),
            lons.ravel(),
        )
        counts = []
        for key, _label, _spacing in BARB_DENSITIES:
            step, maximum = barb_density_parameters(key)
            _geometry, count = WindPainter.live_geometry_for_view(
                flat,
                0,
                0,
                9,
                1000,
                1000,
                step=step,
                max_count=maximum,
            )
            counts.append(count)
        self.assertTrue(all(a < b for a, b in zip(counts, counts[1:])), counts)
        self.assertEqual(counts[-1], side * side)
        self.assertGreater(counts[-1], BARB_MAX_COUNT)
        self.assertEqual(
            barb_density_parameters("old-or-invalid"),
            barb_density_parameters("standard"),
        )

    def test_calm_symbol_retains_shaft_and_hollow_circle(self):
        geometry = WindPainter.build_barb_geometry([100], [100], [0], [0])
        lines, triangles, dots = geometry[0]
        self.assertEqual(lines, [(100, 100, 100, 80)])
        self.assertEqual(triangles, [])
        self.assertEqual(dots, [(100, 100)])

    def test_direction_speed_and_pennants_preserve_meteorological_notation(self):
        for direction in (0, 90, 180, 270):
            # 65 kt = one pennant, one full feather, one half feather.
            geometry = WindPainter.build_barb_geometry(
                [100], [100], [direction], [65 / 1.94384]
            )
            lines, triangles, dots = next(iter(geometry.values()))
            self.assertEqual(len(lines), 3)
            self.assertEqual(len(triangles), 1)
            self.assertEqual(dots, [])
            x1, y1, x2, y2 = lines[0]
            self.assertAlmostEqual(math.hypot(x2 - x1, y2 - y1), 20)
            angle = math.radians(direction)
            self.assertAlmostEqual(x2, 100 + 20 * math.sin(angle))
            self.assertAlmostEqual(y2, 100 - 20 * math.cos(angle))

    def test_dense_grid_is_bounded_without_losing_whole_view_coverage(self):
        side = 400
        lons, lats = np.meshgrid(np.linspace(-6, 6, side), np.linspace(-6, 6, side))
        flat = (
            np.zeros(side * side),
            np.full(side * side, 8.0),
            lats.ravel(),
            lons.ravel(),
        )
        geometry, count = WindPainter.live_geometry_for_view(flat, 0, 0, 9, 4000, 3000)
        self.assertGreater(count, 100)
        self.assertLessEqual(count, BARB_MAX_COUNT)
        lines = next(iter(geometry.values()))[0]
        origins = np.asarray(lines[::3])[:, :2]  # 8 m/s = shaft + 2 feathers
        self.assertLess(origins[:, 0].min(), 100)
        self.assertGreater(origins[:, 0].max(), 3900)
        self.assertLess(origins[:, 1].min(), 100)
        self.assertGreater(origins[:, 1].max(), 2900)

    def test_cached_projection_preserves_exact_geometry_for_every_density(self):
        lons, lats = np.meshgrid(np.linspace(-181, 181, 90), np.linspace(-90, 90, 70))
        flat = (
            np.arange(lats.size, dtype=float) % 360,
            np.arange(lats.size, dtype=float) % 35,
            lats.ravel(),
            lons.ravel(),
        )
        for zoom in (1, 4.3, 9):
            projected = mercator_project_nodes(flat[2], flat[3], zoom)
            for latitude, longitude in ((0, 0), (48.4, -123.4), (85, 180)):
                for density, _label, _spacing in BARB_DENSITIES:
                    step, maximum = barb_density_parameters(density)
                    args = (flat, latitude, longitude, zoom, 800, 600)
                    options = dict(step=step, max_count=maximum)
                    self.assertEqual(
                        WindPainter.live_geometry_for_view(*args, **options),
                        WindPainter.live_geometry_for_view(
                            *args, **options, projected=projected
                        ),
                    )

    @staticmethod
    def renderer_frame(flat):
        renderer = object.__new__(WindOverlayRenderer)
        canvas = object()
        job = SimpleNamespace(barb_flat=flat, palette=(None, 0.8, True))
        renderer._states = {canvas: SimpleNamespace(frame=(job, None), key=None)}
        renderer._barbs_over_land = {}
        renderer._can_preview = lambda *_: True
        return renderer, canvas, job

    def test_projection_reused_for_pan_and_replaced_on_zoom_and_new_frame(self):
        flat = flatten_wind_columns([[(90, 5, (48.01, -123.02))]])
        renderer, canvas, job = self.renderer_frame(flat)
        target = "gweatherrouting.rendering.windrenderer.mercator_project_nodes"
        with patch(target, wraps=mercator_project_nodes) as project:
            for latitude in (48, 48.01, 48.02):
                renderer.live_geometry_for_view(canvas, 800, 600, latitude, -123, 9)
            self.assertEqual(project.call_count, 1)
            old = job.barb_projection
            self.assertTrue(all(not a.flags.writeable for a in old[1]))
            renderer.live_geometry_for_view(canvas, 800, 600, 48, -123, 9.3)
            self.assertEqual(project.call_count, 2)
            self.assertIsNot(job.barb_projection, old)
            new = SimpleNamespace(barb_flat=flat, palette=(None, 0.8, True))
            renderer._states[canvas].frame = (new, None)
            renderer.live_geometry_for_view(canvas, 800, 600, 48, -123, 9.3)
            self.assertEqual(project.call_count, 3)
            self.assertIsNot(new.barb_projection, job.barb_projection)

    def test_large_frames_do_not_retain_projected_arrays(self):
        array = np.zeros(8 * 1024 * 1024 // 16 + 1)
        renderer, canvas, job = self.renderer_frame((array,) * 4)
        with patch.object(
            WindPainter, "live_geometry_for_view", return_value=({}, 0)
        ) as live:
            renderer.live_geometry_for_view(canvas, 800, 600, 48, -123, 9)
        self.assertFalse(hasattr(job, "barb_projection"))
        self.assertIsNone(live.call_args.kwargs["projected"])

    def test_projection_matches_existing_map_coordinates(self):
        view = SimpleNamespace(
            _center_lat=48.0,
            _center_lon=-123.0,
            _zoom=9.3,
            get_allocated_width=lambda: 800,
            get_allocated_height=lambda: 600,
            queue_draw=lambda: None,
        )
        viewport = MapViewport(view)
        data = [[(90, 5, (48.01, -123.02))]]
        flat = flatten_wind_columns(data)
        expected = viewport.convert_geographic_to_screen_batch(flat[2], flat[3])
        geometry, count = WindPainter.live_geometry_for_view(
            flat, 48, -123, 9.3, 800, 600
        )
        self.assertEqual(count, 1)
        shaft = next(iter(geometry.values()))[0][0]
        np.testing.assert_allclose(shaft[:2], [expected[0][0], expected[1][0]])

    def test_cairo_retina_keeps_logical_size(self):
        painter = WindPainter(
            SimpleNamespace(
                gribArrowOpacity=0.8,
                gribArrowOnGround=True,
            )
        )
        view = SimpleNamespace(
            _center_lat=0,
            _center_lon=0,
            _zoom=8,
            get_allocated_width=lambda: 100,
            get_allocated_height=lambda: 100,
        )
        flat = flatten_wind_columns([[(0, 10, (0, 0))]])
        for factor in (1, 2):
            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32, 100 * factor, 100 * factor
            )
            surface.set_device_scale(factor, factor)
            self.assertEqual(
                painter.draw_barbs_live(view, cairo.Context(surface), flat), 1
            )
            surface.flush()
            alpha = np.ndarray(
                (100 * factor, 100 * factor, 4),
                dtype=np.uint8,
                buffer=surface.get_data(),
                strides=(surface.get_stride(), 4, 1),
            )[:, :, 3]
            ys, _ = np.nonzero(alpha)
            self.assertAlmostEqual((ys.max() - ys.min() + 1) / factor, 20, delta=1)
