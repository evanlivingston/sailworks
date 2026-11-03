"""Race timeline overrides follow explicit selections and forecast coverage."""

import unittest
from datetime import datetime, timezone
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import Mock


@unittest.skipUnless(find_spec("PySide6"), "Requires PySide6")
class TestRaceTimeline(unittest.TestCase):
    def setUp(self):
        from sailworks.qt.window import PreviewWindow

        self.window_class = PreviewWindow
        self.timeline = Mock()
        self.forecast = Mock()
        self.window = SimpleNamespace(
            map=SimpleNamespace(timeline=self.timeline),
            controller=SimpleNamespace(display_forecast=self.forecast),
            _race_timeline_bounds=(100, 200, (150,)),
        )
        self.window._race_window_in_forecast = lambda start, end: (
            PreviewWindow._race_window_in_forecast(self.window, start, end)
        )
        self.window._apply_race_timeline_bounds = lambda **kwargs: (
            PreviewWindow._apply_race_timeline_bounds(self.window, **kwargs)
        )

    def test_forecast_change_preserves_only_overlapping_race(self):
        for sources, overlaps in (
            ([SimpleNamespace(first_valid=200, last_valid=300)], True),
            ([SimpleNamespace(first_valid=201, last_valid=300)], False),
            ([], False),
        ):
            with self.subTest(sources=sources):
                self.timeline.reset_mock()
                self.forecast.sources.return_value = sources
                self.window_class._apply_race_timeline_bounds(self.window)
                if overlaps:
                    self.timeline.set_bounds_override.assert_called_once_with(
                        100, 200, (150,)
                    )
                    self.timeline.clear_bounds_override.assert_not_called()
                else:
                    self.timeline.clear_bounds_override.assert_called_once_with()
                    self.timeline.set_bounds_override.assert_not_called()

    def test_race_selection_forces_override_without_forecast_overlap(self):
        self.forecast.sources.return_value = []
        race = SimpleNamespace(
            start_time=datetime.fromtimestamp(100, timezone.utc),
            end_time=datetime.fromtimestamp(200, timezone.utc),
            boats=(),
        )
        self.window_class._race_selected(self.window, race)
        self.timeline.set_bounds_override.assert_called_once_with(100, 200, ())

    def test_missing_bounds_and_forecast_errors_clear_override(self):
        self.window._race_timeline_bounds = None
        self.window_class._apply_race_timeline_bounds(self.window)
        self.timeline.clear_bounds_override.assert_called_once_with()
        self.timeline.reset_mock()
        self.window._race_timeline_bounds = (100, 200, ())
        self.forecast.sources.side_effect = RuntimeError("unavailable")
        self.window_class._apply_race_timeline_bounds(self.window)
        self.timeline.clear_bounds_override.assert_called_once_with()
