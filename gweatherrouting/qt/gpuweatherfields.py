"""GPU-resident numerical fields: a stable identity and an explicit budget.

A *field* is the immutable numerical payload of one forecast window: the two
endpoint instants of the resident time window, the spatial window it covers and
the resolution of the mesh it is published on.  Its identity is

    source + parameter + time window + spatial window + resolution

and deliberately **not** a per-frame image identity, so a field that is still
resident is re-published by reference instead of being sampled, encoded and
uploaded again.

The store is owned by the numerical worker.  Residency is accounted with the
byte-accounted LRU of :mod:`gweatherrouting.rendering.weathercache`; the
RGBA8888 transport expands every float32 into two opaque texels, so a resident
field costs eight bytes of texture for every four bytes of value and that
expansion is part of the declared budget.
"""

from dataclasses import dataclass
import hashlib

from gweatherrouting.rendering.weathercache import (
    ByteAccountedLRU,
    WeatherBandLimitError,
)

# Two opaque RGBA8888 texels carry one float32: eight bytes of texture for
# four bytes of value.
TEXTURE_BYTES_PER_FLOAT32 = 8

# A resident field is published as four endpoint images (u/v at two instants)
# plus two anchor images: six float32 per mesh cell.
TEXTURE_BYTES_PER_CELL = 6 * TEXTURE_BYTES_PER_FLOAT32


@dataclass(frozen=True)
class FieldIdentity:
    """The stable identity of one numerical field.

    ``source`` is the source signature of the selected forecast files,
    ``parameter`` the sampled field name, ``before``/``after`` the resident
    time window (equal for an exact-instant field), ``window`` the geographic
    bounds the field covers and ``resolution`` the published mesh shape.
    """

    source: tuple
    parameter: str
    before: float
    after: float
    window: tuple
    resolution: tuple

    @property
    def time_window(self):
        return (self.before, self.after)

    @property
    def exact(self):
        """True for a field that serves one instant only."""
        return self.before == self.after

    def contains(self, stamp):
        """The instants this field can serve, with exact-time semantics."""
        if self.exact:
            return stamp == self.before
        return self.before < stamp < self.after

    def key(self):
        """A short, stable publication key for this identity.

        The same field always produces the same key, so a field that is still
        resident keeps its published URLs and is not uploaded again.
        """
        digest = hashlib.sha256()
        digest.update(repr(self.source).encode("utf-8", "replace"))
        digest.update(f"|{self.parameter}|".encode())
        digest.update(f"{self.before!r}|{self.after!r}|".encode())
        digest.update(repr(self.window).encode("utf-8", "replace"))
        digest.update(f"|{self.resolution!r}".encode())
        return digest.hexdigest()[:16]


@dataclass
class ResidentField:
    """One resident field: the published payload and its numerical values."""

    identity: FieldIdentity
    payload: object
    values: tuple = ()

    @property
    def texture_bytes(self):
        images = getattr(self.payload, "images", ())
        return sum(image.sizeInBytes() for image in images)

    @property
    def value_bytes(self):
        return sum(getattr(value, "nbytes", 0) for value in self.values)

    @property
    def nbytes(self):
        """The accounted cost: the texture bytes plus the numerical values."""
        return self.texture_bytes + self.value_bytes

    @property
    def key(self):
        return self.identity.key()


class FieldStore:
    """Byte-budgeted residency for numerical fields.

    The store holds the fields a worker has already paid for.  A hit means
    the field is published again by reference: no re-decode, no re-sample, no
    re-encode and no re-upload.  Eviction is least-recently-used, and the
    field that was stored last is protected, so the current content is never
    evicted before the older speculation.
    """

    def __init__(self, byte_limit, on_evict=None):
        self._lru = ByteAccountedLRU(byte_limit, on_evict=self._evicted)
        self._on_evict = on_evict
        self._protected = None
        self.hits = self.misses = 0
        self.released = 0

    @property
    def byte_limit(self):
        return self._lru.byte_limit

    @property
    def current_bytes(self):
        return self._lru.current_bytes

    def _evicted(self, key, value):
        if self._on_evict is not None:
            self._on_evict(key, value)

    def protect(self, identity):
        """Pin the field that is currently published against eviction."""
        self._protected = identity
        self._lru.protected = identity

    def acquire(self, identity):
        """The resident field for ``identity``, or None on a miss."""
        field = self._lru.get(identity)
        if field is None:
            self.misses += 1
            return None
        self.hits += 1
        return field.payload

    def store(self, field):
        """Retain ``field``, evicting the least recently used fields.

        The retained field becomes the protected one: the visible content is
        never evicted before the older speculation.  A field that does not
        fit the declared budget is published but not retained.
        """
        self._lru.protected = field.identity
        try:
            self._lru.reserve(field.identity, field.nbytes)
        except WeatherBandLimitError:
            self._lru.protected = self._protected
            return False
        self._lru.store(field.identity, field)
        self.protect(field.identity)
        return True

    def release(self, identity):
        """Drop one field, for example after a source change."""
        if self._lru.remove(identity):
            self.released += 1
            return True
        return False

    def clear(self):
        """Release every resident field."""
        self.released += self._lru.clear()
        self._protected = None
        self._lru.protected = None

    def statistics(self):
        stats = self._lru.statistics()
        stats.update(
            acquire_hits=self.hits,
            acquire_misses=self.misses,
            released=self.released,
            protected=self._protected is not None,
        )
        return stats


__all__ = [
    "FieldIdentity",
    "FieldStore",
    "ResidentField",
    "TEXTURE_BYTES_PER_CELL",
    "TEXTURE_BYTES_PER_FLOAT32",
    "WeatherBandLimitError",
]
