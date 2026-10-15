"""Model selection, cache reuse, forecast-hour bracketing and view coverage."""

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch, MagicMock

from gweatherrouting.core.forecastselection import (
    ForecastSelection,
    forecast_source,
    IMPORTED,
)
from gweatherrouting.core.grib import Grib
from gweatherrouting.core.modeldownloads import (
    newest_entries,
    install_download,
    ModelForecastRecovery,
)
from gweatherrouting.core.gribsources import NOAAGFSSource, NOAAHRRRSource
from gweatherrouting.core.marine.fields import Area
from gweatherrouting.core.routingjob import calculate_route
from gweatherrouting.qt.weathercoverage import coverage_text
from test import test_grib_sampling as sampling_fixture
from test.test_grib_sampling import START
from test import test_routingjob as routing_fixture


class ModelWeatherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = sampling_fixture.TestGribSampling()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def source(self, process=96):
        raw = self.fixture.file(
            self.fixture.field(0) + self.fixture.field(3), process=process
        )
        grib = Grib.parse(raw.path)
        self.addCleanup(grib.close)
        return grib

    def test_selection_round_trips_and_routing_requires_wind(self):
        selection = ForecastSelection(("NOAA HRRR",), 24, 3, ("pressure",), (0, 12, 24))
        self.assertEqual(ForecastSelection.from_json(selection.to_json()), selection)
        self.assertEqual(selection.options().fields, ("pressure",))
        self.assertEqual(selection.options(routing=True).fields, ("pressure", "wind"))
        with self.assertRaisesRegex(ValueError, "Select at least"):
            ForecastSelection(()).options(routing=True)

    def test_newest_hrrr_run_that_actually_covers_route_time(self):
        cycles = [datetime(2026, 10, 15, h, tzinfo=timezone.utc) for h in (13, 12)]
        query = datetime(2026, 10, 16, 14, tzinfo=timezone.utc)
        selection = ForecastSelection(("NOAA HRRR",), 12, 3, ("pressure",))
        with patch.object(NOAAHRRRSource, "_get_available_cycles", return_value=cycles):
            entries, issues = newest_entries(selection, (45, -125, 50, -120), query)
        self.assertFalse(issues)
        source, row = entries[0]
        config = json.loads(row[4])
        self.assertEqual(config["cycle"], "12")
        self.assertEqual(config["hours"], 27)
        self.assertIn("wind", config["fields"])
        self.assertEqual(source.bounds, (45, -125, 50, -120))

    def test_manual_download_chooses_one_recent_run_per_checked_model(self):
        with patch(
            "gweatherrouting.core.modeldownloads.PROVIDERS",
            {"NOAA GFS": NOAAGFSSource, "NOAA HRRR": NOAAHRRRSource},
        ):
            entries, issues = newest_entries(
                ForecastSelection(("NOAA GFS", "NOAA HRRR")), (45, -125, 50, -120)
            )
        self.assertFalse(issues)
        self.assertEqual(
            [source.name for source, _ in entries], ["NOAA GFS", "NOAA HRRR"]
        )

    def test_covering_cache_avoids_network_and_unchecked_models(self):
        source = self.source()
        recovery = ModelForecastRecovery(
            ForecastSelection(), self.directory.name, Event()
        )
        with patch("gweatherrouting.core.modeldownloads.newest_entries") as network:
            result = recovery.download(
                [source], [(0.5, 0.5)], START + timedelta(hours=1), Area(0, 0, 1, 1)
            )
        self.assertEqual(result, [])
        network.assert_not_called()
        self.assertEqual(forecast_source(source), "NOAA GFS")

    def test_unavailable_selected_provider_never_downloads_unchecked_gfs(self):
        selected = MagicMock()
        selected.name = "NOAA HRRR"
        selected.get_download_list.side_effect = OSError("service unavailable")
        gfs = MagicMock()
        recovery = ModelForecastRecovery(
            ForecastSelection(("NOAA HRRR",)), self.directory.name, Event()
        )
        with patch(
            "gweatherrouting.core.modeldownloads.PROVIDERS",
            {"NOAA HRRR": lambda: selected, "NOAA GFS": gfs},
        ):
            result = recovery.download(
                [], [(0.5, 0.5)], datetime.now(timezone.utc), Area(0, 0, 1, 1)
            )
        self.assertFalse(result)
        self.assertIn("service unavailable", recovery.issues[0])
        gfs.assert_not_called()

    def test_atomic_installs_preserve_same_run_downloads_for_other_areas(self):
        fixture = self.source()
        source = MagicMock()

        def download(identifier, directory, progress, cancel=None):
            destination = Path(directory) / "same-run.grib2"
            shutil.copyfile(fixture.path, destination)
            return str(destination)

        source.download.side_effect = download
        first = install_download(
            source, ["", "", "", "", "first"], self.directory.name, lambda _: None
        )
        second = install_download(
            source, ["", "", "", "", "second"], self.directory.name, lambda _: None
        )
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(Path(first.path).read_bytes(), Path(second.path).read_bytes())
        self.assertEqual(len(list(Path(self.directory.name).glob("*.grib2"))), 2)

    def test_selected_cached_routing_matches_legacy_calculation(self):
        case = routing_fixture.TestRoutingJob()
        case.setUp()
        self.addCleanup(case.doCleanups)
        args = (
            case.core,
            case.request,
            case.core.grib_manager.gribs,
            (),
            Event(),
            lambda *_: None,
        )
        legacy = calculate_route(*args)
        args = (
            case.core,
            replace(case.request, forecast_selection=ForecastSelection()),
            *args[2:],
        )
        with patch("gweatherrouting.core.routingjob.GRIB_DIR", self.directory.name):
            selected = calculate_route(*args)
        self.assertEqual(selected.points, legacy.points)
        self.assertEqual(selected.isochrones, legacy.isochrones)
        self.assertEqual(selected.provenance["weather_models"], ["NOAA GFS"])

    def test_current_routing_uses_only_checked_model_even_if_unchecked_run_is_newer(
        self,
    ):
        from test import test_routingoptions as routing_options
        from test.test_current_routing import current_field

        case = routing_options.TestRoutingOptions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        selected = routing_options.CurrentWind()
        selected.name, selected.path, selected.model_name = "hrrr", "hrrr", "HRRR"
        unchecked = routing_options.CurrentWind()
        unchecked.name, unchecked.path = "gfs", "gfs"
        unchecked.start_time += timedelta(hours=1)
        case.core.grib_manager.gribs = [unchecked, selected]
        case.core.marine_manager.fields.add(current_field(east=0.4))
        request = replace(
            case.request,
            use_currents=True,
            departure=selected.start_time,
            destination=(0, 0.005),
            forecast_selection=ForecastSelection(("NOAA HRRR",)),
        )
        with (
            patch("gweatherrouting.core.routingjob.GRIB_DIR", self.directory.name),
            patch("gweatherrouting.core.modeldownloads.newest_entries") as network,
        ):
            result = case.calculate(request, confirm=lambda report: True)
        network.assert_not_called()
        self.assertEqual(result.provenance["weather_models"], ["NOAA HRRR"])
        self.assertEqual(result.provenance["wind"]["actual_sources"], ["hrrr"])

    def test_unchecked_cached_model_cannot_feed_routing(self):
        case = routing_fixture.TestRoutingJob()
        case.setUp()
        request = replace(
            case.request, forecast_selection=ForecastSelection((IMPORTED,))
        )
        from weatherrouting import RoutingNoWindError

        with (
            patch("gweatherrouting.core.routingjob.GRIB_DIR", self.directory.name),
            self.assertRaises(RoutingNoWindError),
        ):
            calculate_route(
                case.core,
                request,
                case.core.grib_manager.gribs,
                (),
                Event(),
                lambda *_: None,
            )

    def test_view_notice_distinguishes_space_time_model_and_field_gaps(self):
        source = self.source()
        models, fields = ("NOAA GFS",), ("wind", "pressure")
        readers = {"NOAA GFS": [source]}
        self.assertEqual(
            coverage_text(models, fields, START, [(0.5, 0.5)], readers), ""
        )
        outside = coverage_text(models, fields, START, [(40, 40)], readers)
        self.assertIn("wind unavailable", outside)
        self.assertIn("pressure unavailable", outside)
        partial = coverage_text(models, fields, START, [(0.5, 0.5), (40, 40)], readers)
        self.assertIn("wind partial", partial)
        expired = coverage_text(
            models, fields, START + timedelta(hours=4), [(0.5, 0.5)], readers
        )
        self.assertIn("unavailable", expired)
        other_model = coverage_text(
            ("NOAA HRRR",), ("wind",), START, [(0.5, 0.5)], readers
        )
        self.assertEqual(other_model, "HRRR: wind unavailable")
