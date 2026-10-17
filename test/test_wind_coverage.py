"""Automatic wind recovery, forecast prefetch, cache reuse and atomic downloads."""

import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gweatherrouting.core.gribsources.noaagfs import NOAAGFSSource
from gweatherrouting.core.marine.chs import DownloadCancelledError
from gweatherrouting.core.marine.fields import FieldSelection
from gweatherrouting.core.marine.routing import (
    CurrentRoutingEnvironment,
    DataGap,
    FrozenRoutingWind,
    RoutingDataGapError,
    step_with_coverage,
)
from gweatherrouting.core.marine.windcoverage import WindCoverageRecovery
from test.test_current_routing import START


class TestWindCoverage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        old = SimpleNamespace(
            name="gfs_old.grib2",
            path="old.grib2",
            start_time=START,
            get_wind_at=lambda t, *_: (
                (0, 10) if t <= START + timedelta(hours=1) else None
            ),
        )
        self.new = SimpleNamespace(
            name="gfs_new.grib2",
            path="new.grib2",
            start_time=START,
            get_wind_at=lambda *_: (90, 5),
        )
        self.wind = FrozenRoutingWind(SimpleNamespace(gribs=[old]))
        self.env = CurrentRoutingEnvironment(self.wind, FieldSelection([]))
        self.routing = SimpleNamespace(
            current_environment=self.env,
            position=(0, 0),
            track=[(0, 0), (1, 1)],
            wp=1,
            time=START,
        )
        self.source = Mock()
        self.source._get_available_cycles.return_value = [START]
        self.source.download.return_value = str(self.directory / "new.grib2")
        self.save = Mock()
        self.recovery = WindCoverageRecovery(
            self.routing, self.save, self.directory, lambda: self.source
        )
        self.env.wind_recovery = self.recovery
        self.addCleanup(self.recovery.close)
        parser = patch(
            "gweatherrouting.core.marine.windcoverage.Grib.parse", return_value=self.new
        )
        parser.start()
        self.addCleanup(parser.stop)
        self.gap = DataGap(
            (0.5, 0.5), START + timedelta(hours=2), "wind", "no_coverage"
        )

    def test_wind_gap_downloads_and_retries_same_step(self):
        self.routing.step = Mock(side_effect=[RoutingDataGapError([self.gap]), "done"])
        report = Mock()
        self.assertEqual(step_with_coverage(self.routing, Mock(), True, report), "done")
        self.source.download.assert_called_once()
        config = json.loads(self.source.download.call_args.args[0])
        self.assertEqual((config["start_hour"], config["hours"]), (0, 27))
        self.assertIn("cache_key", config)
        self.assertEqual(self.wind.get_wind_at(START, 0, 0), (0, 10))
        self.assertEqual(self.wind.get_wind_at(self.gap.time, 0.5, 0.5), (90, 5))
        self.save.assert_called_once_with([self.new])

    def test_automatic_downloads_off_do_not_fetch_wind(self):
        self.routing.step = Mock(side_effect=RoutingDataGapError([self.gap]))
        with self.assertRaises(RoutingDataGapError):
            step_with_coverage(self.routing, Mock(), False, Mock())
        self.source.download.assert_not_called()

    def test_existing_coverage_needs_no_download_or_provenance_change(self):
        gap = DataGap((0, 0), START, "wind", "no_coverage")
        self.recovery.recover([gap])
        self.assertEqual(self.wind.used, set())
        self.source.download.assert_not_called()

    def test_prefetch_overlaps_search_and_actual_gap_joins_download(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def download(*args, **kwargs):
            started.set()
            release.wait(2)
            return str(self.directory / "new.grib2")

        self.source.download.side_effect = download
        self.recovery.prefetch()
        self.assertTrue(started.wait(1))
        self.routing.step = Mock(return_value="running")
        self.assertEqual(
            step_with_coverage(self.routing, Mock(), True, Mock()), "running"
        )
        self.assertEqual(len(self.wind.gribs), 1)
        release.set()
        self.recovery.recover([self.gap])
        self.source.download.assert_called_once()
        self.assertEqual(len(self.wind.gribs), 2)

    def test_saved_automatic_forecast_reused_before_network(self):
        (self.directory / "gfs_20261012_00z_120h_saved.grib2").write_bytes(b"cached")
        self.recovery.recover([self.gap])
        self.source.download.assert_not_called()
        self.assertEqual(len(self.wind.gribs), 2)

    def test_non_gfs_model_is_not_silently_replaced(self):
        self.wind.gribs[0].name = "regional-model.grib2"
        with self.assertRaisesRegex(ValueError, "will not be replaced"):
            self.recovery.recover([self.gap])
        self.source.download.assert_not_called()

    def test_cancel_and_unavailable_forecast_stop_without_unbounded_retries(self):
        self.env.cancel.set()
        with self.assertRaises(DownloadCancelledError):
            self.recovery.recover([self.gap])
        self.env.cancel.clear()
        self.source.download.side_effect = ValueError("offline")
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "offline"):
                self.recovery.recover([self.gap])
        self.source.download.assert_called_once()

    def test_scenario_adjustments_apply_to_downloaded_sources(self):
        self.wind.override = SimpleNamespace(_dir_offset=10, _speed_factor=2)
        self.recovery.recover([self.gap])
        self.assertEqual(self.wind.get_wind_at(START, 0, 0), (10, 20))
        self.assertEqual(self.wind.get_wind_at(self.gap.time, 0.5, 0.5), (100, 10))

    def test_newest_run_wins_in_scalar_and_batch_with_older_gap_fallback(self):
        import numpy as np
        from gweatherrouting.core.marine.batchwind import ArrayWind
        from gweatherrouting.core.gribmanager import GribManager

        old = self.wind.gribs[0]
        old.get_wind_at = lambda *_: (0, 10)
        self.new.start_time = START + timedelta(hours=1)
        self.new.get_wind_at = lambda t, *_: (
            (90, 5) if t >= self.new.start_time else None
        )
        self.wind.add_gribs([self.new])
        times = [START, self.gap.time]
        expected = [(0, 10), (90, 5)]
        manager = GribManager.__new__(GribManager)
        manager.gribs = [old, self.new]
        initially_sorted = FrozenRoutingWind(manager)
        for time, value in zip(times, expected):
            self.assertEqual(self.wind.get_wind_at(time, 0, 0), value)
            self.assertEqual(manager.get_wind_at(time, 0, 0), value)
            self.assertEqual(initially_sorted.get_wind_at(time, 0, 0), value)
        values, sources = ArrayWind(self.wind).query_us(
            np.zeros((2, 2)), np.array([round(t.timestamp() * 1e6) for t in times])
        )
        np.testing.assert_allclose(values, expected)
        self.assertEqual(list(sources), [old.name, self.new.name])

    def test_cache_prefers_issue_time_and_stale_cache_does_not_block_refresh(self):
        stale = SimpleNamespace(**vars(self.new))
        stale.start_time = START - timedelta(hours=6)
        stale.path = "stale.grib2"
        paths = [
            self.directory / "gfs_20261012_00z_120h_a.grib2",
            self.directory / "gfs_20261012_00z_120h_z.grib2",
        ]
        for path in paths:
            path.write_bytes(b"cache")
        with patch(
            "gweatherrouting.core.marine.windcoverage.Grib.parse",
            side_effect=[self.new, stale],
        ):
            self.assertIs(self.recovery._cached_forecast([self.gap]), self.new)
        with patch(
            "gweatherrouting.core.marine.windcoverage.Grib.parse",
            side_effect=[stale, stale, self.new],
        ):
            self.recovery.refresh()
        self.source.download.assert_called_once()
        self.assertEqual(self.wind.used, set())

    def test_refresh_reuses_latest_cache_or_enabled_run_without_network(self):
        (self.directory / "gfs_20261012_00z_120h_saved.grib2").write_bytes(b"cache")
        self.recovery.refresh()
        self.source.download.assert_not_called()
        self.save.assert_called_once_with([self.new])
        self.recovery.refresh()
        self.source.download.assert_not_called()
        self.assertEqual(self.save.call_count, 1)

    def test_refresh_preserves_non_gfs_and_checks_cancellation(self):
        self.wind.gribs[0].name = "regional.grib2"
        self.recovery.refresh()
        self.source.download.assert_not_called()
        self.wind.gribs[0].name = "gfs_old.grib2"
        self.env.cancel.set()
        with self.assertRaises(DownloadCancelledError):
            self.recovery.refresh()

    def test_gap_message_gives_action_without_sample_diagnostics(self):
        message = str(RoutingDataGapError([self.gap] * 100))
        self.assertIn("Load a wind forecast", message)
        self.assertNotIn("no_coverage", message)
        self.assertNotIn("sample", message)
        self.assertLess(len(message), 180)


