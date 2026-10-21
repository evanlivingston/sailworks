"""The viewport's ENC geometry cache: the prepared features, kept.

The application's ENC portrayal prepares every feature it draws: the
geometry is read from the cell, simplified to the tolerance of the view and
collected into the drawer's own structures.  That preparation is the
expensive part of an ENC composition, and it is repeated whenever the
prepared geometry is not kept somewhere that outlives the chart handle.

Two things stand in the way today:

* the drawer's own cache lives on the drawer, and the drawer lives on the
  chart handle, and the renderer keeps only eight handles.  A view that
  touches more than eight cells reopens the cells it dropped, and their
  features are prepared again;
* the drawer's own cache is capped at 4096 entries, while the largest
  visible cell holds 7474 features, and the cells are read in a sequential
  scan: the entry that is dropped is the entry the scan reaches next.

This module keeps the prepared geometry in a cache that belongs to the
renderer, so it outlives the chart handles, and keys it by the cell's own
path, so that reopening a cell finds the geometry that was already prepared
for it.  The cache is bounded by accounted retained bytes rather than by a
number of entries, because the size of an entry is the size of the geometry
it holds, and the entries differ by orders of magnitude.

Nothing here changes what is drawn.  The features, the tolerance, the
collection order, the styles and the clipping are the drawer's own; only
the place the prepared geometry is kept, and the key it is kept under,
change.
"""

import sys
from collections import OrderedDict

from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

#: The retained bytes the viewport's ENC geometry cache may hold.
#:
#: The measured working set of the six cells drawn at the application's saved
#: view (1000x680) was about 25.4 MB of prepared geometry, and the largest
#: visible cell alone holds 7474 features in view -- far more than the
#: drawer's own 4096-entry cap can hold, which is why that cell was prepared
#: again on every composition.  64 MiB is about 2.5 times that working set:
#: it holds the drawn cells with room for the next view, while the memory the
#: chart worker retains stays bounded.
PREPARED_FEATURE_CACHE_BYTES = 64 * 1024 * 1024


def payload_bytes(payload):
    """The bytes one prepared feature occupies.

    The containers are measured exactly.  The vertices are counted exactly
    and charged the measured cost of one vertex of the same shape, which is
    the real retained size of the payload without walking every vertex in
    Python -- the walk would cost as much as the preparation it accounts
    for.
    """
    total = 0
    for features in payload:
        for feature in features:
            total += feature_bytes(feature)
    return total


def feature_bytes(feature):
    """The bytes one prepared feature occupies."""
    total = sys.getsizeof(feature) + sys.getsizeof(feature.attributes)
    for value in feature.attributes.values():
        total += value_bytes(value)
    geometry = feature.geometry
    total += sys.getsizeof(geometry)
    if not geometry:
        return total
    first = geometry[0]
    if isinstance(first, tuple):
        # A line or a point: a flat list of vertices.
        return total + len(geometry) * vertex_bytes(first)
    # An area: a list of rings, each a list of vertices.
    for ring in geometry:
        total += sys.getsizeof(ring)
        if ring:
            total += len(ring) * vertex_bytes(ring[0])
    return total


def vertex_bytes(vertex):
    """The bytes one vertex occupies."""
    total = sys.getsizeof(vertex)
    for value in vertex:
        total += sys.getsizeof(value)
    return total


def value_bytes(value):
    """The bytes one attribute value occupies."""
    if isinstance(value, (list, tuple)):
        return sys.getsizeof(value) + sum(value_bytes(item) for item in value)
    return sys.getsizeof(value)


class PreparedFeatureCache:
    """The prepared features, kept across the eviction of the chart handles.

    The cache is the renderer's own, and it is bounded by accounted retained
    bytes: the least recently used entries are dropped until the accounted
    bytes are under the limit.  An entry that alone would break the limit is
    never kept, so the accounted bytes never exceed the limit.
    """

    def __init__(self, limit_bytes=PREPARED_FEATURE_CACHE_BYTES, enabled=True):
        self._limit = int(limit_bytes)
        self._enabled = bool(enabled)
        self._entries = OrderedDict()
        self._bytes = 0
        self._hits = 0
        self._misses = 0

    @property
    def limit(self):
        """The bytes the cache may retain."""
        return self._limit

    @property
    def bytes(self):
        """The bytes the cache retains."""
        return self._bytes

    @property
    def entries(self):
        """The entries the cache holds."""
        return len(self._entries)

    @property
    def hits(self):
        """The features that were already prepared."""
        return self._hits

    @property
    def misses(self):
        """The features that had to be prepared."""
        return self._misses

    def get(self, key):
        """The prepared feature, if the cache holds it.

        A feature that is found is the most recently used one.
        """
        if not self._enabled:
            self._misses += 1
            return None
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            return None
        self._hits += 1
        self._entries.move_to_end(key)
        return entry[0]

    def touch(self, key):
        """The entry is the most recently used one."""
        if key in self._entries:
            self._entries.move_to_end(key)

    def put(self, key, payload):
        """Keep the prepared feature, and drop the oldest ones when full."""
        if not self._enabled:
            return
        held = self._entries.pop(key, None)
        if held is not None:
            self._bytes -= held[1]
        size = payload_bytes(payload)
        if size > self._limit:
            # The entry alone would break the bound: it is not kept, and the
            # accounted bytes stay under the limit.
            return
        self._entries[key] = (payload, size)
        self._bytes += size
        while self._bytes > self._limit and self._entries:
            _oldest, (_payload, old_size) = self._entries.popitem(last=False)
            self._bytes -= old_size

    def clear(self):
        """Drop everything the cache holds."""
        self._entries.clear()
        self._bytes = 0


class ViewportS57ChartDrawer(S57ChartDrawer):
    """The ENC drawer of the viewport: the same portrayal, kept elsewhere.

    The drawer is the application's own drawer and draws exactly what it
    draws today.  Only the prepared geometry moves: it is kept in the
    renderer's cache, under the cell's own path, so that reopening the cell
    finds the geometry that was already prepared for it.
    """

    def __init__(self, cell_path="", cache=None, palette=""):
        self._cell_path = cell_path
        self._shared_cache = cache if cache is not None else PreparedFeatureCache()
        self.palette = palette

    def bind(self, cell_path, cache, palette):
        """The cell this drawer draws, and the cache it keeps its geometry in."""
        self._cell_path = cell_path
        self._shared_cache = cache
        self.palette = palette

    def _ensure_feature_cache(self):
        """The shared cache stands in for the drawer's own cache."""
        return

    def _feature_cache_key(self, vector_file, tolerance, name, feature):
        """The key of one prepared feature.

        The key is the cell's own path, the tolerance of the view, the layer
        and the feature's own id.  The path, and not the open handle, is
        what survives the reopening of a cell.
        """
        return (self._cell_path, tolerance, name, feature.GetFID())

    def _feature_cache_get(self, cache_key):
        """The prepared feature, if the shared cache holds it."""
        return self._shared_cache.get(cache_key)

    def _feature_cache_touch(self, cache_key):
        """The entry is the most recently used one."""
        self._shared_cache.touch(cache_key)

    def _feature_cache_put(self, cache_key, collected):
        """Keep the prepared feature in the shared cache."""
        self._shared_cache.put(cache_key, collected)
