"""Boat-safety policy: TWS caps, motoring fallback, wave proxy."""

import math
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import numpy as np

from gweatherrouting.core.marine.fields import CurrentField, FieldSelection
from gweatherrouting.core.marine.models import canonical_json
from gweatherrouting.core.marine.routing import (
    CurrentIsoRouter,
    CurrentRoutingEnvironment,
    InvalidMarineLegError,
)
from gweatherrouting.core.safetypolicy import SafetyPolar, SafetyPolicy, policy_of

START = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def current_field():
    metadata = dict(
        schema_version=1,
        provider="test",
        model="Analytical field",
        composition="total",
        vector_reference="true_east_north",
        sources=[],
        resolution_m=9000,
        run=START.isoformat(),
    )
    return CurrentField(
        canonical_json(metadata),
        np.array([START.timestamp(), (START + timedelta(seconds=3600)).timestamp()]),
        np.array([-1, 1]),
        np.array([-1, 1]),
        np.zeros((2, 2, 2)),
        np.zeros((2, 2, 2)),
    )


def setup_router(wind_ms, polar_speed_kn=6, policy=None):
    wind = Mock(get_wind_at=Mock(return_value=(0.0, wind_ms)))
    env = CurrentRoutingEnvironment(wind, FieldSelection([current_field()]))
    polar = Mock(get_speed=Mock(return_value=polar_speed_kn))
    if policy is not None:
        polar = SafetyPolar(polar, policy)
    router = CurrentIsoRouter(polar, wind, environment=env)
    return router, env


class TestSafetyPolicy(unittest.TestCase):
    def test_inert_by_default(self):
        policy = SafetyPolicy()
        self.assertFalse(policy.active)
        self.assertFalse(policy.is_nogo(100))
        self.assertEqual(policy.apply(10, 6), 6)

    def test_unknown_polars_get_inert_policy(self):
        self.assertFalse(policy_of(Mock()).active)
        self.assertFalse(policy_of(object()).active)

    def test_unwrap_chain(self):
        inner = Mock()
        wrapped = SafetyPolar(inner, SafetyPolicy(tws_max_kn=30))
        self.assertEqual(policy_of(wrapped).tws_max_kn, 30)


class TestSafetyPolar(unittest.TestCase):
    def test_cap_returns_zero(self):
        polar = SafetyPolar(
            Mock(get_speed=Mock(return_value=8)), SafetyPolicy(tws_max_kn=30)
        )
        self.assertEqual(polar.get_speed(35, 1.0), 0.0)
        self.assertEqual(polar.get_speed(30, 1.0), 8)

    def test_motor_floor(self):
        policy = SafetyPolicy(min_sailing_speed_kn=3, motor_speed_kn=5)
        polar = SafetyPolar(Mock(get_speed=Mock(return_value=2)), policy)
        self.assertEqual(polar.get_speed(10, 1.0), 5)
        fast = SafetyPolar(Mock(get_speed=Mock(return_value=6)), policy)
        self.assertEqual(fast.get_speed(10, 1.0), 6)

    def test_wave_proxy_reduces_with_wind(self):
        policy = SafetyPolicy(
            wave_correct=True, wave_threshold_kn=20, wave_max_reduction=0.25
        )
        polar = SafetyPolar(Mock(get_speed=Mock(return_value=8)), policy)
        calm = polar.get_speed(15, 1.0)
        breezy = polar.get_speed(30, 1.0)
        storm = polar.get_speed(60, 1.0)
        self.assertEqual(calm, 8)
        self.assertLess(breezy, 8)
        self.assertLess(storm, breezy)
        self.assertAlmostEqual(storm, 8 * 0.75)

    def test_apply_array_matches_scalar(self):
        policy = SafetyPolicy(
            tws_max_kn=30,
            min_sailing_speed_kn=3,
            motor_speed_kn=5,
            wave_correct=True,
        )
        polar = SafetyPolar(Mock(), policy)
        ws = np.array([10.0, 25.0, 35.0, 10.0])
        base = np.array([2.0, 8.0, 9.0, 6.0])
        adjusted, nogo = polar.apply_array(ws, np.zeros(4), base)
        self.assertEqual(list(nogo), [False, False, True, False])
        self.assertEqual(adjusted[3], 6.0)
        self.assertEqual(adjusted[0], 5.0)
        self.assertTrue(math.isnan(adjusted[2]))


class TestRoutingSafety(unittest.TestCase):
    def test_over_limit_velocity_prunes(self):
        router, env = setup_router(20.0, policy=SafetyPolicy(tws_max_kn=30))
        with self.assertRaises(InvalidMarineLegError):
            router.velocity((0, 0), START, 0.0)
        self.assertEqual(env.policy_pruned, 1)

    def test_over_limit_headings_empty(self):
        router, env = setup_router(20.0, policy=SafetyPolicy(tws_max_kn=30))
        self.assertEqual(router.headings(Mock(pos=(0, 0), time=START), (1, 1)), [])
        self.assertEqual(env.policy_pruned, 1)

    def test_motor_floor_flows_through_velocity(self):
        router, env = setup_router(
            5.0,
            polar_speed_kn=2,
            policy=SafetyPolicy(min_sailing_speed_kn=3, motor_speed_kn=5),
        )
        result = router.velocity((0, 0), START, 0.0)
        self.assertAlmostEqual(result[2] / 0.5144444, 5.0, places=4)
        self.assertEqual(env.policy_pruned, 0)

    def test_no_policy_no_pruning(self):
        router, env = setup_router(50.0, polar_speed_kn=6)
        result = router.velocity((0, 0), START, 0.0)
        self.assertAlmostEqual(result[2] / 0.5144444, 6.0, places=4)
        self.assertEqual(env.policy_pruned, 0)

    def test_all_pruned_reports_limit_error(self):
        router, env = setup_router(20.0, policy=SafetyPolicy(tws_max_kn=30))
        with self.assertRaisesRegex(ValueError, "exceed the configured"):
            router.route(None, START, 1, (0, 0), (1, 1))


if __name__ == "__main__":
    unittest.main()
