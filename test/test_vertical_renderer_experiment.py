"""A0a resource eligibility and camera path: no app frame assembly required."""

from dataclasses import dataclass, replace
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import experiment_vertical_renderer as probe
from gweatherrouting.core.mapviewport import MercatorProjection


@dataclass(frozen=True)
class View(MercatorProjection):
    width: int = 100
    height: int = 80
    _center_lat: float = 36
    _center_lon: float = -122
    _zoom: float = 10


class TestVerticalViewport(unittest.TestCase):
    def test_partial_preview_uses_only_common_geographic_image_coverage(self):
        initial = View()
        targets = [(-32, -32, 164, 144), (-16, -16, 132, 112)]
        moved, covered = probe.partial_image_coverage(
            initial, probe.pan(initial, -40, 0), targets
        )
        self.assertAlmostEqual(moved[0][0], -72)
        self.assertAlmostEqual(covered[0], 0)
        self.assertAlmostEqual(covered[2], 76)
        self.assertEqual(covered[1::2], (0, 80))
        _, empty = probe.partial_image_coverage(
            initial, probe.pan(initial, -1000, 0), targets
        )
        self.assertEqual(empty[2], 0)
        with self.assertRaisesRegex(ValueError, "scale escape"):
            probe.partial_image_coverage(initial, replace(initial, _zoom=11), targets)

    def test_pan_matches_reference_world_pixel_update_and_return(self):
        initial = View()
        view = initial
        for i in range(24):
            old = view
            view = probe.pan(view, -8 if i < 12 else 8, 0)
            self.assertAlmostEqual(
                view._lon_to_x(view._center_lon) - old._lon_to_x(old._center_lon),
                8 if i < 12 else -8,
            )
        self.assertAlmostEqual(view._center_lon, initial._center_lon)
        self.assertAlmostEqual(view._center_lat, initial._center_lat)

    def test_camera_moves_persistent_layers_without_changing_size(self):
        initial = View()
        view = probe.pan(initial, -8, 0)
        targets = probe.targets_for_view(initial, view, [(-32, -32, 164, 144)])
        self.assertAlmostEqual(targets[0][0], -40)
        self.assertAlmostEqual(targets[0][1], -32)
        self.assertEqual(targets[0][2:], (164, 144))

    def test_image_coverage_escape_cannot_count_as_responsive_success(self):
        initial = View()
        with self.assertRaisesRegex(ValueError, "coverage escape"):
            probe.targets_for_view(
                initial, probe.pan(initial, -40, 0), [(-32, -32, 164, 144)]
            )

    def test_resize_or_zoom_requires_preparation_not_stretched_success(self):
        initial = View()
        for view in (replace(initial, width=101), replace(initial, _zoom=11)):
            with self.assertRaisesRegex(ValueError, "scale escape"):
                probe.targets_for_view(initial, view, [(-32, -32, 164, 144)])

    def test_clock_is_part_of_view_observation(self):
        self.assertEqual(
            probe.state(View(), "exact-second")["forecast_time"], "exact-second"
        )

    def test_coverage_trajectory_has_constant_stride_and_returns(self):
        initial = View()
        view = initial
        steps = probe.coverage_miss_trajectory()
        self.assertEqual(len(steps), 24)
        self.assertEqual(steps[:12], [(-48, 0)] * 12)
        for dx, dy in steps:
            view = probe.pan(view, dx, dy)
        self.assertAlmostEqual(view._center_lon, initial._center_lon)
        self.assertAlmostEqual(view._center_lat, initial._center_lat)
        for count, stride in ((1, 48), (3, 48), (24, 0), (24, -1)):
            with (
                self.subTest(count=count, stride=stride),
                self.assertRaises(ValueError),
            ):
                probe.coverage_miss_trajectory(count, stride)

    def test_images_and_field_must_both_cover_view(self):
        initial = View()
        targets = [(-32, -32, 164, 144)]
        field = SimpleNamespace(covers=lambda view: True)
        moved, reason = probe.coverage_for_view(initial, initial, targets, field)
        self.assertIsNone(reason)
        self.assertEqual(moved, targets)
        moved, reason = probe.coverage_for_view(
            initial, probe.pan(initial, -40, 0), targets, field
        )
        self.assertIsNone(moved)
        self.assertIn("image coverage", reason)
        field = SimpleNamespace(covers=lambda view: False)
        moved, reason = probe.coverage_for_view(initial, initial, targets, field)
        self.assertIsNone(moved)
        self.assertIn("numerical field coverage", reason)

    def test_scale_escape_is_unavailable_not_stretched_content(self):
        moved, reason = probe.coverage_for_view(
            View(),
            replace(View(), _zoom=11),
            [],
            SimpleNamespace(covers=lambda v: True),
        )
        self.assertIsNone(moved)
        self.assertIn("scale escape", reason)


if __name__ == "__main__":
    unittest.main()
