"""Byte-bounded residency of decoded weather bands and whole-grid fields.

Every cache is accounted by exact bytes, evicts the oldest entry first and
keeps at least one (or ``min_entries``) alive, so a sampling pass never
thrashes.  The reused launcher fixture builds real GeoTIFFs.
"""

from types import SimpleNamespace
import unittest

import numpy as np

from sailworks.core.grib import Grib, RindexData, _RindexCache
from sailworks.core.gribsampling import GridIndex, SourceBandLRU
from test import test_grib_sampling as fixture

SHAPE = (4, 8)
COUNT = int(np.prod(SHAPE))
BAND_BYTES = COUNT * 8
ENTRY_BYTES = 2 * BAND_BYTES  # decoded u and v
GRID_BYTES = 2 * BAND_BYTES  # lat and lon shared by every hour


def band(value=0.0):
    return np.full(SHAPE, value, dtype=np.float64)


def rindex(value, lat, lon):
    u = np.full(SHAPE, value, dtype=np.float64)
    v = np.full(SHAPE, value + 1.0, dtype=np.float64)
    return RindexData(lat, lon, u.ravel(), v.ravel())


class TestSourceBandBudget(unittest.TestCase):
    def test_byte_budget_evicts_oldest_and_accounts_exactly(self):
        cache = SourceBandLRU(byte_limit=2 * BAND_BYTES, min_entries=1)
        for key in (1, 2):
            cache[key] = band(key)
        self.assertEqual(cache.current_bytes, 2 * BAND_BYTES)
        self.assertEqual(cache.peak_bytes, 2 * BAND_BYTES)
        self.assertEqual(cache.evictions, 0)

        cache[3] = band(3)
        self.assertEqual(sorted(cache), [2, 3])
        self.assertEqual(cache.evictions, 1)
        self.assertEqual(cache.current_bytes, 2 * BAND_BYTES)
        self.assertEqual(cache.peak_bytes, 3 * BAND_BYTES)

        cache[4] = band(4)
        self.assertEqual(sorted(cache), [3, 4])
        self.assertEqual(cache.evictions, 2)
        self.assertEqual(cache.current_bytes, sum(a.nbytes for a in cache.values()))

    def test_min_entries_retains_four_bands_with_a_tiny_budget(self):
        cache = SourceBandLRU(byte_limit=1, min_entries=4)
        for key in range(5):
            cache[key] = band(key)
        self.assertEqual(sorted(cache), [1, 2, 3, 4])
        self.assertEqual(len(cache), 4)
        self.assertEqual(cache.evictions, 1)
        self.assertEqual(cache.current_bytes, 4 * BAND_BYTES)
        self.assertEqual(cache.statistics()["min_entries"], 4)


class TestGridIndexResidency(unittest.TestCase):
    setUp = fixture.TestGribSampling.setUp
    file = fixture.TestGribSampling.file
    field = fixture.TestGribSampling.field

    def index(self, byte_limit=64):
        source = self.file(self.field(0))
        index = GridIndex(source)
        index.arrays.byte_limit = byte_limit
        index.arrays.min_entries = 1
        return index

    def test_touched_band_survives_the_next_insertion(self):
        index = self.index()
        records = {record.band: record for record in index.records}
        self.assertEqual(sorted(records), [1, 2, 3])

        index.array(records[1])
        index.array(records[2])
        self.assertIs(index.array(records[1]), index.array(records[1]))
        index.array(records[3])

        self.assertEqual(sorted(index.arrays), [1, 3])
        self.assertEqual(index.arrays.evictions, 1)

    def test_band_decodes_once_across_repeated_calls(self):
        index = self.index()
        record = next(record for record in index.records if record.band == 1)
        first = index.array(record)
        self.assertIs(index.array(record), first)
        self.assertIs(index.array(record), first)
        stats = index.arrays.statistics()
        self.assertEqual(stats["decodes"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["hits"], 2)
        self.assertEqual(stats["entries"], 1)


class TestRindexCache(unittest.TestCase):
    def cache(self, byte_limit):
        lat = np.linspace(-5.0, 5.0, int(np.prod(SHAPE)))
        lon = np.linspace(-180.0, 180.0, int(np.prod(SHAPE)))
        cache = _RindexCache(byte_limit=byte_limit)
        cache.share(SHAPE, lat, lon)
        return cache, lat, lon

    def test_budget_evicts_oldest_and_shares_lat_lon_once(self):
        cache, lat, lon = self.cache(GRID_BYTES + ENTRY_BYTES)
        cache.put(0, rindex(1.0, lat, lon))
        cache.put(3, rindex(2.0, lat, lon))

        stats = cache.statistics()
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["evictions"], 1)
        self.assertEqual(cache.current_bytes, GRID_BYTES + ENTRY_BYTES)
        self.assertEqual(stats["current_bytes"], sum_bytes(cache))
        # Room is made before the new hour is stored, so the budget is never
        # exceeded even transiently.
        self.assertEqual(stats["peak_bytes"], GRID_BYTES + ENTRY_BYTES)

    def test_at_least_one_entry_survives_an_impossible_budget(self):
        cache, lat, lon = self.cache(1)
        cache.put(0, rindex(1.0, lat, lon))
        self.assertEqual(cache.statistics()["entries"], 1)
        self.assertEqual(cache.evictions, 0)

    def test_hits_and_misses_count_lookups(self):
        cache, lat, lon = self.cache(10**6)
        self.assertIsNone(cache.get(0))
        data = rindex(1.0, lat, lon)
        cache.put(0, data)
        self.assertIs(cache.get(0), data)
        self.assertEqual((cache.hits, cache.misses), (1, 1))

    def test_clear_resets_bytes_and_entries(self):
        cache, lat, lon = self.cache(10**6)
        cache.put(0, rindex(1.0, lat, lon))
        cache.get(0)
        cache.clear()
        stats = cache.statistics()
        self.assertEqual(stats["entries"], 0)
        self.assertEqual(stats["current_bytes"], 0)
        self.assertIsNone(cache.get(0))


