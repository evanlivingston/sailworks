"""E2d pure selection-boundary and report acceptance controls."""

from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import experiment_app_persistent_weather as probe
from test.test_native_retained_weather_experiment import run


class TestAppPersistentControl(unittest.TestCase):
    def test_native_land_mask_is_detached_and_rejects_unknown_nodes_or_tiers(self):
        points = np.array([[1.0, 2.0], [3.0, 4.0]])
        flags = np.array([True, False])
        mask = probe.NativeLandMask(points, flags, 100)
        points[:] = 0
        flags[:] = False
        self.assertTrue(mask.contains(1, 2))
        self.assertFalse(mask.contains(3, 4))
        mask.refresh_source(150)
        with self.assertRaisesRegex(ValueError, "scale"):
            mask.refresh_source(501)
        with self.assertRaisesRegex(ValueError, "coordinate"):
            mask.contains(0, 0)
        with self.assertRaises(TypeError):
            mask._mapping[(1, 2)] = False

    def test_native_land_mask_rejects_invalid_payloads(self):
        for points, flags in (
            ([[1, 2], [1, 2]], np.array([True, False])),
            ([[np.nan, 2]], np.array([True])),
            ([[1, 2]], np.array([1])),
            ([[1, 2]], np.array([True, False])),
        ):
            with self.subTest(points=points), self.assertRaises(ValueError):
                probe.NativeLandMask(points, flags, 100)

    def test_provider_group_preserves_scene_cleanup_contract(self):
        cleared = []
        a = SimpleNamespace(requests=4, clear=lambda: cleared.append("field"))
        b = SimpleNamespace(requests=3, clear=lambda: cleared.append("selection"))
        group = probe.ProviderGroup(a, b)
        self.assertEqual(group.requests, 7)
        group.clear()
        self.assertEqual(cleared, ["field", "selection"])

    def test_geographic_membership_preserves_order_and_rejects_escape(self):
        points = np.array([[-2.0, 0.0], [0.0, -2.0], [0.0, 0.0], [0.0, 100.0]])
        retained = SimpleNamespace(points=points, covers=lambda _v: True)
        view = SimpleNamespace(
            width=10,
            height=10,
            _center_lat=0,
            _center_lon=0,
            _lon_to_x=lambda x: x,
            _lat_to_y=lambda x: x,
            _x_to_lon=lambda x: x,
            _y_to_lat=lambda x: x,
        )
        np.testing.assert_array_equal(
            probe.current_candidates(retained, view), points[:3]
        )
        retained.covers = lambda _v: False
        with self.assertRaisesRegex(ValueError, "escape"):
            probe.current_candidates(retained, view)

    def reports(self):
        a = dict(
            protocol="app-persistent-field-e2d-v1",
            control="reference",
            workload=dict(frames=2, interval_ms=100),
            harness_identity={},
            app_commit="one",
            run=run(),
        )
        a["run"]["environment"] = {}
        b = deepcopy(a)
        b["control"] = "retained"
        b["run"]["native_fidelity"] = {"exact_glyph_pixels": True}
        b["run"]["native_render_probe"]["submissions"] = [
            dict(
                submitted=1.01,
                content=dict(
                    numerical_weather=dict(
                        active=True, mixed=False, field_image_requests=4
                    )
                ),
            )
        ]
        return a, b

    def test_matched_controls_do_not_claim_full_speedup(self):
        result = probe.compare(*self.reports())
        self.assertTrue(result["static_glyph_pixels_exact"])
        self.assertEqual(result["mixed_candidate_submissions"], 0)
        self.assertFalse(result["end_to_end_speedup_proven"])

    def test_mixed_modes_and_missing_pixels_are_rejected(self):
        a, b = self.reports()
        b["run"]["native_render_probe"]["submissions"][0]["content"][
            "numerical_weather"
        ]["mixed"] = True
        with self.assertRaisesRegex(ValueError, "Mixed"):
            probe.compare(a, b)
        a, b = self.reports()
        b["run"]["native_fidelity"] = {}
        with self.assertRaisesRegex(ValueError, "glyph fidelity"):
            probe.compare(a, b)

    def test_changed_trajectory_or_field_reupload_is_not_accepted(self):
        a, b = self.reports()
        b["run"]["native_render_probe"]["inputs"][0]["state"] = {}
        with self.assertRaisesRegex(ValueError, "trajectory"):
            probe.compare(a, b)
        a, b = self.reports()
        b["run"]["retained_control_events"].append(dict(kind="field_images", at=1.01))
        with self.assertRaisesRegex(ValueError, "republished"):
            probe.compare(a, b)


if __name__ == "__main__":
    unittest.main()