class TestAtomicGFSDownload(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = NOAAGFSSource()
        self.config = dict(date="20261012", cycle="00", hours=75, start_hour=72)
        self.response = Mock()
        self.response.iter_content.return_value = [b"GRIB"]

    def test_only_bracketing_forecast_steps_are_downloaded(self):
        with patch(
            "gweatherrouting.core.gribsources.noaagfs.requests.get",
            return_value=self.response,
        ) as get:
            path = self.source.download(
                json.dumps(self.config), str(self.directory), Mock()
            )
        self.assertEqual(Path(path).read_bytes(), b"GRIBGRIB")
        self.assertEqual(
            [c.kwargs["params"]["file"] for c in get.call_args_list],
            ["gfs.t00z.pgrb2.0p25.f072", "gfs.t00z.pgrb2.0p25.f075"],
        )
        self.assertTrue(
            all(c.kwargs["timeout"] == (10, 60) for c in get.call_args_list)
        )

    def test_failed_or_cancelled_download_does_not_replace_saved_file(self):
        target = self.directory / "gfs_20261012_00z_75h.grib2"
        target.write_bytes(b"original")
        with patch(
            "gweatherrouting.core.gribsources.noaagfs.requests.get",
            side_effect=ValueError("offline"),
        ):
            with self.assertRaisesRegex(ValueError, "offline"):
                self.source.download(
                    json.dumps(self.config), str(self.directory), Mock()
                )
        cancel = threading.Event()

        def chunks(**kwargs):
            cancel.set()
            yield b"partial"

        self.response.iter_content.side_effect = chunks
        with patch(
            "gweatherrouting.core.gribsources.noaagfs.requests.get",
            return_value=self.response,
        ):
            with self.assertRaises(DownloadCancelledError):
                self.source.download(
                    json.dumps(self.config), str(self.directory), Mock(), cancel
                )
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(self.directory.glob("*.part")), [])


