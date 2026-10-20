"""Byte-accounted decoded bands for private numerical-display workers only.

All access stays on the owning worker. Shared routing/sampling sources retain
their existing GridIndex behavior. This does not bound GDAL/decode temporaries.

:class:`ByteAccountedLRU` is the single byte-accounted LRU of the renderer.
The decoded-band cache and the GPU-resident field store both use it, so a
second, differently accounted budget cannot appear.
"""

from collections import OrderedDict

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import GridIndex


class WeatherBandLimitError(Exception):
    """Resource failure, deliberately not a recoverable sampler exception."""


class ByteAccountedLRU:
    """Least-recently-used retention with one explicit byte budget.

    The budget is declared once, every retained entry is accounted in bytes
    and eviction is least-recently-used.  One entry may be protected: the
    entry that is currently published is never evicted, so speculation is
    reduced before selected content is.
    """

    def __init__(
        self,
        byte_limit,
        on_evict=None,
        label="Retained resource",
        limit_phrase="limit",
    ):
        if byte_limit <= 0:
            raise ValueError("Byte budget must be positive")
        self.byte_limit = byte_limit
        self.on_evict = on_evict
        self.label = label
        self.limit_phrase = limit_phrase
        self.protected = None
        self._entries = OrderedDict()
        self.current_bytes = self.peak_bytes = 0
        self.hits = self.misses = self.evictions = 0

    def limit_error(self, size):
        return WeatherBandLimitError(
            f"{self.label} needs {size} bytes; "
            f"{self.limit_phrase} is {self.byte_limit}"
        )

    def _victim(self):
        for key in self._entries:
            if key != self.protected:
                return key
        return None

    def reserve(self, key, size):
        """Make room for ``size`` bytes, evicting the oldest entries."""
        if key in self._entries:
            self.hits += 1
            return
        if size > self.byte_limit:
            raise self.limit_error(size)
        while self.current_bytes + size > self.byte_limit:
            victim = self._victim()
            if victim is None:
                raise self.limit_error(size)
            value = self._entries.pop(victim)
            self.current_bytes -= value.nbytes
            self.evictions += 1
            if self.on_evict is not None:
                self.on_evict(victim, value)
        self.misses += 1

    def store(self, key, value):
        """Retain ``value``; it must fit the budget it reserved."""
        old = self._entries.get(key)
        previous_bytes = old.nbytes if old is not None else 0
        if self.current_bytes - previous_bytes + value.nbytes > self.byte_limit:
            raise WeatherBandLimitError(f"{self.label} exceeded its reservation")
        self._entries[key] = value
        self._entries.move_to_end(key)
        self.current_bytes += value.nbytes - previous_bytes
        self.peak_bytes = max(self.peak_bytes, self.current_bytes)

    def get(self, key):
        """The retained value, moved to the most recently used end."""
        value = self._entries.get(key)
        if value is None:
            return None
        self._entries.move_to_end(key)
        return value

    def values(self):
        """Read-only snapshot of retained values, least to most recently used.

        Callers sharing a store between threads must hold their own lock; the
        tuple is a snapshot of the entries, not a copy of the values.
        """
        return tuple(self._entries.values())

    def discard(self, key):
        """Remove one entry and release its bytes."""
        value = self._entries.pop(key)
        self.current_bytes -= value.nbytes
        return value

    def remove(self, key):
        """Remove one entry if it is retained."""
        try:
            self.discard(key)
        except KeyError:
            return False
        return True

    def clear(self):
        """Release every retained entry; returns the number released."""
        count = len(self._entries)
        for key, value in list(self._entries.items()):
            self.current_bytes -= value.nbytes
            if self.on_evict is not None:
                self.on_evict(key, value)
        self._entries.clear()
        return count

    def statistics(self):
        return dict(
            limit_bytes=self.byte_limit,
            current_bytes=self.current_bytes,
            peak_bytes=self.peak_bytes,
            retained_entries=len(self._entries),
            hits=self.hits,
            misses=self.misses,
            evictions=self.evictions,
        )


class DecodedBandCache:
    def __init__(self, byte_limit=128 * 1024 * 1024):
        if byte_limit <= 0:
            raise ValueError("Decoded-band budget must be positive")
        self._lru = ByteAccountedLRU(
            byte_limit,
            on_evict=self._evict,
            label="Decoded weather band",
            limit_phrase="worker limit",
        )

    def _evict(self, key, value):
        owner, band = key
        owner.arrays.discard_local(band)

    @property
    def byte_limit(self):
        return self._lru.byte_limit

    @property
    def current_bytes(self):
        return self._lru.current_bytes

    @property
    def peak_bytes(self):
        return self._lru.peak_bytes

    @property
    def hits(self):
        return self._lru.hits

    @property
    def misses(self):
        return self._lru.misses

    @property
    def evictions(self):
        return self._lru.evictions

    def reserve(self, owner, band, size):
        self._lru.reserve((owner, band), size)

    def store(self, owner, band, value):
        self._lru.store((owner, band), value)

    def discard(self, owner, band):
        self._lru.discard((owner, band))

    def statistics(self):
        statistics = self._lru.statistics()
        statistics["retained_bands"] = statistics.pop("retained_entries")
        return statistics


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
        self.cache._lru._entries.move_to_end((self.owner, band), last=last)

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
