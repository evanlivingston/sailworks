"""Wind display cropping, private GRIB handles, and background image reuse."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

import cairo
import numpy as np
from osgeo import gdal, ogr

from gweatherrouting.core.grib import Grib
from gweatherrouting.rendering.windpainter import (
    WindPainter,
    wind_on_ground,
)
from gweatherrouting.rendering.windrenderer import (
    DisplayGrib,
    WindLandMask,
    WindOverlayRenderer,
)
from test.map_helpers import MapCanvas as TestMap, QueuedScheduler


class TestDisplayGrib(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "wind.tif")
        dataset = gdal.GetDriverByName("GTiff").Create(path, 4, 3, 4, gdal.GDT_Float32)
        dataset.SetGeoTransform((-124.5, 1, 0, 50.5, 0, -1))
        for index, (hour, element, value) in enumerate(
            ((0, "UGRD", 2), (0, "VGRD", 4), (3, "UGRD", 8), (3, "VGRD", 10)), 1
        ):
            band = dataset.GetRasterBand(index)
            band.SetMetadata(
                {"GRIB_ELEMENT": element, "GRIB_FORECAST_SECONDS": str(hour * 3600)}
            )
            band.WriteArray(np.full((3, 4), value, dtype=np.float32))
        band = None
        dataset = None
        self.source = Grib(path, "wind", "test", [], datetime(2026, 1, 1), 3)
        self.display = DisplayGrib(self.source)

    def test_visible_crop_matches_existing_forecast_interpolation(self):
        bounds = ((48.9, -123.1), (49.1, -121.9))
        forecast = self.source.start_time + timedelta(hours=1)
        expected = self.source.get_wind(forecast, bounds)
        actual = self.display.get_wind(forecast, bounds)
        self.assertEqual(len(actual), 2)
        self.assertEqual(actual, expected)
        self.assertIsNot(self.source.dataset, self.display.dataset)
        # No global list of GRIB tuples is built for the display.
        self.assertEqual(len(self.display.sampling.arrays), 4)

    def test_outside_grid_does_not_read_band_arrays(self):
        self.assertEqual(self.display._get_wind_data(0, ((0, 0), (1, 1))), [])
        self.assertFalse(self.display._arrays)

    def test_repeated_crops_reuse_bands_and_four_hour_cache_is_bounded(self):
        bounds = ((48, -124), (50, -121))
        first = self.display._get_wind_data(0, bounds)
        arrays = self.display._arrays[0]
        self.assertEqual(self.display._get_wind_data(0, bounds), first)
        self.assertIs(self.display._arrays[0], arrays)
        self.display._find_bands_for_time = Mock(return_value=(Mock(), Mock()))
        for band in self.display._find_bands_for_time.return_value:
            band.ReadAsArray.return_value = np.ones((3, 4))
        for hour in range(1, 8):
            self.display._get_wind_data(hour, bounds)
        self.assertEqual(len(self.display._arrays), 4)

    def test_prepared_land_mask_matches_known_land_and_water(self):
        mask = WindLandMask()
        for lat, lon, land in (
            (49.28, -123.12, True),
            (48.5, -125, False),
            (40, -100, True),
            (0, -140, False),
            (51.5, -0.1, True),
        ):
            self.assertEqual(mask.contains(lat, lon), land)
            self.assertEqual(mask.contains(lat, lon), land)
        self.assertEqual(mask.contains.cache_info().hits, 5)

    def test_prepared_land_mask_preserves_holes(self):
        shapes = [
            {
                "properties": {"bbox": [[0, 0], [4, 4]]},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
                        [[1, 1], [1, 3], [3, 3], [3, 1], [1, 1]],
                    ],
                },
            }
        ]
        with patch("gweatherrouting.rendering.windrenderer.COUNTRY_SHAPES", shapes):
            mask = WindLandMask()
            self.assertTrue(mask.contains(0.5, 0.5))
            self.assertFalse(mask.contains(2, 2))

    def test_installed_coastline_keeps_known_offshore_points(self):
        mask = WindLandMask()
        mask.refresh_source(scale=100)
        if mask._path is None:
            self.skipTest("Installed GSHHS shoreline is unavailable")
        self.assertFalse(mask.contains(48.5, -125))
        self.assertFalse(mask.contains(49.1, -123.6))
        self.assertTrue(mask.contains(40, -100))

    def test_installed_shoreline_overrides_countries_and_reuses_tiles(self):
        with TemporaryDirectory() as directory:
            mask = WindLandMask(directory)
            mask.refresh_source()
            with patch("gweatherrouting.rendering.windrenderer.COUNTRY_SHAPES", []):
                self.assertFalse(mask.contains(0.2, 0.2))
                path = Path(directory) / "gshhs/GSHHS_shp/f/GSHHS_f_L1.shp"
                path.parent.mkdir(parents=True)
                dataset = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(
                    str(path)
                )
                layer = dataset.CreateLayer("land", geom_type=ogr.wkbPolygon)
                feature = ogr.Feature(layer.GetLayerDefn())
                feature.SetGeometry(
                    ogr.CreateGeometryFromWkt(
                        "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0), "
                        "(0.4 0.4, 0.4 0.6, 0.6 0.6, 0.6 0.4, 0.4 0.4))"
                    )
                )
                layer.CreateFeature(feature)
                feature = layer = dataset = None
                mask.refresh_source()
                self.assertTrue(mask.contains(0.2, 0.2))
                self.assertFalse(mask.contains(0.5, 0.5))
                self.assertFalse(mask.contains(0.2, 1.2))
                self.assertTrue(mask.contains(0.2, 360.2))
                self.assertEqual(len(mask._prepared), 1)
                self.assertEqual(len(mask._tiles), 2)
                mask.refresh_source()
                self.assertTrue(mask.contains(0.2, 0.2))
                self.assertEqual(mask.contains.cache_info().hits, 1)

    def test_shoreline_detail_tracks_chart_scale_and_invalidates_old_tiles(self):
        with TemporaryDirectory() as directory:
            mask = WindLandMask(directory)
            for quality in ("c", "l", "i", "h", "f"):
                path = mask.directory / quality / f"GSHHS_{quality}_L1.shp"
                path.parent.mkdir(parents=True)
                path.touch()
            for scale, quality in (
                (6000, "c"),
                (1000, "l"),
                (300, "i"),
                (30, "h"),
                (1, "f"),
            ):
                mask._tiles[(0, 0)] = ()
                mask.refresh_source(scale)
                self.assertEqual(mask._path.parent.name, quality)
                self.assertFalse(mask._tiles)
            mask._tiles[(0, 0)] = ()
            mask.refresh_source(1)
            self.assertTrue(mask._tiles)  # Same source keeps the working set.

    def test_dense_island_bucket_does_not_churn_prepared_geometry_for_water(self):
        with TemporaryDirectory() as directory:
            mask = WindLandMask(directory)
            path = mask.directory / "f/GSHHS_f_L1.shp"
            path.parent.mkdir(parents=True)
            dataset = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(str(path))
            layer = dataset.CreateLayer("islands", geom_type=ogr.wkbPolygon)
            for index in range(100):  # More islands than the 64-polygon cache.
                x, y = (index % 10) * 0.08, (index // 10) * 0.08
                feature = ogr.Feature(layer.GetLayerDefn())
                feature.SetGeometry(
                    ogr.CreateGeometryFromWkt(
                        f"POLYGON (({x} {y}, {x + 0.01} {y}, "
                        f"{x + 0.01} {y + 0.01}, {x} {y + 0.01}, {x} {y}))"
                    )
                )
                layer.CreateFeature(feature)
            feature = layer = dataset = None
            mask.refresh_source()
            self.assertFalse(mask.contains(0.95, 0.95))
            self.assertEqual(len(mask._tiles[(0, 0)]), 100)
            self.assertEqual(len(mask._prepared), 0)
            self.assertTrue(mask.contains(0.005, 0.005))
            self.assertEqual(len(mask._prepared), 1)
            for longitude in np.linspace(0.8, 0.99, 100):
                self.assertFalse(mask.contains(0.95, float(longitude)))
            self.assertEqual(len(mask._prepared), 1)


class TestWindOverlay(unittest.TestCase):
    def setUp(self):
        self.canvas = TestMap()
        self.canvas.connect = Mock()
        self.settings = SimpleNamespace(gribArrowOpacity=0.8, gribArrowOnGround=True)
        self.wind_visible = True
        self.clock = SimpleNamespace(time=datetime(2026, 1, 1), connect=Mock())
        self.source = object()
        self.manager = SimpleNamespace(gribs=[self.source])
        self.schedule = QueuedScheduler()
        self.renderer = WindOverlayRenderer(WindPainter, schedule=self.schedule)
        self.renderer._land_mask.contains = Mock(return_value=False)
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
        self.cr = cairo.Context(self.surface)
        self.renderer._load_wind = Mock(
            return_value=[
                (
                    -122.5 + col * 0.1,
                    np.array([47.5 + row * 0.1 for row in range(3)]),
                    np.full(3, 180.0),
                    np.full(3, 6.0),
                )
                for col in range(3)
            ]
        )
        self.addCleanup(self.renderer.forget, self.canvas)

    def draw(self):
        self.renderer.draw(
            self.canvas,
            self.cr,
            tuple((grib,) for grib in self.manager.gribs),
            (
                self.clock.time,
                self.settings.gribArrowOpacity,
                self.settings.gribArrowOnGround,
                True,
                self.wind_visible,
                4.0,
                0.85,
                True,
                getattr(self.settings, "gribWindStyle", "barbs"),
                getattr(self.settings, "gribBarbDensity", "standard"),
            ),
            barb_density=getattr(self.settings, "gribBarbDensity", "standard"),
        )

    def drain(self):
        deadline = time.monotonic() + 2
        while self.renderer._running and time.monotonic() < deadline:
            self.schedule.drain()
            time.sleep(0.001)
        self.assertFalse(self.renderer._running)

    def test_wind_toggle_restores_frame_without_rebuilding_weather(self):
        self.draw()
        self.drain()
        original = self.renderer._states[self.canvas].frame
        self.wind_visible = False
        self.draw()
        self.drain()
        pressure_only = self.renderer._states[self.canvas].frame
        self.assertIsNone(pressure_only[0].barb_flat)
        self.wind_visible = True
        with patch.object(
            self.renderer, "_render", wraps=self.renderer._render
        ) as render:
            self.draw()
            self.assertIs(self.renderer._states[self.canvas].frame, original)
            self.assertFalse(self.renderer._running)
            render.assert_not_called()
        self.renderer._load_wind.assert_called_once()
        self.assertNotIn(self.canvas, self.renderer._hidden_wind_frames)

    def test_streamlines_are_opt_in_cached_and_respect_visibility_toggles(self):
        from gweatherrouting.rendering.streamlines import trace_streamlines

        lats = np.linspace(47, 48.2, 9)
        lons = np.linspace(-123.2, -121.6, 9)
        self.renderer._load_wind.return_value = [
            (float(lon), lats.copy(), np.full(9, 270.0), np.full(9, 8.0))
            for lon in lons
        ]
        self.renderer._land_mask.contains = Mock(
            side_effect=lambda lat, lon: lon > -122.4
        )
        self.renderer._load_pressure = Mock(return_value=[])
        with patch(
            "gweatherrouting.rendering.windrenderer.trace_streamlines",
            wraps=trace_streamlines,
        ) as trace:
            self.draw()
            self.drain()
            trace.assert_not_called()
            self.settings.gribWindStyle = "streamlines"
            self.draw()
            self.drain()
            trace.assert_called_once()

            frame = self.renderer._states[self.canvas].frame
            self.assertIsNotNone(frame[0].streamlines)
            args = (self.canvas, 400, 300, 47.6, -122.4, 12)
            shown, _ = self.renderer.live_geometry_for_view(*args)
            self.assertTrue(shown)
            self.assertIs(self.renderer.live_geometry_for_view(*args)[0], shown)
            self.settings.gribArrowOnGround = False
            self.draw()
            unchanged, _ = self.renderer.live_geometry_for_view(*args)
            self.assertEqual(
                sum(len(v[0]) for v in unchanged.values()),
                sum(len(v[0]) for v in shown.values()),
            )
            self.wind_visible = False
            self.draw()
            self.drain()
            self.wind_visible = True
            self.draw()
            self.assertIs(self.renderer._states[self.canvas].frame, frame)
            trace.assert_called_once()

    def test_streamline_density_retraces_on_worker_while_retaining_preview(self):
        self.settings.gribWindStyle = "streamlines"
        self.settings.gribBarbDensity = "sparse"
        lats = np.linspace(47, 48.2, 9)
        lons = np.linspace(-123.2, -121.6, 9)
        self.renderer._load_wind.return_value = [
            (float(lon), lats.copy(), np.full(9, 270.0), np.full(9, 8.0))
            for lon in lons
        ]
        self.renderer._load_pressure = Mock(return_value=[])
        self.draw()
        self.drain()
        frame = self.renderer._states[self.canvas].frame
        before = len(frame[0].streamlines[0])
        geometry, _ = self.renderer.live_geometry_for_view(
            self.canvas, 400, 300, 47.6, -122.4, 12
        )
        self.settings.gribBarbDensity = "dense"
        self.draw()
        self.assertIs(self.renderer._states[self.canvas].frame, frame)
        self.assertIs(
            self.renderer.live_geometry_for_view(
                self.canvas, 400, 300, 47.6, -122.4, 12
            )[0],
            geometry,
        )
        self.drain()
        current = self.renderer._states[self.canvas].frame
        self.assertEqual(current[0].palette[9], "dense")
        self.assertGreater(len(current[0].streamlines[0]), before)

    def test_wind_restore_does_not_wait_for_pressure_only_job(self):
        self.draw()
        self.drain()
        original = self.renderer._states[self.canvas].frame
        started, release = Event(), Event()
        self.addCleanup(release.set)

        def pressure(job):
            started.set()
            release.wait(2)
            return []

        self.renderer._load_pressure = Mock(side_effect=pressure)
        self.wind_visible = False
        self.draw()
        self.assertTrue(started.wait(1))
        self.wind_visible = True
        self.draw()
        self.assertIs(self.renderer._states[self.canvas].frame, original)
        geometry, opacity = self.renderer.live_geometry_for_view(
            self.canvas, 400, 300, 47.6, -122.4, 12
        )
        self.assertTrue(geometry)
        self.assertGreater(opacity, 0)
        release.set()
        self.drain()
        self.assertIs(self.renderer._states[self.canvas].frame, original)
        self.renderer._load_wind.assert_called_once()

    def test_wind_toggle_never_restores_incompatible_forecast_or_style(self):
        for change in ("time", "sources", "opacity"):
            with self.subTest(change=change):
                self.draw()
                self.drain()
                self.wind_visible = False
                self.draw()
                self.drain()
                pressure_only = self.renderer._states[self.canvas].frame
                if change == "time":
                    self.clock.time += timedelta(hours=1)
                elif change == "sources":
                    self.manager.gribs = [object()]
                elif change == "opacity":
                    self.settings.gribArrowOpacity = 0.5
                self.wind_visible = True
                self.draw()
                self.assertIs(self.renderer._states[self.canvas].frame, pressure_only)
                self.drain()

    def test_forget_releases_retained_hidden_wind_frame(self):
        self.draw()
        self.drain()
        self.wind_visible = False
        self.draw()
        self.drain()
        self.assertIn(self.canvas, self.renderer._hidden_wind_frames)
        self.renderer.forget(self.canvas)
        self.assertNotIn(self.canvas, self.renderer._hidden_wind_frames)

    def test_wind_over_land_is_never_masked(self):
        self.renderer._load_wind.return_value = [
            (-122.4, np.array([47.6]), np.array([0.0]), np.array([10.0])),
            (-122.41, np.array([47.61]), np.array([0.0]), np.array([10.0])),
        ]
        self.renderer._land_mask.contains = Mock(
            side_effect=lambda lat, lon: lat == 47.6
        )
        self.renderer._load_pressure = Mock(return_value=[])
        self.draw()
        self.drain()

        def geometry():
            return self.renderer.live_geometry_for_view(
                self.canvas, 400, 300, 47.6, -122.4, 12, density="all"
            )[0]

        all_barbs = geometry()
        self.assertEqual(len(all_barbs[5][0]), 6)  # two shafts + four feathers
        self.settings.gribArrowOnGround = False
        self.draw()
        self.assertEqual(geometry(), all_barbs)
        self.renderer._load_wind.assert_called_once()
        self.renderer._land_mask.contains.assert_not_called()

    def test_slow_grib_load_never_blocks_ui_and_uses_one_worker(self):
        started, release = Event(), Event()
        self.addCleanup(release.set)
        ui_thread = get_ident()

        def slow(job):
            self.assertNotEqual(get_ident(), ui_thread)
            started.set()
            release.wait(2)
            return []

        self.renderer._load_wind.side_effect = slow
        self.draw()
        self.assertTrue(started.wait(1))
        for _ in range(20):
            self.canvas._center_lon += 0.00001
            self.draw()
        self.assertEqual(self.renderer._load_wind.call_count, 1)
        self.assertFalse(release.is_set())
        release.set()
        self.drain()
        self.assertIsNotNone(self.renderer._states[self.canvas].frame)

    def test_pan_and_animated_zoom_reuse_frame_without_grib_work(self):
        self.draw()
        self.drain()
        for _ in range(20):
            self.canvas._center_lon += 0.00001
            self.draw()
        self.canvas._zoom_animating = True
        for zoom in (12.1, 12.4, 12.8, 13):
            self.canvas._zoom = zoom
            self.draw()
        self.renderer._load_wind.assert_called_once()
        self.canvas._zoom_animating = False
        self.draw()
        self.drain()
        self.assertEqual(self.renderer._load_wind.call_count, 2)

    def test_barbs_stay_out_of_raster_and_keep_size_through_zoom(self):
        # A lone sample produces no fill: all visible ink must be live barbs.
        self.renderer._load_wind.return_value = [
            (-122.4, np.array([47.6]), np.array([0.0]), np.array([10.0]))
        ]
        self.renderer._load_pressure = Mock(return_value=[])
        self.draw()
        self.drain()
        job, background = self.renderer._states[self.canvas].frame
        background.flush()
        self.assertFalse(any(bytes(background.get_data())))
        self.canvas._zoom_animating = True
        for zoom in (12, 12.3, 13, 14):
            self.canvas._zoom = zoom
            self.clear_output()
            self.draw()
            self.surface.flush()
            pixels = np.ndarray(
                (300, 400, 4),
                dtype=np.uint8,
                buffer=self.surface.get_data(),
                strides=(self.surface.get_stride(), 4, 1),
            )
            ys, xs = np.nonzero(pixels[:, :, 3])
            self.assertGreater(len(xs), 0)
            # Same position/size at each zoom; never a stretched preview.
            bounds = (xs.min(), ys.min(), xs.max(), ys.max())
            if zoom == 12:
                initial = bounds
            self.assertEqual(bounds, initial)
        self.renderer._load_wind.assert_called_once()

    def test_land_filter_is_disabled_by_default(self):
        self.renderer._load_wind.return_value = [
            (-122.4, np.array([47.6]), np.array([0.0]), np.array([10.0])),
            (-122.41, np.array([47.61]), np.array([0.0]), np.array([10.0])),
        ]
        self.renderer._load_pressure = Mock(return_value=[])
        self.renderer._land_mask.contains = Mock()
        self.draw()
        self.drain()
        job, _ = self.renderer._states[self.canvas].frame
        np.testing.assert_array_equal(job.barb_flat[2], [47.6, 47.61])
        self.assertIsNone(job.barb_land)
        self.assertEqual(self.renderer._land_mask.contains.call_count, 0)

    def test_stationary_repaints_reuse_geometry_but_zoom_reprojects(self):
        self.draw()
        self.drain()
        with patch.object(
            WindPainter,
            "live_geometry_for_view",
            wraps=WindPainter.live_geometry_for_view,
        ) as build:
            self.draw()
            self.draw()
            self.assertEqual(build.call_count, 1)
            self.canvas._zoom_animating = True
            self.canvas._zoom += 0.5
            self.draw()
            self.draw()
            self.assertEqual(build.call_count, 2)
        self.renderer._load_wind.assert_called_once()

    def test_density_changes_rebuild_foreground_without_reloading_weather(self):
        self.draw()
        self.drain()
        frame = self.renderer._states[self.canvas].frame
        with patch.object(
            WindPainter,
            "live_geometry_for_view",
            wraps=WindPainter.live_geometry_for_view,
        ) as build:
            self.draw()
            standard_step = build.call_args.kwargs["step"]
            self.settings.gribBarbDensity = "dense"
            self.draw()
            self.assertLess(build.call_args.kwargs["step"], standard_step)
            self.settings.gribBarbDensity = "all"
            self.draw()
            self.assertEqual(build.call_args.kwargs["step"], 0)
            self.assertIsNone(build.call_args.kwargs["max_count"])
            self.assertEqual(build.call_count, 3)
        self.assertIs(self.renderer._states[self.canvas].frame, frame)
        self.renderer._load_wind.assert_called_once()

    def test_forecast_style_sources_and_retina_invalidate_frame(self):
        self.draw()
        self.drain()
        for change in (
            lambda: setattr(self.clock, "time", self.clock.time + timedelta(hours=1)),
            lambda: setattr(self.settings, "gribArrowOpacity", 0.3),
            lambda: setattr(self.canvas, "factor", 2),
            lambda: setattr(self.manager, "gribs", [object()]),
        ):
            change()
            self.draw()
            self.drain()
        self.assertEqual(self.renderer._load_wind.call_count, 5)
        job, surface = self.renderer._states[self.canvas].frame
        self.assertEqual(surface.get_device_scale(), (2, 2))
        self.assertEqual(surface.get_width(), job.viewport.width * 2)

    def test_disabling_cancels_running_job_and_cannot_publish_stale_frame(self):
        self.renderer._start_next = Mock()
        self.draw()
        job = self.renderer._states[self.canvas].requested
        self.renderer.forget(self.canvas)
        self.assertTrue(job.cancel.is_set())
        self.renderer._completed(self.canvas, job, self.surface, None)
        self.assertNotIn(self.canvas, self.renderer._states)
        self.assertFalse(self.renderer._pending)

    def test_no_sources_clears_overlay_and_destroy_releases_map(self):
        self.draw()
        self.drain()
        self.manager.gribs.clear()
        self.draw()
        self.assertNotIn(self.canvas, self.renderer._states)
        self.renderer.forget(self.canvas)
        self.assertNotIn(self.canvas, self.renderer._states)

    def test_old_forecast_completion_cannot_replace_new_time(self):
        self.renderer._start_next = Mock()
        self.draw()
        old = self.renderer._states[self.canvas].requested
        self.clock.time += timedelta(hours=1)
        self.draw()
        self.assertTrue(old.cancel.is_set())
        self.renderer._completed(self.canvas, old, self.surface, None)
        self.assertIsNone(self.renderer._states[self.canvas].frame)

    def test_continuous_scrubbing_publishes_running_frame_and_queues_latest(self):
        started, release = Event(), Event()
        self.addCleanup(release.set)
        data = self.renderer._load_wind.return_value

        def slow(job):
            started.set()
            release.wait(2)
            return data

        self.renderer._load_wind.side_effect = slow
        published = []
        self.canvas.queue_draw.side_effect = lambda: published.append(
            self.renderer._states[self.canvas].frame[0].palette[0]
        )
        self.draw()
        self.assertTrue(started.wait(1))
        running = self.renderer._active_job
        for _ in range(10):
            self.clock.time += timedelta(minutes=1)
            self.draw()
        latest = self.renderer._states[self.canvas].requested
        self.assertFalse(running.cancel.is_set())
        self.assertEqual(len(self.renderer._pending), 1)
        self.assertIs(self.renderer._pending[self.canvas], latest)
        release.set()
        self.drain()
        self.assertEqual(published, [running.palette[0], self.clock.time])
        self.assertEqual(self.renderer._load_wind.call_count, 2)
        self.assertIs(self.renderer._states[self.canvas].frame[0], latest)

    def test_scrubbing_then_disabling_cancels_active_and_queued_frames(self):
        self.renderer._start_next = Mock()
        self.draw()
        running = self.renderer._states[self.canvas].requested
        self.renderer._active_canvas = self.canvas
        self.renderer._active_job = running
        self.clock.time += timedelta(minutes=1)
        self.draw()
        queued = self.renderer._states[self.canvas].requested
        self.renderer.forget(self.canvas)
        self.assertTrue(running.cancel.is_set())
        self.assertTrue(queued.cancel.is_set())
        self.renderer._completed(self.canvas, running, self.surface, None)
        self.assertNotIn(self.canvas, self.renderer._states)

    def clear_output(self):
        self.cr.save()
        self.cr.set_operator(cairo.OPERATOR_CLEAR)
        self.cr.paint()
        self.cr.restore()

    def test_playback_keeps_gradient_until_latest_forecast_is_complete(self):
        self.draw()
        self.drain()
        self.clear_output()
        self.draw()
        self.surface.flush()
        initial = bytes(self.surface.get_data())
        self.assertTrue(any(initial))
        self.renderer._start_next = Mock()
        jobs = []
        for _ in range(5):
            self.clock.time += timedelta(hours=1)
            self.clear_output()
            self.draw()
            self.surface.flush()
            self.assertEqual(bytes(self.surface.get_data()), initial)
            jobs.append(self.renderer._states[self.canvas].requested)
        # An obsolete forecast must not replace the visible image.
        latest = jobs[-1]
        replacement = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, latest.viewport.width, latest.viewport.height
        )
        context = cairo.Context(replacement)
        context.set_source_rgba(0, 0, 1, 0.3)
        context.paint()
        self.renderer._completed(self.canvas, jobs[0], replacement, None)
        self.clear_output()
        self.draw()
        self.surface.flush()
        self.assertEqual(bytes(self.surface.get_data()), initial)
        self.renderer._completed(self.canvas, latest, replacement, None)
        self.clear_output()
        self.draw()
        self.surface.flush()
        self.assertNotEqual(bytes(self.surface.get_data()), initial)
        self.assertTrue(any(bytes(self.surface.get_data())))

    def test_playback_preview_never_keeps_removed_sources_or_old_style(self):
        self.draw()
        self.drain()
        self.renderer._start_next = Mock()
        for change in (
            lambda: setattr(self.settings, "gribArrowOpacity", 0.2),
            lambda: setattr(self.canvas, "factor", 2),
            lambda: setattr(self.manager, "gribs", [object()]),
        ):
            self.settings.gribArrowOpacity = 0.8
            self.settings.gribArrowOnGround = True
            self.canvas.factor = 1
            self.manager.gribs = [self.source]
            change()
            self.clear_output()
            self.draw()
            self.surface.flush()
            self.assertFalse(any(bytes(self.surface.get_data())))

    def test_forecast_without_data_replaces_preview_with_empty_image(self):
        self.draw()
        self.drain()
        self.clock.time += timedelta(hours=1000)
        self.renderer._load_wind.return_value = []
        self.clear_output()
        self.draw()
        self.surface.flush()
        self.assertTrue(any(bytes(self.surface.get_data())))
        self.drain()
        self.clear_output()
        self.draw()
        self.surface.flush()
        self.assertFalse(any(bytes(self.surface.get_data())))

    def test_land_mask_is_reused_between_painters(self):
        wind_on_ground.cache_clear()
        self.addCleanup(wind_on_ground.cache_clear)
        with patch(
            "gweatherrouting.rendering.windpainter.point_in_country",
            return_value=True,
        ) as mask:
            self.assertTrue(wind_on_ground(49, -123))
            self.assertTrue(wind_on_ground(49, -123))
            mask.assert_called_once()

    def test_bad_forecast_does_not_hide_other_sources(self):
        self.renderer._start_next = Mock()
        self.manager.gribs = [object(), object()]
        self.draw()
        job = self.renderer._states[self.canvas].requested
        unavailable, available = Mock(), Mock()
        unavailable.get_wind.side_effect = ValueError("Missing forecast hour")
        point = (180, 6, (47.6, -122.4))
        available.get_wind.return_value = [point]
        self.renderer._worker_grib = Mock(side_effect=[unavailable, available])
        from gweatherrouting.rendering.windrenderer import WindOverlayRenderer

        result = WindOverlayRenderer._load_wind(self.renderer, job)
        self.assertEqual(len(result), 1)
        lon, lats, dirs, speeds = result[0]
        self.assertEqual(lon, point[2][1])
        self.assertEqual(list(lats), [point[2][0]])
        self.assertEqual(list(dirs), [point[0]])
        self.assertEqual(list(speeds), [point[1]])

    def test_overlapping_forecasts_use_newest_once_and_keep_older_gap_coverage(self):
        from gweatherrouting.rendering.windrenderer import WindOverlayRenderer

        old = SimpleNamespace(start_time=self.clock.time)
        new = SimpleNamespace(start_time=self.clock.time + timedelta(hours=6))
        old_points = [(90, 3, (0, 0)), (90, 3, (1, 0)), (90, 3, (0, 1))]
        new_points = [(180, 8, (0, 0)), (180, 8, (1, 0))]
        self.renderer._worker_grib = lambda source: SimpleNamespace(
            get_wind=lambda *_: new_points if source is new else old_points
        )
        job = SimpleNamespace(
            viewport=self.canvas,
            palette=(self.clock.time,),
            charts=((old,), (new,)),
            cancel=Event(),
        )
        data = WindOverlayRenderer._load_wind(self.renderer, job)
        self.assertEqual(len(data), 2)
        # Column 0: new points at lon=0
        self.assertEqual(data[0][0], 0.0)
        self.assertEqual(list(data[0][1]), [0.0, 1.0])
        self.assertEqual(list(data[0][2]), [180.0, 180.0])
        self.assertEqual(list(data[0][3]), [8.0, 8.0])
        # Column 1: old point at lon=1
        self.assertEqual(data[1][0], 1.0)
        self.assertEqual(list(data[1][1]), [0.0])
        self.assertEqual(list(data[1][2]), [90.0])
        self.assertEqual(list(data[1][3]), [3.0])
        self.assertEqual(sum(len(col[1]) for col in data), 3)

    def test_gradient_has_no_alpha_seam_at_fractional_cell_boundaries(self):
        painter = WindPainter(self.settings)
        view = SimpleNamespace(
            render_cancelled=lambda: False,
            convert_geographic_to_screen_batch=lambda lat, lon: (
                10.25 + lon * 90,
                10.25 + lat * 90,
            ),
        )
        data = [
            (
                float(lon),
                np.array([float(lat) for lat in range(3)]),
                np.full(3, 90.0),
                np.full(3, 8.0),
            )
            for lon in range(3)
        ]
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 200)
        painter._draw_color_fill(view, cairo.Context(surface), data, 1)
        surface.flush()
        pixels = np.ndarray(
            (200, 200, 4),
            dtype=np.uint8,
            buffer=surface.get_data(),
            strides=(surface.get_stride(), 4, 1),
        )
        # Cross a cell edge and the intersection of four cells. Equal wind
        # speeds should give equal opacity even at subpixel mesh boundaries.
        alpha = pixels[98:103, 98:103, 3]
        self.assertGreater(int(alpha.min()), 0)
        self.assertLessEqual(int(alpha.max()) - int(alpha.min()), 1)

    def test_painter_keeps_colored_fill_and_barbs(self):
        self.draw()
        self.drain()
        job, surface = self.renderer._states[self.canvas].frame
        surface.flush()
        self.assertTrue(any(bytes(surface.get_data())))
        self.assertIsInstance(self.renderer._painter(self.settings), WindPainter)
