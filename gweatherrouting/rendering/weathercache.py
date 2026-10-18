"""Byte-accounted decoded bands for private numerical-display workers only.

All access stays on the owning worker. Shared routing/sampling sources retain
their existing GridIndex behavior. This does not bound GDAL/decode temporaries.
"""

from collections import OrderedDict
from collections.abc import MutableMapping

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import GridIndex


class WeatherBandLimitExceeded(Exception):
    """Resource failure, deliberately not a recoverable sampler exception."""


class DecodedBandCache:
    def __init__(self, byte_limit=128 * 1024 * 1024):
        if byte_limit <= 0:
            raise ValueError("Decoded-band budget must be positive")
        self.byte_limit = byte_limit
        self._entries = OrderedDict()
        self.current_bytes = self.peak_bytes = 0
        self.hits = self.misses = self.evictions = 0

    def reserve(self, owner, band, size):
        if (owner, band) in self._entries:
            self.hits += 1
            return
        if size > self.byte_limit:
            raise WeatherBandLimitExceeded(
                f"Decoded weather band needs {size} bytes; worker limit is {self.byte_limit}"
            )
        while self.current_bytes + size > self.byte_limit:
            _, value = self._entries.popitem(last=False)
            self.current_bytes -= value.nbytes
            self.evictions += 1
        self.misses += 1

    def store(self, owner, band, value):
        key = (owner, band)
        old = self._entries.get(key)
        previous_bytes = old.nbytes if old is not None else 0
        if self.current_bytes - previous_bytes + value.nbytes > self.byte_limit:
            raise WeatherBandLimitExceeded(
                "Decoded weather band exceeded its reservation"
            )
        self._entries[key] = value
        self._entries.move_to_end(key)
        self.current_bytes += value.nbytes - previous_bytes
        self.peak_bytes = max(self.peak_bytes, self.current_bytes)

    def discard(self, owner, band):
        self.current_bytes -= self._entries.pop((owner, band)).nbytes

    def statistics(self):
        return dict(
            limit_bytes=self.byte_limit,
            current_bytes=self.current_bytes,
            peak_bytes=self.peak_bytes,
            retained_bands=len(self._entries),
            hits=self.hits,
            misses=self.misses,
            evictions=self.evictions,
        )


class SourceBandCache(MutableMapping):
    """Per-index OrderedDict interface backed by the shared worker LRU."""

    def __init__(self, cache, owner):
        self.cache, self.owner = cache, owner

    def __getitem__(self, band):
        return self.cache._entries[self.owner, band]

    def __setitem__(self, band, value):
        self.cache.store(self.owner, band, value)

    def __delitem__(self, band):
        self.cache.discard(self.owner, band)

    def __iter__(self):
        return iter(
            [band for owner, band in self.cache._entries if owner is self.owner]
        )

    def __len__(self):
        return sum(owner is self.owner for owner, _ in self.cache._entries)

    def move_to_end(self, band):
        self.cache._entries.move_to_end((self.owner, band))

    def popitem(self, last=True):
        keys = list(self)
        if not keys:
            raise KeyError("No decoded bands for source")
        band = keys[-1] if last else keys[0]
        value = self[band]
        del self[band]
        return band, value


class DisplayGridIndex(GridIndex):
    def __init__(self, source, cache):
        super().__init__(source)
        self.band_cache = cache
        self.arrays = SourceBandCache(cache, self)

    def array(self, record):
        if self.dataset is None:
            raise ValueError("Forecast handle is closed")
        # GridIndex decodes float64 and applies the same missing-data handling.
        self.band_cache.reserve(self, record.band, self.width * self.height * 8)
        return super().array(record)


class DisplayWeatherSource(Grib):
    def __init__(self, *args, band_cache, **kwargs):
        super().__init__(*args, **kwargs)
        self.band_cache = band_cache

    @property
    def sampling(self):
        if "_sampling" not in self.__dict__:
            self._sampling = DisplayGridIndex(self, self.band_cache)
        return self._sampling
