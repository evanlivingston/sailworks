"""Differential test: native route values come from real Sailworks sampling."""

from __future__ import annotations

import copy
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR))

from test_native_routing_fixture import (  # noqa: E402
    START,
    _forecast_inputs,
    _load,
    _write_forecast,
)

from sailworks.core.gribsampling import WeatherSampler  # noqa: E402
from sailworks.core.marine.routingnative import (  # noqa: E402
    wind_callable_from_sampler,
)

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
    "start_epoch_s": int(START.timestamp()),
    "end_epoch_s": int(START.timestamp()) + 6 * 3600,
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


def _current(latitude, longitude, epoch_s):
    return (0.0, 0.0, True)


def _sampled_components(sampler, leg):
    instant = datetime.fromtimestamp(leg["start_time"], tz=timezone.utc)
    value = sampler.wind_at(
        instant, leg["start"]["latitude"], leg["start"]["longitude"]
    )
    assert value is not None, "sampler lost coverage of a delivered leg"
    from_degrees, speed_ms = value
    radians = math.radians(from_degrees)
    return (
        -math.sin(radians) * speed_ms * KNOTS_PER_METRE_PER_SECOND,
        -math.cos(radians) * speed_ms * KNOTS_PER_METRE_PER_SECOND,
    )


def test_delivered_legs_carry_exactly_the_sampled_wind(tmp_path):
    bridge = _bridge()
    with _forecast_inputs(tmp_path) as source:
        sampler = WeatherSampler([source])
        run = bridge.route_with_weather(
            wind_callable_from_sampler(sampler),
            _current,
            COVERAGE,
            provider_identity="frozen-sampler",
        )
        assert run["accepted"] is True
        assert run["legs"]
        for leg in run["legs"]:
            east, north = _sampled_components(sampler, leg)
            assert leg["wind_east_knots"] == pytest.approx(east, abs=1e-9)
            assert leg["wind_north_knots"] == pytest.approx(north, abs=1e-9)
            assert leg["wind_source"]["dataset_identity"] == "frozen-sampler"


def test_scaled_forecast_changes_the_native_route(tmp_path):
    """A scaled copy of the frozen field must reach the solver unchanged.

    The built-in synthetic provider uses the unscaled formulas, so matching the
    scaled sampler values proves the native route consumed real sampled data
    rather than its internal fixture.
    """
    bridge = _bridge()
    contract_data = _load("contract.json")
    forecast_data = copy.deepcopy(_load("forecast.json"))
    for band in forecast_data["bands"]:
        if band["element"] not in ("UGRD", "VGRD"):
            continue
        for frame in band["frames"]:
            frame["values"] = [
                [0.5 * value for value in row] for row in frame["values"]
            ]

    path = _write_forecast(tmp_path / "scaled.tif", forecast_data, contract_data)
    from sailworks.core.grib import Grib

    source = Grib(str(path), "scaled", "7", [], START, 6, "synthetic")
    try:
        sampler = WeatherSampler([source])
        run = bridge.route_with_weather(
            wind_callable_from_sampler(sampler),
            _current,
            COVERAGE,
            provider_identity="scaled-sampler",
        )
        assert run["accepted"] is True
        for leg in run["legs"]:
            east, north = _sampled_components(sampler, leg)
            assert leg["wind_east_knots"] == pytest.approx(east, abs=1e-9)
            assert leg["wind_north_knots"] == pytest.approx(north, abs=1e-9)

        # The unscaled synthetic field is materially different from the scaled one.
        unscaled_east = 6.0
        assert abs(run["legs"][0]["wind_east_knots"]) < (
            unscaled_east * KNOTS_PER_METRE_PER_SECOND * 0.75
        )
    finally:
        source.close()