def sum_bytes(cache):
    entries = sum(data.u.nbytes + data.v.nbytes for data, _ in cache._entries.values())
    return cache._shared_bytes + entries


class _CountingBand:
    def __init__(self, values):
        self.values = values
        self.reads = 0

    def ReadAsArray(self):
        self.reads += 1
        return self.values.copy()


class TestGribRindexResidency(unittest.TestCase):
    GEOTRANSFORM = (-180.0, 0.25, 0.0, 90.0, 0.0, -0.25)

    def setUp(self):
        grib = object.__new__(Grib)
        grib._rindex_cache = _RindexCache(byte_limit=GRID_BYTES + ENTRY_BYTES)
        grib._rindex_grid = None
        grib.dataset = SimpleNamespace(
            GetGeoTransform=lambda: self.GEOTRANSFORM,
        )
        self.bands = {}
        for hour in (0, 3):
            self.bands[hour] = (
                _CountingBand(np.full(SHAPE, 3.0 + hour)),
                _CountingBand(np.full(SHAPE, 4.0 + hour)),
            )
        grib._find_bands_for_time = lambda hour: self.bands[hour]
        self.grib = grib

    def reads(self, hour=(0, 3)):
        return sum(band.reads for key in hour for band in self.bands.get(key, ()))

    def test_same_hour_decodes_each_band_once(self):
        first = self.grib.get_rindex_data(0)
        second = self.grib.get_rindex_data(0)

        self.assertIs(first, second)
        self.assertEqual(self.reads(), 2)
        np.testing.assert_array_equal(first.u, second.u)
        np.testing.assert_array_equal(first.v, second.v)
        stats = self.grib._rindex_cache.statistics()
        self.assertEqual(stats["decodes"], 1)
        self.assertEqual(stats["hits"], 1)

    def test_two_hours_share_the_same_lat_lon_objects(self):
        first = self.grib.get_rindex_data(0)
        second = self.grib.get_rindex_data(3)

        self.assertIs(first.lat, second.lat)
        self.assertIs(first.lon, second.lon)
        self.assertFalse(np.array_equal(first.u, second.u))
        self.assertEqual(self.reads(), 4)
        self.assertEqual(self.grib._rindex_cache.statistics()["evictions"], 1)

    def test_clearing_the_cache_empties_it(self):
        self.grib.get_rindex_data(0)
        self.grib._rindex_cache.clear()
        stats = self.grib._rindex_cache.statistics()
        self.assertEqual(stats["entries"], 0)
        self.assertEqual(stats["current_bytes"], 0)

    def test_lat_lon_formula_is_bit_identical_to_prechange_expression(self):
        data = self.grib.get_rindex_data(0)
        gt = self.GEOTRANSFORM
        ys, xs = np.indices(SHAPE, dtype=np.float64)
        lat = (gt[3] + (xs + 0.5) * gt[4] + (ys + 0.5) * gt[5]).ravel()
        lon = (gt[0] + (xs + 0.5) * gt[1] + (ys + 0.5) * gt[2]).ravel()
        np.testing.assert_array_equal(data.lat, lat)
        np.testing.assert_array_equal(data.lon, lon)
