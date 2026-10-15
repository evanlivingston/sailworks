"""Whole-frontier parity, numerical boundaries and cancellation regressions."""

import io
import math
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from weatherrouting import Polar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gweatherrouting.core.marine.batchwind import ArrayWind, instant
from gweatherrouting.core.marine.chs import DownloadCancelledError
from gweatherrouting.core.marine.fields import FieldSelection
from gweatherrouting.core.marine.frontierrouting import (
    ArrayEnvironment,
    array_clock_compatible,
    polar_many,
)
from gweatherrouting.core.marine.routing import FrozenRoutingWind
from gweatherrouting.core.polarwrapper import PolarWrapper
from test.test_current_routing import START, current_field, setup_router


def table_polar():
    return Polar(
        "unused",
        io.StringIO("TWA 0 5 10 20\n0 0 0 0 0\n45 0 2 4 6\n90 0 3 5 8\n180 0 2 4 7\n"),
    )


class TestFrontierRouting(unittest.TestCase):
    def assert_frontiers_equal(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for a, b in zip(actual, expected):
            self.assertEqual(a.prev_idx, b.prev_idx)
            self.assertLessEqual(abs((a.time - b.time).total_seconds()), 1e-5)
            np.testing.assert_allclose(a.pos, b.pos, atol=1e-11, rtol=0)
            np.testing.assert_allclose(
                (a.twd, a.tws, a.speed, a.brg, a.next_wp_dist),
                (b.twd, b.tws, b.speed, b.brg, b.next_wp_dist),
                atol=1e-8,
                rtol=1e-8,
            )
            self.assertEqual(len(a.marine_samples), len(b.marine_samples))
            for m, n in zip(a.marine_samples, b.marine_samples):
                self.assertEqual(set(m), set(n))
                for key in m:
                    if isinstance(m[key], (float, np.floating)):
                        self.assertAlmostEqual(m[key], n[key], places=8)
                    elif key == "time":
                        from datetime import datetime

                        self.assertLessEqual(
                            abs(
                                (
                                    datetime.fromisoformat(m[key])
                                    - datetime.fromisoformat(n[key])
                                ).total_seconds()
                            ),
                            1e-5,
                        )
                    elif isinstance(m[key], dict):
                        self.assertEqual(set(m[key]), set(n[key]))
                        for subkey in m[key]:
                            self.assertAlmostEqual(
                                m[key][subkey], n[key][subkey], places=4
                            )
                    else:
                        self.assertEqual(m[key], n[key])

    def test_multiple_frontiers_preserve_pruning_parents_and_provenance(self):
        results = []
        for arrays in (False, True):
            router, env, _ = setup_router(
                current_field(east=-1, end_east=2, resolution=500),
                wind=SimpleNamespace(
                    get_wind_at=lambda *_: (0, 10), last_source="test"
                ),
            )
            router.use_numpy = arrays
            router.polar = PolarWrapper(table_polar(), 0.83)
            # Exercise both scalar and batched validity hooks on every RK2 leg.
            router.points_validity = lambda points: [p[0] < 0.008 for p in points]
            router.lines_validity = lambda lines: [line[3] > -0.01 for line in lines]
            router.point_validity = lambda lat, lon: lat > -0.01
            router.line_validity = lambda a, b, c, d: d < 0.1
            env.progress = Mock()
            previous = None
            for _ in range(3):
                previous = router.route(previous, START, 1, (0, 0), (0, 0.5))
            results.append(previous)
            self.assertEqual(env.progress.call_args_list[0].args[1:], (0, 1))
            self.assertEqual(
                env.progress.call_args_list[-1].args[1],
                env.progress.call_args_list[-1].args[2],
            )
        for a, b in zip(results[1].isochrones[1:], results[0].isochrones[1:]):
            self.assert_frontiers_equal(a, b)

    def test_nearest_corner_records_survive_deferred_materialization(self):
        field = replace(
            current_field(east=1, masked=True, resolution=500),
            latitudes=np.array([0, 0.004]),
            longitudes=np.array([0, 0.004]),
        )
        results = []
        for arrays in (False, True):
            router, env, _ = setup_router(
                field,
                wind=SimpleNamespace(
                    get_wind_at=lambda *_: (0, 10), last_source="test"
                ),
            )
            router.use_numpy = arrays
            env.selection.neighbor_valid = lambda *_: True
            result = router.route(None, START, 5 / 3600, (0.003, 0.003), (0.01, 0.01))
            results.append(result.isochrones[-1])
        self.assert_frontiers_equal(results[1], results[0])
        self.assertTrue(
            all(
                m["current_sampling"] == ("nearest_wet_cell",)
                for p in results[1]
                for m in p.marine_samples
            )
        )

    def test_naive_engine_clock_stays_naive_but_provenance_is_utc(self):
        router, _, _ = setup_router()
        local_start = START.astimezone().replace(tzinfo=None)
        result = router.route(None, local_start, 1, (0, 0), (0, 0.5))
        self.assertIsNone(result.time.tzinfo)
        self.assertEqual(result.time, local_start + timedelta(minutes=10))
        self.assertTrue(
            result.isochrones[-1][0].marine_samples[0]["time"].endswith("+00:00")
        )

    def test_daylight_saving_clock_changes_use_the_reference_path(self):
        try:
            zone = ZoneInfo("America/Los_Angeles")
        except ZoneInfoNotFoundError:
            self.skipTest("System time zone database unavailable")
        before = datetime(2026, 11, 1, 1, 55, tzinfo=zone)
        self.assertFalse(array_clock_compatible([before], 600))
        self.assertFalse(array_clock_compatible([before.replace(fold=1)], 600))
        self.assertTrue(array_clock_compatible([START], 600))
        router, _, _ = setup_router()
        # A zero-time step returns at the starting position. Its ambiguous
        # clock still forces the legacy route, rather than the UTC array clock.
        from unittest.mock import patch
        from gweatherrouting.core.marine.routing import RoutingDataGapError

        with patch(
            "gweatherrouting.core.marine.frontierrouting.route_frontier"
        ) as batch:
            with self.assertRaises(RoutingDataGapError):
                router.route(None, before.replace(fold=1), 0, (0, 0), (0, 0.5))
            batch.assert_not_called()

    def test_cancellation_from_progress_stops_before_sampling(self):
        router, env, _ = setup_router()
        env.progress = lambda *_: env.cancel.set()
        with self.assertRaises(DownloadCancelledError):
            router.route(None, START, 1, (0, 0), (0, 0.5))
        env.wind.get_wind_at.assert_not_called()

    def test_cancellation_during_nearest_corner_recovery_stops_the_batch(self):
        field = replace(
            current_field(masked=True, resolution=500),
            latitudes=np.array([0, 0.004]),
            longitudes=np.array([0, 0.004]),
        )
        _, env, _ = setup_router(field)
        env.selection.neighbor_valid = lambda *_: env.cancel.set() or True
        points = np.array([[0.003, 0.003], [0.003, 0.003]])
        times = np.full(2, round(START.timestamp() * 1e6), dtype=np.int64)
        with self.assertRaises(DownloadCancelledError):
            ArrayEnvironment(env).sample(np.arange(2), points, times, {})
        env.wind.get_wind_at.assert_not_called()

    def test_polar_arrays_match_table_boundaries_and_extrapolation(self):
        rng = np.random.default_rng(81)
        polar = table_polar()
        speeds = np.r_[rng.uniform(-5, 50, 200), polar.tws]
        angles = np.r_[rng.uniform(-1, 4, 200), polar.twa]
        for p in (polar, PolarWrapper(polar, 0.83)):
            np.testing.assert_allclose(
                polar_many(p, speeds, angles),
                [p.get_speed(w, a) for w, a in zip(speeds, angles)],
                rtol=1e-13,
                atol=1e-12,
            )

        # Custom speed implementations must not be bypassed by table inspection.
        class CustomPolar(Polar):
            def get_speed(self, *_):
                return 17

        custom = CustomPolar("unused", io.StringIO("TWA 0 5\n0 0 0\n90 1 2\n"))
        np.testing.assert_array_equal(polar_many(custom, speeds, angles), 17)

    def test_current_array_selection_matches_scalar_including_missing_counters(self):
        rng = np.random.default_rng(123)
        coarse = replace(
            current_field(east=2),
            latitudes=np.array([-2, 2]),
            longitudes=np.array([-2, 2]),
        )
        fields = [current_field(masked=True), coarse]
        points = rng.uniform(-3, 3, (200, 2))
        times = [
            START + timedelta(seconds=float(t))
            for t in rng.uniform(-60, 3700, len(points))
        ]
        scalar, batch = FieldSelection(fields), FieldSelection(fields)
        actual = batch.sample_arrays(points, np.array([t.timestamp() for t in times]))
        expected = [scalar.sample(*p, t) for p, t in zip(points, times)]
        for i, answer in enumerate(expected):
            self.assertEqual(actual.query(i), answer)
        self.assertEqual(batch.used, scalar.used)
        self.assertEqual(batch.missing, scalar.missing)

    def test_wind_batch_keeps_source_precedence_exceptions_and_override(self):
        broken = SimpleNamespace(
            name="broken", get_wind_at=Mock(side_effect=ValueError)
        )
        regional = SimpleNamespace(
            name="regional",
            get_wind_at=lambda t, lat, lon: (350, 10) if lon < 0 else None,
        )
        global_wind = SimpleNamespace(name="global", get_wind_at=lambda *_: (20, 7))
        override = SimpleNamespace(_dir_offset=25, _speed_factor=1.2)
        wind = FrozenRoutingWind(
            SimpleNamespace(gribs=[broken, regional, global_wind]), override
        )
        points = np.array([[0, -1], [0, 1], [0, -2]])
        times = np.full(3, round(START.timestamp() * 1e6), dtype=np.int64)
        actual, sources = ArrayWind(wind).query_us(points, times)
        np.testing.assert_allclose(
            actual, [wind.get_wind_at(instant(t), *p) for p, t in zip(points, times)]
        )
        self.assertEqual(list(sources), ["regional", "global", "regional"])
        self.assertEqual(wind.used, {"regional", "global"})

    def test_fractional_wind_grouping_matches_cell_edges_and_forecast_hours(self):
        from osgeo import gdal
        from gweatherrouting.core.grib import Grib

        data = gdal.GetDriverByName("MEM").Create("", 4, 4, 6, gdal.GDT_Float64)
        data.SetGeoTransform((-1.125, 0.25, 0, 1.125, 0, -0.25))
        for i, (element, hour) in enumerate(
            [(e, h) for h in (0, 3, 6) for e in ("UGRD", "VGRD")], 1
        ):
            band = data.GetRasterBand(i)
            band.SetMetadata(
                dict(GRIB_ELEMENT=element, GRIB_FORECAST_SECONDS=str(hour * 3600))
            )
            band.WriteArray(np.arange(16).reshape(4, 4) + hour + i)
        band = None
        grib = Grib.__new__(Grib)
        grib.dataset, grib.start_time, grib.end_time = (
            data,
            START,
            START + timedelta(hours=6),
        )
        grib.name = "test"
        wind = FrozenRoutingWind(SimpleNamespace(gribs=[grib]))
        points = np.array(
            [
                [0.5, -0.5],
                [0.75, -0.75],
                [0.6, -0.6],
                [0, 180],
                [0.5, -180],
                [-90, 0],
                [90, 0],
                [1, 0.1],
            ]
        )
        times = np.array(
            [
                round((START.timestamp() + h * 3600) * 1e6)
                for h in (0, 1.5, 3, 6, 3.001, 2.999, -1, 7)
            ],
            dtype=np.int64,
        )
        actual, _ = ArrayWind(wind).query_us(points, times)
        expected = [wind.get_wind_at(instant(t), *p) for p, t in zip(points, times)]
        np.testing.assert_allclose(
            actual,
            [a if a else (math.nan, math.nan) for a in expected],
            rtol=1e-13,
            atol=1e-12,
            equal_nan=True,
        )


if __name__ == "__main__":
    unittest.main()
