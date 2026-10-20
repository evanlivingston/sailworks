"""GPU-resident numerical fields: a publish-once field, an exact selection.

A *field* is the immutable, geographical payload of one forecast window: the
native nodes of a stable spatial window, the endpoint values of the wind
parameter at those nodes, the per-node provenance, and the packed endpoint
images the render context publishes once.  Its identity is

    source + parameter + time window + spatial window + resolution

and deliberately **not** a per-frame image identity, so a field that is still
resident is published by reference instead of being sampled and uploaded
again.

The *selection* is the current-view portrayal: the node id drawn in each mesh
cell, its anchor and the count.  It is recomputed for every view and published
separately, so a covered camera move publishes selection traffic only.

The worker retains the immutable CPU payload; the GPU-resident resources (the
published images) belong to the render context.  Residency is accounted with
the byte-accounted LRU of :mod:`gweatherrouting.rendering.weathercache`, and
the RGBA8888 packing is charged at its real cost: every float32 of a field
becomes eight bytes of texture.
"""

from dataclasses import dataclass
import hashlib
from typing import TYPE_CHECKING

import numpy as np

from gweatherrouting.core.gribsampling import GridIndex
from gweatherrouting.rendering.weathercache import (
    ByteAccountedLRU,
    WeatherBandLimitError,
)

if TYPE_CHECKING:
    from PySide6.QtGui import QImage
    from pyproj import Transformer

    from gweatherrouting.rendering.weatherbarbs import BarbAtlas
    from gweatherrouting.rendering.weathergrid import GriddedField
    from gweatherrouting.rendering.weatherstaging import StagedWind

# Two opaque RGBA8888 texels carry one float32: eight bytes of texture for
# four bytes of value.
TEXTURE_BYTES_PER_FLOAT32 = 8

# The bounded width of a field's texture.  A long one-dimensional texture is
# not portable, so the field's nodes are laid out in rows of this width and
# the node id is the row-major index.
FIELD_COLUMNS = 64

# The accounted cost of one entry of the coordinate lookup, in bytes.  This is
# an estimate: a Python dictionary entry with a tuple key costs more than the
# two floats it holds, and the exact figure depends on the interpreter.
LOOKUP_ENTRY_BYTES_ESTIMATE = 120


@dataclass(frozen=True)
class NativeGridGeometry:
    """Read-only projection metadata, with no dataset, records or sampler.

    Reuse the sampler's geometry-only enumeration arithmetic. The optional
    pyproj transformers contain projection definitions, not GDAL handles.
    """

    width: int
    height: int
    transform: tuple
    inverse: tuple
    forward: "Transformer | None"
    backward: "Transformer | None"
    periodic: bool

    nodes = GridIndex.nodes
    pixel = GridIndex.pixel
    _geographic_nodes = GridIndex._geographic_nodes

    @classmethod
    def from_index(cls, index):
        return cls(
            index.width,
            index.height,
            tuple(index.transform),
            tuple(index.inverse),
            index.forward,
            index.backward,
            index.periodic,
        )

    def key(self):
        """Equal node geometry, not equal forecast values or source priority."""
        return (
            self.width,
            self.height,
            self.transform,
            self.inverse,
            self.periodic,
            None if self.forward is None else self.forward.definition,
            None if self.backward is None else self.backward.definition,
        )


@dataclass(frozen=True)
class FieldIdentity:
    """The stable identity of one numerical field.

    ``source`` is the source signature of the selected forecast files,
    ``parameter`` the sampled field name, ``before``/``after`` the resident
    time window (equal for an exact-instant field), ``window`` the geographic
    bounds the field covers and ``resolution`` the resolution of the grids the
    field is sampled on.
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
        """A short, stable resource identity for this identity."""
        digest = hashlib.sha256()
        digest.update(repr(self.source).encode("utf-8", "replace"))
        digest.update(f"|{self.parameter}|".encode())
        digest.update(f"{self.before!r}|{self.after!r}|".encode())
        digest.update(repr(self.window).encode("utf-8", "replace"))
        digest.update(f"|{self.resolution!r}".encode())
        return digest.hexdigest()[:16]


@dataclass
class GeographicField:
    """The resident CPU payload of one numerical field.

    The payload is immutable once staged.  ``index`` maps a native node to
    its node id; it is the metadata the current-view selection needs to bind
    a view's nodes to the field, and it is accounted for as an estimate.
    ``grid`` is the field's *gridded form*: the same values on a regular
    geographic lattice, which is what the GPU's fill interpolates.  The node
    list stays the barbs' form.
    """

    identity: FieldIdentity
    points: np.ndarray
    staged: "StagedWind"
    index: dict
    images: tuple
    generation: str
    grids: tuple = ()
    land_flags: np.ndarray | None = None
    land_zoom: float | None = None
    land_quality: str | None = None
    grid: "GriddedField | None" = None

    @property
    def node_count(self):
        return len(self.points)

    @property
    def value_bytes(self):
        return sum(
            getattr(value, "nbytes", 0)
            for value in (self.staged.first, self.staged.second)
        )

    @property
    def coordinate_bytes(self):
        return int(getattr(self.points, "nbytes", 0))

    @property
    def mask_bytes(self):
        return int(getattr(self.land_flags, "nbytes", 0))

    @property
    def lookup_bytes(self):
        """The accounted cost of the coordinate lookup (an estimate)."""
        return LOOKUP_ENTRY_BYTES_ESTIMATE * len(self.index)

    @property
    def texture_bytes(self):
        """The published endpoint images: eight bytes of texture per float32."""
        return sum(image.sizeInBytes() for image in self.images)

    @property
    def grid_bytes(self):
        """The gridded form's values and its published images."""
        return 0 if self.grid is None else self.grid.nbytes

    @property
    def nbytes(self):
        """The accounted cost of the retained payload."""
        return (
            self.value_bytes
            + self.coordinate_bytes
            + self.mask_bytes
            + self.lookup_bytes
            + self.texture_bytes
            + self.grid_bytes
        )

    def sources(self, node_ids):
        """The per-node source provenance of the given node ids."""
        return tuple(self.staged.sources[int(node)] for node in node_ids)


