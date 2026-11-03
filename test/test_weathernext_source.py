import json
from datetime import datetime, timezone

import numpy as np
import pytest

from sailworks.core.forecastcapabilities import MODEL_CAPABILITIES
from sailworks.core.gribsources.options import DownloadOptions
from sailworks.core.gribsources.weathernext import (
    PRESSURE,
    WeatherNextSource,
)
from sailworks.core.weathernext import U_WIND, V_WIND
from sailworks.qt.forecastdownload import MODELS

RUN = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def _dataset():
    xr = pytest.importorskip("xarray")
    return xr.Dataset(
        {
            U_WIND: (
                ("lead_time", "lat_0p1", "lon_0p1"),
                np.full((1, 3, 3), 5.0, dtype=np.float32),
            ),
            V_WIND: (
                ("lead_time", "lat_0p1", "lon_0p1"),
                np.zeros((1, 3, 3), dtype=np.float32),
            ),
            PRESSURE: (
                ("lead_time", "lat_0p1", "lon_0p1"),
                np.full((1, 3, 3), 101325.0, dtype=np.float32),
            ),
        },
        coords={
            "lead_time": [np.timedelta64(6, "h")],
            "lat_0p1": [47.0, 48.0, 49.0],
            "lon_0p1": [236.0, 237.0, 238.0],
            "init_time": np.datetime64("2026-11-03T12:00:00"),
        },
    )


def test_catalogue_returns_pinned_weathernext_rows():
    source = WeatherNextSource(runs=[("20261103", "12", "01_preds", RUN)])
    options = DownloadOptions(12, 3, ("wind",), ())
    rows = source.get_download_list(options)
    assert len(rows) == 1
    payload = json.loads(rows[0][4])
    assert payload["source"] == "Google WeatherNext 3"
    assert payload["date"] == "20261103"
    assert payload["cycle"] == "12"
    assert payload["fields"] == ["wind"]
    assert payload["steps"] == [3, 6, 9, 12]
    assert payload["run_tag"] == "01_preds"


def test_download_writes_validated_ugrd_vgrd_and_prmsl(tmp_path, monkeypatch):
    gdal = pytest.importorskip("osgeo.gdal")
    pytest.importorskip("eccodes")
    dataset = _dataset()
    monkeypatch.setattr(
        "sailworks.core.gribsources.weathernext.open_statistics_store",
        lambda *args, **kwargs: dataset,
    )
    monkeypatch.setattr(
        "sailworks.core.gribsources.weathernext.WeatherNextCredentials.credentials",
        lambda self: None,
    )
    source = WeatherNextSource()
    source.bounds = (47.0, -124.0, 49.0, -122.0)
    identifier = json.dumps(
        {
            "date": "20261103",
            "cycle": "12",
            "steps": [6],
            "fields": ["wind", "pressure"],
            "run_tag": "01_preds",
        }
    )
    path = source.download(identifier, str(tmp_path), lambda _percent: None)
    result = gdal.Open(path)
    assert result is not None
    elements = [
        result.GetRasterBand(index).GetMetadata().get("GRIB_ELEMENT")
        for index in range(1, result.RasterCount + 1)
    ]
    assert elements == ["UGRD", "VGRD", "PRMSL"]
    for index in range(1, result.RasterCount + 1):
        metadata = result.GetRasterBand(index).GetMetadata()
        assert metadata["GRIB_FORECAST_SECONDS"] == "21600"
        assert "CENTER=250" in metadata["GRIB_IDS"]


def test_weathernext_is_registered_in_capabilities_and_download_ui():
    capability = MODEL_CAPABILITIES["WEATHERNEXT"]
    assert capability.provider_name == "Google WeatherNext 3"
    assert capability.native_resolution == "0.1deg"
    assert capability.max_horizon() == 360
    assert any(row[0] == "Google WeatherNext 3" for row in MODELS)


def test_weathernext_interim_runs_publish_only_48_leads():
    capability = MODEL_CAPABILITIES["WEATHERNEXT"]
    # Synoptic cycles carry the full horizon; every other hour is an interim run.
    for hour in (0, 6, 12, 18):
        assert capability.max_horizon(hour) == 360
        assert capability.available_steps(hour, "wind")[-1] == 360
    for hour in (1, 5, 7, 13, 14, 23):
        assert capability.max_horizon(hour) == 48
        assert capability.available_steps(hour, "wind")[-1] == 48


def test_weathernext_display_name_is_short_but_identity_is_stable():
    from sailworks.core.forecastcapabilities import display_model_name

    assert display_model_name("WEATHERNEXT") == "WN3"
    assert MODEL_CAPABILITIES["WEATHERNEXT"].provider_name == "Google WeatherNext 3"
    # Unknown models fall back to their identifier.
    assert display_model_name("NOT_A_MODEL") == "NOT_A_MODEL"


def test_written_artifact_feeds_the_routing_sampler(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from sailworks.core.grib import Grib

    dataset = _dataset()
    monkeypatch.setattr(
        "sailworks.core.gribsources.weathernext.open_statistics_store",
        lambda *args, **kwargs: dataset,
    )
    monkeypatch.setattr(
        "sailworks.core.gribsources.weathernext.WeatherNextCredentials.credentials",
        lambda self: None,
    )
    source = WeatherNextSource()
    source.bounds = (47.0, -124.0, 49.0, -122.0)
    identifier = json.dumps(
        {
            "date": "20261103",
            "cycle": "12",
            "steps": [6],
            "fields": ["wind", "pressure"],
            "run_tag": "01_preds",
        }
    )
    path = source.download(identifier, str(tmp_path), lambda _percent: None)
    run = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    grib = Grib(
        path,
        "WeatherNext",
        "250",
        (-124.0, 47.0, -122.0, 49.0),
        run,
        6,
        "Google WeatherNext 3",
    )
    valid = datetime(2026, 11, 3, 18, tzinfo=timezone.utc)
    direction, speed = grib.get_wind_at(valid, 48.0, -123.0)
    assert round(speed, 3) == 5.0
    assert round(direction, 3) == 270.0
    pressure = grib.get_pressure(valid, ((47.0, -124.0), (49.0, -122.0)))
    assert pressure
    assert all(abs(value[2] - 1013.25) < 0.01 for value in pressure)
