"""Issue #190: the avoid-zone digest must describe the geometry it names."""

import os
import sys
import unittest
from pathlib import Path

import pytest

from sailworks.core.geo.mapshapes import (
    AvoidZone,
    avoid_zones_digest,
    avoid_zones_digest_from_native,
)
from sailworks.core.marine import nativeexecution
from sailworks.core.marine.nativerequest import NativeRequest

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        str(ROOT / "build/native-routing/python"),
    )
)

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
    "use_waves": False,
    "coastline_checks": False,
    "land_safety_margin_nm": 0.0,
    "waypoints": [],
    "roundings": [],
}
VERTICES = (
    (48.50, -123.50),
    (48.50, -123.40),
    (48.60, -123.40),
    (48.60, -123.50),
)
STALE_DIGEST = "0" * 64


def _bridge():
    if not BUILD_PYTHON.is_dir():
        pytest.fail(
            "native routing module build directory is missing: "
            f"{BUILD_PYTHON}. Configure and build it before running this test."
        )
    sys.path.insert(0, str(BUILD_PYTHON))
    import sailworks_native_routing

    return sailworks_native_routing


def _run(settings, wind=None):
    wind = wind or (lambda latitude, longitude, epoch_s: (0.0, 8.0, True))
    return _bridge().route_user_request(
        wind,
        lambda latitude, longitude, epoch_s: (0.0, 0.0, False),
        COVERAGE,
        POLAR,
        settings,
        None,
        "selected-forecast",
        "selected-polar",
        "main",
    )


def _zone(vertices=VERTICES):
    return AvoidZone("avoid-a", "Charted reef", vertices)


class CanonicalFromNativePayloadTest(unittest.TestCase):
    """The helper the bridge calls must reproduce the frozen digest."""

    def test_payload_round_trip_matches_the_frozen_digest(self):
        zone = _zone()
        self.assertEqual(
            avoid_zones_digest_from_native([zone.to_native()]),
            avoid_zones_digest((zone,)),
        )

    def test_ring_winding_and_rotation_do_not_change_the_digest(self):
        canonical = avoid_zones_digest((_zone(),))
        for vertices in (
            tuple(reversed(VERTICES)),
            VERTICES[2:] + VERTICES[:2],
        ):
            payload = [_zone(vertices).to_native()]
            self.assertEqual(avoid_zones_digest_from_native(payload), canonical)

    def test_mutated_geometry_changes_the_recomputed_digest(self):
        moved = list(VERTICES)
        moved[0] = (48.51, -123.50)
        self.assertNotEqual(
            avoid_zones_digest_from_native([_zone(tuple(moved)).to_native()]),
            avoid_zones_digest((_zone(),)),
        )

    def test_empty_payload_stays_inert(self):
        self.assertEqual(avoid_zones_digest_from_native([]), "")
        self.assertEqual(avoid_zones_digest_from_native(()), "")


class BridgeZoneDigestTest(unittest.TestCase):
    """A direct bridge call cannot attest a digest for other geometry."""

    @staticmethod
    def _settings(zone, digest):
        return {
            **SETTINGS,
            "avoid_zones": [zone.to_native()],
            "avoid_zones_digest": digest,
        }

    def test_matching_digest_and_vertices_are_accepted(self):
        zone = _zone()
        run = _run(self._settings(zone, avoid_zones_digest((zone,))))
        self.assertTrue(run["accepted"], run["message"])
        self.assertEqual(run["avoid_zones_digest"], avoid_zones_digest((zone,)))
        self.assertTrue(run["avoid_zones_active"])

    def test_mutated_geometry_with_stale_digest_is_rejected_before_search(self):
        zone = _zone()
        digest = avoid_zones_digest((zone,))
        calls = []

        def wind(latitude, longitude, epoch_s):
            calls.append((latitude, longitude, epoch_s))
            return (0.0, 8.0, True)

        moved = list(zone.to_native()["vertices"])
        moved[0] = [48.51, -123.50]
        mutated = {**zone.to_native(), "vertices": moved}
        settings = {
            **SETTINGS,
            "avoid_zones": [mutated],
            "avoid_zones_digest": digest,
        }
        with self.assertRaisesRegex(ValueError, "zone digest mismatch"):
            _run(settings, wind)
        self.assertEqual(calls, [])


class WorkerZoneDigestTest(unittest.TestCase):
    """The worker re-derives the digest before it trusts the frozen job."""

    @staticmethod
    def _bare(**fields):
        job = object.__new__(NativeRequest)
        values = {
            "version": 1,
            "avoid_zones": (),
            "avoid_zones_digest": "",
            "provider_descriptors": (),
            "coastline_checks": False,
            "current_field_ids": (),
            "use_currents": False,
            "wind_speed_pct": 0.0,
        }
        values.update(fields)
        for key, value in values.items():
            object.__setattr__(job, key, value)
        return job

    def test_mutated_zone_with_a_stale_digest_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "zone digest mismatch"):
            nativeexecution.run_native_request(
                self._bare(avoid_zones=(_zone(),), avoid_zones_digest=STALE_DIGEST)
            )

    def test_consistent_zone_passes_the_gate(self):
        zone = _zone()
        job = self._bare(
            avoid_zones=(zone,),
            avoid_zones_digest=avoid_zones_digest((zone,)),
            coastline_checks=True,
        )
        # The digest gate accepts it and the next unchanged check fires.
        with self.assertRaisesRegex(ValueError, "shoreline"):
            nativeexecution.run_native_request(job)

    def test_no_zone_request_reaches_the_unchanged_checks(self):
        job = self._bare(wind_speed_pct=-100.0)
        with self.assertRaisesRegex(ValueError, "Wind intensity"):
            nativeexecution.run_native_request(job)
