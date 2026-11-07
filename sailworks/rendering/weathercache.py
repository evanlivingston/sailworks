"""Session-scoped, byte-accounted decoded forecast data.

Decoded GRIB bands are immutable NumPy arrays.  A session owns one
:class:`DecodedBandCache` shared by every display worker, so a band requested
by wind, wave fill, wave arrows and chart pressure is decoded once while it is
resident.  Workers keep their own GDAL handles: only the immutable result and
the source metadata are shared.

The cache is exact-identity keyed: a key is the source file identity (path and
on-disk version) plus the raster band.  A changed source therefore cannot hit
an older entry, and eviction removes the retained array from every consumer's
local view instead of exposing it again.

:class:`ByteAccountedLRU` is the single byte-accounted LRU of the renderer.
The decoded-band cache and the GPU-resident field store both use it, so a
second, differently accounted budget cannot appear.
"""

from collections import OrderedDict
import os
from threading import Condition, RLock
import weakref

import numpy as np

from sailworks.core.grib import Grib
from sailworks.core.gribsampling import GridIndex


class WeatherBandLimitError(Exception):
    """Resource failure, deliberately not a recoverable sampler exception."""


# One explicit budget for every display consumer in a forecast session.  It is
# larger than one worker's former 128 MiB because the formerly separate wind,
# wave and chart budgets now share it; the total is still well below their sum
# and every retained band is accounted in bytes.
SESSION_BYTE_LIMIT = 256 * 1024 * 1024


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

    def peek(self, key):
        """The retained value without changing its recency."""
        return self._entries.get(key)

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


def file_identity(path):
    """The exact on-disk identity of one forecast file.

    The device/inode/size/mtime tuple distinguishes an in-place replacement
    from the file that was decoded, so no cache or metadata entry can cross a
    source change.
    """
    path = os.path.abspath(os.fspath(path))
    try:
        stat = os.stat(path)
        version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except OSError:
        version = None
    return (path, version)


def source_identity(source):
    """The exact identity of one opened forecast source for decoded bands."""
    return (
        file_identity(source.path),
        source.name,
        source.centre,
        source.start_time,
        source.last_forecast,
    )


class _DecodedBand:
    """One immutable decoded plane and the indices that currently retain it."""

    __slots__ = ("value", "owners")

    def __init__(self, value):
        self.value = value
        self.owners = weakref.WeakSet()

    @property
    def nbytes(self):
        return self.value.nbytes


