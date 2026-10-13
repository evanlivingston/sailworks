"""Deterministic wind source precedence without loading GRIB files."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from gweatherrouting.core.gribmanager import GribManager


class TestGribSelection(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 12, tzinfo=timezone.utc)
        self.old = SimpleNamespace(
            start_time=self.start, get_wind_at=Mock(return_value=(90, 5))
        )
        self.new = SimpleNamespace(
            start_time=self.start + timedelta(hours=6),
            get_wind_at=Mock(return_value=(180, 8)),
        )
        self.manager = GribManager.__new__(GribManager)
        self.manager.gribs = [self.old, self.new]

    def sample(self):
        return self.manager.get_wind_at(self.start + timedelta(hours=12), 49, -123)

    def test_newest_model_run_wins_without_reordering_enabled_files(self):
        self.assertEqual(self.sample(), (180, 8))
        self.old.get_wind_at.assert_not_called()
        self.assertEqual(self.manager.gribs, [self.old, self.new])

    def test_missing_or_failed_newer_coverage_falls_back_to_older_run(self):
        for error in (None, ValueError("outside forecast coverage")):
            self.new.get_wind_at.return_value = None
            self.new.get_wind_at.side_effect = error
            self.assertEqual(self.sample(), (90, 5))

    def test_equal_issue_times_preserve_load_order_across_utc_offsets(self):
        self.new.start_time = self.start.astimezone(timezone(timedelta(hours=-7)))
        self.assertEqual(self.sample(), (90, 5))
        self.new.get_wind_at.assert_not_called()

    def test_empty_or_unavailable_selection_returns_no_wind(self):
        self.old.get_wind_at.return_value = None
        self.new.get_wind_at.return_value = None
        self.assertIsNone(self.sample())
        self.manager.gribs = []
        self.assertIsNone(self.sample())
