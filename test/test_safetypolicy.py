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
    def test_invalid_thresholds_are_rejected(self):
        for options in (
            {"tws_max_kn": -1},
            {"motor_speed_kn": float("nan")},
            {"wave_threshold_kn": 0},
            {"wave_max_reduction": 1.1},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                SafetyPolicy(**options)

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
    def test_scalar_and_whole_frontier_agree_with_motor_and_wave_proxy(self):
        from test.test_current_routing import setup_router as current_router
        from test.test_frontier_routing import table_polar
        from gweatherrouting.core.polarwrapper import PolarWrapper
        from types import SimpleNamespace

        results = []
        for arrays in (False, True):
            router, _, _ = current_router(
                wind=SimpleNamespace(get_wind_at=lambda *_: (0, 15), last_source="test")
            )
            router.use_numpy = arrays
            router.polar = SafetyPolar(
                PolarWrapper(table_polar(), 0.83),
                SafetyPolicy(
                    tws_max_kn=40,
                    min_sailing_speed_kn=3,
                    motor_speed_kn=5,
                    wave_correct=True,
                ),
            )
            result = router.route(None, START, 1, (0, 0), (0, 0.5))
            results.append(result.isochrones[-1])
        self.assertEqual(len(results[0]), len(results[1]))
        for a, b in zip(*results):
            np.testing.assert_allclose(
                (*a.pos, a.speed), (*b.pos, b.speed), rtol=1e-8, atol=1e-10
            )

    def test_scalar_and_whole_frontier_both_reject_over_limit_wind(self):
        from test.test_current_routing import setup_router as current_router
        from test.test_frontier_routing import table_polar
        from types import SimpleNamespace

        for arrays in (False, True):
            router, _, _ = current_router(
                wind=SimpleNamespace(get_wind_at=lambda *_: (0, 20), last_source="test")
            )
            router.use_numpy = arrays
            router.polar = SafetyPolar(table_polar(), SafetyPolicy(tws_max_kn=30))
            with self.assertRaisesRegex(ValueError, "exceed the configured"):
                router.route(None, START, 1, (0, 0), (0, 0.5))

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
