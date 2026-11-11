"""Real-request bridge checks without importing a Sailworks or Qt process."""

import os
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(
    0,
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        str(ROOT / "build/native-routing-system/python"),
    ),
)

import sailworks_native_routing as bridge  # noqa: E402

START = 1767225600
COVERAGE = {
    "south": 48,
    "north": 50,
    "west": -124,
    "east": -122,
    "start_epoch_s": START,
    "end_epoch_s": START + 6 * 3600,
}
POLAR = [
    {"true_wind_speed_knots": speed, "points": [[30, 3], [90, 3], [180, 3]]}
    for speed in (4, 8, 12, 16)
]
SETTINGS = {
    "start": (48.25, -123.65),
    "destination": (48.29, -123.61),
    "departure_epoch_s": START + 1800,
    "use_currents": False,
    "polar_efficiency": 1.0,
    "tws_max_kn": None,
    "min_sailing_speed_kn": None,
    "motor_speed_kn": 5.0,
    "wave_correct": False,
    "coastline_checks": False,
}


def _wind(latitude, longitude, epoch_s):
    return (0.0, 8.0, True)


def _missing_current(latitude, longitude, epoch_s):
    return (0.0, 0.0, False)


def _run(settings, wind=_wind):
    return bridge.route_user_request(
        wind,
        _missing_current,
        COVERAGE,
        POLAR,
        settings,
        "selected-forecast",
        "selected-polar",
        "main",
    )


class NativeUserBridgeTest(unittest.TestCase):
    def test_selected_geometry_time_and_polar_are_not_fixture_values(self):
        run = _run(SETTINGS)
        self.assertTrue(run["accepted"], run["message"])
        self.assertTrue(run["validation"]["passed"])
        self.assertTrue(run["independent_validation"]["passed"])
        self.assertEqual(run["request"]["departure"], START + 1800)
        self.assertEqual(run["request"]["maximum_route_duration_seconds"], 19800)
        self.assertEqual(
            run["request"]["start"], {"latitude": 48.25, "longitude": -123.65}
        )
        self.assertEqual(
            run["request"]["destination"], {"latitude": 48.29, "longitude": -123.61}
        )
        self.assertEqual(
            run["legs"][0]["wind_source"]["dataset_identity"], "selected-forecast"
        )
        self.assertEqual(run["legs"][0]["start_time"], START + 1800)

    def test_all_safety_settings_change_the_request_or_outcome(self):
        with_motor = _run({**SETTINGS, "min_sailing_speed_kn": 4.0})
        self.assertTrue(with_motor["accepted"], with_motor["message"])
        self.assertTrue(
            all(leg["speed_through_water_knots"] == 5.0 for leg in with_motor["legs"])
        )
        capped = _run({**SETTINGS, "tws_max_kn": 1.0})
        self.assertFalse(capped["accepted"])

    def test_efficiency_and_current_switch_are_applied(self):
        scaled = _run({**SETTINGS, "polar_efficiency": 0.85})
        self.assertTrue(scaled["accepted"], scaled["message"])
        self.assertTrue(
            all(
                abs(leg["speed_through_water_knots"] - 3.0 * 0.85) < 1e-8
                for leg in scaled["legs"]
            )
        )
        self.assertFalse(
            _run({**SETTINGS, "use_currents": True})["accepted"],
            "a missing selected current must not silently become calm water",
        )

    def test_wave_proxy_and_motor_match_legacy_safety_policy(self):
        path = ROOT / "sailworks/core/safetypolicy.py"
        spec = importlib.util.spec_from_file_location(
            "sailworks_safety_reference", path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        speed_ms = 24.0 / (3600.0 / 1852.0)

        def high_wind(latitude, longitude, epoch_s):
            return (0.0, speed_ms, True)

        options = {
            **SETTINGS,
            "wave_correct": True,
            "min_sailing_speed_kn": 3.0,
        }
        run = _run(options, high_wind)
        self.assertTrue(run["accepted"], run["message"])
        policy = module.SafetyPolicy(
            min_sailing_speed_kn=3.0, motor_speed_kn=5.0, wave_correct=True
        )
        expected = policy.apply(24.0, 3.0)
        self.assertEqual(expected, 5.0)
        for leg in run["legs"]:
            self.assertAlmostEqual(leg["speed_through_water_knots"], expected)
            self.assertEqual(leg["propulsion_mode"], "sail")
        self.assertTrue(run["independent_validation"]["passed"])

    def test_unsupported_inputs_cannot_be_ignored(self):
        for key, value in (
            ("waypoints", [(48.26, -123.64)]),
            ("wind_speed_pct", 20),
            ("algorithm", "legacy"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                _run({**SETTINGS, key: value})
        with self.assertRaisesRegex(ValueError, "shoreline"):
            _run({**SETTINGS, "coastline_checks": True})


if __name__ == "__main__":
    unittest.main()
