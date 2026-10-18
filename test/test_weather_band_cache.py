"""Worker-private cache limits must not change sampling or source selection."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.rendering.weathercache import (
    DecodedBandCache,
    DisplayGridIndex,
    DisplayWeatherSource,
    SourceBandCache,
    WeatherBandLimitExceeded,
)
from test.test_weather_staging import gribs, START  # noqa: F401


def private_source(source, cache):
    return DisplayWeatherSource(
        source.path,
        source.name,
        source.centre,
        source.bounds,
        source.start_time,
        source.last_forecast,
        source.model_name,
        band_cache=cache,
    )


def read(cache, owner, band):
    values = SourceBandCache(cache, owner)
    cache.reserve(owner, band, 32)
    value = values.get(band)
    if value is None:
        value = np.full((2, 2), band, dtype=np.float64)
        values[band] = value
    values.move_to_end(band)
    return value


def test_shared_lru_evicts_across_sources_and_protects_recent_hits():
    cache = DecodedBandCache(64)
    first, second = object(), object()
    read(cache, first, 1)
    read(cache, second, 1)
    read(cache, first, 1)
    read(cache, second, 2)
    assert 1 in SourceBandCache(cache, first)
    assert 1 not in SourceBandCache(cache, second)
    assert 2 in SourceBandCache(cache, second)
    assert cache.statistics() == dict(
        limit_bytes=64,
        current_bytes=64,
        peak_bytes=64,
        retained_bands=2,
        hits=1,
        misses=3,
        evictions=1,
    )


def test_source_clear_releases_only_its_own_retention():
    cache = DecodedBandCache(64)
    first, second = object(), object()
    read(cache, first, 1)
    read(cache, second, 1)
    SourceBandCache(cache, first).clear()
    assert cache.current_bytes == 32
    assert 1 in SourceBandCache(cache, second)
    SourceBandCache(cache, second).clear()
    assert cache.current_bytes == 0
    assert cache.statistics()["retained_bands"] == 0


def test_oversized_band_fails_before_gdal_read():
    cache = DecodedBandCache(31)
    index = object.__new__(DisplayGridIndex)
    index.dataset = SimpleNamespace(GetRasterBand=Mock())
    index.width = index.height = 2
    index.band_cache = cache
    index.arrays = SourceBandCache(cache, index)
    with pytest.raises(WeatherBandLimitExceeded, match="worker limit"):
        index.array(SimpleNamespace(band=1))
    index.dataset.GetRasterBand.assert_not_called()
    assert cache.current_bytes == 0


def test_budget_exception_cannot_be_swallowed_as_missing_weather():
    assert not issubclass(
        WeatherBandLimitExceeded, (ValueError, RuntimeError, TypeError, IndexError)
    )


def test_unexpected_decoded_size_does_not_corrupt_existing_accounting():
    cache = DecodedBandCache(32)
    owner = object()
    original = read(cache, owner, 1)
    with pytest.raises(WeatherBandLimitExceeded):
        SourceBandCache(cache, owner)[1] = np.ones((3, 3), dtype=np.float64)
    assert cache.current_bytes == 32
    assert SourceBandCache(cache, owner)[1] is original


@pytest.mark.parametrize("limit", [0, -1])
def test_budget_requires_positive_limit(limit):
    with pytest.raises(ValueError, match="positive"):
        DecodedBandCache(limit)


def test_per_source_six_band_cap_remains_and_close_releases_it(gribs):
    source = gribs.file(sum((gribs.field(hour) for hour in range(4)), []))
    cache = DecodedBandCache(4096)
    private = private_source(source, cache)
    try:
        index = private.sampling
        for record in index.records:
            index.array(record)
        assert len(index.arrays) == 6
        assert cache.current_bytes == 6 * 32
        assert "_sampling" not in source.__dict__
    finally:
        private.close()
    assert cache.current_bytes == 0
    assert index.dataset is None


def test_eviction_reload_preserves_exact_values_masks_and_point_sources(gribs):
    old = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    newer_u = gribs.file(
        [("UGRD", 0, [[9, -9999], [9, 9]])],
        start=START + timedelta(hours=1),
        end=2,
    )
    newer_v = gribs.file(
        [("VGRD", 0, [[-3, -3], [-3, -3]])],
        start=START + timedelta(hours=1),
        end=2,
    )
    sources = [old, newer_u, newer_v]
    reference = WeatherSampler(sources)
    cache = DecodedBandCache(32)
    private = [private_source(source, cache) for source in sources]
    try:
        candidate = WeatherSampler(private)
        lats = np.array([0, 0.25, 0.5, 1, 10])
        lons = np.array([0, 0.25, 0.75, 1, 10])
        for hours in (0, 0.5, 1, 1.0003, 2.5, 0.5):
            stamp = START.timestamp() + hours * 3600
            expected = reference.sample_timestamps("wind", stamp, lats, lons)
            actual = candidate.sample_timestamps("wind", stamp, lats, lons)
            np.testing.assert_array_equal(
                actual.view(np.uint64), expected.view(np.uint64)
            )
            assert tuple(candidate.point_sources) == tuple(reference.point_sources)
            assert cache.current_bytes <= 32
        assert cache.evictions > 0
    finally:
        for source in private:
            source.close()
    assert cache.current_bytes == 0


def test_resource_failure_propagates_instead_of_falling_back_to_older_model(gribs):
    old = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    newer = gribs.file(gribs.field(0, size=3), start=START + timedelta(hours=1), end=2)
    cache = DecodedBandCache(32)
    private = [private_source(source, cache) for source in (old, newer)]
    try:
        sampler = WeatherSampler(private)
        with pytest.raises(WeatherBandLimitExceeded):
            sampler.sample_timestamps("wind", START.timestamp() + 3600, [0.5], [0.5])
    finally:
        for source in private:
            source.close()
    assert cache.current_bytes == 0
