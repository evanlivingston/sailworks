"""Route-list fallback badging from current provenance."""

import unittest

from gweatherrouting.rendering.navigation import routing_summary


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


if __name__ == "__main__":
    unittest.main()