class TestSupplementalGrib(unittest.TestCase):
    def test_forecast_subset_with_nonzero_first_hour_fills_expiry_gap(self):
        import math
        import numpy as np
        from osgeo import gdal
        from gweatherrouting.core.grib import Grib
        from gweatherrouting.core.marine.batchwind import ArrayWind

        data = gdal.GetDriverByName("MEM").Create("", 5, 5, 4, gdal.GDT_Float64)
        data.SetGeoTransform((-0.5, 0.25, 0, 0.5, 0, -0.25))
        for i, (element, hour, value) in enumerate(
            [("UGRD", 12, 4), ("VGRD", 12, 3), ("UGRD", 15, 4), ("VGRD", 15, 3)], 1
        ):
            band = data.GetRasterBand(i)
            band.SetMetadata(
                dict(GRIB_ELEMENT=element, GRIB_FORECAST_SECONDS=str(hour * 3600))
            )
            band.WriteArray(np.full((5, 5), value))
        supplement = Grib.__new__(Grib)
        supplement.dataset = data
        supplement.start_time = START
        supplement.end_time = START + timedelta(hours=15)
        supplement.path = "supplement.grib2"
        supplement.name = "gfs_supplement.grib2"
        old = SimpleNamespace(
            name="gfs_old.grib2",
            path="old.grib2",
            start_time=START,
            get_wind_at=lambda t, *_: (
                (0, 10) if t <= START + timedelta(hours=12) else None
            ),
        )
        wind = FrozenRoutingWind(SimpleNamespace(gribs=[old]))
        wind.add_gribs([supplement])
        self.assertEqual(
            wind.get_wind_at(START + timedelta(hours=12), 0.1, 0.1), (0, 10)
        )
        instant = START + timedelta(hours=12, minutes=1)
        expected = ((math.degrees(math.atan2(-4, -3)) + 360) % 360, 5)
        np.testing.assert_allclose(wind.get_wind_at(instant, 0.1, 0.1), expected)
        values, sources = ArrayWind(wind).query_us(
            np.array([[0.1, 0.1]]), np.array([round(instant.timestamp() * 1e6)])
        )
        np.testing.assert_allclose(values[0], expected)
        self.assertEqual(sources[0], supplement.name)
