"""Analytical space/time fields and cross-file selection, independent of legacy answers."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import math
import unittest

import numpy as np
from osgeo import gdal
from pyproj import Transformer

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribmanager import GribManager
from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.core.routingjob import RoutingWind, freeze_wind_sources
from gweatherrouting.rendering.windcursor import WindCursorSampler
from gweatherrouting.rendering.windrenderer import DisplayGrib, WindOverlayRenderer
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

START = datetime(2026, 1, 1)


class TestGribSampling(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.files = []

    def file(
        self,
        records,
        transform=(-0.5, 1, 0, -0.5, 0, 1),
        start=START,
        end=3,
        process=96,
        projection=None,
    ):
        path = str(Path(self.directory.name) / (str(len(self.files)) + ".tif"))
        first = np.asarray(records[0][2])
        dataset = gdal.GetDriverByName("GTiff").Create(
            path, first.shape[1], first.shape[0], len(records), gdal.GDT_Float64
        )
        dataset.SetGeoTransform(transform)
        if projection:
            dataset.SetProjection(projection)
        for band_id, (element, hour, array, *extra) in enumerate(records, 1):
            metadata = dict(
                GRIB_ELEMENT=element,
                GRIB_FORECAST_SECONDS=str(hour * 3600),
                GRIB_REF_TIME=str(start.timestamp()),
                GRIB_VALID_TIME=str((start + timedelta(hours=hour)).timestamp()),
                GRIB_SHORT_NAME=(
                    "10-HTGL"
                    if element in ("UGRD", "VGRD")
                    else "0-SFC" if element == "GUST" else "0-MSL"
                ),
                GRIB_IDS="CENTER=7 SUBCENTER=0",
                GRIB_PDS_PDTN="0",
                GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES=(
                    f"2 2 2 0 {process} 0 0 1 {hour} 103 0 10 255 0 0"
                ),
            )
            if extra:
                metadata.update(extra[0])
            band = dataset.GetRasterBand(band_id)
            band.SetMetadata(metadata)
            band.SetNoDataValue(-9999)
            band.WriteArray(np.asarray(array, dtype=float))
        band = dataset = None
        source = Grib(path, str(len(self.files)), "test", [], start, end)
        self.files.append(source)
        self.addCleanup(source.close)
        return source

    def field(self, hour, size=2):
        y, x = np.mgrid[:size, :size]
        return [
            ("UGRD", hour, 2 + 4 * x + 6 * y + 2 * hour),
            ("VGRD", hour, 1 + 2 * x - 2 * y + hour),
            ("PRMSL", hour, 100000 + 100 * x + 200 * y + 100 * hour),
        ]

    def assert_uv(self, sampler, instant, lat, lon, u, v):
        actual = sampler.wind_at(instant, lat, lon)
        self.assertIsNotNone(actual)
        direction, speed = actual
        self.assertAlmostEqual(speed, math.hypot(u, v), places=10)
        self.assertAlmostEqual(
            direction, math.degrees(math.atan2(-u, -v)) % 360, places=10
        )

    def test_local_forecast_model_uses_metadata_and_does_not_change_sampling_identity(
        self,
    ):
        from gweatherrouting.core.grib import forecast_model_name
        from types import SimpleNamespace

        source = self.file(self.field(0))
        self.assertEqual(Grib.parse_metadata(source.path).model_name, "GFS")
        reopened = Grib.parse(source.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.model_name, "GFS")
        before = source.sampling.records[0].identity
        source.model_name = "Label only"
        self.assertEqual(source.sampling.records[0].identity, before)
        dataset = SimpleNamespace(
            GetGeoTransform=lambda: (0, 0.0225, 0, 0, 0, -0.0225),
            GetProjection=lambda: "Pole rotation (GRIB convention)",
        )
        metadata = {
            "GRIB_IDS": "CENTER=54(Montreal)",
            "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": "2 2 2 50 50",
        }
        self.assertEqual(forecast_model_name(dataset, metadata), "HRDPS")
        dataset.GetGeoTransform = lambda: (0, 0.09, 0, 0, 0, -0.09)
        self.assertEqual(forecast_model_name(dataset, metadata), "Unknown")
        metadata["GRIB_IDS"] = "CENTER=7"
        metadata["GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"] = "2 2 2 0 83"
        self.assertEqual(forecast_model_name(dataset, metadata), "HRRR")
        metadata["GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"] = "2 2 2 0 999"
        self.assertEqual(forecast_model_name(dataset, metadata), "Unknown")

    def test_bilinear_and_fractional_time_have_analytical_answers(self):
        source = self.file(self.field(0) + self.field(3))
        sampler = source.sampler()
        for seconds in (0, 1, 900, 1800, 3599, 5400, 10800):
            hour = seconds / 3600
            self.assert_uv(
                sampler,
                START + timedelta(seconds=seconds),
                0.25,
                0.75,
                2 + 4 * 0.75 + 6 * 0.25 + 2 * hour,
                1 + 2 * 0.75 - 2 * 0.25 + hour,
            )
            pressure = sampler.sample(
                "pressure", START + timedelta(seconds=seconds), 0.25, 0.75
            )[0, 0]
            self.assertAlmostEqual(pressure, 1000 + 0.75 + 2 * 0.25 + hour)

    def test_vector_blend_does_not_average_direction_angles(self):
        def uv(angle):
            return -10 * math.sin(math.radians(angle)), -10 * math.cos(
                math.radians(angle)
            )

        a, b = uv(350), uv(10)
        source = self.file(
            [
                ("UGRD", 0, [[a[0]]]),
                ("VGRD", 0, [[a[1]]]),
                ("UGRD", 2, [[b[0]]]),
                ("VGRD", 2, [[b[1]]]),
            ],
            end=2,
        )
        self.assert_uv(
            source.sampler(),
            START + timedelta(hours=1),
            0,
            0,
            0,
            -10 * math.cos(math.radians(10)),
        )

    def test_brackets_and_components_can_live_in_separate_files(self):
        files = [self.file([record]) for record in self.field(0) + self.field(3)]
        sampler = WeatherSampler(files)
        self.assert_uv(sampler, START + timedelta(minutes=30), 0.5, 0.5, 8, 1.5)
        self.assertAlmostEqual(
            sampler.sample("pressure", START + timedelta(minutes=30), 0.5, 0.5)[0, 0],
            1002,
        )
        self.assertGreater(len(sampler.last_sources), 1)

    def test_exact_single_record_and_declared_coverage_fallback(self):
        source = self.file(self.field(1), end=3)
        for hour in (0, 1, 2, 3):
            self.assert_uv(source.sampler(), START + timedelta(hours=hour), 0, 0, 4, 2)
        self.assertIsNone(source.get_wind_at(START - timedelta(seconds=1), 0, 0))
        self.assertIsNone(
            source.get_wind_at(START + timedelta(hours=3, seconds=1), 0, 0)
        )
        self.assertIsNone(source.get_wind_at(START, 0, 2))

    def test_level_and_parameter_pairing_ignore_upper_air_wind(self):
        source = self.file(
            self.field(0)
            + [
                ("UGRD", 0, [[999] * 2] * 2, {"GRIB_SHORT_NAME": "500-ISBL"}),
                ("VGRD", 0, [[999] * 2] * 2, {"GRIB_SHORT_NAME": "500-ISBL"}),
            ]
        )
        self.assert_uv(source.sampler(), START, 0.5, 0.5, 7, 1)
        missing = self.file([("UGRD", 0, [[3]])])
        self.assertIsNone(missing.get_wind_at(START, 0, 0))

    def test_newest_run_fills_only_its_coverage_and_never_blends_runs(self):
        old = self.file(self.field(0) + self.field(3), end=6)
        new = self.file(
            [("UGRD", 0, [[100, 100], [100, 100]]), ("VGRD", 0, [[0, 0], [0, 0]])],
            start=START + timedelta(hours=1),
            end=1,
        )
        sampler = WeatherSampler([old, new])
        self.assert_uv(sampler, START + timedelta(hours=1.5), 0.5, 0.5, 100, 0)
        self.assert_uv(sampler, START + timedelta(hours=2.5), 0.5, 0.5, 12, 3.5)
        self.assert_uv(sampler, START + timedelta(hours=0.5), 0.5, 0.5, 8, 1.5)

    def test_distinct_models_and_ensemble_members_do_not_form_brackets(self):
        a = self.file(self.field(0), end=3, process=10)
        b = self.file(self.field(3), end=3, process=20)
        # Equal issue times: first enabled model wins via a single-record fallback.
        self.assert_uv(WeatherSampler([a, b]), START + timedelta(hours=1), 0, 0, 2, 1)
        self.assert_uv(WeatherSampler([b, a]), START + timedelta(hours=1), 0, 0, 8, 4)

    def test_equal_issue_times_prefer_finest_grid_then_largest_coverage(self):
        coarse = self.file(
            [("UGRD", 0, [[100, 100], [100, 100]]), ("VGRD", 0, [[0, 0], [0, 0]])],
            end=1,
            process=10,
        )
        fine = self.file(
            [
                ("UGRD", 0, [[1] * 4] * 4),
                ("VGRD", 0, [[2] * 4] * 4),
            ],
            transform=(-0.5, 0.5, 0, -0.5, 0, 0.5),
            end=1,
            process=20,
        )
        for files in ([coarse, fine], [fine, coarse]):
            self.assert_uv(WeatherSampler(files), START, 0.5, 0.5, 1, 2)
        small = self.file(
            [("UGRD", 0, [[10, 10], [10, 10]]), ("VGRD", 0, [[0, 0], [0, 0]])],
            end=1,
            process=30,
        )
        large = self.file(
            [("UGRD", 0, [[20] * 3] * 3), ("VGRD", 0, [[0] * 3] * 3)],
            end=1,
            process=40,
        )
        for files in ([small, large], [large, small]):
            self.assert_uv(WeatherSampler(files), START, 0.5, 0.5, 20, 0)

    def test_missing_corners_do_not_contaminate_exact_nodes_or_bridge_holes(self):
        source = self.file(
            [("UGRD", 0, [[2, -9999], [2, 2]]), ("VGRD", 0, [[4, 4], [4, 4]])]
        )
        self.assert_uv(source.sampler(), START, 0, 0, 2, 4)
        self.assertIsNone(source.get_wind_at(START, 0.5, 0.5))
        fallback = self.file(
            [("UGRD", 0, [[1, 1], [1, 1]]), ("VGRD", 0, [[1, 1], [1, 1]])]
        )
        self.assert_uv(WeatherSampler([source, fallback]), START, 0.5, 0.5, 1, 1)

    def test_reversed_axes_and_dateline_use_real_cell_geometry(self):
        source = self.file(
            [("UGRD", 0, [[2, 4], [6, 8]]), ("VGRD", 0, [[0, 0], [0, 0]])],
            transform=(181.5, -1, 0, 1.5, 0, -1),
        )
        self.assert_uv(source.sampler(), START, 0.5, -179.5, 5, 0)
        self.assertIsNone(source.get_wind_at(START, 0.5, 177))

    def test_global_grid_wraps_across_storage_seam(self):
        source = self.file(
            [("UGRD", 0, [[0, 2, 4, 6], [0, 2, 4, 6]]), ("VGRD", 0, [[0] * 4] * 2)],
            transform=(-45, 90, 0, -0.5, 0, 1),
        )
        self.assert_uv(source.sampler(), START, 0.5, -45, 3, 0)
        self.assert_uv(source.sampler(), START, 0.5, 315, 3, 0)

    def test_projected_grid_interpolates_in_native_coordinates(self):
        from pyproj import CRS

        source = self.file(
            [("UGRD", 0, [[0, 2], [4, 6]]), ("VGRD", 0, [[0, 0], [0, 0]])],
            transform=(-500, 1000, 0, -500, 0, 1000),
            projection=CRS("EPSG:3857").to_wkt(),
        )
        lon, lat = Transformer.from_crs(
            "EPSG:3857", "EPSG:4326", always_xy=True
        ).transform(500, 500)
        self.assert_uv(source.sampler(), START, lat, lon, 3, 0)

    def test_grid_relative_lambert_wind_rotates_before_interpolation(self):
        from pyproj import CRS

        crs = CRS(
            "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 "
            "+lon_0=-97.5 +R=6371229 +units=m"
        )
        to_grid = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        to_world = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        x, y = to_grid.transform(-124, 48)
        source = self.file(
            [("UGRD", 0, [[10, 10], [10, 10]]), ("VGRD", 0, [[0, 0], [0, 0]])],
            transform=(x - 1500, 3000, 0, y - 1500, 0, 3000),
            projection=crs.to_wkt(),
        )
        with patch(
            "gweatherrouting.core.gribsampling.vector_flags", return_value=[True, True]
        ):
            sampler = source.sampler()
        corners = [
            to_world.transform(x + dx, y + dy)
            for dx, dy in ((0, 0), (3000, 0), (0, 3000), (3000, 3000))
        ]
        angles = [
            math.sin(math.radians(38.5)) * math.radians(lon + 97.5)
            for lon, lat in corners
        ]
        expected = (
            np.mean([10 * math.cos(a) for a in angles]),
            np.mean([-10 * math.sin(a) for a in angles]),
        )
        lon, lat = to_world.transform(x + 1500, y + 1500)
        batch = sampler.sample("wind", START, np.array([lat]), np.array([lon]))[0]
        np.testing.assert_allclose(batch, expected, atol=1e-6)
        direction, speed = sampler.wind_at(START, lat, lon)
        self.assertAlmostEqual(speed, np.hypot(*expected), places=6)
        self.assertAlmostEqual(
            direction, math.degrees(math.atan2(-batch[0], -batch[1])) % 360
        )

    def test_rotated_geographic_crs_transforms_query_and_display_nodes(self):
        from pyproj import CRS

        crs = CRS(
            "+proj=ob_tran +o_proj=longlat +o_lon_p=0 +o_lat_p=36.08852 "
            "+lon_0=65.305142 +R=6371229"
        )
        source = self.file(self.field(0), projection=crs.to_wkt())
        lon, lat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(
            0.5, 0.5
        )
        self.assert_uv(source.sampler(), START, lat, lon, 7, 1)
        self.assertIsNotNone(source.sampling.backward)
        nodes = source.sampling.nodes(((lat - 2, lon - 2), (lat + 2, lon + 2)))
        self.assertEqual(len(nodes), 4)

    def test_all_consumers_use_same_cross_file_space_time_values(self):
        sources = [self.file(self.field(0)), self.file(self.field(3))]
        instant = START + timedelta(minutes=30)
        manager = GribManager.__new__(GribManager)
        manager.gribs = sources
        expected = manager.get_wind_at(instant, 0.5, 0.5)
        self.assertIsNotNone(expected)
        cursor = WindCursorSampler(Mock())
        self.addCleanup(cursor.close)
        self.assertEqual(cursor._sample(sources, instant, 0.5, 0.5), expected)
        routing = RoutingWind(freeze_wind_sources(sources), Event())
        self.addCleanup(routing.close)
        self.assertEqual(routing.get_wind_at(instant, 0.5, 0.5), expected)
        private = [DisplayGrib(source) for source in sources]
        for source in private:
            self.addCleanup(source.close)
        points = WeatherSampler(private).grid("wind", instant, ((0, 0), (1, 1)))
        dirs, spds, lats, lons = points
        for direction, speed, lat, lon in zip(dirs, spds, lats, lons):
            self.assertEqual(manager.get_wind_at(instant, lat, lon), (direction, speed))
        renderer = WindOverlayRenderer(Mock())
        renderer._worker_grib = lambda source: private[sources.index(source)]
        renderer._viewport_bounds = lambda job: ((0, 0), (1, 1))
        job = SimpleNamespace(
            charts=tuple((s,) for s in sources), palette=(instant,), cancel=Event()
        )
        # The display sampler and the grid answer with the same points.
        self.assertEqual(
            sorted(
                (float(lon), float(lat), float(direction), float(speed))
                for lon, col_lats, col_dirs, col_speeds in renderer._load_wind(job)
                for lat, direction, speed in zip(col_lats, col_dirs, col_speeds)
            ),
            sorted(
                (float(lon), float(lat), float(direction), float(speed))
                for direction, speed, lat, lon in zip(dirs, spds, lats, lons)
            ),
        )

    def test_subhour_record_times_are_not_rounded_to_hours(self):
        source = self.file(self.field(0) + self.field(0.5), end=0.5)
        self.assert_uv(
            source.sampler(), START + timedelta(minutes=15), 0.5, 0.5, 7.5, 1.25
        )

    def test_gfs_analysis_and_forecast_process_ids_share_one_run(self):
        analysis = self.file(self.field(0), process=81)
        forecast = self.file(self.field(3), process=96)
        self.assert_uv(
            WeatherSampler([analysis, forecast]),
            START + timedelta(minutes=30),
            0.5,
            0.5,
            8,
            1.5,
        )

    def test_ensemble_members_are_separate_even_with_same_run_and_process(self):
        a = self.file(
            [
                (
                    e,
                    h,
                    v,
                    {
                        "GRIB_PDS_PDTN": "1",
                        "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": (
                            "2 2 2 0 96 0 0 1 0 103 0 10 255 0 0 1 1 10"
                        ),
                    },
                )
                for e, h, v in self.field(0)
            ]
        )
        b = self.file(
            [
                (
                    e,
                    h,
                    v,
                    {
                        "GRIB_PDS_PDTN": "1",
                        "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": (
                            "2 2 2 0 96 0 0 1 3 103 0 10 255 0 0 1 2 10"
                        ),
                    },
                )
                for e, h, v in self.field(3)
            ]
        )
        self.assert_uv(
            WeatherSampler([a, b]), START + timedelta(hours=1), 0.5, 0.5, 7, 1
        )

    def test_missing_temporal_bracket_uses_valid_single_record(self):
        source = self.file(
            self.field(0) + [("UGRD", 3, [[-9999] * 2] * 2), ("VGRD", 3, [[4] * 2] * 2)]
        )
        self.assert_uv(source.sampler(), START + timedelta(hours=1), 0.5, 0.5, 7, 1)

    def test_rotated_affine_grid_uses_all_transform_coefficients(self):
        source = self.file(
            [("UGRD", 0, [[0, 2], [4, 6]]), ("VGRD", 0, [[0, 0], [0, 0]])],
            transform=(0, 1, 0.25, 0, 0.5, 1),
        )
        # Pixel (1,1) is the midpoint between four sample centers.
        self.assert_uv(source.sampler(), START, 1.5, 1.25, 3, 0)

    def test_newest_run_is_shared_by_cursor_and_current_assisted_routing(self):
        from gweatherrouting.core.marine.routing import FrozenRoutingWind

        old = self.file(self.field(0) + self.field(3))
        new = self.file(
            [("UGRD", 0, [[100] * 2] * 2), ("VGRD", 0, [[0] * 2] * 2)],
            start=START + timedelta(hours=1),
        )
        instant = START + timedelta(hours=1.5)
        manager = GribManager.__new__(GribManager)
        manager.gribs = [old, new]
        expected = manager.get_wind_at(instant, 0.5, 0.5)
        cursor = WindCursorSampler(Mock())
        self.addCleanup(cursor.close)
        self.assertEqual(cursor._sample([old, new], instant, 0.5, 0.5), expected)
        routing = FrozenRoutingWind(manager)
        self.assertEqual(routing.get_wind_at(instant, 0.5, 0.5), expected)
        self.assertEqual(routing.used, {new.name})

    def test_display_nodes_cover_world_and_dateline_viewports(self):
        source = self.file(self.field(0), transform=(178.5, 1, 0, -0.5, 0, 1))
        world = source.get_wind(START, ((-90, -180), (90, 180)))
        self.assertEqual(len(world), 4)
        crossing = source.get_wind(START, ((0, 179), (1, -179)))
        self.assertEqual(len(crossing), 4)

    def test_pressure_units_hpa_are_preserved(self):
        source = self.file([("PRMSL", 0, [[1013]], {"GRIB_UNIT": "[hPa]"})])
        self.assertEqual(source.sampler().sample("pressure", START, 0, 0)[0, 0], 1013)

    def test_mixed_time_batch_matches_scalar_queries_and_source_provenance(self):
        from gweatherrouting.core.marine.batchwind import ArrayWind
        from gweatherrouting.core.marine.routing import FrozenRoutingWind

        sources = [self.file(self.field(0)), self.file(self.field(3))]
        wind = FrozenRoutingWind(SimpleNamespace(gribs=sources))
        positions = np.array([[0.2, 0.3], [0.5, 0.5], [1, 1], [2, 2]])
        times = np.array(
            [
                int((START + timedelta(minutes=m)).timestamp() * 1e6)
                for m in (10, 30, 90, 180)
            ]
        )
        result, names = ArrayWind(wind).query_us(positions, times)
        expected = [
            wind.get_wind_at(datetime.fromtimestamp(t / 1e6), *p)
            for p, t in zip(positions, times)
        ]
        np.testing.assert_allclose(
            result,
            np.array([x if x is not None else (np.nan, np.nan) for x in expected]),
            equal_nan=True,
            rtol=1e-13,
        )
        self.assertEqual(names[0], ", ".join(s.name for s in sources))
        self.assertIsNone(names[-1])

    def test_renderer_releases_evicted_handles_after_cross_file_sampling(self):
        sources = [self.file([record]) for record in self.field(0) + self.field(3)]
        renderer = WindOverlayRenderer(Mock())
        renderer._source_retention_limit = 2
        job = SimpleNamespace(
            charts=tuple((s,) for s in sources),
            palette=(START + timedelta(minutes=30),),
            cancel=Event(),
        )
        renderer._viewport_bounds = lambda job: ((0, 0), (1, 1))
        opened = []
        original = renderer._worker_grib

        def capture(source):
            private = original(source)
            opened.append(private)
            return private

        renderer._worker_grib = capture
        self.assertTrue(renderer._load_wind(job))
        self.assertEqual(len(renderer._gribs), 2)
        self.assertTrue(all(g.dataset is None for g in opened[:-2]))
        self.assertTrue(all(g.dataset is not None for g in opened[-2:]))
        renderer.forget(object())
        self.assertFalse(renderer._gribs)
        self.assertTrue(all(g.dataset is None for g in opened))

    def test_gdal_mask_is_missing_data_without_a_nodata_sentinel(self):
        source = self.file(self.field(0))
        source.dataset = None
        data = gdal.Open(source.path, gdal.GA_Update)
        band = data.GetRasterBand(1)
        band.DeleteNoDataValue()
        band.CreateMaskBand(0)
        band.GetMaskBand().WriteArray(np.array([[255, 0], [255, 255]], dtype=np.uint8))
        band = data = None
        source.dataset = gdal.Open(source.path)
        self.assertIsNotNone(source.get_wind_at(START, 0, 0))
        self.assertIsNone(source.get_wind_at(START, 0.5, 0.5))

    def test_multi_run_file_end_does_not_extend_an_older_record(self):
        later = START + timedelta(hours=6)
        records = self.field(0) + [
            (
                e,
                0,
                v,
                {
                    "GRIB_REF_TIME": str(later.timestamp()),
                    "GRIB_VALID_TIME": str(later.timestamp()),
                },
            )
            for e, h, v in self.field(3)
        ]
        source = self.file(records, end=12)
        self.assertIsNotNone(source.get_wind_at(START, 0.5, 0.5))
        self.assertIsNone(source.get_wind_at(START + timedelta(hours=3), 0.5, 0.5))
        self.assertIsNotNone(source.get_wind_at(later, 0.5, 0.5))

    def test_decoded_cache_is_bounded(self):
        source = self.file(sum((self.field(hour) for hour in range(12)), []), end=11)
        for hour in range(12):
            source.get_wind_at(START + timedelta(hours=hour), 0.5, 0.5)
        self.assertLessEqual(len(source.sampling.arrays), 6)

    def test_gust_is_interpolated_in_space_and_time_and_missing_gust_is_none(self):
        start_gust = np.array([[2, 4], [6, 8]], dtype=float)
        end_gust = np.array([[8, 10], [12, 14]], dtype=float)
        source = self.file(
            self.field(0)
            + [("GUST", 0, start_gust)]
            + self.field(3)
            + [("GUST", 3, end_gust)]
        )
        sampler = WeatherSampler([source])
        # At the fractional grid point the spatial means are 5 and 11 m/s;
        # the 90-minute time midpoint is therefore 8 m/s.
        self.assertAlmostEqual(
            sampler.gust_at(START + timedelta(minutes=90), 0.5, 0.5), 8.0
        )

        no_gust = self.file(self.field(0))
        self.assertIsNone(WeatherSampler([no_gust]).gust_at(START, 0.5, 0.5))

    def test_gust_rejects_non_surface_levels(self):
        source = self.file(
            [
                (
                    "GUST",
                    0,
                    [[12, 12], [12, 12]],
                    {"GRIB_SHORT_NAME": "500-ISBL"},
                )
            ]
        )
        self.assertIsNone(WeatherSampler([source]).gust_at(START, 0.5, 0.5))
