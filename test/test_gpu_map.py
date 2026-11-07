"""CPU-safe contract tests and isolated Qt/GPU integration launchers."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest

import numpy as np

from sailworks.rendering.windgeometry import stroke_mesh


class TestWindMeshes(unittest.TestCase):
    def test_mesh_matches_experiment_and_is_read_only(self):
        spec = importlib.util.spec_from_file_location(
            "reference_scene",
            Path(__file__).resolve().parents[1] / "tools/gpu_rendering_scene.py",
        )
        reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
        lines = [(0, 0, 15, 20), (4, 2, 4, 2), (-3, 8, 9, -2)]
        expected = reference.stroke_mesh(lines, [(10, 20)])
        actual = stroke_mesh(lines, [(10, 20)])
        np.testing.assert_array_equal(actual, expected)
        self.assertFalse(actual.flags.writeable)

@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Qt dependencies")
class TestGpuMap(unittest.TestCase):
    def run_checks(self, name, native=False):
        environment = dict(os.environ)
        if native:
            environment.pop("QT_QPA_PLATFORM", None)
            environment["GWR_RUN_GPU_TESTS"] = "1"
        else:
            environment["QT_QPA_PLATFORM"] = "offscreen"
        result = subprocess.run(
            [sys.executable, "-m", "unittest", f"test.qt_gpu_checks.{name}", "-q"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_preferences_and_recovery(self):
        self.run_checks("PreferenceChecks")

    @unittest.skipUnless(
        os.environ.get("GWR_RUN_GPU_TESTS") == "1", "Native GPU opt-in"
    )
    def test_native_gpu_workspace(self):
        self.run_checks("NativeGpuChecks", native=True)


if __name__ == "__main__":
    unittest.main()
