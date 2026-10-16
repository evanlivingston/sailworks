"""CPU-safe contract tests and isolated Qt/GPU integration launchers."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest

import numpy as np

from gweatherrouting.rendering.windgeometry import stroke_mesh


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

    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "Qt frame contracts")
    def test_frame_owns_image_snapshot_and_mutable_vertices_are_frozen(self):
        from PySide6.QtGui import QColor, QImage
        from gweatherrouting.qt.mapframe import (
            ImageFrame,
            MapFrame,
            WindBatch,
            resource_estimate,
        )

        source = QImage(16, 12, QImage.Format.Format_ARGB32_Premultiplied)
        source.fill(QColor("red"))
        layer = ImageFrame(source, (0, 0, 16, 12))
        source.fill(QColor("blue"))
        self.assertEqual(layer.image.pixelColor(3, 3), QColor("red"))
        vertices = np.zeros((6, 2), dtype=np.float32)
        batch = WindBatch(1, 0, vertices)
        vertices.fill(3)
        np.testing.assert_array_equal(batch.vertices, 0)
        self.assertFalse(batch.vertices.flags.writeable)
        frame = MapFrame(1, 16, 12, 1, (0, 0, 0, 1), layer, layer, (batch,), 0.85)
        estimate = resource_estimate(frame, (), 16 * 12)
        self.assertEqual(
            estimate["upload_bytes"], layer.image.sizeInBytes() + batch.vertices.nbytes
        )
        self.assertGreater(estimate["gpu_estimate_bytes"], estimate["upload_bytes"])


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
