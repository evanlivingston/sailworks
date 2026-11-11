"""Real GFS differential for the native routing provider path.

Artifact: ``test/fixtures/native_routing/gfs_20261026_12z_f000_f120_full.grib2``
(17x21 GFS 0.25 degree subset, cycle 2026-10-26 12Z, every valid hour 0..120
step 3, cropped from raw NOAA S3 GRIB2 messages), bound by
``...manifest.json``.

The bridge's frozen request (48.2N -123.7, destination 48.4N -123.5,
departing 2026-01-01T00:00Z) predates this artifact. This module exercises the
approved narrow override on ``route_with_weather`` (``start``,
``destination``, ``departure_epoch_s``) so the real GRIB wind and a strong
``CurrentField`` selection drive a route inside the artifact's own space and
time domain, then compares every bridge leg against direct samples. Only the
request geometry is relocated: the fixture polar, constraints, engine options
and all sampling/routing policy are unchanged, and the frozen defaults are
asserted intact. No threshold or FPS gate: timings are descriptive only.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time as time_module
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = TEST_DIR / "fixtures" / "native_routing"
GRIB_NAME = "gfs_20261026_12z_f000_f120_full.grib2"
MANIFEST_NAME = "gfs_20261026_12z_f000_f120_full.manifest.json"
sys.path.insert(0, str(TEST_DIR))

from sailworks.core.grib import Grib  # noqa: E402
from sailworks.core.gribsampling import WeatherSampler  # noqa: E402
from sailworks.core.gribsources.noaagfs import GFS_FILTER_URL  # noqa: E402
from sailworks.core.marine.fields import CurrentField, FieldSelection  # noqa: E402
from sailworks.core.marine.routingnative import (  # noqa: E402
    current_callable_from_selection,
    wind_callable_from_sampler,
)

BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        ROOT / "build" / "native-routing" / "python",
    )
)
KNOTS_PER_METRE_PER_SECOND = 3600.0 / 1852.0
CYCLE = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
CYCLE_EPOCH_S = int(CYCLE.timestamp())
ARTIFACT_BOUNDS = {
    "west": -126.125,
    "south": 41.875,
    "east": -121.875,
    "north": 47.125,
}
# Cell centers on the 0.25 degree subset, chosen inside the 17x21 window.
# The destination sits down-current of the start (bearing ~126 vs current
# ~118) and ~25.6 nm away, beyond the 10 nm direct-completion radius, so the
# route is a real optimisation rather than an immediate short-circuit.
CELL_CENTERS = ((46.0, -124.5), (45.75, -124.0))
ENVELOPE_CORNER = (41.875, -126.125)

# Both endpoints are cell centers, so every leg endpoint and intermediate
# point has a real GFS wind sample on the 17x21 grid.
ROUTE_START = CELL_CENTERS[0]
ROUTE_DESTINATION = CELL_CENTERS[1]
ARTIFACT_REQUEST_COVERAGE = {
    "south": 42.0,
    "north": 47.0,
    "west": -126.0,
    "east": -122.0,
    "start_epoch_s": CYCLE_EPOCH_S,
    "end_epoch_s": CYCLE_EPOCH_S + 120 * 3600,
}
# Declared coverage reaches before the cycle so an early override reaches the
# sampler instead of failing an engine-side coverage pre-check.
EARLY_REQUEST_COVERAGE = {
    **ARTIFACT_REQUEST_COVERAGE,
    "start_epoch_s": CYCLE_EPOCH_S - 6 * 3600,
}

# synthetic.cpp fixtureRequest() defaults. Kept literal so the frozen
# behaviour is asserted rather than assumed.
FROZEN_REQUEST_START = (48.2, -123.7)
FROZEN_REQUEST_DESTINATION = (48.4, -123.5)
FROZEN_REQUEST_DEPARTURE = datetime(2026, 1, 1, tzinfo=timezone.utc)
# The frozen start is inside this box while remaining outside the artifact.
FROZEN_REQUEST_COVERAGE = {
    "south": 48.0,
    "north": 48.7,
    "west": -124.7,
    "east": -123.4,
    "start_epoch_s": CYCLE_EPOCH_S,
    "end_epoch_s": CYCLE_EPOCH_S + 3 * 3600,
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


def _manifest():
    return json.loads((FIXTURE_DIR / MANIFEST_NAME).read_text(encoding="utf-8"))


def _open_source():
    return Grib.parse(str(FIXTURE_DIR / GRIB_NAME))


def _band(dataset, element, hour):
    for band_index in range(1, dataset.RasterCount + 1):
        band = dataset.GetRasterBand(band_index)
        metadata = band.GetMetadata()
        if metadata.get("GRIB_ELEMENT") == element and metadata.get(
            "GRIB_FORECAST_SECONDS"
        ) == str(hour * 3600):
            return np.asarray(band.ReadAsArray(), dtype=float)
    raise AssertionError(f"missing {element} at forecast hour {hour}")


def _raw_uv(dataset, hour):
    return _band(dataset, "UGRD", hour), _band(dataset, "VGRD", hour)


def _grid_index(dataset, latitude, longitude):
    """Pixel index of the cell whose center is nearest (lat, lon)."""
    transform = dataset.GetGeoTransform()
    return (
        int(round((latitude - transform[3]) / transform[5] - 0.5)),
        int(round((longitude - transform[0]) / transform[1] - 0.5)),
    )


def _wind_components(value):
    from_degrees, speed_ms = value
    radians = math.radians(from_degrees)
    return (
        -math.sin(radians) * speed_ms * KNOTS_PER_METRE_PER_SECOND,
        -math.cos(radians) * speed_ms * KNOTS_PER_METRE_PER_SECOND,
    )


def _current_field(east, north, times, latitudes, longitudes, *, run):
    metadata = json.dumps(
        {
            "schema_version": 1,
            "provider": "test",
            "model": "real-forecast differential current selection",
            "composition": "total",
            "vector_reference": "true_east_north",
            "sources": [],
            "resolution_m": 1000,
            "run": run,
        },
        sort_keys=True,
    )
    shape = (len(times), len(latitudes), len(longitudes))
    return CurrentField(
        metadata,
        np.asarray(times, dtype=float),
        np.asarray(latitudes, dtype=float),
        np.asarray(longitudes, dtype=float),
        np.full(shape, east, dtype=float),
        np.full(shape, north, dtype=float),
    )


def _strong_current_selection():
    times = [CYCLE_EPOCH_S + 3600 * hour for hour in range(121)]
    return FieldSelection(
        [
            _current_field(
                2.6,
                -1.4,
                times,
                [41.9, 47.1],
                [-126.1, -121.9],
                run=CYCLE.isoformat(),
            )
        ]
    )


def test_manifest_and_grib_identity_are_checked_offline():
    manifest = _manifest()
    artifact = FIXTURE_DIR / manifest["artifact"]["path"]
    payload = artifact.read_bytes()

    assert artifact.name == GRIB_NAME
    assert manifest["artifact"]["bytes"] == len(payload) == 372813
    assert manifest["artifact"]["sha256"] == (
        "d7cede11957637649c718719309303767629e581a1686fab92f733e8c59809b1"
    )
    assert hashlib.sha256(payload).hexdigest() == manifest["artifact"]["sha256"]
    # Cycle and source identity come from the local manifest and file headers,
    # never a network request.
    assert manifest["provider"]["cycle_epoch_s"] == CYCLE_EPOCH_S
    assert datetime.fromtimestamp(CYCLE_EPOCH_S, timezone.utc) == CYCLE
    assert manifest["provider"]["valid_hours"] == list(range(0, 121, 3))
    assert manifest["provider"]["horizon_hours"] == 120
    assert manifest["provider"]["step_hours"] == 3
    assert manifest["source_identity"]["nomads_filter_url"] == GFS_FILTER_URL
    assert manifest["source_identity"]["producer_url"] == (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261026/12/atmos/"
    )
    source_identity = manifest["source_identity"]
    assert source_identity["object_url_pattern"].endswith(
        "gfs.t12z.pgrb2.0p25.f{hour:03d}"
    )
    assert source_identity["index_url_pattern"].endswith(".f{hour:03d}.idx")
    assert "PRMSL/mean sea level" in source_identity["retrieval_rule"]
    assert "UGRD/10 m above ground" in source_identity["retrieval_rule"]
    assert "VGRD/10 m above ground" in source_identity["retrieval_rule"]
    frames = source_identity["frames"]
    assert [frame["hour"] for frame in frames] == list(range(0, 121, 3))
    for frame in frames:
        assert set(frame["messages"]) == {"PRMSL", "UGRD", "VGRD"}
        for offset, length, digest in frame["messages"].values():
            assert offset >= 0
            assert length > 0
            assert len(digest) == 64
    assert manifest["licence"]["name"] == "NOAA public domain"
    assert "gdal_translate" in manifest["generator"]["method"]
    assert manifest["generator"]["value_fidelity"].startswith("exact")
    assert manifest["consumer"]["path"] == ("test/test_native_routing_real_forecast.py")

    source = _open_source()
    try:
        assert source.model_name == "GFS"
        assert source.last_forecast == 120
        assert source.dataset.RasterXSize == manifest["provider"]["grid"]["nx"] == 17
        assert source.dataset.RasterYSize == manifest["provider"]["grid"]["ny"] == 21
        # 41 forecast hours x 3 elements as 123 original GRIB2 messages.
        assert source.dataset.RasterCount == 123
        assert source.bounds == [
            manifest["provider"]["grid"]["envelope"]["west"],
            manifest["provider"]["grid"]["envelope"]["south"],
            manifest["provider"]["grid"]["envelope"]["east"],
            manifest["provider"]["grid"]["envelope"]["north"],
        ]
        forecast_hours = set()
        for index in range(1, source.dataset.RasterCount + 1):
            metadata = source.dataset.GetRasterBand(index).GetMetadata()
            forecast_hours.add(int(metadata["GRIB_FORECAST_SECONDS"]) // 3600)
        assert sorted(forecast_hours) == manifest["provider"]["valid_hours"]
    finally:
        source.close()


def test_bridge_override_routes_real_grib_wind_and_currents():
    bridge = _bridge()
    source = _open_source()
    try:
        sampler = WeatherSampler([source])
        selection = _strong_current_selection()

        started = time_module.perf_counter()
        run = bridge.route_with_weather(
            wind_callable_from_sampler(sampler),
            current_callable_from_selection(selection),
            ARTIFACT_REQUEST_COVERAGE,
            provider_identity="gfs-20261026-12z-real-forecast",
            start=ROUTE_START,
            destination=ROUTE_DESTINATION,
            departure_epoch_s=CYCLE_EPOCH_S,
        )
        elapsed = time_module.perf_counter() - started

        # The bridge echoes the relocated request, so the differential is
        # demonstrably on the artifact's domain rather than the frozen one.
        request = run["request"]
        assert (
            request["start"]["latitude"],
            request["start"]["longitude"],
        ) == pytest.approx(ROUTE_START, abs=1e-12)
        assert (
            request["destination"]["latitude"],
            request["destination"]["longitude"],
        ) == pytest.approx(ROUTE_DESTINATION, abs=1e-12)
        assert request["departure"] == CYCLE_EPOCH_S

        assert run["accepted"] is True, run["message"]
        assert run["legs"]
        for leg in run["legs"]:
            instant = datetime.fromtimestamp(leg["start_time"], tz=timezone.utc)
            latitude = leg["start"]["latitude"]
            longitude = leg["start"]["longitude"]
            assert ARTIFACT_BOUNDS["south"] <= latitude <= ARTIFACT_BOUNDS["north"]
            assert ARTIFACT_BOUNDS["west"] <= longitude <= ARTIFACT_BOUNDS["east"]

            sampled_wind = sampler.wind_at(instant, latitude, longitude)
            assert sampled_wind is not None, "bridge leg left GFS coverage"
            east, north = _wind_components(sampled_wind)
            assert leg["wind_east_knots"] == pytest.approx(east, abs=1e-9)
            assert leg["wind_north_knots"] == pytest.approx(north, abs=1e-9)

            sample = selection.sample(latitude, longitude, instant)
            assert sample.status == "available"
            assert (
                math.hypot(*sample.values) * KNOTS_PER_METRE_PER_SECOND > 2.0
            ), "the strong-current case is not strong"
            assert leg["current_east_knots"] == pytest.approx(
                sample.values[0] * KNOTS_PER_METRE_PER_SECOND, abs=1e-9
            )
            assert leg["current_north_knots"] == pytest.approx(
                sample.values[1] * KNOTS_PER_METRE_PER_SECOND, abs=1e-9
            )

        print(
            "native real-forecast GFS differential: "
            f"{len(run['legs'])} legs, solver={run['solver_path']} in "
            f"{elapsed:.3f} s (descriptive, no threshold)"
        )
    finally:
        source.close()


def test_bridge_defaults_stay_frozen_and_never_fabricate_wind():
    """Calling without overrides keeps the synthetic request byte-for-byte."""
    bridge = _bridge()
    source = _open_source()
    try:
        sampler = WeatherSampler([source])
        wind = wind_callable_from_sampler(sampler)
        assert sampler.wind_at(CYCLE, *CELL_CENTERS[0]) is not None
        assert sampler.wind_at(FROZEN_REQUEST_DEPARTURE, *FROZEN_REQUEST_START) is None
        assert wind(
            *FROZEN_REQUEST_START, int(FROZEN_REQUEST_DEPARTURE.timestamp())
        ) == (0.0, 0.0, False)

        started = time_module.perf_counter()
        run = bridge.route_with_weather(
            wind,
            current_callable_from_selection(FieldSelection([])),
            FROZEN_REQUEST_COVERAGE,
            provider_identity="gfs-20261026-12z-real-forecast",
        )
        elapsed = time_module.perf_counter() - started

        request = run["request"]
        assert (
            request["start"]["latitude"],
            request["start"]["longitude"],
        ) == pytest.approx(FROZEN_REQUEST_START, abs=1e-12)
        assert (
            request["destination"]["latitude"],
            request["destination"]["longitude"],
        ) == pytest.approx(FROZEN_REQUEST_DESTINATION, abs=1e-12)
        assert request["departure"] == int(FROZEN_REQUEST_DEPARTURE.timestamp())

        assert run["accepted"] is False
        assert run["status"] != "complete"
        assert run["leg_count"] == 0
        print(
            "native real-forecast frozen-default differential: "
            f"status={run['status']} in {elapsed:.3f} s (descriptive, no threshold)"
        )
    finally:
        source.close()


def test_bridge_override_before_the_cycle_is_reported_unavailable():
    bridge = _bridge()
    source = _open_source()
    try:
        sampler = WeatherSampler([source])
        selection = _strong_current_selection()
        departure = CYCLE - timedelta(hours=1)

        run = bridge.route_with_weather(
            wind_callable_from_sampler(sampler),
            current_callable_from_selection(selection),
            EARLY_REQUEST_COVERAGE,
            provider_identity="gfs-20261026-12z-real-forecast",
            start=ROUTE_START,
            destination=ROUTE_DESTINATION,
            departure_epoch_s=int(departure.timestamp()),
        )

        assert run["request"]["departure"] == int(departure.timestamp())
        assert run["accepted"] is False
        assert run["status"] != "complete"
    finally:
        source.close()