class DecodedBandCache:
    """One session's shared, thread-safe, byte-accounted decoded bands.

    ``read`` is the decode path: the first caller for a key decodes with its
    own GDAL handle while concurrent callers for the same exact key wait and
    then share the result.  Arrays are marked read-only before publication.
    ``clear`` ends the session's retention.
    """

    def __init__(self, byte_limit=128 * 1024 * 1024, limit_phrase="worker limit"):
        if byte_limit <= 0:
            raise ValueError("Decoded-band budget must be positive")
        self._lru = ByteAccountedLRU(
            byte_limit,
            on_evict=self._evicted,
            label="Decoded weather band",
            limit_phrase=limit_phrase,
        )
        self._lock = RLock()
        self._ready = Condition(self._lock)
        self._loading = set()
        self._closed = False
        self.decodes = 0
        self.requests = 0
        self.deduplicated = 0

    # ---------------------------------------------------------------- keys
    @staticmethod
    def key(owner, band):
        identity = getattr(owner, "cache_identity", None)
        if identity is None:
            identity = id(owner)
        return (identity, band)

    # --------------------------------------------------------------- reads
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

    def read(self, owner, key, size, loader):
        """The retained band for ``key``, decoding it at most once.

        ``loader`` runs with the cache lock released.  A caller that arrives
        while another owner decodes the same key waits for that decode instead
        of repeating it.
        """
        with self._ready:
            self.requests += 1
            while True:
                entry = self._lru.get(key)
                if entry is not None:
                    self._lru.hits += 1
                    self._attach(owner, key, entry)
                    return entry.value
                if key not in self._loading:
                    self._loading.add(key)
                    try:
                        self._lru.reserve(key, size)
                    except BaseException:
                        self._loading.discard(key)
                        self._ready.notify_all()
                        raise
                    break
                self.deduplicated += 1
                self._ready.wait()
        try:
            value = loader()
        except BaseException:
            with self._ready:
                self._loading.discard(key)
                self._ready.notify_all()
            raise
        if isinstance(value, np.ndarray) and value.flags.writeable:
            value.setflags(write=False)
        with self._ready:
            self.decodes += 1
            self._loading.discard(key)
            try:
                if not self._closed:
                    entry = _DecodedBand(value)
                    self._lru.store(key, entry)
                    self._attach(owner, key, entry)
                    if not entry.owners:
                        # The sole consumer closed or cancelled while this
                        # decode was in flight; do not retain it unattached.
                        self._lru.discard(key)
            finally:
                self._ready.notify_all()
        return value

    def _attach(self, owner, key, entry):
        """Record that ``owner`` retains ``entry`` in its bounded local view."""
        if hasattr(owner, "dataset") and owner.dataset is None:
            # A closed consumer must not repopulate its local view after the
            # decode it cancelled has returned.
            return
        entry.owners.add(owner)
        arrays = getattr(owner, "arrays", None)
        if arrays is not None:
            arrays._local_set(key[1], entry.value)
            dropped = arrays._local_trim(arrays.limit)
            for band in dropped:
                if band != key[1]:
                    self._release_entry(owner, (key[0], band))

    def _release_entry(self, owner, key):
        entry = self._lru.peek(key)
        if entry is None:
            return
        entry.owners.discard(owner)
        if not entry.owners:
            # Nobody retains the plane: release the bytes instead of keeping
            # an unreferenced entry until the next eviction.
            self._lru.discard(key)

    def release(self, owner, key):
        """Drop one owner's retention; the entry stays available to others."""
        with self._ready:
            self._release_entry(owner, key)

    def _evicted(self, key, entry):
        """Remove an evicted band from every consumer's local view."""
        band = key[1]
        for owner in tuple(entry.owners):
            arrays = getattr(owner, "arrays", None)
            if arrays is not None:
                arrays._local_discard(band)
        entry.owners.clear()

    # ---------------------------------------------------------- lifecycle
    def clear(self):
        """Release every retained band and stop retaining new ones."""
        with self._ready:
            if self._closed:
                return 0
            self._closed = True
            count = self._lru.clear()
            self._loading.clear()
            self._ready.notify_all()
            return count

    close = clear

    def statistics(self):
        statistics = self._lru.statistics()
        statistics["retained_bands"] = statistics["retained_entries"]
        del statistics["retained_entries"]
        statistics.update(
            decodes=self.decodes,
            requests=self.requests,
            deduplicated=self.deduplicated,
            in_flight=len(self._loading),
            closed=self._closed,
        )
        return statistics


class SourceBandCache(OrderedDict):
    """One index's bounded local view of the shared decoded-band cache.

    The local dictionary and the session cache reference the same immutable
    arrays, not copies.  Dropping a local entry releases only this owner's
    reference; the session entry stays while another consumer retains it.
    """

    def __init__(self, cache, owner, limit=6):
        super().__init__()
        self.cache, self.owner = cache, owner
        self.limit = int(limit)

    def _key(self, band):
        return self.cache.key(self.owner, band)

    def __delitem__(self, band):
        super().__delitem__(band)
        self.cache.release(self.owner, self._key(band))

    def popitem(self, last=True):
        band, value = super().popitem(last=last)
        self.cache.release(self.owner, self._key(band))
        return band, value

    def clear(self):
        for band in list(self):
            del self[band]

    def _local_set(self, band, value):
        super().__setitem__(band, value)
        super().move_to_end(band)

    def _local_trim(self, limit):
        dropped = []
        while len(self) > limit:
            band, _value = super().popitem(last=False)
            dropped.append(band)
        return dropped

    def _local_discard(self, band):
        """Eviction already accounted for this entry; only drop the view."""
        if band in self:
            super().__delitem__(band)


