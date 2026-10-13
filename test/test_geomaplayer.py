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


class TestLogTrackRendering(unittest.TestCase):
    def setUp(self):
        from test.map_helpers import MapCanvas

        self.canvas = MapCanvas()
        self.points = []
        self.layer = GeoMapLayer(
            SimpleNamespace(logManager=SimpleNamespace(log=self.points)), None
        )

    def point(self, x, y):
        return self.canvas.convert_screen_to_geographic(x, y).get_degrees()

    def test_offscreen_segments_are_skipped_and_crossing_segments_retained(self):
        self.points.extend(
            self.point(x, y)
            for x, y in ((-100, -100), (-50, -50), (-100, 150), (500, 150), (500, 400))
        )
        cr = Mock()
        self.layer._draw_log(self.canvas, cr)
        self.assertEqual(cr.stroke.call_count, 1)
        self.assertAlmostEqual(cr.move_to.call_args.args[0], -100)
        self.assertAlmostEqual(cr.line_to.call_args.args[0], 500)

    def test_batched_projection_preserves_visible_pixels_and_segment_alpha(self):
        import cairo
        import numpy
        from gweatherrouting.gtk.style import Style
        from gweatherrouting.gtk.widgets.mapwidget import MapPoint

        self.points.extend(
            self.point(x, y)
            for x, y in (
                (-50, 20),
                (50, 80),
                (350, 220),
                (50, 80),
                (400, 150),
                (401, 250),
                (600, 350),
                (-200, 350),
                (-20, 150),
                (420, 150),
            )
        )
        for zoom in (11.7, 12, 12.3):
            for factor in (1, 2):
                with self.subTest(zoom=zoom, factor=factor):
                    self.canvas._zoom = zoom
                    surfaces = [
                        cairo.ImageSurface(
                            cairo.FORMAT_ARGB32, 400 * factor, 300 * factor
                        )
                        for _ in range(2)
                    ]
                    for surface in surfaces:
                        surface.set_device_scale(factor, factor)
                    legacy = cairo.Context(surfaces[0])
                    for a, b in zip(self.points, self.points[1:]):
                        Style.Track.LogTrack.apply(legacy)
                        legacy.move_to(
                            *self.canvas.convert_geographic_to_screen(MapPoint(*a))
                        )
                        legacy.line_to(
                            *self.canvas.convert_geographic_to_screen(MapPoint(*b))
                        )
                        legacy.stroke()
                    self.layer._draw_log(self.canvas, cairo.Context(surfaces[1]))
                    for surface in surfaces:
                        surface.flush()
                    pixels = [
                        numpy.frombuffer(s.get_data(), dtype=numpy.uint8).astype(int)
                        for s in surfaces
                    ]
                    self.assertLessEqual(numpy.max(numpy.abs(pixels[0] - pixels[1])), 1)

    def test_appends_edits_replacement_and_clear_are_visible_immediately(self):
        self.points.extend([self.point(50, 50), self.point(100, 100)])
        for action, expected in (
            (lambda: None, 1),
            (lambda: self.points.append(self.point(150, 150)), 2),
            (lambda: self.points.__setitem__(1, self.point(-1000, -1000)), 2),
            (
                lambda: setattr(
                    self.layer.core.logManager,
                    "log",
                    [self.point(10, 10), self.point(20, 20)],
                ),
                1,
            ),
            (lambda: self.layer.core.logManager.log.clear(), 0),
        ):
            action()
            cr = Mock()
            self.layer._draw_log(self.canvas, cr)
            self.assertEqual(cr.stroke.call_count, expected)


class TestRouteTimeParsing(unittest.TestCase):
    def test_parsing_is_reused_but_point_edits_are_seen(self):
        from gweatherrouting.gtk.maplayers.geomaplayer import _route_datetime

        _route_datetime.cache_clear()
        with patch(
            "gweatherrouting.gtk.maplayers.geomaplayer.dateutil.parser.parse",
            wraps=__import__("dateutil.parser", fromlist=["parse"]).parse,
        ) as parse:
            first = _route_datetime("2026-10-13T12:00:00Z")
            self.assertIs(_route_datetime("2026-10-13T12:00:00Z"), first)
            self.assertNotEqual(_route_datetime("2026-10-13T13:00:00Z"), first)
            self.assertEqual(parse.call_count, 2)
        _route_datetime.cache_clear()
