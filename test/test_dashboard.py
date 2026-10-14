"""Instrument fidelity and complete gauge visibility on maps with sidebars."""

import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import cairo

from gweatherrouting.core.core import BoatInfo
from gweatherrouting.rendering.dashboard import DashboardRenderer


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.connections = SimpleNamespace(get_status=lambda: [], polling_active=False)
        self.renderer = DashboardRenderer(self.connections)
        self.boat = BoatInfo()
        self.renderer.boat_info = self.boat

    def values(self):
        self.renderer._update_gauge_values()
        return {g.label: g.value for g in self.renderer._gauges}

    def test_reported_true_wind_and_zero_values_are_retained(self):
        self.boat.sog = 0
        self.boat.cog = 0
        self.boat.aws = 15
        self.boat.awa = 270
        self.boat.tws = 8
        self.boat.twa = 0
        self.boat.depth = 0
        self.assertEqual(
            self.values(), dict(SOG=0, COG=0, AWS=15, AWA=270, TWS=8, TWA=0, Depth=0)
        )

    def test_missing_true_wind_uses_existing_apparent_wind_and_sog_formula(self):
        self.boat.aws, self.boat.sog = 10, 6
        for apparent, expected_angle in (
            (90, math.degrees(math.atan2(10, -6))),
            (270, 360 - math.degrees(math.atan2(10, -6))),
        ):
            self.boat.awa = apparent
            values = self.values()
            self.assertAlmostEqual(values["TWS"], math.sqrt(136))
            self.assertAlmostEqual(values["TWA"], expected_angle)
            self.assertIsNone(self.boat.tws)
            self.assertIsNone(self.boat.twa)

    def test_reset_or_incomplete_measurements_display_missing_values(self):
        self.boat.aws, self.boat.awa = 10, 45
        self.assertIsNone(self.values()["TWS"])
        self.boat.tws, self.boat.twa = 5, 10
        self.values()
        self.renderer.boat_info = BoatInfo()
        self.assertTrue(all(value is None for value in self.values().values()))

    def test_every_gauge_fits_maps_with_sidebars_without_changing_order(self):
        for width, height in (
            (240, 400),
            (400, 300),
            (650, 220),
            (806, 180),
            (1200, 600),
        ):
            with self.subTest(width=width):
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
                view = SimpleNamespace(
                    get_allocated_width=lambda: width,
                    get_allocated_height=lambda: height,
                )
                gauges = self.renderer._gauges
                for gauge in gauges:
                    gauge.draw = Mock()
                self.renderer._draw_dashboard(view, cairo.Context(surface))
                positions = []
                for gauge in gauges:
                    gauge.draw.assert_called_once()
                    _, x, y = gauge.draw.call_args.args
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + gauge.WIDTH, width)
                    self.assertLessEqual(y + gauge.HEIGHT, height)
                    positions.append((y, x))
                self.assertEqual(positions, sorted(positions))

    def test_connection_summary_and_disconnected_details_match_instrument_state(self):
        for polling, statuses, expected in (
            (False, [], ["Data off"]),
            (True, [], ["No connections"]),
            (
                True,
                [dict(name="GPS", connected=True, packets=120)],
                ["1/1 connected", "120 pkt"],
            ),
            (
                True,
                [
                    dict(name="GPS", connected=True, packets=120),
                    dict(name="Wind", connected=False, packets=30),
                ],
                ["1/2 connected", "150 pkt", "Wind"],
            ),
        ):
            self.connections.polling_active = polling
            self.connections.get_status = lambda: statuses
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 900, 400)
            cr = Mock(wraps=cairo.Context(surface))
            self.renderer._draw_connection_status(cr, 40, 300, 806)
            texts = [call.args[0] for call in cr.show_text.call_args_list]
            self.assertEqual(texts, expected)
