"""Geometry/quality checks plus an explicitly opted-in native GPU integration test."""

import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

SPEC = importlib.util.spec_from_file_location(
    "gpu_scene", Path(__file__).resolve().parents[1] / "tools/gpu_rendering_scene.py"
)
scene = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scene)


class TestGpuRenderingMath(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "Qt raster reference")
    def test_raster_reference_preserves_texels_at_unit_scale_on_both_axes(self):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        for dpr in (1, 2):
            for axis in (0, 1):
                width, height = (24, 6) if axis == 0 else (6, 24)
                source = QImage(
                    width, height, QImage.Format.Format_ARGB32_Premultiplied
                )
                source.setDevicePixelRatio(dpr)
                source.fill(0)
                for along in (12, 13):
                    for across in range(6):
                        x, y = (along, across) if axis == 0 else (across, along)
                        source.setPixelColor(x, y, QColor("white"))
                for offset in (-0.75, -0.5, -0.25, 0.25, 0.5, 0.75):
                    target = QImage(64, 64, source.format())
                    target.setDevicePixelRatio(dpr)
                    target.fill(0)
                    painter = QPainter(target)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                    painter.drawImage(
                        QRectF(
                            offset / dpr if axis == 0 else 0,
                            offset / dpr if axis == 1 else 0,
                            width / dpr,
                            height / dpr,
                        ),
                        source,
                        QRectF(0, 0, width, height),
                    )
                    painter.end()
                    pixels = scene.image_array(target)
                    alpha = pixels[2, :, 3] if axis == 0 else pixels[:, 2, 3]
                    # Nearest texel selection, not bilinear softening. Do not
                    # impose a new convention for negative half-pixel ties.
                    self.assertEqual(alpha[alpha > 0].tolist(), [255, 255])

    def test_unit_texture_scale_uses_device_pixels_and_does_not_snap_zoom(self):
        for dpr in (1, 1.5, 2):
            self.assertTrue(
                scene.unit_scale_texture(600 * dpr, 400 * dpr, 600, 400, dpr)
            )
            self.assertFalse(
                scene.unit_scale_texture(600 * dpr, 400 * dpr, 600.01, 400, dpr)
            )
            self.assertFalse(
                scene.unit_scale_texture(600 * dpr, 400 * dpr, 600, 399.99, dpr)
            )
        self.assertFalse(scene.unit_scale_texture(1200, 800, 600, 400, 1))

    def test_flat_cap_stroke_width_area_and_zero_length(self):
        vertices = scene.stroke_mesh([(0, 0, 10, 0), (2, 3, 2, 3)], [])
        self.assertEqual(vertices.shape, (6, 2))
        self.assertEqual(vertices.dtype, np.float32)
        self.assertTrue(vertices.flags.c_contiguous)
        np.testing.assert_allclose(vertices.min(axis=0), [0, -0.5])
        np.testing.assert_allclose(vertices.max(axis=0), [10, 0.5])
        triangles = vertices.reshape(-1, 3, 2)
        a, b = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
        area = np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]).sum() / 2
        self.assertAlmostEqual(float(area), 10)
        self.assertEqual(scene.stroke_mesh([], []).shape, (0, 2))

    def test_calm_ring_is_stroked_not_filled_and_has_bounded_chord_error(self):
        vertices = scene.stroke_mesh([], [(0, 0)])
        self.assertEqual(len(vertices), 64 * 6)
        radii = np.linalg.norm(vertices, axis=1)
        self.assertAlmostEqual(float(radii.min()), 1.5, places=6)
        self.assertAlmostEqual(float(radii.max()), 2.5, places=6)
        self.assertLess(2.5 * (1 - math.cos(math.pi / 64)), 0.004)

    def test_quality_checks_are_foreground_sensitive_and_reject_missing_glyphs(self):
        reference = np.zeros((200, 200, 4), dtype=np.uint8)
        reference[40:60, 40:42] = [210, 0, 0, 210]
        self.assertTrue(
            scene.pixel_metrics(reference, reference)["within_experimental_tolerance"]
        )
        missing = np.zeros_like(reference)
        metrics = scene.pixel_metrics(reference, missing)
        self.assertFalse(metrics["within_experimental_tolerance"])
        self.assertEqual(metrics["alpha_coverage_ratio"], 0)
        self.assertGreater(metrics["foreground_premultiplied_rgba_mae"], 50)
        with self.assertRaises(ValueError):
            scene.pixel_metrics(reference, reference[:10])

    def test_geometry_signature_includes_coordinates_and_primitive_groups(self):
        geometry = {1: ([(0, 0, 10, 0)], [], [])}
        self.assertEqual(
            scene.geometry_digest(geometry), scene.geometry_digest(geometry)
        )
        self.assertNotEqual(
            scene.geometry_digest(geometry),
            scene.geometry_digest({1: ([(1, 0, 10, 0)], [], [])}),
        )


@unittest.skipUnless(
    os.environ.get("GWR_RUN_GPU_TESTS") == "1" and sys.platform == "darwin",
    "Opt in with GWR_RUN_GPU_TESTS=1 on a native Metal-capable macOS session",
)
class TestNativeGpuExperiment(unittest.TestCase):
    def test_all_modes_preserve_controlled_glyphs_on_metal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, artifacts, report = (
                root / "fixture",
                root / "images",
                root / "report.json",
            )
            script = (
                Path(__file__).resolve().parents[1]
                / "tools/experiment_gpu_rendering.py"
            )
            commands = (
                [sys.executable, str(script), "--synthetic", "--fixture", str(fixture)],
                [
                    sys.executable,
                    str(script),
                    "--fixture",
                    str(fixture),
                    "--frames",
                    "8",
                    "--repeats",
                    "1",
                    "--artifacts",
                    str(artifacts),
                    "--output",
                    str(report),
                ],
            )
            for command in commands:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=180
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(report.read_text())
            self.assertTrue(data["complete"])
            self.assertTrue(data["identical_selected_geometry"])
            self.assertEqual(len(data["runs"]), 9)
            for run in data["runs"]:
                self.assertEqual(run["environment"]["graphics_api"], "Metal")
                # R0.1 closes the historical fractional-pan failure. Require
                # the COMPLETE gate now; do not excuse composite failures just
                # because the foreground glyphs still match.
                self.assertTrue(run["quality_gate_passed"], run["quality"])
                self.assertTrue(
                    all(q["within_experimental_tolerance"] for q in run["quality"])
                )
                self.assertTrue(
                    all(
                        q["composite_within_experimental_tolerance"]
                        for q in run["quality"]
                    )
                )
                self.assertEqual(len(run["selected_samples_per_frame"]), 8)
                self.assertTrue(all(n > 0 for n in run["selected_samples_per_frame"]))
                self.assertTrue(
                    any("Metal device:" in line for line in run["backend_diagnostics"])
                )
                if run["mode"] == "gpu_mesh":
                    self.assertEqual(run["measured_uploads"]["texture_uploads"], 0)


if __name__ == "__main__":
    unittest.main()
