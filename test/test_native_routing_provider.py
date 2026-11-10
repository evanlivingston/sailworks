"""External weather-provider path for the native routing bridge."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        ROOT / "build" / "native-routing" / "python",
    )
)

KNOTS_PER_METRE_PER_SECOND = 3600.0 / 1852.0
COVERAGE = {
    "south": 48.0,
    "north": 50.0,
    "west": -124.0,
    "east": -122.0,
    "start_epoch_s": 1767225600,
    "end_epoch_s": 1767225600 + 6 * 3600,
}


def _bridge():
    if not BUILD_PYTHON.is_dir():
        pytest.fail(
            "native routing module build directory is missing: "
            f"{BUILD_PYTHON}. Configure and build it before running this test."
        )
    sys.path.insert(0, str(BUILD_PYTHON))
    import sailworks_native_routing

    return sailworks_native_routing


def _counted(values):
    calls = []

    def sample(latitude, longitude, epoch_s):
        calls.append((latitude, longitude, epoch_s))
        return values(latitude, longitude, epoch_s)

    return sample, calls


def test_python_weather_values_drive_the_native_solver():
    bridge = _bridge()
    wind, wind_calls = _counted(lambda lat, lon, hour: (270.0, 10.0, True))
    current, current_calls = _counted(lambda lat, lon, hour: (0.0, 0.0, True))

    run = bridge.route_with_weather(
        wind,
        current,
        COVERAGE,
        provider_identity="test-python-weather",
        engine="main",
    )

    assert run["status"] == "complete"
    assert run["accepted"] is True
    assert run["validation"]["passed"] is True
    assert wind_calls and current_calls
    assert run["layers"], "external weather run should still retain layers"

    # A uniform 10 m/s wind FROM 270 degrees is a toward-east wind of
    # 10 m/s, which the engine stores in knots.
    expected_knots = 10.0 * KNOTS_PER_METRE_PER_SECOND
    for leg in run["legs"]:
        assert leg["wind_available"] is True
        assert leg["wind_east_knots"] == pytest.approx(expected_knots, abs=1e-6)
        assert leg["wind_north_knots"] == pytest.approx(0.0, abs=1e-6)
        assert leg["wind_source"]["dataset_identity"] == "test-python-weather"


def test_python_current_values_kept_as_toward_components():
    bridge = _bridge()
    wind, _ = _counted(lambda lat, lon, hour: (270.0, 10.0, True))
    current, _ = _counted(lambda lat, lon, hour: (0.5, -0.25, True))

    run = bridge.route_with_weather(wind, current, COVERAGE)
    assert run["status"] == "complete"
    for leg in run["legs"]:
        assert leg["current_available"] is True
        assert leg["current_east_knots"] == pytest.approx(
            0.5 * KNOTS_PER_METRE_PER_SECOND, abs=1e-6
        )
        assert leg["current_north_knots"] == pytest.approx(
            -0.25 * KNOTS_PER_METRE_PER_SECOND, abs=1e-6
        )


def test_unavailable_python_wind_is_reported_not_assumed_calm():
    bridge = _bridge()
    wind, _ = _counted(lambda lat, lon, hour: (0.0, 0.0, False))
    current, _ = _counted(lambda lat, lon, hour: (0.0, 0.0, True))

    run = bridge.route_with_weather(wind, current, COVERAGE)
    assert run["accepted"] is False
    assert run["status"] != "complete"


def test_python_provider_outside_coverage_is_structured_failure():
    bridge = _bridge()
    wind, _ = _counted(lambda lat, lon, hour: (270.0, 10.0, True))
    current, _ = _counted(lambda lat, lon, hour: (0.0, 0.0, True))
    narrow = dict(COVERAGE, south=49.8, north=50.0, west=-124.0, east=-123.9)

    run = bridge.route_with_weather(wind, current, narrow)
    assert run["accepted"] is False
    assert run["leg_count"] == 0
    assert run["status"] != "complete"


def test_external_weather_cancelled_request_never_completes():
    bridge = _bridge()
    wind, _ = _counted(lambda lat, lon, hour: (270.0, 10.0, True))
    current, _ = _counted(lambda lat, lon, hour: (0.0, 0.0, True))

    run = bridge.route_with_weather(wind, current, COVERAGE, cancelled=True)
    assert run["status"] == "cancelled"
    assert run["accepted"] is False


def test_identity_discloses_external_weather_contract():
    bridge = _bridge()
    capabilities = bridge.identity()["capabilities"]
    assert capabilities["external_weather_provider"] is True
    assert "GIL" in capabilities["external_weather_contract"]
    assert "not wired yet" in capabilities["external_weather_vessel_model"]
