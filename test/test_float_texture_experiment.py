"""CPU oracle for the bounded native-float texture feasibility probe."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

import numpy as np

SPEC = importlib.util.spec_from_file_location(
    "float_probe",
    Path(__file__).resolve().parents[1] / "tools/experiment_float_textures.py",
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class TestFloatSamplingOracle(unittest.TestCase):
    def test_manual_shader_does_not_delegate_interpolation_to_texture_filter(self):
        self.assertIn("texelFetch", probe.MANUAL_FRAGMENT)
        self.assertNotIn("texture(field", probe.MANUAL_FRAGMENT)
        self.assertIn("texture(field", probe.FRAGMENT)

    def test_shader_baker_supports_wheel_and_qt_layouts(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            for suffix in ("qsb", "Qt/libexec/qsb", "Qt/bin/qsb"):
                path = package / suffix
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                self.assertEqual(probe.find_qsb(package), path)
                path.unlink()

    def test_missing_shader_baker_is_explicit_not_auto_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "do not install"):
                probe.find_qsb(Path(temporary))

    def test_native_size_samples_texel_centers(self):
        field = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
        np.testing.assert_array_equal(probe.bilinear_reference(field, 3, 2), field)

    def test_single_texel_clamps_everywhere(self):
        field = np.array([[[-3.0, 0.0, 1.5, 200.0]]], dtype=np.float32)
        np.testing.assert_array_equal(
            probe.bilinear_reference(field, 9, 6), np.broadcast_to(field, (6, 9, 4))
        )

    def test_center_blends_physical_components_before_portrayal(self):
        field = np.array([[[-1, 1, 2, 3], [1, -1, 4, 5]]], dtype=np.float32)
        actual = probe.bilinear_reference(field, 3, 1)
        np.testing.assert_array_equal(actual[0, 1], [0, 0, 3, 4])
        np.testing.assert_array_equal(actual[0, 0], field[0, 0])
        np.testing.assert_array_equal(actual[0, -1], field[0, -1])


if __name__ == "__main__":
    unittest.main()
