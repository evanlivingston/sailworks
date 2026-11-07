"""Pure reference-coordinate contracts for the shared pan foundation."""

from dataclasses import dataclass
import unittest

from sailworks.qt.viewporttransform import (
    CameraChange,
    LayerPlacement,
    MAX_LAT,
    PanReference,
    PaddedPanWindow,
    continuous_longitude,
    reference_local_point,
    wrap_longitude,
)
from sailworks.rendering.weatherbarbs import BarbView


@dataclass
class Camera:
    center_lat: float = 48.3
    center_lon: float = -123.1
    zoom: float = 9.0
    width: int = 900
    height: int = 700


def expected_translation(reference, camera):
    view = BarbView(
        int(reference.width),
        int(reference.height),
        float(reference.center_lat),
        float(reference.center_lon),
        float(reference.zoom),
        "standard",
        True,
    )
    current_lon = continuous_longitude(camera.center_lon, reference.center_lon)
    return (
        view._lon_to_x(reference.center_lon) - view._lon_to_x(current_lon),
        view._lat_to_y(reference.center_lat) - view._lat_to_y(camera.center_lat),
    )


class TestViewportTransform(unittest.TestCase):
    def setUp(self):
        self.camera = Camera()
        self.reference = PanReference.from_camera(self.camera)

    def test_wrap_is_continuous_at_dateline(self):
        self.assertEqual(wrap_longitude(180), -180)
        self.assertEqual(continuous_longitude(-179.9, 179.9), 180.1)
        reference = PanReference(0, 179.9, 4, 640, 480)
        east = Camera(0, -179.9, 4, 640, 480)
        self.assertLess(abs(reference.translation_to(east)[0]), 10.0)

    def test_same_anchor_translation_is_shared(self):
        camera = Camera(center_lon=-123.10125, center_lat=48.301)
        first = LayerPlacement(100.25, -50.5, self.reference)
        second = LayerPlacement(100.25, -50.5, self.reference)
        one = first.screen_anchor(camera)
        two = second.screen_anchor(camera)
        self.assertLess(max(abs(one[0] - two[0]), abs(one[1] - two[1])), 0.001)

    def test_pan_is_distinct_from_zoom_resize_and_dpr(self):
        self.assertEqual(
            self.reference.classify(Camera(center_lon=-122)), CameraChange.PAN
        )
        self.assertEqual(
            self.reference.classify(Camera(zoom=10)),
            CameraChange.REFERENCE_INVALIDATION,
        )
        self.assertEqual(
            self.reference.classify(Camera(width=901)),
            CameraChange.REFERENCE_INVALIDATION,
        )
        self.assertEqual(
            self.reference.classify(self.camera, dpr=2),
            CameraChange.REFERENCE_INVALIDATION,
        )

    def test_references_are_per_map(self):
        other = PanReference.from_camera(Camera(center_lon=10))
        moved = Camera(center_lon=-122)
        self.assertNotEqual(
            self.reference.translation_to(moved), other.translation_to(moved)
        )

    def test_translation_matches_production_projection_normal(self):
        camera = Camera(center_lon=-123.05, center_lat=48.25)
        actual = self.reference.translation_to(camera)
        expected = expected_translation(self.reference, camera)
        self.assertLess(abs(actual[0] - expected[0]), 0.001)
        self.assertLess(abs(actual[1] - expected[1]), 0.001)

    def test_translation_matches_production_projection_high_zoom_clamp(self):
        reference = PanReference(MAX_LAT, 10.0, 18.0, 640, 480)
        camera = Camera(MAX_LAT, 10.0005, 18.0, 640, 480)
        actual = reference.translation_to(camera)
        expected = expected_translation(reference, camera)
        self.assertLess(abs(actual[0] - expected[0]), 0.001)
        self.assertLess(abs(actual[1] - expected[1]), 0.001)

        reference2 = PanReference(-MAX_LAT, -170.0, 16.0, 800, 600)
        camera2 = Camera(-MAX_LAT + 0.0001, -169.9999, 16.0, 800, 600)
        actual2 = reference2.translation_to(camera2)
        expected2 = expected_translation(reference2, camera2)
        self.assertLess(abs(actual2[0] - expected2[0]), 0.001)
        self.assertLess(abs(actual2[1] - expected2[1]), 0.001)

    def test_translation_fractional_movements(self):
        reference = PanReference(12.345678, -98.765432, 11.25, 1024, 768)
        camera = Camera(12.346, -98.765, 11.25, 1024, 768)
        actual = reference.translation_to(camera)
        expected = expected_translation(reference, camera)
        self.assertLess(abs(actual[0] - expected[0]), 0.001)
        self.assertLess(abs(actual[1] - expected[1]), 0.001)

    def test_antimeridian_policy(self):
        reference = PanReference(0.0, 179.9, 4.0, 640, 480)
        east = Camera(0.0, -179.9, 4.0, 640, 480)
        actual = reference.translation_to(east)
        # continuous choice: small movement across seam
        self.assertLess(abs(actual[0]), 10.0)
        expected = expected_translation(reference, east)
        self.assertLess(abs(actual[0] - expected[0]), 0.001)
        self.assertLess(abs(actual[1] - expected[1]), 0.001)

    def test_rejects_incompatible_translation_to(self):
        reference = self.reference
        with self.assertRaises(ValueError):
            reference.translation_to(Camera(zoom=10))
        with self.assertRaises(ValueError):
            reference.translation_to(Camera(width=901))
        with self.assertRaises(ValueError):
            reference.translation_to(self.camera, dpr=2)

    def test_per_map_isolation_translation(self):
        ref_a = PanReference.from_camera(Camera(center_lon=10))
        ref_b = PanReference.from_camera(Camera(center_lon=-120))
        moved = Camera(center_lon=-122)
        self.assertNotEqual(ref_a.translation_to(moved), ref_b.translation_to(moved))

    def test_padded_pan_window_translates_until_padding_is_exhausted(self):
        window = PaddedPanWindow(self.reference, padding=500.0)
        covered = Camera(center_lon=-122.5)
        dx, dy, rebase = window.placement(covered)
        self.assertFalse(rebase)
        self.assertNotEqual((dx, dy), (0.0, 0.0))

        outside = Camera(center_lon=-118.0)
        dx, dy, rebase = window.placement(outside)
        self.assertTrue(rebase)

    def test_padded_pan_window_rebases_on_camera_invalidation(self):
        window = PaddedPanWindow(self.reference, padding=500.0)
        dx, dy, rebase = window.placement(Camera(zoom=10))
        self.assertTrue(rebase)
        self.assertEqual((dx, dy), (0.0, 0.0))

    def test_reference_local_point_matches_retained_pan_placement(self):
        camera = Camera(center_lon=-122.5, center_lat=48.5)
        x, y = reference_local_point(self.reference, camera, 125.0, 275.0)
        dx, dy = self.reference.translation_to(camera)
        self.assertAlmostEqual(x + dx, 125.0)
        self.assertAlmostEqual(y + dy, 275.0)

    def test_reference_local_point_is_continuous_at_antimeridian(self):
        reference = PanReference(0.0, 179.9, 4.0, 640, 480)
        camera = Camera(0.0, -179.9, 4.0, 640, 480)
        x, y = reference_local_point(reference, camera, 100.0, 200.0)
        self.assertLess(abs(x - 100.0), 10.0)
        self.assertEqual(y, 200.0)


if __name__ == "__main__":
    unittest.main()
