"""The UI-neutral routing worker matches the existing wind-only engine."""

from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import math

import weatherrouting

from gweatherrouting.core import Core
from gweatherrouting.core.gribmanager import GribManager
from gweatherrouting.core.geo.poi import POI
from gweatherrouting.core.routingjob import (
    RouteRequest,
    RoutingCancelledError,
    RoutingWind,
    _integrate_wind_terminal_leg,
    _linear_route_with_missing_destination_wind,
    calculate_route,
    freeze_wind_sources,
)
from gweatherrouting.core.marine.routing import KNOT


class ConstantWind:
    model_name = "GFS"
    last_forecast = 384
    name = "constant"
    start_time = datetime(2026, 10, 1)

    def get_wind_at(self, instant, lat, lon):
        return 270, 14


class TestRoutingJob(unittest.TestCase):
    def setUp(self):
        self.core = Core.__new__(Core)
        self.core.grib_manager = GribManager.__new__(GribManager)
        self.core.grib_manager.gribs = [ConstantWind()]
        self.request = RouteRequest(
            "test",
            (0, 0),
            (0, 0.15),
            datetime(2026, 10, 1, 12),
            str(Path(__file__).parents[1] / "gweatherrouting/data/polars/first317.pol"),
            weatherrouting.list_routing_algorithms()[0]["class"],
            False,
        )

    def test_result_is_identical_to_existing_core_route(self):
        updates = []
        result = calculate_route(
            self.core,
            self.request,
            self.core.grib_manager.gribs,
            (),
            Event(),
            lambda percent, points: updates.append((percent, points)),
        )
        old = self.core.create_routing(
            self.request.algorithm,
            self.request.polar_file,
            POI("end", self.request.destination),
            self.request.departure,
            self.request.start,
            (),
            True,
        )
        while not old.end:
            expected = old.step()
        legacy = tuple(
            (
                p.pos[0],
                p.pos[1],
                p.time.strftime("%m/%d/%Y, %H:%M:%S"),
                p.twd,
                p.tws,
                p.speed,
                p.brg,
            )
            for p in expected.path
        )
        self.assertEqual(result.points[: len(legacy)], legacy)
        self.assertEqual(result.points[-1][:2], self.request.destination)
        self.assertGreater(result.points[-1][2], result.points[-2][2])
        self.assertEqual(updates[-1][0], 100)
        self.assertTrue(any(getattr(points, "isochrones", ()) for _, points in updates))
        self.assertTrue(
            all(
                isinstance(p, tuple)
                for _, update in updates
                for front in getattr(update, "isochrones", ())
                for p in front
            )
        )
        self.assertEqual(result.provenance["currents"], "disabled")

    def test_wind_only_terminal_leg_integrates_two_kilometres_to_exact_target(self):
        start = datetime(2026, 10, 1, 12)
        end = (0.0, 0.018)
        candidate = weatherrouting.IsoPoint(
            (0.0, 0.0), time=start, twd=0.0, tws=10.0, speed=6.0
        )
        router = SimpleNamespace(
            grib=SimpleNamespace(get_wind_at=lambda *_: (270.0, 10.0 * KNOT)),
            polar=SimpleNamespace(get_speed=lambda *_: 6.0),
            points_validity=None,
            point_validity=None,
            lines_validity=None,
            line_validity=None,
        )
        result = SimpleNamespace(
            path=[candidate], position=candidate.pos, time=start, isochrones=[]
        )
        with patch(
            "gweatherrouting.core.routingjob._original_linear_route",
            return_value=result,
        ):
            completed = _linear_route_with_missing_destination_wind(
                router, None, start, 1, candidate.pos, end, None
            )
        self.assertEqual(completed.path[-1].pos, end)
        self.assertEqual(completed.position, end)
        self.assertGreater(completed.time, candidate.time)
        distance = 111_319.5 * (end[1] - candidate.pos[1])
        expected_seconds = distance / (6 * KNOT)
        self.assertAlmostEqual(
            (completed.time - candidate.time).total_seconds(), expected_seconds, delta=1
        )
        self.assertAlmostEqual(completed.path[-1].twd, math.radians(270), places=12)
        self.assertTrue(
            all(a.time < b.time for a, b in zip(completed.path, completed.path[1:]))
        )

    def test_wind_only_terminal_leg_does_not_complete_blocked_or_missing_path(self):
        start = datetime(2026, 10, 1, 12)
        end = (0.0, 0.018)
        candidate = weatherrouting.IsoPoint(
            (0.0, 0.0), time=start, twd=0.0, tws=10.0, speed=6.0
        )
        result = SimpleNamespace(
            path=[candidate], position=candidate.pos, time=start, isochrones=[]
        )
        blocked = SimpleNamespace(
            grib=SimpleNamespace(get_wind_at=lambda *_: (270.0, 10.0 * KNOT)),
            polar=SimpleNamespace(get_speed=lambda *_: 6.0),
            points_validity=lambda points: [False] * len(points),
            point_validity=None,
            lines_validity=None,
            line_validity=None,
        )
        with patch(
            "gweatherrouting.core.routingjob._original_linear_route",
            return_value=result,
        ):
            rejected = _linear_route_with_missing_destination_wind(
                blocked, None, start, 1, candidate.pos, end, None
            )
        self.assertEqual(rejected.path, [])
        self.assertEqual(rejected.position, candidate.pos)

        line_blocked = SimpleNamespace(
            grib=SimpleNamespace(get_wind_at=lambda *_: (270.0, 10.0 * KNOT)),
            polar=SimpleNamespace(get_speed=lambda *_: 6.0),
            points_validity=None,
            point_validity=None,
            lines_validity=None,
            line_validity=lambda *_: False,
        )
        line_result = SimpleNamespace(
            path=[candidate], position=candidate.pos, time=start, isochrones=[]
        )
        with patch(
            "gweatherrouting.core.routingjob._original_linear_route",
            return_value=line_result,
        ):
            rejected_line = _linear_route_with_missing_destination_wind(
                line_blocked, None, start, 1, candidate.pos, end, None
            )
        self.assertEqual(rejected_line.path, [])

        missing = SimpleNamespace(
            grib=SimpleNamespace(get_wind_at=lambda *_: None),
            polar=SimpleNamespace(get_speed=lambda *_: 6.0),
            points_validity=None,
            point_validity=None,
            lines_validity=None,
            line_validity=None,
        )
        missing_result = SimpleNamespace(
            path=[candidate], position=candidate.pos, time=start, isochrones=[]
        )
        with (
            patch(
                "gweatherrouting.core.routingjob._original_linear_route",
                return_value=missing_result,
            ),
            self.assertRaises(weatherrouting.RoutingNoWindError),
        ):
            _linear_route_with_missing_destination_wind(
                missing, None, start, 1, candidate.pos, end, None
            )

    def test_wind_only_terminal_leg_rejects_sailing_beyond_one_hour_budget(self):
        start = datetime(2026, 10, 1, 12)
        end = (0.0, 0.018)
        candidate = weatherrouting.IsoPoint(
            (0.0, 0.0), time=start, twd=0.0, tws=10.0, speed=0.001
        )
        router = SimpleNamespace(
            grib=SimpleNamespace(get_wind_at=lambda *_: (270.0, 10.0 * KNOT)),
            polar=SimpleNamespace(get_speed=lambda *_: 0.001),
            points_validity=None,
            point_validity=None,
            lines_validity=None,
            line_validity=None,
        )
        result = SimpleNamespace(
            path=[candidate], position=candidate.pos, time=start, isochrones=[]
        )
        with patch(
            "gweatherrouting.core.routingjob._original_linear_route",
            return_value=result,
        ):
            rejected = _linear_route_with_missing_destination_wind(
                router, None, start, 1, candidate.pos, end, None
            )
        self.assertEqual(rejected.path, [])
        self.assertEqual(rejected.position, candidate.pos)

    def test_cancellation_interrupts_wind_query_and_does_not_become_missing_wind(self):
        cancel = Event()
        wind = RoutingWind(self.core.grib_manager.gribs, cancel)
        cancel.set()
        with self.assertRaises(RoutingCancelledError):
            wind.get_wind_at(self.request.departure, 0, 0)
        with self.assertRaises(RoutingCancelledError):
            calculate_route(
                self.core,
                self.request,
                self.core.grib_manager.gribs,
                (),
                cancel,
                lambda *args: None,
            )

    def test_snapshot_keeps_legacy_forecast_sampling_with_private_dataset_handles(self):
        import numpy as np
        from osgeo import gdal
        from gweatherrouting.core.grib import Grib

        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "wind.tif")
            dataset = gdal.GetDriverByName("GTiff").Create(
                path, 4, 3, 4, gdal.GDT_Float32
            )
            dataset.SetGeoTransform((-124.5, 1, 0, 50.5, 0, -1))
            for index, (hour, element, value) in enumerate(
                ((0, "UGRD", 2), (0, "VGRD", 4), (3, "UGRD", 8), (3, "VGRD", 10)), 1
            ):
                band = dataset.GetRasterBand(index)
                band.SetMetadata(
                    {"GRIB_ELEMENT": element, "GRIB_FORECAST_SECONDS": str(hour * 3600)}
                )
                band.WriteArray(np.full((3, 4), value, dtype=np.float32))
            band = dataset = None
            source = Grib(path, "test", "test", [], self.request.departure, 3)
            worker = RoutingWind(freeze_wind_sources([source]), Event())
            private = worker.manager.gribs[0]
            self.assertIsNot(private.dataset, source.dataset)
            self.assertEqual(private._index_data.cache_info().maxsize, 2)
            for minutes in (0, 60, 90, 180):
                instant = self.request.departure + timedelta(minutes=minutes)
                self.assertEqual(
                    worker.get_wind_at(instant, 49, -123),
                    source.get_wind_at(instant, 49, -123),
                )
            worker.close()
            self.assertFalse(worker.manager.gribs)
            self.assertIsNone(private.dataset)
            self.assertEqual(private._index_data.cache_info().currsize, 0)

    def test_wind_only_limit_rejects_stationary_unsafe_candidates(self):
        from dataclasses import replace

        with self.assertRaisesRegex(ValueError, "exceed the configured TWS limit"):
            calculate_route(
                self.core,
                replace(self.request, tws_max_kn=1),
                self.core.grib_manager.gribs,
                (),
                Event(),
                lambda *_: None,
            )

    def test_no_forecast_is_a_clear_error_without_a_saved_partial_route(self):
        with self.assertRaisesRegex(weatherrouting.RoutingNoWindError, "forecast"):
            calculate_route(
                self.core, self.request, (), (), Event(), lambda *args: None
            )
