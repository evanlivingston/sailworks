"""The UI-neutral routing worker matches the existing wind-only engine."""

from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
import unittest

import weatherrouting

from gweatherrouting.core import Core
from gweatherrouting.core.gribmanager import GribManager
from gweatherrouting.core.geo.poi import POI
from gweatherrouting.core.routingjob import (
    RouteRequest,
    RoutingCancelledError,
    RoutingWind,
    calculate_route,
    freeze_wind_sources,
)


class ConstantWind:
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
        self.assertEqual(
            result.points,
            tuple(
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
            ),
        )
        self.assertEqual(updates[-1][0], 100)
        self.assertEqual(result.provenance["currents"], "disabled")

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

    def test_no_forecast_is_a_clear_error_without_a_saved_partial_route(self):
        with self.assertRaisesRegex(weatherrouting.RoutingNoWindError, "forecast"):
            calculate_route(
                self.core, self.request, (), (), Event(), lambda *args: None
            )