class DisplayGridIndex(GridIndex):
    def __init__(self, source, cache):
        super().__init__(source)
        self.band_cache = cache
        self.cache_identity = source_identity(source)
        self.arrays = SourceBandCache(cache, self)

    def array(self, record):
        if self.dataset is None:
            raise ValueError("Forecast handle is closed")
        key = self.band_cache.key(self, record.band)
        return self.band_cache.read(
            self,
            key,
            self.width * self.height * 8,
            lambda: GridIndex.array(self, record),
        )


class DisplayWeatherSource(Grib):
    def __init__(self, *args, band_cache, display_identity=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.band_cache = band_cache
        self.display_identity = display_identity

    @property
    def sampling(self):
        if "_sampling" not in self.__dict__:
            from .displayforecast import restrict_index

            self._sampling = restrict_index(
                DisplayGridIndex(self, self.band_cache), self.display_identity
            )
        return self._sampling


class ForecastSession:
    """Session-level immutable forecast metadata and decoded bands.

    One session is shared by every display consumer of the same selected
    forecasts.  Metadata is parsed once per exact file version; decoded bands
    live in one byte-accounted cache.  Each consumer still opens its own GDAL
    handles through :class:`DisplayWeatherSource`.
    """

    def __init__(self, byte_limit=SESSION_BYTE_LIMIT):
        self.band_cache = DecodedBandCache(byte_limit, limit_phrase="session limit")
        self._lock = RLock()
        self._ready = Condition(self._lock)
        self._metadata = {}
        self._parsing = {}
        self._closed = False
        self.metadata_parses = 0
        self.metadata_deduplicated = 0

    def metadata(self, path):
        """The cached metadata of ``path`` at its current on-disk version.

        After the session is closed, metadata is parsed without being retained
        so a worker that is still unwinding cannot be exposed to a stale or
        cached failure; its decoded bands are likewise not retained.
        """
        key = file_identity(path)
        closed = False
        with self._ready:
            metadata = self._metadata.get(key)
            if metadata is not None:
                return metadata
            closed = self._closed
            waiting = (not closed) and key in self._parsing
            if not closed and not waiting:
                self._parsing[key] = True
        if closed:
            return Grib.parse_metadata(path)
        if waiting:
            with self._ready:
                self.metadata_deduplicated += 1
                while key in self._parsing:
                    self._ready.wait()
                metadata = self._metadata.get(key)
            if metadata is not None:
                return metadata
            # The first parse failed; this caller retries rather than
            # inheriting a cached failure.
        try:
            metadata = Grib.parse_metadata(path)
        except BaseException:
            with self._ready:
                self._parsing.pop(key, None)
                self._ready.notify_all()
            raise
        with self._ready:
            if not self._closed:
                self.metadata_parses += 1
                self._metadata[key] = metadata
            self._parsing.pop(key, None)
            self._ready.notify_all()
        return metadata

    def source(self, path, display_identity=None):
        """A private GDAL source over the session's shared decoded bands."""
        expected = file_identity(path)
        metadata = self.metadata(path)
        source = DisplayWeatherSource(
            metadata.path,
            metadata.name,
            metadata.centre,
            metadata.bounds,
            metadata.start_time,
            metadata.last_forecast,
            metadata.model_name,
            band_cache=self.band_cache,
            display_identity=display_identity,
        )
        if file_identity(path) != expected:
            source.close()
            raise ValueError("Forecast file changed while it was being opened")
        return source

    def close(self):
        with self._ready:
            if self._closed:
                return
            self._closed = True
            self._metadata.clear()
            self._parsing.clear()
            self._ready.notify_all()
        self.band_cache.close()

    def statistics(self):
        with self._ready:
            parsed = len(self._metadata)
            parses = self.metadata_parses
            deduplicated = self.metadata_deduplicated
        return dict(
            metadata_entries=parsed,
            metadata_parses=parses,
            metadata_deduplicated=deduplicated,
            bands=self.band_cache.statistics(),
        )
