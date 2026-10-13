"""Route markers compare actual instants for both legacy and offset timestamps."""

import os
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gweatherrouting.core.geo.routing import Routing
from gweatherrouting.gtk.maplayers.geomaplayer import GeoMapLayer


@unittest.skipUnless(hasattr(time, "tzset"), "Requires process timezone selection")
class TestRouteMarkerTime(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, TZ="America/Los_Angeles")
        env.start()
        time.tzset()
        self.addCleanup(time.tzset)
        self.addCleanup(env.stop)

    def draw_marker(self, start, end, clock):
        route = Routing(
            "test",
            points=[
                (48, -123, start, 0, 0, 5, 0),
                (49, -123, end, 0, 0, 5, 0),
            ],
        )
        core = SimpleNamespace(
            logManager=SimpleNamespace(log=[]),
            routingManager=[route],
            trackManager=[],
            poiManager=[],
        )
        layer = GeoMapLayer(core, SimpleNamespace(time=clock))
        canvas = Mock()
        canvas.convert_geographic_to_screen.return_value = (10, 20)
        cr = Mock()
        with (
            patch(
                "gweatherrouting.gtk.maplayers.geomaplayer.utils.point_distance",
                return_value=1000,
            ),
            patch(
                "gweatherrouting.gtk.maplayers.geomaplayer.utils.routage_point_distance",
                return_value=(48.5, -123),
            ) as move,
        ):
            layer.do_draw(canvas, cr)
            move.assert_called_once_with(48, -123, 500, 0)
        self.assertEqual(sum(call.args[2] == 8 for call in cr.arc.call_args_list), 2)

    def test_legacy_local_and_offset_routes_place_marker_at_same_instant(self):
        for start, end in (
            ("2026-10-12T09:00:00", "2026-10-12T10:00:00"),
            ("2026-10-12T16:00:00Z", "2026-10-12T17:00:00Z"),
            ("2026-10-12T09:00:00-07:00", "2026-10-12T17:00:00+00:00"),
        ):
            with self.subTest(start=start):
                self.draw_marker(start, end, datetime(2026, 10, 12, 9, 30))

    def test_marker_handles_repeated_local_hour_at_daylight_saving_transition(self):
        stamp = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc).timestamp()
        self.draw_marker(
            "2026-11-01T01:30:00-07:00",
            "2026-11-01T01:30:00-08:00",
            datetime.fromtimestamp(stamp),
        )
