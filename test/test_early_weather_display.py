"""Early display refuses stale viewport/time/provenance before publication."""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import early_weather_display as early


class TestEarlyIdentity(unittest.TestCase):
    def test_camera_displacement_returns_exactly_and_never_reprojects_latitude(self):
        @dataclass(frozen=True)
        class Camera:
            _center_lon: float = -123.09473088970692
            _center_lat: float = 48.30887922260005

            def _lon_to_x(self, lon):
                return (lon + 180) * 651.6

            def _x_to_lon(self, x):
                return x / 651.6 - 180

        initial = Camera()
        offset = 0
        for dx in (8, 8, -8, -8):
            offset += dx
            moved = early.camera_at_offset(initial, offset)
            self.assertEqual(moved._center_lat, initial._center_lat)
        self.assertIs(moved, initial)

    def fixture(self):
        meta = dict(
            view={"width": 902},
            clock="2026-10-19T08:00:00",
            before=1,
            after=2,
            generation="new",
            node_count=50,
        )
        reference = dict(
            view=meta["view"].copy(),
            clocks=[meta["clock"]],
            field_meta=[
                {k: meta[k] for k in ("before", "after", "generation", "node_count")}
            ],
        )
        return meta, reference

    def test_exact_identity_accepted(self):
        meta, reference = self.fixture()
        early.validate_identity(meta, reference)

    def test_each_stale_identity_rejected(self):
        meta, reference = self.fixture()
        for key, value in (
            ("view", {"width": 710}),
            ("clock", "old"),
            ("before", 0),
            ("after", 3),
            ("generation", "old"),
            ("node_count", 1),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                changed = deepcopy(meta)
                changed[key] = value
                early.validate_identity(changed, reference)


if __name__ == "__main__":
    unittest.main()
