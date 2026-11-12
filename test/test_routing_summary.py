"""Route-list fallback badging from current provenance."""

import unittest

from sailworks.rendering.navigation import routing_summary


class FakeRoute(list):
    def __init__(self, provenance):
        super().__init__(
            [
                (49.0, -125.0, "2026-01-01 00:00:00", 0, 0, 5.0, 90.0),
                (49.0, -124.9, "2026-01-01 01:00:00", 0, 0, 5.0, 90.0),
            ]
        )
        self.provenance = provenance


def provenance(actual):
    return {
        "routing": "isochrone-with-currents",
        "currents": {
            "selected_sources": [
                {
                    "dataset_id": "fine",
                    "model": "CIOPS-SalishSea",
                    "resolution_m": 500,
                },
                {
                    "dataset_id": "coarse",
                    "model": "SMOC circulation + tides",
                    "resolution_m": 9000,
                },
            ],
            "actual_sources": actual,
        },
    }


class TestRoutingSummaryFallback(unittest.TestCase):
    def test_coarser_serving_model_adds_fallback_badge(self):
        summary = routing_summary(FakeRoute(provenance(["coarse"])))
        self.assertIn("SMOC circulation + tides", summary)
        self.assertIn("incl. fallback", summary)

    def test_finest_serving_model_has_no_badge(self):
        summary = routing_summary(FakeRoute(provenance(["fine"])))
        self.assertIn("CIOPS-SalishSea", summary)
        self.assertNotIn("fallback", summary)

    def test_missing_resolutions_never_badges(self):
        bare = {
            "routing": "isochrone-with-currents",
            "currents": {
                "selected_sources": [{"dataset_id": "a", "model": "X"}],
                "actual_sources": ["a"],
            },
        }
        self.assertNotIn("fallback", routing_summary(FakeRoute(bare)))


class TestRoutingSummarySpeed(unittest.TestCase):
    def test_distance_and_duration_define_average_speed(self):
        # 3.5 degrees on the equator is about 210 nm; 42 h is 1d 18h, so the
        # ground speed is about 5.0 kts. The old mean of 2.0 and 4.8 was 3.4.
        route = [
            (0.0, 0.0, "2026-01-01 00:00:00", 0, 0, 2.0, 90.0),
            (0.0, 3.5, "2026-01-02 18:00:00", 0, 0, 4.8, 90.0),
        ]
        summary = routing_summary(_Route(route))
        self.assertIn("210 nm", summary)
        self.assertIn("1d 18h", summary)
        self.assertIn("avg 5.0 kts", summary)

    def test_duration_crossing_midnight_uses_elapsed_time(self):
        # 22:00 on one day to 16:00 two days later is still 42 h, not 18 h or
        # 66 h; the midnight boundary must not truncate the elapsed duration.
        route = [
            (0.0, 0.0, "2026-03-01 22:00:00", 0, 0, 2.0, 90.0),
            (0.0, 3.5, "2026-03-03 16:00:00", 0, 0, 4.8, 90.0),
        ]
        summary = routing_summary(_Route(route))
        self.assertIn("1d 18h", summary)
        self.assertIn("avg 5.0 kts", summary)

    def test_unparsable_timestamps_fall_back_to_leg_mean(self):
        route = [
            (0.0, 0.0, "not-a-time", 0, 0, None, 90.0),
            (0.0, 0.5, "", 0, 0, 3.0, 90.0),
            (0.0, 1.0, "", 0, 0, 5.0, 90.0),
        ]
        summary = routing_summary(_Route(route))
        self.assertIn("avg 4.0 kts (leg mean)", summary)

    def test_missing_speeds_are_absent_in_the_fallback(self):
        route = [
            (0.0, 0.0, None, 0, 0, None, 90.0),
            (0.0, 1.0, None, 0, 0, 3.0, 90.0),
            (0.0, 2.0, None, 0, 0, None, 90.0),
            (0.0, 3.0, None, 0, 0, 5.0, 90.0),
        ]
        summary = routing_summary(_Route(route))
        # Only the two present leg speeds count: averaging them gives 4.0
        # rather than dragging the result toward zero with the missing one.
        self.assertIn("avg 4.0 kts (leg mean)", summary)

    def test_missing_speeds_do_not_affect_timed_average(self):
        route = [
            (0.0, 0.0, "2026-01-01 00:00:00", 0, 0, None, 90.0),
            (0.0, 3.5, "2026-01-02 18:00:00", 0, 0, None, 90.0),
        ]
        summary = routing_summary(_Route(route))
        self.assertIn("avg 5.0 kts", summary)

    def test_no_speeds_and_no_timestamps_omit_the_average(self):
        route = [
            (0.0, 0.0, None, 0, 0, None, 90.0),
            (0.0, 1.0, None, 0, 0, None, 90.0),
        ]
        summary = routing_summary(_Route(route))
        self.assertIn("60 nm", summary)
        self.assertNotIn("avg", summary)


class _Route(list):
    def __init__(self, points):
        super().__init__(points)
        self.provenance = None


if __name__ == "__main__":
    unittest.main()


def test_partial_points_do_not_crash_the_summary():
    class PartialRoute(list):
        def __init__(self):
            super().__init__(
                [
                    (48.2, -123.7),
                    (48.4, -123.5),
                ]
            )
            self.provenance = None

    # A saved route with coordinate-only points has no usable summary, but it
    # must not raise while the navigation panel lists routes.
    assert routing_summary(PartialRoute()) == ""


def test_partial_points_are_skipped_beside_complete_ones():
    class MixedRoute(list):
        def __init__(self):
            super().__init__(
                [
                    (49.0, -125.0, "2026-01-01 00:00:00", 0, 0, 5.0, 90.0),
                    (49.0, -124.9),
                    (49.0, -124.8, "2026-01-01 01:00:00", 0, 0, 5.0, 90.0),
                ]
            )
            self.provenance = None

    summary = routing_summary(MixedRoute())
    assert "nm" in summary
