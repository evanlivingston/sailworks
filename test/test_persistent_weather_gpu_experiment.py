"""CPU controls for GPU readback gates; no native rendering in unit checks."""

import importlib.util
from pathlib import Path
import unittest

import numpy as np

SPEC = importlib.util.spec_from_file_location(
    "persistent_gpu_probe",
    Path(__file__).resolve().parents[1] / "tools/experiment_persistent_weather_gpu.py",
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class TestReadbackGate(unittest.TestCase):
    def pixels(self):
        return np.array(
            [[[1, 2, 10, 20]], [[np.nan, np.nan, 30, 40]]], dtype=np.float32
        )

    def test_orientation_is_fixed_and_missing_values_remain_missing(self):
        expected = self.pixels()
        self.assertEqual(probe.compare_pixels(expected[::-1], expected, True), 0)
        with self.assertRaises(ValueError):
            probe.compare_pixels(expected[::-1], expected, False)

    def test_blank_output_cannot_pass_and_anchor_drift_is_exact_failure(self):
        expected = self.pixels()
        with self.assertRaisesRegex(ValueError, "mask"):
            probe.compare_pixels(np.zeros_like(expected), expected, False)
        actual = expected.copy()
        actual[0, 0, 2] += 0.0001
        with self.assertRaisesRegex(ValueError, "anchors"):
            probe.compare_pixels(actual, expected, False)

    def test_declared_vector_limit_is_not_relaxed(self):
        expected = self.pixels()
        actual = expected.copy()
        actual[0, 0, 0] += 1e-5
        self.assertLessEqual(probe.compare_pixels(actual, expected, False), 2e-5)
        actual[0, 0, 0] += 1e-4
        with self.assertRaisesRegex(ValueError, "declared gate"):
            probe.compare_pixels(actual, expected, False)


if __name__ == "__main__":
    unittest.main()
