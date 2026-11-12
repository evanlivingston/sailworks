"""Real-request bridge checks without importing a Sailworks or Qt process."""

import os
import importlib.util
import sys
import unittest
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        str(ROOT / "build/native-routing/python"),
    )
)


def _bridge():
    if not BUILD_PYTHON.is_dir():
        pytest.fail(
            "native routing module build directory is missing: "
            f"{BUILD_PYTHON}. Configure and build it before running this test."
        )
    sys.path.insert(0, str(BUILD_PYTHON))
    import sailworks_native_routing

    return sailworks_native_routing


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
WAVE_COVERAGE = {
    **COVERAGE,
    "dataset_identity": "GFSWAVE:2026-01-01T00:00Z:abc123",
}
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
    "use_waves": False,
    "coastline_checks": False,
    "land_safety_margin_nm": 0.0,
    "waypoints": [],
}


def _wind(latitude, longitude, epoch_s):
    return (0.0, 8.0, True)


def _missing_current(latitude, longitude, epoch_s):
    return (0.0, 0.0, False)


def _run(settings, wind=_wind, land=None, *, waves=None, wave_coverage=None):
    return _bridge().route_user_request(
        wind,
        _missing_current,
        COVERAGE,
        POLAR,
        settings,
        land,
        "selected-forecast",
        "selected-polar",
        "main",
        waves=waves,
        wave_coverage=wave_coverage,
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

    def test_real_wave_data_is_sampled_and_its_provenance_is_explicit(self):
        def waves(latitude, longitude, epoch_s):
            return (1.4, 8.0, 250.0, True, "GFSWAVE:run", "GFSWAVE")

        run = _run(
            {**SETTINGS, "use_waves": True},
            waves=waves,
            wave_coverage=WAVE_COVERAGE,
        )
        self.assertTrue(run["accepted"], run["message"])
        provenance = run["wave_provenance"]
        self.assertTrue(provenance["uses_real_wave_heights"])
        self.assertFalse(provenance["uses_wind_proxy"])
        self.assertEqual(provenance["mode"], "real_waves")
        self.assertEqual(provenance["dataset_identity"], "GFSWAVE:run")
        self.assertEqual(provenance["units"]["height"], "m")
        self.assertEqual(provenance["units"]["period"], "s")
        for leg in run["legs"]:
            self.assertTrue(leg["waves_available"])
            self.assertAlmostEqual(leg["wave_height_metres"], 1.4)
            self.assertAlmostEqual(leg["wave_period_seconds"], 8.0)
            self.assertAlmostEqual(leg["wave_direction_from_degrees"], 250.0)
            self.assertEqual(leg["wave_source"]["dataset_identity"], "GFSWAVE:run")
        from sailworks.core.marine.routingnative import to_routing_result

        record = to_routing_result(
            run,
            calculation_id=str(uuid4()),
            generation=1,
            sequence=0,
            created_epoch_s=START,
            identity=_bridge().identity(),
        )
        self.assertEqual(record.capabilities.features["waves"], "sampled")
        height = record.legs[0].environment["waves_height_m"]
        self.assertEqual(height.availability, "reported")
        self.assertAlmostEqual(height.value, 1.4)
        self.assertEqual(record.legs[0].environment["waves_period_s"].unit, "s")

    def test_missing_wave_coverage_never_falls_back_to_the_wind_proxy(self):
        def waves(latitude, longitude, epoch_s):
            return (1.4, 8.0, 250.0, True, "GFSWAVE:run", "GFSWAVE")

        run = _run(
            {**SETTINGS, "use_waves": True, "wave_correct": True},
            waves=waves,
            wave_coverage={**WAVE_COVERAGE, "end_epoch_s": START + 600},
        )
        self.assertFalse(run["accepted"])
        self.assertEqual(run["status"], "wave_data_required")
        actions = run["wave_provenance"]["preflight"]["required_actions"]
        self.assertIn("confirm_routing_without_wave_data", actions)
        self.assertTrue(run["wave_provenance"]["uses_wind_proxy"])
        self.assertEqual(run["legs"], [])

    def test_unsupported_inputs_cannot_be_ignored(self):
        for key, value in (
            ("wind_speed_pct", 20),
            ("algorithm", "legacy"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                _run({**SETTINGS, key: value})
        with self.assertRaisesRegex(ValueError, "shoreline"):
            _run({**SETTINGS, "coastline_checks": True})

    def test_selected_shoreline_governs_search_and_independent_replay(self):
        class Land:
            identity = "selected-test-shoreline"

            def __init__(self):
                self.segments = 0
                self.distances = 0

            def point_forbidden(self, latitude, longitude):
                return False

            def segment_forbidden(self, *args):
                self.segments += 1
                # Reject the leg that crosses the query rectangle interior.
                return True

            def distance_nm(self, latitude, longitude):
                self.distances += 1
                return 5.0

        land = Land()
        blocked = _run(
            {**SETTINGS, "coastline_checks": True, "land_safety_margin_nm": 0.5},
            land=land,
        )
        self.assertFalse(blocked["accepted"])
        self.assertGreater(land.segments, 0)
        self.assertGreater(blocked["diagnostics"]["land_checks"], 0)

        class Open(Land):
            def segment_forbidden(self, *args):
                self.segments += 1
                return False

        open_land = Open()
        accepted = _run(
            {**SETTINGS, "coastline_checks": True, "land_safety_margin_nm": 0.5},
            land=open_land,
        )
        self.assertTrue(accepted["accepted"], accepted["message"])
        self.assertGreater(open_land.segments, 0)
        self.assertTrue(accepted["independent_validation"]["passed"])

    def test_clearance_without_coastline_checks_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "coastline checks"):
            _run({**SETTINGS, "land_safety_margin_nm": 1.0})

    def test_waypoint_chain_uses_selected_points_in_order(self):
        waypoint = (48.255, -123.60)
        run = _run({**SETTINGS, "waypoints": [waypoint, [48.29, -123.61]]})
        self.assertTrue(run["accepted"], run["message"])
        self.assertEqual(len(run["segments"]), 2)
        self.assertTrue(all(item["accepted"] for item in run["segments"]))
        self.assertTrue(run["segments"][-1]["independent_validation_passed"])
        legs = run["legs"]
        self.assertGreaterEqual(len(legs), 2)
        for previous, following in zip(legs, legs[1:]):
            self.assertEqual(previous["end"], following["start"])
            self.assertGreaterEqual(following["start_time"], previous["end_time"])
        self.assertLess(abs(legs[0]["end"]["latitude"] - waypoint[0]), 1 / 60)
        self.assertLess(abs(legs[0]["end"]["longitude"] - waypoint[1]), 1 / 60)

    def test_failed_intermediate_leg_stays_diagnostic(self):
        short = {**COVERAGE, "end_epoch_s": START + 3600}
        run = _bridge().route_user_request(
            _wind,
            _missing_current,
            short,
            POLAR,
            {
                **SETTINGS,
                "waypoints": [[48.255, -123.60], [49.9, -122.01]],
                "destination": (49.9, -122.01),
            },
            None,
            "selected-forecast",
            "selected-polar",
            "main",
        )
        self.assertFalse(run["accepted"])
        self.assertEqual(len(run["segments"]), 2)
        self.assertTrue(run["segments"][0]["accepted"])
        self.assertFalse(run["segments"][1]["accepted"])
        self.assertIn("leg 2", run["message"])
        self.assertTrue(run["legs"], "first-leg geometry stays inspectable")
        self.assertFalse(run["validation"]["passed"])
        from sailworks.core.marine.routingnative import to_routing_result

        record = to_routing_result(
            run,
            calculation_id=str(uuid4()),
            generation=1,
            sequence=0,
            created_epoch_s=START,
            identity=_bridge().identity(),
        )
        # A failed chain has no delivered whole route to validate: the record
        # must not display a passed (or failed) validation verdict.
        self.assertEqual(record.outcome.validation.state, "unknown")
        self.assertFalse(record.outcome.validation.independent)
        self.assertFalse(record.outcome.delivered)


if __name__ == "__main__":
    unittest.main()
