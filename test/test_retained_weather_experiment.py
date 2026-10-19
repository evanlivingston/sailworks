"""Retained-field subset, freshness, coverage and blocked-preparation controls."""

from dataclasses import dataclass
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

SPEC = importlib.util.spec_from_file_location(
    "retained_probe",
    Path(__file__).resolve().parents[1] / "tools/experiment_retained_weather.py",
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@dataclass
class Stage:
    first: np.ndarray
    second: np.ndarray
    sources: tuple
    before: float = 0
    after: float = 10

    def contains(self, stamp):
        return self.before < stamp < self.after


class TestRetainedField(unittest.TestCase):
    def view(self, longitude=0, zoom=9):
        return SimpleNamespace(
            width=10,
            height=20,
            _zoom=zoom,
            _center_lat=0,
            _center_lon=longitude,
            _lon_to_x=lambda x: x,
            _lat_to_y=lambda y: y,
        )

    def field(self):
        points = np.array([[0, 0], [1, 1], [2, 2]], dtype=np.float64)
        first = np.arange(6, dtype=np.float32).reshape(1, 3, 2)
        stage = Stage(first, first + 10, ("a", None, "b"))
        return probe.RetainedWind(points, stage, self.view(), 256)

    def test_query_keeps_coordinate_order_endpoints_and_provenance(self):
        field = self.field()
        result = field.sample(None, 1, np.array([[2, 0]]), np.array([[2, 0]]))
        np.testing.assert_array_equal(result.first, [[[4, 5], [0, 1]]])
        np.testing.assert_array_equal(result.second, [[[14, 15], [10, 11]]])
        self.assertEqual(result.sources, ("b", "a"))
        self.assertFalse(result.first.flags.writeable)
        self.assertFalse(result.second.flags.writeable)
        self.assertEqual((result.before, result.after), (0, 10))

    def test_published_field_is_independent_of_mutated_preparation_buffers(self):
        points = np.array([[0.0, 0.0]])
        values = np.array([[[1.0, 2.0]]], dtype=np.float32)
        field = probe.RetainedWind(
            points, Stage(values, values, ("a",)), self.view(), 256
        )
        points[:] = 99
        values[:] = 77
        result = field.sample(None, 1, [[0.0]], [[0.0]])
        np.testing.assert_array_equal(result.first, [[[1, 2]]])
        self.assertIs(result.first, result.second)
        self.assertFalse(field.points.flags.writeable)

    def test_uncached_coordinates_fail_instead_of_blank_or_resample(self):
        with self.assertRaisesRegex(ValueError, "outside retained field"):
            self.field().sample(None, 1, [[9]], [[9]])

    def test_source_selection_event_and_budget_escape_fail(self):
        field = self.field()
        for stamp in (0, 10, 11):
            with (
                self.subTest(stamp=stamp),
                self.assertRaisesRegex(ValueError, "source-selection"),
            ):
                field.sample(None, stamp, [[0]], [[0]])
        with self.assertRaisesRegex(ValueError, "oversized"):
            field.sample(None, 1, [[0, 1]], [[0, 1]], byte_limit=31)

    def test_cancellation_never_publishes_a_partial_subset(self):
        field = self.field()
        self.assertIsNone(field.sample(None, 1, [[0]], [[0]], cancel=lambda: True))
        decisions = iter((False, True))
        self.assertIsNone(
            field.sample(None, 1, [[0]], [[0]], cancel=lambda: next(decisions))
        )
        self.assertIsNone(field.last_result)

    def test_coverage_and_zoom_rejection(self):
        field = self.field()
        self.assertTrue(field.covers(self.view(256)))
        self.assertTrue(field.covers(self.view(-256)))
        self.assertFalse(field.covers(self.view(257)))
        self.assertFalse(field.covers(self.view(0, zoom=10)))

    def test_blocked_preparation_control_restores_all_methods_on_error(self):
        field = self.field()
        side, array, stage = lambda: "side", lambda: "array", lambda: "stage"
        index = SimpleNamespace(array=array)
        sampler = SimpleNamespace(_side=side, indices=[index])
        barbs = SimpleNamespace(stage_wind=stage)
        with self.assertRaisesRegex(RuntimeError, "control exit"):
            with probe.retained_mode(barbs, sampler, field):
                with self.assertRaisesRegex(AssertionError, "preparation"):
                    sampler._side()
                with self.assertRaisesRegex(AssertionError, "preparation"):
                    index.array()
                self.assertEqual(barbs.stage_wind, field.sample)
                raise RuntimeError("control exit")
        self.assertIs(sampler._side, side)
        self.assertIs(index.array, array)
        self.assertIs(barbs.stage_wind, stage)


if __name__ == "__main__":
    unittest.main()
