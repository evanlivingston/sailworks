"""A real NOAA HRRR forecast must reach the viewport as current wind.

Opt-in: this downloads a few megabytes from NOAA NOMADS, so it runs only with
``GWR_NETWORK_TESTS=1``.  It uses the application's own downloader and the
viewport model, and it skips with the reason when NOAA is unreachable.
"""

from datetime import timedelta
import os
import time as clock

import pytest

from test.test_weather_barbs import Land

BOUNDS = (47.5, -124.5, 49.5, -122.0)  # south, west, north, east
NETWORK = os.environ.get("GWR_NETWORK_TESTS") == "1"

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(not NETWORK, reason="set GWR_NETWORK_TESTS=1 to download"),
]


@pytest.fixture(scope="module")
def hrrr_grib(tmp_path_factory):
    from gweatherrouting.core.gribsources.noaahrrr import NOAAHRRRSource
    from gweatherrouting.core.modeldownloads import install_download

    source = NOAAHRRRSource()
    source.bounds = BOUNDS
    try:
        rows = source.get_download_list()
    except Exception as exc:  # network or NOAA unavailable
        pytest.skip(f"NOAA HRRR listing unavailable: {exc}")
    if not rows:
        pytest.skip("NOAA HRRR has no published run right now")
    directory = tmp_path_factory.mktemp("hrrr")
    try:
        return install_download(source, rows[0], directory, lambda _percent: None)
    except Exception as exc:
        pytest.skip(f"NOAA HRRR download unavailable: {exc}")


def test_a_downloaded_hrrr_forecast_is_current_wind_in_the_viewport(hrrr_grib):
    from gweatherrouting.qt.gribviewport import (
        GribViewportModel,
        ViewportCamera,
        _covers,
    )

    centre_lat = (BOUNDS[0] + BOUNDS[2]) / 2
    centre_lon = (BOUNDS[1] + BOUNDS[3]) / 2
    camera = ViewportCamera(
        center_lat=centre_lat,
        center_lon=centre_lon,
        zoom=9,
        width=640,
        height=480,
        time=hrrr_grib.start_time + timedelta(hours=1),
    )
    model = GribViewportModel([hrrr_grib.path], land_mask=Land())
    try:
        deadline = clock.monotonic() + 60
        state, field, selection = model.update(camera)
        while state != "ready" and clock.monotonic() < deadline:
            if model.state == "unavailable":
                pytest.fail(f"viewport rejected the real forecast: {model.error}")
            clock.sleep(0.05)
            state, field, selection = model.update(camera)
        assert state == "ready", "the viewport never presented the real forecast"
        assert _covers(field, camera)
        assert selection is not None and selection.count > 0
    finally:
        model.close()
