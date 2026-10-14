"""Shared forecast bounds and bounded elapsed-time stepping, including DST."""

import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gweatherrouting.core.forecasttimeline import (
    forecast_bounds,
    forecast_intervals,
    step_forecast_time,
)
from gweatherrouting.core.timecontrol import TimeControl


class TestForecastTimeline(unittest.TestCase):
    def test_bounds_union_only_enabled_metadata_without_opening_forecasts(self):
        start = datetime(2040, 1, 1, tzinfo=timezone.utc)
        grib = SimpleNamespace(
            start_time=start,
            last_forecast=3,
            get_wind=Mock(side_effect=AssertionError("Metadata only")),
        )
        fields = SimpleNamespace(
            fields={
                "included": SimpleNamespace(
                    id="included", times=[start.timestamp() - 3600, start.timestamp()]
                ),
                "excluded": SimpleNamespace(id="excluded", times=[0, 1]),
            },
            excluded={"excluded"},
        )
        datasets = [
            SimpleNamespace(
                id="included",
                requested_start=start + timedelta(hours=6),
                requested_end=start + timedelta(hours=9),
            ),
            SimpleNamespace(
                id="excluded",
                requested_start=start - timedelta(days=20),
                requested_end=start + timedelta(days=20),
            ),
        ]
        marine = SimpleNamespace(
            fields=fields,
            ordered_datasets=lambda: datasets,
            is_enabled=lambda id: id == "included",
        )
        intervals = forecast_intervals(SimpleNamespace(gribs=[grib]), marine)
        self.assertEqual(
            intervals,
            (
                (start.timestamp() - 3600, start.timestamp()),
                (start.timestamp(), start.timestamp() + 10800),
                (start.timestamp() + 21600, start.timestamp() + 32400),
            ),
        )
        self.assertEqual(
            forecast_bounds(intervals),
            (start.timestamp() - 3600, start.timestamp() + 32400),
        )
        grib.get_wind.assert_not_called()
        self.assertIsNone(forecast_bounds(forecast_intervals()))

    def test_manual_time_is_retained_until_a_bounded_step_and_endpoints_are_exact(self):
        start = datetime(2026, 1, 1, 12)
        bounds = (start.timestamp(), (start + timedelta(hours=1)).timestamp())
        clock = TimeControl()
        clock.set_time(start - timedelta(hours=2))
        self.assertEqual(clock.time, start - timedelta(hours=2))
        step_forecast_time(clock, 900, bounds)
        self.assertEqual(clock.time, start)
        step_forecast_time(clock, 900, bounds)
        self.assertEqual(clock.time, start + timedelta(minutes=15))
        step_forecast_time(clock, 86400, bounds)
        self.assertEqual(clock.time, start + timedelta(hours=1))
        step_forecast_time(clock, -86400, bounds)
        self.assertEqual(clock.time, start)
        step_forecast_time(clock, 86400)
        self.assertEqual(clock.time, start + timedelta(days=1))

    @unittest.skipUnless(hasattr(time, "tzset"), "Requires local timezone selection")
    def test_bounded_steps_preserve_elapsed_seconds_across_repeated_dst_hour(self):
        try:
            with patch.dict(os.environ, TZ="America/Los_Angeles"):
                time.tzset()
                first = datetime(2026, 11, 1, 8, 50, tzinfo=timezone.utc).timestamp()
                clock = TimeControl()
                clock.set_time(datetime.fromtimestamp(first))
                step_forecast_time(clock, 900, (first - 3600, first + 7200))
                self.assertEqual(clock.time.timestamp(), first + 900)
                self.assertEqual(
                    (clock.time.hour, clock.time.minute, clock.time.fold), (1, 5, 1)
                )
                step_forecast_time(clock, -900, (first - 3600, first + 7200))
                self.assertEqual(clock.time.timestamp(), first)
                # Without coverage bounds GTK uses wall-time timedelta stepping.
                step_forecast_time(clock, 900)
                self.assertEqual((clock.time.hour, clock.time.minute), (2, 5))
        finally:
            time.tzset()
