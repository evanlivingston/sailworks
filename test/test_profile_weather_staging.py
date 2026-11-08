"""The weather-staging diagnostic's workload helpers."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "profile_weather_staging.py"
spec = importlib.util.spec_from_file_location("profile_weather_staging", SCRIPT)
profile_weather_staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile_weather_staging)


def test_forecast_instant_prefers_a_covered_opened_run():
    manifest = {"forecast_time": "2026-11-03T07:00:00"}
    paths = (
        "/data/gfs_20261026_12z_120h_aaaa.grib2",
        "/data/hrrr_20261026_17z_18h_bbbb.grib2",
    )
    assert profile_weather_staging.forecast_instant(paths, manifest) == datetime(
        2026, 10, 27, tzinfo=timezone.utc
    )


def test_forecast_instant_falls_back_to_the_manifest_clock():
    manifest = {"forecast_time": "2026-11-03T07:00:00"}
    assert profile_weather_staging.forecast_instant(
        ("/data/plain.grib2",), manifest
    ) == datetime(2026, 11, 3, 7, tzinfo=timezone.utc)
