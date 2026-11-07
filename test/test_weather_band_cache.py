"""Shared decoded bands: deduplication, exact identity and lifecycle.

A session's cache is byte-accounted once, shared by every consumer and keyed
by exact source identity.  These checks are headless.
"""

from collections import OrderedDict
from datetime import timedelta
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from sailworks.core.gribsampling import WeatherSampler
from sailworks.rendering.weathercache import (
    DecodedBandCache,
    DisplayGridIndex,
    DisplayWeatherSource,
    SourceBandCache,
    WeatherBandLimitError,
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


def cache_owner(cache, identity="same-source"):
    class Owner:
        pass

    owner = Owner()
    owner.cache_identity = identity
    owner.arrays = SourceBandCache(cache, owner)
    return owner


def read(cache, owner, band, value=None):
    key = cache.key(owner, band)
    value = np.full((2, 2), band if value is None else value, dtype=np.float64)
    return cache.read(owner, key, 32, lambda: value)


def test_shared_cache_decodes_one_band_once_for_two_owners():
    cache = DecodedBandCache(64)
    first, second = cache_owner(cache), cache_owner(cache)
    value = read(cache, first, 1)
    assert read(cache, second, 1) is value
    assert not value.flags.writeable
    assert cache.statistics()["decodes"] == 1
    assert cache.statistics()["hits"] == 1
    assert cache.statistics()["current_bytes"] == 32
    assert 1 in first.arrays and 1 in second.arrays


def test_concurrent_consumers_wait_for_the_single_decode():
    cache = DecodedBandCache(64)
    first, second = cache_owner(cache), cache_owner(cache)
    started = Event()
    release = Event()
    results = []
    calls = []

    def loader():
        calls.append(1)
        started.set()
        assert release.wait(5)
        return np.full((2, 2), 7.0)

    def consume(owner):
        results.append(cache.read(owner, cache.key(owner, 1), 32, loader))

    one = Thread(target=consume, args=(first,))
    two = Thread(target=consume, args=(second,))
    one.start()
    assert started.wait(5)
    two.start()
    # The second caller is waiting on the in-flight exact key, not decoding.
    release.set()
    one.join(5)
    two.join(5)
    assert not one.is_alive() and not two.is_alive()
    assert calls == [1]
    assert results[0] is results[1]
    assert cache.statistics()["decodes"] == 1
    assert cache.statistics()["deduplicated"] == 1


def test_distinct_source_identities_do_not_share_a_band():
    cache = DecodedBandCache(64)
    first, second = cache_owner(cache, "file-a"), cache_owner(cache, "file-b")
    one = read(cache, first, 1, value=1)
    two = read(cache, second, 1, value=2)
    assert one is not two
    assert one[0, 0] == 1 and two[0, 0] == 2
    assert cache.statistics()["decodes"] == 2


def test_owner_release_keeps_the_entry_until_the_last_consumer_leaves():
    cache = DecodedBandCache(64)
    first, second = cache_owner(cache), cache_owner(cache)
    value = read(cache, first, 1)
    read(cache, second, 1)
    first.arrays.clear()
    assert 1 not in first.arrays
    assert 1 in second.arrays
    assert cache.current_bytes == 32
    assert read(cache, second, 1) is value
    second.arrays.clear()
    # The last consumer release frees the retained plane.
    assert cache.current_bytes == 0
    assert cache.statistics()["retained_bands"] == 0


def test_eviction_removes_the_band_from_every_local_view():
    cache = DecodedBandCache(64)
    first, second = cache_owner(cache), cache_owner(cache)
    read(cache, first, 1)
    read(cache, second, 1)
    read(cache, first, 2)
    read(cache, first, 3)  # evicts band 1 from both consumers
    assert 1 not in first.arrays
    assert 1 not in second.arrays
    assert cache.current_bytes == 64
    assert cache.statistics()["evictions"] == 1


def test_cancelled_owner_does_not_retain_an_in_flight_decode():
    cache = DecodedBandCache(64)

    class Owner:
        cache_identity = "cancelled"
        dataset = object()

    owner = Owner()
    owner.arrays = SourceBandCache(cache, owner)
    started = Event()
    release = Event()
    result = []

    def loader():
        started.set()
        assert release.wait(5)
        return np.ones((2, 2))

    def consume():
        result.append(cache.read(owner, cache.key(owner, 1), 32, loader))

    worker = Thread(target=consume)
    worker.start()
    assert started.wait(5)
    owner.dataset = None  # the consumer closes while the decode is in flight
    owner.arrays.clear()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert np.array_equal(result[0], np.ones((2, 2)))
    assert cache.current_bytes == 0
    assert 1 not in owner.arrays


def test_oversized_band_fails_before_any_decode():
    cache = DecodedBandCache(31)
    index = object.__new__(DisplayGridIndex)
    index.dataset = SimpleNamespace(GetRasterBand=Mock())
    index.width = index.height = 2
    index.band_cache = cache
    index.cache_identity = ("source", None)
    index.arrays = SourceBandCache(cache, index)
    with pytest.raises(WeatherBandLimitError, match="limit"):
        index.array(SimpleNamespace(band=1))
    index.dataset.GetRasterBand.assert_not_called()
    assert cache.current_bytes == 0
    assert cache.statistics()["decodes"] == 0


def test_budget_exception_cannot_be_swallowed_as_missing_weather():
    assert not issubclass(
        WeatherBandLimitError, (ValueError, RuntimeError, TypeError, IndexError)
    )


def test_unexpected_decoded_size_does_not_corrupt_existing_accounting():
    cache = DecodedBandCache(32)
    owner = cache_owner(cache)
    key = cache.key(owner, 1)
    with pytest.raises(WeatherBandLimitError):
        cache.read(owner, key, 32, lambda: np.ones((3, 3), dtype=np.float64))
    assert cache.current_bytes == 0
    assert cache.statistics()["retained_bands"] == 0
    # A failed resize must not leave the key claimed as in flight.
    assert cache.read(owner, key, 32, lambda: np.ones((2, 2))) is not None


@pytest.mark.parametrize("limit", [0, -1])
def test_budget_requires_positive_limit(limit):
    with pytest.raises(ValueError, match="positive"):
        DecodedBandCache(limit)


def test_local_six_band_cap_remains_and_session_outlives_the_index(gribs):
    source = gribs.file(sum((gribs.field(hour) for hour in range(4)), []))
    cache = DecodedBandCache(4096)
    private = private_source(source, cache)
    try:
        index = private.sampling
        assert isinstance(index.arrays, OrderedDict)
        for record in index.records:
            index.array(record)
        assert len(index.arrays) == 6
        assert cache.current_bytes == 6 * 32
        assert "_sampling" not in source.__dict__
    finally:
        private.close()
    assert len(index.arrays) == 0
    assert cache.current_bytes == 0
    cache.clear()
    assert cache.current_bytes == 0


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
    cache.clear()
    assert cache.current_bytes == 0


def test_resource_failure_propagates_instead_of_falling_back_to_older_model(gribs):
    old = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    newer = gribs.file(gribs.field(0, size=3), start=START + timedelta(hours=1), end=2)
    cache = DecodedBandCache(32)
    private = [private_source(source, cache) for source in (old, newer)]
    try:
        sampler = WeatherSampler(private)
        with pytest.raises(WeatherBandLimitError):
            sampler.sample_timestamps("wind", START.timestamp() + 3600, [0.5], [0.5])
    finally:
        for source in private:
            source.close()
    assert cache.current_bytes == 0
