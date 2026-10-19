"""Differential and candidate-count gates for read-only PR staging experiments."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

SPEC = importlib.util.spec_from_file_location(
    "staging_probe",
    Path(__file__).resolve().parents[1] / "tools/experiment_staging_residency.py",
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class TestStagingDifferential(unittest.TestCase):
    def fixture(self):
        values = np.array([[1.1, 2.2], [3.3, 4.4]])
        sampler = SimpleNamespace(
            sample_timestamps=lambda *_args: values,
            point_sources=np.array(["a", "b"], dtype=object),
        )
        staged = SimpleNamespace(
            first=values.astype(np.float32)[None, ...],
            second=values.astype(np.float32)[None, ...],
            sources=("a", "b"),
            weight=lambda _stamp: 0.5,
        )
        return staged, sampler, np.zeros((2, 2))

    def test_float32_difference_is_measured_not_claimed_exact(self):
        staged, sampler, points = self.fixture()
        result = probe.check_values(staged, sampler, 1, points)
        self.assertEqual(result["usable_points"], 2)
        self.assertTrue(result["masks_match"])
        self.assertGreater(result["max_component_error_mps"], 0)
        self.assertLess(result["max_component_error_mps"], 2e-5)

    def test_mask_changes_fail(self):
        staged, sampler, points = self.fixture()
        staged.first[0, 0, 0] = np.nan
        with self.assertRaisesRegex(ValueError, "finite masks"):
            probe.check_values(staged, sampler, 1, points)

    def test_source_changes_fail(self):
        staged, sampler, points = self.fixture()
        staged.sources = ("b", "a")
        with self.assertRaisesRegex(ValueError, "provenance"):
            probe.check_values(staged, sampler, 1, points)

    def test_component_error_beyond_declared_bound_fails(self):
        staged, sampler, points = self.fixture()
        staged.first[0, 0, 0] += 0.01
        with self.assertRaisesRegex(ValueError, "float32 error"):
            probe.check_values(staged, sampler, 1, points)


class TestNativeNodeUnion(unittest.TestCase):
    def view(self):
        return SimpleNamespace(
            _center_lat=0,
            _center_lon=0,
            width=10,
            height=20,
            _lon_to_x=lambda x: x,
            _lat_to_y=lambda y: y,
            _x_to_lon=lambda x: x,
            _y_to_lat=lambda y: y,
        )

    def index(self, nodes):
        return SimpleNamespace(
            records=[SimpleNamespace(element="UGRD")], nodes=lambda _bounds: nodes
        )

    def test_native_nodes_are_unique_and_longitude_major(self):
        sampler = SimpleNamespace(
            indices=[self.index(np.array([[2, 1], [1, 1], [0, 2], [2, 1]]))]
        )
        points, count = probe.native_points(sampler, self.view())
        self.assertEqual(count, 4)
        np.testing.assert_array_equal(points, [[1, 1], [2, 1], [0, 2]])

    def test_candidate_cap_is_applied_before_deduplication(self):
        sampler = SimpleNamespace(indices=[self.index(np.zeros((262145, 2)))])
        with self.assertRaisesRegex(ValueError, "candidate budget"):
            probe.native_points(sampler, self.view())

    def test_missing_native_coverage_fails_instead_of_blank_success(self):
        sampler = SimpleNamespace(indices=[self.index(np.empty((0, 2)))])
        with self.assertRaisesRegex(ValueError, "No native wind nodes"):
            probe.native_points(sampler, self.view())


if __name__ == "__main__":
    unittest.main()
