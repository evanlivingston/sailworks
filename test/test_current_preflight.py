"""Offline screening warns before search without changing data or routing policy."""

import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from gweatherrouting.core.marine.chs import DownloadCancelledError
from gweatherrouting.core.marine.fields import FieldSelection
from gweatherrouting.core.marine.preflight import check_current_coverage
from gweatherrouting.core.marine.routing import CurrentRoutingEnvironment
from test.test_current_routing import START, current_field


def routing(field=None, start=(0, 0), end=(0, 0.05), water=None):
    times = np.array([(START + timedelta(hours=h)).timestamp() for h in range(25)])
    if field is None:
        field = replace(
            current_field(),
            times=times,
            east=np.zeros((25, 2, 2)),
            north=np.zeros((25, 2, 2)),
        )
    environment = CurrentRoutingEnvironment(Mock(), FieldSelection([field]))
    return SimpleNamespace(
        position=start,
        track=[start, end],
        wp=1,
        time=START,
        current_environment=environment,
        algorithm=SimpleNamespace(
            points_validity=water or (lambda points: [True] * len(points)),
            point_validity=None,
        ),
    )


class TestCurrentPreflight(unittest.TestCase):
    def test_clear_screen_has_no_warning_and_does_not_change_provenance(self):
        route = routing()
        report = check_current_coverage(route)
        self.assertFalse(report.blocked)
        self.assertFalse(report.needs_warning)
        self.assertGreater(report.positions, 2)
        self.assertEqual(report.end - report.start, timedelta(hours=24))
        self.assertFalse(route.current_environment.selection.used)
        self.assertFalse(route.current_environment.selection.missing)
        route.current_environment.wind.get_wind_at.assert_not_called()

    def test_masked_nearby_water_warns_even_when_track_line_is_clear(self):
        route = routing()
        original = route.current_environment.selection.fields[0]
        axes = np.linspace(-0.2, 0.2, 81)
        east = np.zeros((25, 81, 81))
        east[:, (axes > 0.035) & (axes < 0.095), :] = np.nan
        field = replace(
            original, latitudes=axes, longitudes=axes, east=east, north=east.copy()
        )
        route.current_environment.selection = FieldSelection([field])
        report = check_current_coverage(route)
        masks = [f for f in report.findings if f.status == "masked_or_missing"]
        self.assertTrue(masks)
        self.assertTrue(all(f.region == "nearby" for f in masks))
        self.assertFalse(report.blocked)
        self.assertIn("Routing will avoid them", report.describe(False))
        # Shoreline checks discard land before screening current masks.
        route.algorithm.points_validity = lambda points: [p[0] < 0.03 for p in points]
        report = check_current_coverage(route)
        self.assertFalse(report.findings)

    def test_departure_mask_blocks_but_auto_download_can_fill_absent_data(self):
        route = routing(current_field(masked=True))
        self.assertTrue(check_current_coverage(route, True).blocked)
        self.assertTrue(check_current_coverage(route, False).blocked)
        route.current_environment.selection = FieldSelection([])
        self.assertFalse(check_current_coverage(route, True).blocked)
        self.assertTrue(check_current_coverage(route, False).blocked)

    def test_future_missing_times_warn_without_claiming_arrival_time(self):
        route = routing(current_field())
        report = check_current_coverage(route)
        self.assertFalse(report.blocked)
        self.assertTrue(any(f.status == "no_coverage" for f in report.findings))
        self.assertIn("Enable automatic downloads", report.describe(False))

    def test_land_waypoint_blocks_and_disabled_coastline_checks_warn(self):
        route = routing(water=lambda points: [p[1] < 0.04 for p in points])
        report = check_current_coverage(route)
        self.assertTrue(report.blocked)
        self.assertTrue(any(f.status == "land" for f in report.findings))
        route.algorithm.points_validity = None
        report = check_current_coverage(route)
        self.assertFalse(report.blocked)
        self.assertTrue(report.needs_warning)
        self.assertIn("Coastline checks are off", report.describe(False))

    def test_cancellation_and_wind_only_mode(self):
        route = routing()
        route.current_environment.cancel.set()
        with self.assertRaises(DownloadCancelledError):
            check_current_coverage(route)
        route.current_environment = None
        self.assertIsNone(check_current_coverage(route))

    def test_explicit_start_position_and_remaining_waypoints_are_used(self):
        route = routing()
        route.track = [(40, 40), (0, 0.05)]
        route.wp = 1
        self.assertFalse(check_current_coverage(route).findings)
