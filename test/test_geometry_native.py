"""Native routing geometry matches the Python reference and the route engine."""

import math
import os
import random
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest
import weatherrouting
from weatherrouting import utils as routing_utils

from sailworks import geometry
from sailworks.core import Core
from sailworks.core.gribmanager import GribManager
from sailworks.core.routingjob import RouteRequest, calculate_route

requires_native = pytest.mark.skipif(
    not geometry.NATIVE_AVAILABLE,
    reason="native routing geometry extension is not built",
)


def reference(name, *args, **kwargs):
    return geometry.REFERENCE[name](*args, **kwargs)


def test_native_extension_is_expected_in_native_validation():
    if os.environ.get("SAILWORKS_EXPECT_NATIVE_GEOMETRY") != "1":
        pytest.skip("set SAILWORKS_EXPECT_NATIVE_GEOMETRY=1 to require the extension")
    assert geometry.NATIVE_AVAILABLE


@pytest.fixture(autouse=True)
def restore_backend():
    previous = geometry.active_backend()
    yield
    geometry.set_backend(previous)


@requires_native
def test_backend_installs_reference_and_native_functions():
    assert geometry.set_backend("python") == "python"
    assert geometry.active_backend() == "python"
    for name in geometry.REFERENCE:
        assert getattr(routing_utils, name) is geometry.REFERENCE[name]

    assert geometry.set_backend("native") == "native"
    assert geometry.active_backend() == "native"
    for name in geometry.REFERENCE:
        assert getattr(routing_utils, name) is getattr(geometry._geometry_native, name)


@requires_native
def test_random_points_match_reference():
    rng = random.Random(20261107)
    worst_distance = 0.0
    worst_azimuth = 0.0
    for _ in range(2_000):
        a = (rng.uniform(-80.0, 80.0), rng.uniform(-180.0, 180.0))
        b = (rng.uniform(-80.0, 80.0), rng.uniform(-180.0, 180.0))
        for unit in ("nm", "km"):
            actual = geometry._geometry_native.point_distance(*a, *b, unit=unit)
            expected = reference("point_distance", *a, *b, unit=unit)
            worst_distance = max(worst_distance, abs(actual - expected))

        for name in ("ortodromic", "lossodromic"):
            actual = getattr(geometry._geometry_native, name)(*a, *b)
            expected = reference(name, *a, *b)
            worst_distance = max(worst_distance, abs(actual[0] - expected[0]))
            # The inverse azimuth is ill-conditioned only near antipodes; every
            # route-scale pair below is checked without that exception.
            if actual[0] < 2_000:
                delta = abs(actual[1] - expected[1])
                worst_azimuth = max(worst_azimuth, min(delta, abs(delta - 2 * math.pi)))

    assert worst_distance < 1e-9
    assert worst_azimuth < 1e-9


@requires_native
def test_local_points_match_reference_including_azimuth():
    rng = random.Random(112)
    for _ in range(2_000):
        a = (rng.uniform(24.0, 50.0), rng.uniform(-130.0, -60.0))
        b = (a[0] + rng.uniform(-1.0, 1.0), a[1] + rng.uniform(-1.0, 1.0))
        for name in ("ortodromic", "lossodromic"):
            actual = getattr(geometry._geometry_native, name)(*a, *b)
            expected = reference(name, *a, *b)
            assert actual[0] == pytest.approx(expected[0], abs=1e-9)
            assert actual[1] == pytest.approx(expected[1], abs=1e-9)


def test_pole_dateline_and_coincident_points_match_reference():
    pairs = (
        ((0.0, 0.0), (0.0, 0.0)),
        ((90.0, 0.0), (0.0, 0.0)),
        ((0.0, 180.0), (0.0, -180.0)),
        ((0.0, 179.999), (0.0, -179.999)),
        ((45.0, 0.0), (-45.0, 180.0)),
    )
    for a, b in pairs:
        for unit in ("nm", "km"):
            assert geometry._geometry_native.point_distance(
                *a, *b, unit=unit
            ) == pytest.approx(reference("point_distance", *a, *b, unit=unit), abs=1e-9)
        for name in ("ortodromic", "lossodromic"):
            actual = getattr(geometry._geometry_native, name)(*a, *b)
            expected = reference(name, *a, *b)
            assert actual[0] == pytest.approx(expected[0], abs=1e-9)
            # Azimuth is undefined/ill-conditioned for coincident or antipodal
            # points; compare it only for the well-conditioned edge cases.
            if actual[0] > 1e-9 and actual[0] < 20_000:
                assert actual[1] == pytest.approx(expected[1], abs=1e-9)


@requires_native
def test_direct_points_match_reference_and_normalize_longitude():
    rng = random.Random(317)
    for _ in range(2_000):
        a = (rng.uniform(-80.0, 80.0), rng.uniform(-180.0, 180.0))
        distance = rng.uniform(0.0, 500.0)
        heading = rng.uniform(-math.pi, math.pi)
        for unit in ("nm", "km"):
            actual = geometry._geometry_native.routage_point_distance(
                *a, distance, heading, unit=unit
            )
            expected = reference(
                "routage_point_distance", *a, distance, heading, unit=unit
            )
            assert actual[0] == pytest.approx(expected[0], abs=1e-10)
            assert actual[1] == pytest.approx(expected[1], abs=1e-10)
            assert -180.0 <= actual[1] <= 180.0

    # The reference wraps longitudes through latlon.Longitude.
    actual = geometry._geometry_native.routage_point_distance(0.0, 190.0, 0.0, 0.0)
    assert actual == pytest.approx(
        reference("routage_point_distance", 0.0, 190.0, 0.0, 0.0)
    )


class ConstantWind:
    model_name = "GFS"
    last_forecast = 384
    name = "constant"
    start_time = datetime(2026, 10, 1)

    def get_wind_at(self, instant, lat, lon):
        return 270, 14


def route_workload(backend):
    geometry.set_backend(backend)
    core = Core.__new__(Core)
    core.grib_manager = GribManager.__new__(GribManager)
    core.grib_manager.gribs = [ConstantWind()]
    request = RouteRequest(
        "native-equivalence",
        (0, 0),
        (0, 0.15),
        datetime(2026, 10, 1, 12),
        str(Path(__file__).parents[1] / "sailworks/data/polars/first317.pol"),
        weatherrouting.list_routing_algorithms()[0]["class"],
        False,
    )
    return calculate_route(
        core,
        request,
        core.grib_manager.gribs,
        (),
        Event(),
        lambda *args: None,
    )


@requires_native
def test_route_matches_python_backend():
    expected = route_workload("python")
    actual = route_workload("native")

    assert len(actual.points) == len(expected.points)
    for native_point, python_point in zip(actual.points, expected.points):
        assert native_point[2] == python_point[2]
        assert native_point[:2] == pytest.approx(python_point[:2], abs=1e-9)
        assert native_point[3:] == pytest.approx(python_point[3:], abs=1e-9)
    assert actual.provenance == expected.provenance
