"""Marine presentation values and immutable Quick-render snapshots."""

import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from gweatherrouting.core.mapviewport import MapPoint
from gweatherrouting.core.marine.manager import MarineManager
from gweatherrouting.core.marine.models import Capability
from gweatherrouting.core.timecontrol import TimeControl
from gweatherrouting.rendering.marinepresentation import (
    MarineSources,
    chart_instant,
    current_style,
    flow,
)
from gweatherrouting.rendering.marinesnapshot import (
    CurrentArrow,
    MarineSnapshot,
    current_labels,
    current_triangles,
    sample_currents,
)
from gweatherrouting.qt.gribviewport import ViewportCamera
from test.test_marine import START
from test.test_marine_fields import field


class TestMarineDisplay(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.manager = MarineManager(self._temporary.name)
        self.clock = TimeControl()
        self.clock.set_time(START.astimezone().replace(tzinfo=None))
        self.camera = ViewportCamera(49.5, -125.5, 10, 256, 192, START)

    def sources(self, fields=(), levels=None, currents=None, density="standard"):
        return MarineSources(
            fields,
            levels or self.manager.snapshot(Capability.WATER_LEVEL),
            currents or self.manager.snapshot(Capability.STATION_CURRENT),
            density,
        )

    def test_current_bearing_and_fixed_speed_style(self):
        for east, north, bearing in ((1, 0, 90), (0, 1, 0), (-1, 0, 270), (0, -1, 180)):
            self.assertAlmostEqual(flow(east, north)[1], bearing)
        lengths = [current_style(speed)[0] for speed in (0.25, 1, 2, 3, 5)]
        self.assertEqual(lengths, sorted(set(lengths)))
        self.assertEqual(current_style(8), current_style(5))

    def test_local_aware_clock_converts_to_utc_without_relabelling(self):
        pacific = timezone(timedelta(hours=-7))
        local = datetime(2026, 10, 10, 17, tzinfo=pacific)
        self.assertEqual(chart_instant(local), START)
        wall = local.replace(tzinfo=None)
        self.assertEqual(chart_instant(wall).timestamp(), wall.timestamp())

    def test_native_mask_and_time_coverage_still_control_field_samples(self):
        for source, instant in (
            (field(masked=True), START),
            (field(), START + timedelta(days=1)),
        ):
            snapshot = sample_currents(
                self.sources((source,)), instant, self.camera, lambda: False
            )
            self.assertFalse(snapshot.arrows)
            self.assertEqual(snapshot.state, "no valid data")

    def test_quick_snapshot_contains_tide_and_station_current_labels(self):
        station = SimpleNamespace(
            id="station-1", latitude=49.5, longitude=-125.5, name="Harbor"
        )
        available_level = SimpleNamespace(status="available", values=(2.5,))
        available_current = SimpleNamespace(status="available", values=(1, 0))
        levels = SimpleNamespace(
            datasets=(SimpleNamespace(station=station),),
            sample=Mock(return_value=available_level),
        )
        currents = SimpleNamespace(
            datasets=(SimpleNamespace(station=station),),
            sample=Mock(return_value=available_current),
        )
        sources = self.sources(levels=levels, currents=currents)
        snapshot = sample_currents(sources, START, self.camera, lambda: False)
        tide = next(arrow for arrow in snapshot.arrows if arrow.tide)
        current = next(arrow for arrow in snapshot.arrows if not arrow.tide)
        self.assertIn("2.50 m (source datum)", tide.label)
        self.assertIn("1.9 kn (station)", current.label)
        self.assertTrue(current_triangles(snapshot, self.camera))
        labels = current_labels(snapshot, self.camera)
        self.assertEqual({label[2] for label in labels}, {tide.label, current.label})

    def test_missing_or_cancelled_samples_do_not_publish_false_arrows(self):
        unavailable = SimpleNamespace(status="missing", values=())
        station = SimpleNamespace(
            id="station-1", latitude=49.5, longitude=-125.5, name="Harbor"
        )
        currents = SimpleNamespace(
            datasets=(SimpleNamespace(station=station),),
            sample=Mock(return_value=unavailable),
        )
        snapshot = sample_currents(
            self.sources(currents=currents), START, self.camera, lambda: False
        )
        self.assertEqual(snapshot.arrows, ())
        self.assertEqual(
            sample_currents(self.sources((field(),)), START, self.camera, lambda: True),
            None,
        )


if __name__ == "__main__":
    unittest.main()