@dataclass(frozen=True)
class BarbSelection:
    """The current-view selection of one field.

    ``node_ids`` names, per mesh cell, the node of the field that is drawn
    there, or -1 for a cell without a symbol.  It is bound to the field by
    ``generation``: a selection of another field is stale and is rejected.
    """

    generation: str
    node_ids: np.ndarray
    anchors: tuple
    count: int
    field: GeographicField
    node_image: "QImage | None" = None

    @property
    def images(self):
        """The published selection images: the node ids and the anchors."""
        return (self.node_image,) + tuple(self.anchors)

    @property
    def shape(self):
        return self.node_ids.shape


def validate_binding(field, selection):
    """Reject a selection that does not belong to the published field."""
    if field.generation != selection.generation:
        raise ValueError("Stale field/selection generation")
    ids = selection.node_ids
    if np.any(ids < -1) or np.any(ids >= field.node_count):
        raise ValueError("Selection index outside the field")
    return selection


@dataclass(frozen=True)
class ResidentField:
    """Compatibility payload for the existing exact-view atlas cache."""

    identity: FieldIdentity
    payload: "BarbAtlas"
    values: tuple = ()

    @property
    def texture_bytes(self):
        return sum(image.sizeInBytes() for image in self.payload.images)

    @property
    def value_bytes(self):
        return sum(getattr(value, "nbytes", 0) for value in self.values)

    @property
    def nbytes(self):
        return self.texture_bytes + self.value_bytes

    @property
    def key(self):
        return self.identity.key()


class FieldStore:
    """Byte-budgeted residency for the CPU payloads of numerical fields.

    A hit means the field is reused as it is: no re-decode, no re-sample and
    no re-encode of the field's values.  It does **not** mean that nothing is
    published: the current-view selection is always rebuilt and published, and
    the published images of the field are counted separately by the render
    context.

    Eviction is least-recently-used, and the field that was stored last is
    protected, so the current content is never evicted before the older
    speculation.
    """

    def __init__(self, byte_limit, on_evict=None):
        self._lru = ByteAccountedLRU(byte_limit, on_evict=self._evicted)
        self._on_evict = on_evict
        self._protected = None
        self.hits = self.misses = 0
        self.released = 0
        self.rejected = 0

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
        return field.payload if isinstance(field, ResidentField) else field

    def fields(self):
        """Immutable snapshot of retained payload references (caller owns locking)."""
        return self._lru.values()

    def store(self, field, *, protect=True):
        """Retain ``field``, evicting the least recently used fields.

        A field that does not fit the declared budget is rejected: it is
        published, but it is not retained and it is not counted as resident.
        """
        self._lru.protected = field.identity if protect else self._protected
        try:
            self._lru.reserve(field.identity, field.nbytes)
        except WeatherBandLimitError:
            self._lru.protected = self._protected
            self.rejected += 1
            return False
        self._lru.store(field.identity, field)
        if protect:
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
            rejected=self.rejected,
            protected=self._protected is not None,
        )
        return stats


__all__ = [
    "BarbSelection",
    "FieldIdentity",
    "FieldStore",
    "GeographicField",
    "ResidentField",
    "FIELD_COLUMNS",
    "LOOKUP_ENTRY_BYTES_ESTIMATE",
    "TEXTURE_BYTES_PER_FLOAT32",
    "WeatherBandLimitError",
    "validate_binding",
]
