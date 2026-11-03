from datetime import datetime, timezone

import numpy as np
import pytest

from sailworks.core.weathernext import (
    U_WIND,
    V_WIND,
    open_statistics_store,
    select_hourly_wind,
    statistics_store_url,
)

RUN = datetime(2026, 8, 26, tzinfo=timezone.utc)


def test_store_url_and_injectable_opener():
    assert statistics_store_url(RUN, "00_preds").endswith(
        "20260826_00hr_00_preds/predictions.zarr"
    )
    marker = object()
    assert (
        open_statistics_store(statistics_store_url(RUN, "00_preds"), lambda url: marker)
        is marker
    )
    with pytest.raises(ValueError, match="WeatherNext statistics"):
        open_statistics_store("gs://bucket/path", lambda url: marker)


class Coordinate:
    def __init__(self, values):
        self.values = np.asarray(values)


class LazyDataset:
    def __init__(self, lat=(90, -90), lon=(0, 90, 180, 270), variables=None):
        self.coords = {
            "init_time": Coordinate([np.datetime64("2026-08-26T00:00:00")]),
            "lat_0p1": Coordinate(lat),
            "lon_0p1": Coordinate(lon),
        }
        self.variables = set(variables or (U_WIND, V_WIND))
        self.sizes = {"lat_0p1": len(lat), "lon_0p1": len(lon)}
        self.request = None

    def __contains__(self, name):
        return name in self.variables

    def __getitem__(self, names):
        assert names == [U_WIND, V_WIND]
        return self

    def sel(self, **kwargs):
        self.request = kwargs
        return self


def _select(ds, lon=(-10, 10), lat=(30, 40)):
    return select_hourly_wind(ds, RUN, 6, latitude=lat, longitude=lon)


def test_selects_timedelta_lead_and_prime_meridian_split_bounded_windows():
    xr = pytest.importorskip("xarray")
    ds = xr.Dataset(
        {
            U_WIND: (("lead_time", "lat_0p1", "lon_0p1"), np.zeros((1, 3, 4))),
            V_WIND: (("lead_time", "lat_0p1", "lon_0p1"), np.zeros((1, 3, 4))),
        },
        coords={
            "lead_time": [np.timedelta64(6, "h")],
            "lat_0p1": [50, 40, 30],
            "lon_0p1": [0, 90, 270, 350],
            "init_time": np.datetime64("2026-08-26T00:00:00"),
        },
    )
    selected = _select(ds)
    assert selected.sizes["lat_0p1"] == 2
    assert set(selected.lon_0p1.values) == {0, 350}
    assert selected.lead_time.values == np.timedelta64(6, "h")


def test_selects_single_window_when_not_crossing_prime_meridian():
    ds = LazyDataset(lat=(-90, 90))
    result = _select(ds, lon=(20, 40))
    assert result is ds
    assert ds.request == {
        "lead_time": np.timedelta64(6, "h"),
        "lat_0p1": slice(30, 40),
        "lon_0p1": slice(20, 40),
    }


def test_fails_closed_on_missing_wind_or_wrong_init_time():
    with pytest.raises(KeyError, match=V_WIND):
        _select(LazyDataset(variables=(U_WIND,)))
    ds = LazyDataset()
    ds.coords["init_time"] = Coordinate([np.datetime64("2026-08-27T00:00:00")])
    with pytest.raises(ValueError, match="init_time"):
        _select(ds, lon=(20, 40))


def test_rejects_invalid_region_and_empty_selection():
    with pytest.raises(ValueError, match="longitude"):
        _select(LazyDataset(), lon=(-181, 10))
    ds = LazyDataset()
    ds.sizes["lat_0p1"] = 0
    with pytest.raises(ValueError, match="no grid points"):
        _select(ds, lon=(20, 40))


def test_rejects_full_world_selection():
    with pytest.raises(ValueError, match="full-world"):
        _select(LazyDataset(), lon=(-180, 180))


@pytest.mark.parametrize("lead", [49, 360])
def test_regular_run_accepts_hourly_leads(lead):
    assert select_hourly_wind(
        LazyDataset(), RUN, lead, latitude=(30, 40), longitude=(20, 40)
    )


def test_interim_run_rejects_lead_after_48():
    interim = RUN.replace(hour=1)
    with pytest.raises(ValueError, match="interim"):
        select_hourly_wind(
            LazyDataset(), interim, 49, latitude=(30, 40), longitude=(20, 40)
        )


def test_rejects_lead_after_documented_horizon():
    with pytest.raises(ValueError, match="360"):
        select_hourly_wind(
            LazyDataset(), RUN, 361, latitude=(30, 40), longitude=(20, 40)
        )


@pytest.mark.parametrize(
    "latitude,longitude",
    [
        ((float("nan"), 40), (20, 40)),
        ((30, float("inf")), (20, 40)),
        ((30, 30), (20, 40)),
        ((30, 40), (float("nan"), 40)),
        ((30, 40), (20, 20)),
    ],
)
def test_rejects_nonfinite_or_zero_span_bounds(latitude, longitude):
    with pytest.raises(ValueError):
        select_hourly_wind(
            LazyDataset(), RUN, 6, latitude=latitude, longitude=longitude
        )


@pytest.mark.parametrize("tag", ["preds", "01_bad", "24_preds", "01_preds/evil"])
def test_rejects_invalid_run_tag(tag):
    with pytest.raises(ValueError, match="HH_preds"):
        statistics_store_url(RUN, tag)
