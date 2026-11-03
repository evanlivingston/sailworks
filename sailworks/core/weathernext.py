"""Lazy, bounded reader for WeatherNext 3 spatial-statistics Zarr."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Callable

BASE_URL = "gs://weathernext3_statistics_spatial/weathernext_3_0_0_statistics/zarr/2026_to_present/"
U_WIND = "u_component_of_wind_10m_mean"
V_WIND = "v_component_of_wind_10m_mean"


def _utc_hour(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    value = value.astimezone(timezone.utc)
    if value.minute or value.second or value.microsecond:
        raise ValueError(f"{name} must be an exact hour")
    return value


def statistics_store_url(run_time: datetime, run_tag: str) -> str:
    """Return the documented hourly prediction store for a UTC run."""
    run_time = _utc_hour(run_time, "run_time")
    if not isinstance(run_tag, str) or not re.fullmatch(
        r"(?:0[0-9]|1[0-9]|2[0-3])_preds", run_tag
    ):
        raise ValueError("run_tag must match the documented HH_preds format")
    return f"{BASE_URL}{run_time:%Y%m%d_%Hhr}_{run_tag}/predictions.zarr"


def open_statistics_store(url: str, opener: Callable | None = None):
    """Open a GCS Zarr store lazily. Dependencies are optional until called."""
    if (
        not isinstance(url, str)
        or not url.startswith(BASE_URL)
        or not url.endswith("/predictions.zarr")
    ):
        raise ValueError("url must identify a WeatherNext statistics store")
    if opener is not None:
        return opener(url)
    try:
        import xarray as xr
        from obstore.store import GCSStore
        from zarr.storage import ObjectStore
    except ImportError as exc:
        raise RuntimeError(
            "Opening WeatherNext requires xarray, zarr and obstore"
        ) from exc
    store = GCSStore(
        bucket="weathernext3_statistics_spatial",
        prefix=url.removeprefix("gs://weathernext3_statistics_spatial/"),
    )
    return xr.open_zarr(ObjectStore(store), chunks={})


def _bounds(bounds, name, low, high):
    if (
        not isinstance(bounds, (tuple, list))
        or len(bounds) != 2
        or any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in bounds
        )
        or bounds[0] > bounds[1]
        or bounds[0] == bounds[1]
        or bounds[0] < low
        or bounds[1] > high
    ):
        raise ValueError(f"{name} must be an increasing pair in [{low}, {high}]")


def select_hourly_wind(
    dataset,
    run_time: datetime,
    lead_hour: int,
    *,
    latitude: tuple[float, float],
    longitude: tuple[float, float],
):
    """Select a run, lead and bounded region without materializing the grid.

    Longitude inputs use -180..180; the dataset uses 0..360. Regions crossing
    Greenwich are selected as two bounded pieces and concatenated.
    """
    import numpy as np

    run_time = _utc_hour(run_time, "run_time")
    if (
        isinstance(lead_hour, bool)
        or not isinstance(lead_hour, int)
        or not 1 <= lead_hour <= 360
    ):
        raise ValueError("lead_hour must be an integer from 1 through 360")
    if run_time.hour % 6 and lead_hour > 48:
        raise ValueError("interim lead_hour must not exceed 48")
    _bounds(latitude, "latitude", -90, 90)
    _bounds(longitude, "longitude", -180, 180)
    missing = [name for name in (U_WIND, V_WIND) if name not in dataset]
    if missing:
        raise KeyError(
            f"WeatherNext GCS schema lacks wind variable(s): {', '.join(missing)}"
        )
    if "init_time" not in dataset.coords:
        raise ValueError("WeatherNext dataset is missing init_time")
    initialized = np.asarray(dataset.coords["init_time"].values).reshape(-1)
    expected = np.datetime64(run_time.replace(tzinfo=None), "ns")
    if len(initialized) != 1 or initialized[0].astype("datetime64[ns]") != expected:
        raise ValueError("dataset init_time does not match requested run_time")

    if "lat_0p1" not in dataset.coords or "lon_0p1" not in dataset.coords:
        raise ValueError("WeatherNext dataset lacks lat_0p1/lon_0p1 coordinates")
    lat = dataset.coords["lat_0p1"].values
    lat_slice = (
        slice(*latitude) if lat[0] <= lat[-1] else slice(latitude[1], latitude[0])
    )
    west, east = longitude
    if east - west >= 360:
        raise ValueError("full-world longitude selections are not bounded slices")
    west360, east360 = west % 360, east % 360
    lead = np.timedelta64(lead_hour, "h")
    variables = dataset[[U_WIND, V_WIND]]
    common = {"lead_time": lead, "lat_0p1": lat_slice}
    if west <= east and west360 <= east360:
        selected = variables.sel(**common, lon_0p1=slice(west360, east360))
    else:
        import xarray as xr

        parts = [
            variables.sel(**common, lon_0p1=slice(start, stop))
            for start, stop in ((west360, 360), (0, east360))
        ]
        selected = xr.concat(parts, dim="lon_0p1")
    if any(selected.sizes.get(dim, 0) == 0 for dim in ("lat_0p1", "lon_0p1")):
        raise ValueError("requested WeatherNext region has no grid points")
    return selected
