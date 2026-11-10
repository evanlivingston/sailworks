"""Sailworks sampling adapted into the native weather-provider contract."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sailworks.core.marine.routingnative import wind_callable_from_sampler

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


class FakeSampler:
    """Minimal stand-in exposing the established ``wind_at`` representation."""

    def __init__(self, value=None):
        self.value = value
        self.calls: list[tuple[datetime, float, float]] = []

    def wind_at(self, instant, latitude, longitude):
        self.calls.append((instant, latitude, longitude))
        return self.value


def test_sampler_callable_preserves_from_degrees_and_metres_per_second():
    sampler = FakeSampler((270.0, 10.0))
    wind = wind_callable_from_sampler(sampler)

    assert wind(48.2, -123.7, 1767225600) == (270.0, 10.0, True)
    instant, latitude, longitude = sampler.calls[0]
    assert instant == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert (latitude, longitude) == (48.2, -123.7)


def test_missing_sampler_value_is_unavailable_not_calm():
    wind = wind_callable_from_sampler(FakeSampler(None))
    assert wind(48.2, -123.7, 1767225600) == (0.0, 0.0, False)


def test_sampled_wind_drives_a_native_route():
    bridge = _bridge()
    sampler = FakeSampler((0.0, 8.0))  # wind FROM north, 8 m/s toward south
    current = lambda latitude, longitude, epoch_s: (0.0, 0.0, True)  # noqa: E731

    run = bridge.route_with_weather(
        wind_callable_from_sampler(sampler),
        current,
        COVERAGE,
        provider_identity="sailworks-sampler-test",
    )

    assert run["status"].startswith("complete")
    assert run["accepted"] is True
    assert sampler.calls
    expected_north = -8.0 * KNOTS_PER_METRE_PER_SECOND
    for leg in run["legs"]:
        assert leg["wind_available"] is True
        assert leg["wind_east_knots"] == pytest.approx(0.0, abs=1e-6)
        assert leg["wind_north_knots"] == pytest.approx(expected_north, abs=1e-6)
        assert leg["wind_source"]["dataset_identity"] == "sailworks-sampler-test"
