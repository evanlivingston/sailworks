"""Sailworks sampling adapted into the native weather-provider contract."""

from __future__ import annotations

import math
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


class FakePolar:
    """Minimal Sailworks/weatherrouting polar shape: radians, m/s, [twa][tws]."""

    def __init__(self, twa_radians, tws_ms, table):
        self.twa = twa_radians
        self.tws = tws_ms
        self.speed_table = table


def _constant_polar(speed_kn):
    angles_deg = [30.0, 60.0, 90.0, 120.0, 150.0, 180.0]
    tws_ms = [4.0, 8.0, 12.0, 16.0]
    table = [
        [speed_kn / KNOTS_PER_METRE_PER_SECOND for _ in tws_ms] for _ in angles_deg
    ]
    return FakePolar([math.radians(angle) for angle in angles_deg], tws_ms, table)


def test_polar_conversion_preserves_angles_speeds_and_units():
    from sailworks.core.marine.routingnative import polar_rows_from_polar

    rows = polar_rows_from_polar(_constant_polar(3.0))
    assert [row["true_wind_speed_knots"] for row in rows] == pytest.approx(
        [w * KNOTS_PER_METRE_PER_SECOND for w in (4.0, 8.0, 12.0, 16.0)]
    )
    assert [point[0] for point in rows[0]["points"]] == pytest.approx(
        [30.0, 60.0, 90.0, 120.0, 150.0, 180.0]
    )
    assert all(
        point[1] == pytest.approx(3.0) for row in rows for point in row["points"]
    )


def test_supplied_polar_replaces_the_fixture_vessel_model():
    from sailworks.core.marine.routingnative import polar_rows_from_polar

    bridge = _bridge()
    wind = wind_callable_from_sampler(FakeSampler((270.0, 10.0)))
    current = lambda latitude, longitude, epoch_s: (0.0, 0.0, True)  # noqa: E731

    run = bridge.route_with_weather(
        wind,
        current,
        COVERAGE,
        polar=polar_rows_from_polar(_constant_polar(3.0)),
        polar_identity="test-constant-polar",
    )

    assert run["accepted"] is True
    speeds = [leg["speed_through_water_knots"] for leg in run["legs"]]
    assert speeds
    assert max(speeds) <= 3.05


def test_invalid_supplied_polar_is_rejected():
    bridge = _bridge()
    wind = wind_callable_from_sampler(FakeSampler((270.0, 10.0)))
    current = lambda latitude, longitude, epoch_s: (0.0, 0.0, True)  # noqa: E731
    bad = [
        {"true_wind_speed_knots": 8.0, "points": [[30.0, 3.0], [60.0, 4.0]]},
        {"true_wind_speed_knots": 4.0, "points": [[30.0, 2.0], [60.0, 3.0]]},
    ]

    with pytest.raises(RuntimeError):
        bridge.route_with_weather(wind, current, COVERAGE, polar=bad)


class FakeCurrentSelection:
    def __init__(self, status="available", values=(0.4, -0.2)):
        self.status = status
        self.values = values
        self.calls = []

    def sample(self, latitude, longitude, instant):
        self.calls.append((latitude, longitude, instant))
        return self


def test_current_selection_preserves_toward_components():
    from sailworks.core.marine.routingnative import current_callable_from_selection

    selection = FakeCurrentSelection()
    current = current_callable_from_selection(selection)

    assert current(48.2, -123.7, 1767225600) == (0.4, -0.2, True)
    assert selection.calls[0][2] == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_missing_current_is_unavailable_not_calm():
    from sailworks.core.marine.routingnative import current_callable_from_selection

    current = current_callable_from_selection(
        FakeCurrentSelection(status="no_coverage")
    )
    assert current(48.2, -123.7, 1767225600) == (0.0, 0.0, False)


def test_current_selection_drives_a_native_route():
    from sailworks.core.marine.routingnative import current_callable_from_selection

    bridge = _bridge()
    wind = wind_callable_from_sampler(FakeSampler((270.0, 10.0)))
    selection = FakeCurrentSelection(values=(0.5, -0.25))

    run = bridge.route_with_weather(
        wind,
        current_callable_from_selection(selection),
        COVERAGE,
        provider_identity="sailworks-current-selection",
    )

    assert run["accepted"] is True
    assert selection.calls
    for leg in run["legs"]:
        assert leg["current_east_knots"] == pytest.approx(
            0.5 * KNOTS_PER_METRE_PER_SECOND, abs=1e-6
        )
        assert leg["current_north_knots"] == pytest.approx(
            -0.25 * KNOTS_PER_METRE_PER_SECOND, abs=1e-6
        )
        assert leg["current_source"]["dataset_identity"] == (
            "sailworks-current-selection"
        )
