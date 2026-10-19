"""Missing wind must never become visible calm wind or off-geometry ink."""

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import barb_shader_safety as safety
import diagnose_barb_overlay as diagnostic


class TestBarbShaderSafety(unittest.TestCase):
    def test_palette_gate_rejects_wrong_color_at_correct_location(self):
        cpu = np.zeros((16, 16, 4), dtype=np.uint8)
        cpu[8, 8] = [0, 0, 204, 217]
        gpu = cpu.copy()
        self.assertEqual(diagnostic.color_error(gpu, cpu)["color_mismatch_pixels"], 0)
        gpu[8, 8, :3] = [204, 0, 0]
        self.assertEqual(diagnostic.color_error(gpu, cpu)["color_mismatch_pixels"], 1)

    def test_contract_drift_rejected(self):
        with self.assertRaises(ValueError):
            safety.safe_sources("changed", "changed")

    def test_missing_cell_outputs_are_finite_and_endpoint_blend_is_preserved(self):
        vertex = """layout(location = 2) out vec2 wind;
void main() {
    int id = 0;
    if (id < 0) { a = vec2(uintBitsToFloat(0x7fc00000u)); b = a; }
    wind = blend <= 0.0 ? a : (blend >= 1.0 ? b : a + (b - a) * blend);
    gl_Position = qt_Matrix * vec4(anchor + localPoint, 0.0, 1.0);
}"""
        fragment = """layout(location = 2) in vec2 wind;
void main() {
    if (any(isnan(wind)) || any(isinf(wind))) discard;
}"""
        a, b = safety.safe_sources(vertex, fragment)
        self.assertNotIn("0x7fc00000u", a)
        self.assertIn("floatBitsToUint(v)", a)
        self.assertIn("wind = vec2(0.0)", a)
        self.assertIn("if (!valid) anchor = vec2(0.0)", a)
        self.assertIn("blend <= 0.0 ? finiteVector(a)", a)
        self.assertIn("blend >= 1.0 ? finiteVector(b)", a)
        self.assertIn("if (validWind < 0.99999) discard", b)
        with self.assertRaises(ValueError):
            safety.safe_sources(a, b)

    def test_mask_support_does_not_accept_flooding(self):
        cpu = np.zeros((32, 32, 4), dtype=np.uint8)
        cpu[16, 16, 3] = 255
        gpu = cpu.copy()
        self.assertEqual(
            diagnostic.support_error(gpu, cpu)["gpu_outside_cpu_support"], 0
        )
        gpu[..., 3] = 255
        self.assertGreater(
            diagnostic.support_error(gpu, cpu)["gpu_outside_cpu_support"], 900
        )

    def test_empty_mask_requires_exact_transparency(self):
        empty = np.zeros((8, 8, 4), dtype=np.uint8)
        self.assertEqual(diagnostic.support_error(empty, empty)["gpu_ink_pixels"], 0)
        leaked = empty.copy()
        leaked[0, 0, 3] = 255
        self.assertEqual(
            diagnostic.support_error(leaked, empty)["gpu_outside_cpu_support"], 1
        )

    def test_controls_distinguish_unselected_missing_endpoint_and_real_calm(self):
        cases = {case[0]: case for case in diagnostic.controls()}
        self.assertTrue((cases["all-unselected"][2][..., 0] == -1).all())
        self.assertTrue(np.isnan(cases["all-nan-endpoints"][1]).all())
        field = cases["valid-first-missing-second"][1]
        self.assertTrue(np.isfinite(field[..., :2]).all())
        self.assertTrue(np.isnan(field[..., 2:]).all())


if __name__ == "__main__":
    unittest.main()
