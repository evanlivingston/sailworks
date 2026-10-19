"""Byte-accounted decoded bands for private numerical-display workers only.

All access stays on the owning worker. Shared routing/sampling sources retain
their existing GridIndex behavior. This does not bound GDAL/decode temporaries.
"""

from collections import OrderedDict

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import GridIndex


class WeatherBandLimitError(Exception):
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
            raise WeatherBandLimitError(
                f"Decoded weather band needs {size} bytes; worker limit is {self.byte_limit}"
            )
        while self.current_bytes + size > self.byte_limit:
            (previous_owner, previous_band), value = self._entries.popitem(last=False)
            previous_owner.arrays.discard_local(previous_band)
            self.current_bytes -= value.nbytes
            self.evictions += 1
        self.misses += 1

    def store(self, owner, band, value):
        key = (owner, band)
        old = self._entries.get(key)
        previous_bytes = old.nbytes if old is not None else 0
        if self.current_bytes - previous_bytes + value.nbytes > self.byte_limit:
            raise WeatherBandLimitError("Decoded weather band exceeded its reservation")
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


class SourceBandCache(OrderedDict):
    """GridIndex's concrete OrderedDict contract, with shared LRU accounting.

    The local dictionary and global LRU reference the same arrays, not copies.
    """

    def __init__(self, cache, owner):
        super().__init__()
        self.cache, self.owner = cache, owner

    def __setitem__(self, band, value):
        self.cache.store(self.owner, band, value)
        super().__setitem__(band, value)

    def __delitem__(self, band):
        self.cache.discard(self.owner, band)
        super().__delitem__(band)

    def move_to_end(self, band, last=True):
        super().move_to_end(band, last=last)
        self.cache._entries.move_to_end((self.owner, band), last=last)

    def popitem(self, last=True):
        band, value = super().popitem(last=last)
        self.cache.discard(self.owner, band)
        return band, value

    def clear(self):
        for band in list(self):
            del self[band]

    def discard_local(self, band):
        """The global LRU already removed/accounted for this entry."""
        super().__delitem__(band)


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
