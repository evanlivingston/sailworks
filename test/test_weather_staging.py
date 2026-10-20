"""Temporal texture endpoints must retain the shared sampler's answers."""

from datetime import timedelta

import numpy as np
import pytest

from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.rendering.weatherstaging import NodeSampleMemo, stage_wind
from test import test_grib_sampling as grib_fixtures

START = grib_fixtures.START


@pytest.fixture
def gribs():
    fixtures = grib_fixtures.TestGribSampling()
    fixtures.setUp()
    try:
        yield fixtures
    finally:
        fixtures.doCleanups()


def assert_parity(sampler, staged, lats, lons, hours):
    for hour in hours:
        stamp = (START + timedelta(hours=hour)).timestamp()
        blend = staged.weight(stamp)
        actual = staged.first + (staged.second - staged.first) * blend
        expected = sampler.sample_timestamps("wind", stamp, lats, lons)
        np.testing.assert_allclose(
            actual.reshape(-1, 2), expected, rtol=2e-7, atol=2e-6, equal_nan=True
        )
        assert staged.sources == tuple(sampler.point_sources)


def test_irregular_forecast_intervals_and_fractional_seconds(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    lat, lon = np.mgrid[0:1:3j, 0:1:3j]
    staged = stage_wind(sampler, (START + timedelta(minutes=17)).timestamp(), lat, lon)
    assert_parity(sampler, staged, lat, lon, [1 / 3600, 1 / 60, 0.9997, 1.5, 2.999])
    assert not staged.first.flags.writeable
    assert not staged.second.flags.writeable
    np.testing.assert_allclose(
        staged.texture_pair().weight(START + timedelta(minutes=17)), 17 / 180
    )


def test_new_cycle_boundary_is_never_blended(gribs):
    old = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    new = gribs.file(
        [("UGRD", 0, [[99, 99], [99, 99]]), ("VGRD", 0, [[-8, -8], [-8, -8]])],
        start=START + timedelta(hours=1),
        end=2,
    )
    sampler = WeatherSampler([old, new])
    lat = lon = np.array([[0.25]])
    staged = stage_wind(sampler, (START + timedelta(minutes=59)).timestamp(), lat, lon)
    assert_parity(sampler, staged, lat, lon, [0.01, 0.5, 0.999])
    boundary = (START + timedelta(hours=1)).timestamp()
    assert not staged.contains(boundary)
    exact = stage_wind(sampler, boundary, lat, lon)
    np.testing.assert_allclose(exact.first, [[[99, -8]]])
    assert exact.before == exact.after == boundary
    later = stage_wind(sampler, boundary + 1, lat, lon)
    assert_parity(sampler, later, lat, lon, [1.0003, 1.5, 2.999])


def test_split_files_and_missing_endpoint_fallback(gribs):
    a = gribs.file(gribs.field(0), end=3)
    b = gribs.file(gribs.field(3), end=3)
    sampler = WeatherSampler([a, b])
    lat = lon = np.array([[0, 0.5, 1, 10]])
    staged = stage_wind(sampler, START.timestamp() + 3600, lat, lon)
    assert_parity(sampler, staged, lat, lon, [0.01, 1, 2.9])
    assert np.isnan(staged.first[0, -1]).all()
    # A hole at one valid time uses the sampler's one-sided temporal fallback.
    c = gribs.file(
        gribs.field(0)
        + [
            ("UGRD", 3, [[-9999, -9999], [-9999, -9999]]),
            ("VGRD", 3, [[1, 1], [1, 1]]),
        ],
        process=83,
        end=3,
    )
    sampler = WeatherSampler([c])
    staged = stage_wind(sampler, START.timestamp() + 3600, lat, lon)
    assert_parity(sampler, staged, lat, lon, [0.01, 1, 2.9])


def test_cancellation_and_memory_budget_do_not_publish_partial_fields(gribs):
    sampler = WeatherSampler([gribs.file(gribs.field(0))])
    lat = lon = np.array([[0.5]])
    assert (
        stage_wind(sampler, START.timestamp() + 1, lat, lon, cancel=lambda: True)
        is None
    )
    with pytest.raises(ValueError, match="memory budget"):
        stage_wind(sampler, START.timestamp(), lat, lon, byte_limit=1)
    with pytest.raises(ValueError, match="Invalid forecast time"):
        stage_wind(sampler, np.nan, lat, lon)


def test_projected_grid_relative_vectors_are_rotated_by_existing_sampler(gribs):
    from unittest.mock import patch
    from pyproj import CRS, Transformer

    crs = CRS(
        "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 "
        "+lon_0=-97.5 +R=6371229 +units=m"
    )
    to_grid = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_world = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    x, y = to_grid.transform(-124, 48)
    source = gribs.file(
        gribs.field(0) + gribs.field(3),
        transform=(x - 1500, 3000, 0, y - 1500, 0, 3000),
        projection=crs.to_wkt(),
        end=3,
    )
    with patch(
        "gweatherrouting.core.gribsampling.vector_flags", return_value=[True] * 6
    ):
        sampler = WeatherSampler([source])
    xx, yy = np.meshgrid(x + np.array([500, 1500]), y + np.array([500, 1500]))
    lons, lats = to_world.transform(xx, yy)
    staged = stage_wind(sampler, START.timestamp() + 3600, lats, lons)
    assert_parity(sampler, staged, lats, lons, [0.0003, 0.25, 1.5, 2.999])


def test_node_memo_shifted_window_is_bit_identical_to_cold_staging(gribs):
    """A memoized shift is a pure function of the same inputs."""
    from gweatherrouting.rendering.weatherbarbs import _node_key

    sampler = WeatherSampler([gribs.file(gribs.field(0) + gribs.field(3), end=3)])
    stamp = (START + timedelta(hours=1.5)).timestamp()
    namespace = ("source-set", stamp - 1, stamp + 1)
    # Two overlapping query grids inside the fixture's -0.5..0.5 source extent.
    lat, lon = np.mgrid[-0.4:0.4:5j, -0.4:0.0:5j]
    shifted_lat, shifted_lon = np.mgrid[-0.4:0.4:5j, -0.2:0.2:5j]
    cold_a = stage_wind(sampler, stamp, lat, lon)
    cold_b = stage_wind(sampler, stamp, shifted_lat, shifted_lon)
    memo = NodeSampleMemo()
    warm_a = stage_wind(
        sampler,
        stamp,
        lat,
        lon,
        memo=memo,
        memo_namespace=namespace,
        node_key=_node_key,
    )
    warm_b = stage_wind(
        sampler,
        stamp,
        shifted_lat,
        shifted_lon,
        memo=memo,
        memo_namespace=namespace,
        node_key=_node_key,
    )
    np.testing.assert_array_equal(warm_a.first, cold_a.first)
    np.testing.assert_array_equal(warm_a.second, cold_a.second)
    np.testing.assert_array_equal(warm_b.first, cold_b.first)
    np.testing.assert_array_equal(warm_b.second, cold_b.second)
    assert warm_a.sources == cold_a.sources
    assert warm_b.sources == cold_b.sources
    assert memo.hits > 0
    # A different time window must not reuse the previous window's values.
    other = stage_wind(
        sampler,
        (START + timedelta(hours=2.5)).timestamp(),
        lat,
        lon,
        memo=memo,
        memo_namespace=("source-set", stamp + 1, stamp + 2),
        node_key=_node_key,
    )
    expected = stage_wind(sampler, (START + timedelta(hours=2.5)).timestamp(), lat, lon)
    np.testing.assert_array_equal(other.first, expected.first)
    np.testing.assert_array_equal(other.second, expected.second)


def test_land_memo_round_trips_the_same_per_node_decision():
    memo = NodeSampleMemo()
    key = (1.0, 2.0)
    assert memo.get_land(key, "high") is None
    memo.put_land(key, "high", False)
    assert memo.get_land(key, "high") is False
    # A stored False is a hit, not a miss; another quality is separate.
    assert memo.get_land(key, "low") is None
    memo.put_land(key, "high", True)
    assert memo.get_land(key, "high") is True
