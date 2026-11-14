"""Real-request bridge checks without importing a Sailworks or Qt process."""

import os
import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from uuid import uuid4

import pytest

from sailworks.core.geo.mapshapes import (
    AvoidZone,
    avoid_zones_digest,
    avoid_zones_digest_from_native,
)

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
    "roundings": [],
}


def _wind(latitude, longitude, epoch_s):
    return (0.0, 8.0, True)


def _missing_current(latitude, longitude, epoch_s):
    return (0.0, 0.0, False)


def _run(
    settings,
    wind=_wind,
    land=None,
    progress=None,
    *,
    waves=None,
    wave_coverage=None,
    cancel_event=None,
):
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
        progress=progress,
        cancel_event=cancel_event,
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

    def test_selected_request_forwards_engine_progress_to_the_caller(self):
        updates = []
        run = _run(SETTINGS, progress=updates.append)

        self.assertTrue(run["accepted"], run["message"])
        stages = [update["stage"] for update in updates]
        self.assertEqual(stages[0], "preflight")
        self.assertIn("validation", stages)
        self.assertEqual(stages[-1], "complete")
        self.assertTrue(all(update["effort_percent"] > 0 for update in updates))

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

    # Frozen zone contract shared with issue #190: an ordered list of
    # {id, name, vertices: [[latitude, longitude], ...]} plus the canonical
    # SHA256 over that same payload. The native boundary derives the point
    # count from the active zones; there is no separate polygon count field.
    _AVOID_VERTICES = [
        [48.50, -123.50],
        [48.50, -123.40],
        [48.60, -123.40],
        [48.60, -123.50],
    ]

    @classmethod
    def _avoid_zone(cls, zone_id="avoid-a", name="Charted reef", vertices=None):
        return AvoidZone(
            zone_id,
            name,
            cls._AVOID_VERTICES if vertices is None else vertices,
        )

    @staticmethod
    def _raw_zone_settings(zones, digest="0" * 64):
        return {**SETTINGS, "avoid_zones": zones, "avoid_zones_digest": digest}

    def test_avoid_zones_install_native_provider_without_python_land(self):
        """Frozen zones are native polygons, not a Python land adapter.

        ``coastline_checks`` is off and ``land`` is None, so a run that still
        reports the zone provider active proves the zones crossed the boundary
        as the frozen payload and did not require a worker-local adapter.
        """

        class Land:
            def __init__(self):
                self.calls = 0

            def point_forbidden(self, *args):
                self.calls += 1
                return False

            def segment_forbidden(self, *args):
                self.calls += 1
                return False

            def distance_nm(self, *args):
                self.calls += 1
                return 5.0

        zone = self._avoid_zone()
        settings = {
            **SETTINGS,
            "avoid_zones": [zone.to_native()],
            "avoid_zones_digest": avoid_zones_digest((zone,)),
        }
        self.assertEqual(len(settings["avoid_zones_digest"]), 64)
        int(settings["avoid_zones_digest"], 16)

        detached = Land()
        run = _run(settings, land=detached)
        self.assertTrue(run["accepted"], run["message"])
        self.assertTrue(run["avoid_zones_active"])
        self.assertEqual(run["avoid_zones_digest"], settings["avoid_zones_digest"])
        self.assertEqual(
            run["avoid_zones"],
            [{"id": "avoid-a", "name": "Charted reef", "vertex_count": 4}],
        )
        self.assertEqual(
            detached.calls, 0, "coastline adapter must not be consulted for zones"
        )

        # The same payload installs the provider with no adapter at all.
        without_land = _run(settings)
        self.assertTrue(without_land["accepted"], without_land["message"])
        self.assertTrue(without_land["avoid_zones_active"])
        self.assertEqual(
            without_land["avoid_zones_digest"], settings["avoid_zones_digest"]
        )

        # No zones keeps the open-water baseline payload untouched.
        inert = _run(SETTINGS)
        self.assertTrue(inert["accepted"], inert["message"])
        self.assertNotIn("avoid_zones", inert)
        self.assertNotIn("avoid_zones_digest", inert)

    def test_avoid_zone_digest_is_canonical_over_frozen_geometry(self):
        zone = self._avoid_zone()
        reversed_ring = self._avoid_zone(vertices=list(reversed(self._AVOID_VERTICES)))
        rotated_ring = self._avoid_zone(
            vertices=self._AVOID_VERTICES[2:] + self._AVOID_VERTICES[:2]
        )
        self.assertEqual(zone.vertices, reversed_ring.vertices)
        self.assertEqual(zone.vertices, rotated_ring.vertices)

        digest = avoid_zones_digest((zone,))
        self.assertEqual(digest, avoid_zones_digest((reversed_ring,)))
        self.assertEqual(digest, avoid_zones_digest((rotated_ring,)))
        self.assertEqual(len(digest), 64)
        int(digest, 16)

        # Name is part of the identity a reviewer replays, id order is part of
        # the frozen input, so both change the digest.
        renamed = self._avoid_zone(name="Renamed reef")
        second = self._avoid_zone("avoid-b", "Second reef")
        self.assertNotEqual(digest, avoid_zones_digest((renamed,)))
        self.assertNotEqual(digest, avoid_zones_digest((second, zone)))
        self.assertNotEqual(digest, avoid_zones_digest((zone, second)))

        run = _run(
            {
                **SETTINGS,
                "avoid_zones": [item.to_native() for item in (zone, second)],
                "avoid_zones_digest": avoid_zones_digest((zone, second)),
            }
        )
        self.assertTrue(run["accepted"], run["message"])
        self.assertTrue(run["avoid_zones_active"])
        self.assertEqual(run["avoid_zones_digest"], avoid_zones_digest((zone, second)))
        self.assertEqual(
            [item["id"] for item in run["avoid_zones"]], ["avoid-a", "avoid-b"]
        )

        # Replay of the same frozen payload is stable at the boundary.
        replay = _run(
            {
                **SETTINGS,
                "avoid_zones": [item.to_native() for item in (zone, second)],
                "avoid_zones_digest": avoid_zones_digest((zone, second)),
            }
        )
        self.assertEqual(replay["avoid_zones_digest"], run["avoid_zones_digest"])
        self.assertEqual(replay["avoid_zones"], run["avoid_zones"])

    def test_avoid_zones_reject_endpoints_and_invalid_geometry(self):
        harbour = self._avoid_zone(
            "harbour",
            "Harbour mouth",
            [[48.20, -123.70], [48.20, -123.60], [48.30, -123.60], [48.30, -123.70]],
        )
        with self.assertRaisesRegex(ValueError, "contains a required route point"):
            _run(
                {
                    **SETTINGS,
                    "avoid_zones": [harbour.to_native()],
                    "avoid_zones_digest": avoid_zones_digest((harbour,)),
                }
            )

        bowtie = [
            [48.50, -123.50],
            [48.60, -123.40],
            [48.50, -123.40],
            [48.60, -123.50],
        ]
        # The digest is checked before the bridge inspects geometry, so a
        # malformed ring must carry the digest its raw payload actually
        # commits to. Only then does the geometry error surface.
        cross = [{"id": "cross", "name": "", "vertices": bowtie}]
        with self.assertRaisesRegex(ValueError, "self-intersecting"):
            _run(
                self._raw_zone_settings(
                    cross, digest=avoid_zones_digest_from_native(cross)
                )
            )

        thin = [
            {"id": "thin", "name": "", "vertices": [[48.50, -123.50], [48.60, -123.40]]}
        ]
        with self.assertRaisesRegex(ValueError, "at least three distinct vertices"):
            _run(
                self._raw_zone_settings(
                    thin, digest=avoid_zones_digest_from_native(thin)
                )
            )

        twice = [
            {"id": "twice", "name": "", "vertices": self._AVOID_VERTICES},
            {"id": "twice", "name": "", "vertices": self._AVOID_VERTICES},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate avoid zone id"):
            _run(
                self._raw_zone_settings(
                    twice, digest=avoid_zones_digest_from_native(twice)
                )
            )

        # The digest is a fixed 64-character lowercase SHA256 spelling.
        with self.assertRaisesRegex(ValueError, "64 lowercase hex"):
            _run(
                self._raw_zone_settings(
                    [{"id": "short", "name": "", "vertices": self._AVOID_VERTICES}],
                    digest="0" * 63,
                )
            )
        with self.assertRaisesRegex(ValueError, "64 lowercase hex"):
            _run(
                self._raw_zone_settings(
                    [{"id": "upper", "name": "", "vertices": self._AVOID_VERTICES}],
                    digest="A" * 64,
                )
            )
        with self.assertRaisesRegex(ValueError, "requires a non-empty avoid_zones"):
            _run({**SETTINGS, "avoid_zones_digest": "0" * 64})
        with self.assertRaisesRegex(ValueError, "must be \\(latitude, longitude\\)"):
            _run(
                self._raw_zone_settings(
                    [{"id": "point", "name": "", "vertices": [[48.50]]}]
                )
            )

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


class NativeUserBridgeCancellationTest(unittest.TestCase):
    def test_cancel_event_stops_the_search_cooperatively(self):
        cancel_event = threading.Event()
        updates = []

        def progress(update):
            updates.append(update)
            cancel_event.set()

        run = _run(SETTINGS, progress=progress, cancel_event=cancel_event)

        self.assertTrue(updates, "engine never reported progress")
        self.assertEqual(run["status"], "cancelled")
        self.assertFalse(run["accepted"])
        self.assertFalse(run["validation"]["passed"])
        self.assertFalse(run["independent_validation"]["passed"])
        self.assertEqual(run["legs"], [])

    def test_unset_cancel_event_does_not_change_the_solve(self):
        run = _run(SETTINGS, cancel_event=threading.Event())

        self.assertTrue(run["accepted"], run["message"])
        self.assertTrue(run["validation"]["passed"])
