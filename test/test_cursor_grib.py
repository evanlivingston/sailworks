"""Cursor sampling preserves legacy wind values and releases private datasets."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

import numpy as np
from osgeo import gdal

from gweatherrouting.core.grib import Grib
from gweatherrouting.rendering.windcursor import CursorGrib, WindCursorSampler


class TestCursorGrib(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "wind.tif"
        dataset = gdal.GetDriverByName("GTiff").Create(
            str(self.path), 4, 3, 6, gdal.GDT_Float32
        )
        dataset.SetGeoTransform((-124.5, 1, 0, 50.5, 0, -1))
        for index, (hour, element, value) in enumerate(
            (
                (0, "UGRD", 2),
                (0, "VGRD", 4),
                (0, "GUST", 6),
                (3, "UGRD", 8),
                (3, "VGRD", 10),
                (3, "GUST", 12),
            ),
            1,
        ):
            band = dataset.GetRasterBand(index)
            band.SetMetadata(
                dict(
                    GRIB_ELEMENT=element,
                    GRIB_FORECAST_SECONDS=str(hour * 3600),
                    GRIB_SHORT_NAME="0-SFC" if element == "GUST" else "10-HTGL",
                    GRIB_UNIT="[m/s]",
                )
            )
            band.WriteArray(
                np.full((3, 4), value, dtype=np.float32)
                + np.arange(12, dtype=np.float32).reshape(3, 4) * 0.25
            )
        band = dataset = None
        self.start = datetime(2026, 1, 1)
        self.source = Grib(str(self.path), "wind", "test", [], self.start, 3)

    def test_private_sampling_matches_legacy_at_multiple_times_and_positions(self):
        private = CursorGrib(self.source)
        self.addCleanup(private.close)
        self.assertIsNot(private.dataset, self.source.dataset)
        for minutes in (0, 60, 90, 180):
            for lat, lon in ((49, -123), (48, -122), (49.25, -122.25)):
                with self.subTest(minutes=minutes, lat=lat):
                    instant = self.start + timedelta(minutes=minutes)
                    self.assertEqual(
                        private.get_wind_at(instant, lat, lon),
                        self.source.get_wind_at(instant, lat, lon),
                    )
        self.assertLessEqual(len(private._arrays), 4)
        private.close()
        self.assertFalse(private._arrays)
        self.assertIsNone(private.dataset)
        self.assertIsNotNone(self.source.dataset)

    def test_enabled_source_order_fallback_eviction_and_close(self):
        sampler = WindCursorSampler(Mock())
        self.addCleanup(sampler.close)
        unavailable = SimpleNamespace()
        # Unhashable/invalid sources are skipped; enabled-file order is preserved.
        expected = self.source.get_wind_at(self.start, 49, -123)
        self.assertEqual(
            sampler._sample([unavailable, self.source], self.start, 49, -123), expected
        )
        first = sampler._gribs[self.source]
        for _ in range(3):
            source = Grib(str(self.path), "another", "test", [], self.start, 3)
            self.assertEqual(sampler._sample([source], self.start, 49, -123), expected)
        self.assertEqual(len(sampler._gribs), 2)
        self.assertIsNone(first.dataset)
        held = list(sampler._gribs.values())
        sampler.close()
        self.assertFalse(sampler._gribs)
        self.assertTrue(all(grib.dataset is None for grib in held))

    def test_first_available_enabled_source_wins_and_missing_sources_fall_through(self):
        sampler = WindCursorSampler(Mock(), display_grib=lambda source: source)
        missing, first, second = Mock(), Mock(), Mock()
        missing.get_wind_at.side_effect = ValueError("No coverage")
        first.get_wind_at.return_value = (180, 10)
        second.get_wind_at.return_value = (270, 20)
        self.assertEqual(
            sampler._sample([missing, first, second], self.start, 49, -123), (180, 10)
        )
        second.get_wind_at.assert_not_called()
        self.assertEqual(
            sampler._sample([second, first], self.start, 49, -123), (270, 20)
        )
        sampler.close()

    def test_cursor_weather_includes_interpolated_gust(self):
        sampler = WindCursorSampler(Mock())
        self.addCleanup(sampler.close)
        instant = self.start + timedelta(minutes=90)
        wind, gust = sampler._sample_weather([self.source], instant, 49, -123)
        self.assertEqual(wind, self.source.get_wind_at(instant, 49, -123))
        self.assertAlmostEqual(gust, 10.25)

    def test_cursor_sampling_starts_without_a_debounce_timer(self):
        sampler = WindCursorSampler(Mock())
        self.addCleanup(sampler.close)
        with patch("gweatherrouting.rendering.windcursor.Thread") as thread:
            sampler.request([self.source], self.start, 49, -123, Mock())
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        self.assertTrue(sampler._running)
        self.assertIsNone(sampler._pending)
