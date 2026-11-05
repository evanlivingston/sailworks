"""Shared forecast bounds and bounded elapsed-time stepping, including DST."""

import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sailworks.core.forecasttimeline import (
    forecast_bounds,
    forecast_intervals,
    step_forecast_time,
)
from sailworks.core.timecontrol import TimeControl


class TestForecastTimeline(unittest.TestCase):
    def _now(self, valid_times, now):
        """Run ForecastTimeline.now() against a minimal stand-in widget."""
        from sailworks.qt.forecasttimeline import ForecastTimeline

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        clock = TimeControl()
        timeline = SimpleNamespace(
            closed=False,
            bounds=None,
            region=None,
            clock=clock,
            day_strip=SimpleNamespace(valid_times=tuple(valid_times)),
            pause=lambda: None,
            cancel_commit=lambda: None,
        )
        regions = []

        def set_region(start, end):
            regions.append((start, end))
            timeline.region = (start, end)
            timeline.bounds = (start, end)

        timeline.set_region = set_region
        timeline._now_stamp = lambda: ForecastTimeline._now_stamp(timeline)
        with patch("sailworks.qt.forecasttimeline.datetime", _FixedDatetime):
            ForecastTimeline.now(timeline)
        return regions, clock.time.timestamp()

    def test_now_sets_a_region_from_the_wall_clock(self):
        wall = datetime(2026, 1, 5, 14, 32)
        regions, _stamp = self._now((), wall)
        self.assertEqual(len(regions), 1)
        start, end = regions[0]
        self.assertAlmostEqual(start, wall.timestamp() - 24 * 3600, delta=1)
        self.assertAlmostEqual(end, wall.timestamp() + 48 * 3600, delta=1)

    def test_now_snaps_to_the_nearest_loaded_valid_time_inside_the_region(self):
        wall = datetime(2026, 1, 5, 14, 32)
        early = datetime(2026, 1, 5, 13, 0).timestamp()
        late = datetime(2026, 1, 5, 18, 0).timestamp()
        _regions, stamp = self._now((early, late), wall)
        # 14:32 is closer to 13:00 than to 18:00, and both are inside
        # [now-24h, now+48h].
        self.assertEqual(stamp, early)

    def test_now_ignores_valid_times_outside_the_region(self):
        wall = datetime(2026, 1, 5, 14, 32)
        stale = datetime(2026, 1, 1, 0, 0).timestamp()
        _regions, stamp = self._now((stale,), wall)
        # The only loaded time predates now-24h, so the wall clock stands.
        self.assertAlmostEqual(stamp, wall.timestamp(), delta=1)

    def test_now_falls_back_to_the_wall_clock_without_coverage(self):
        wall = datetime(2026, 1, 5, 14, 32)
        _regions, stamp = self._now((), wall)
        self.assertAlmostEqual(stamp, wall.timestamp(), delta=1)

    def test_timeline_captions_distinguish_region_from_model_coverage(self):
        from sailworks.qt.forecasttimeline import ForecastTimeline

        start = datetime(2026, 1, 5, 12, 0).timestamp()
        end = datetime(2026, 1, 5, 20, 0).timestamp()
        timeline = SimpleNamespace(
            bounds=(start, end),
            _model_intervals=(
                (
                    datetime(2026, 1, 5, 12, 0).timestamp(),
                    datetime(2026, 1, 5, 15, 0).timestamp(),
                ),
            ),
        )
        caption = ForecastTimeline._caption_for
        self.assertEqual(
            caption(timeline, datetime(2026, 1, 5, 11, 0).timestamp()),
            "Selected time is outside the selected time range",
        )
        self.assertEqual(caption(timeline, datetime(2026, 1, 5, 13, 0).timestamp()), "")
        self.assertEqual(
            caption(timeline, datetime(2026, 1, 5, 18, 0).timestamp()),
            "No model data for the selected time",
        )
        timeline.bounds = None
        self.assertIn("Choose a display forecast", caption(timeline, start))

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
