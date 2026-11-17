"""Backend-neutral, versioned whole-cell source scene for one ENC cell.

Stage 1 spike for issue #215: immutable feature records, one WKB copy each, a
per-layer uniform-grid envelope index, and source-ordered exact intersection
tests.  Nothing is wired into the default renderer or the viewport.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass

from osgeo import ogr

# Mirrors of the drawer's layer rules.  They are duplicated rather than
# imported so this module stays independent of Cairo; test_enc_scene asserts
# they stay equal to S57ChartDrawer's.
SKIP_LAYERS = {"DSID", "M_COVR", "M_QUAL", "M_NSYS", "M_CSCL", "M_SDAT", "M_VDAT"}
ATTRIBUTE_FIELDS = {"DEPARE": ("DRVAL1", "DRVAL2"), "BOYLAT": ("COLOUR",)}

_CANCEL_STRIDE = 64  # envelope tests between cancellation polls
_GRID_MAX_SIDE = 64  # coarse-index cap, so query work stays bounded
_OVERSIZED_MARKERS = 64  # bounded negative markers for oversized cells


def _cancelled(cancelled) -> bool:
    return cancelled is not None and bool(cancelled())


def _stage(timings, name, started):
    """Accumulate one opt-in diagnostic duration; a no-op when ``None``."""
    if timings is not None:
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started)


def _freeze_value(value):
    """Copy an OGR field value into an immutable Python value."""
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return value


def _deep_nbytes(value, seen) -> int:
    """Conservative size of an object graph, without double counting."""
    if id(value) in seen:
        return 0
    seen.add(id(value))
    total = sys.getsizeof(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        total += sys.getsizeof(getattr(value, "__dict__", {}))
        for field in dataclasses.fields(value):
            total += _deep_nbytes(getattr(value, field.name), seen)
    elif isinstance(value, dict):
        for key, item in value.items():
            total += _deep_nbytes(key, seen) + _deep_nbytes(item, seen)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            total += _deep_nbytes(item, seen)
    return total


def cell_version(path) -> tuple[int, int, int, int]:
    """The version identity of a cell file: ``(dev, ino, size, mtime_ns)``."""
    stat = os.stat(os.fspath(path))
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _bound_version(path, version) -> tuple[int, int, int, int]:
    actual = cell_version(path)
    if tuple(version) != actual:
        raise ValueError(
            f"scene version does not match the cell file: {path} "
            f"requested {version}, found {actual}"
        )
    return actual


@dataclass(frozen=True)
class FeatureRecord:
    """One whole-cell feature with the only WKB copy of its geometry."""

    layer_index: int
    sequence: int
    fid: int
    source_class: str
    attributes: tuple[tuple[str, object], ...]
    envelope: tuple[float, float, float, float]
    wkb: bytes

    def attribute(self, name, default=None):
        for key, value in self.attributes:
            if key == name:
                return value
        return default

    @property
    def attributes_dict(self) -> dict[str, object]:
        return dict(self.attributes)


@dataclass(frozen=True)
class LayerCandidates:
    """A layer's records plus a coarse uniform-grid envelope index."""

    layer_index: int
    source_class: str
    start: int
    envelopes: tuple[tuple[float, float, float, float], ...]
    grid: tuple[float, float, float, float, int]
    cells: tuple[tuple[int, ...], ...]

    def _span(self, low, high, origin, cell, count) -> tuple[int, int]:
        if count <= 1:
            return 0, 0
        first = int((low - origin) // cell)
        last = int((high - origin) // cell)
        return max(0, min(first, count - 1)), max(0, min(last, count - 1))

    def candidate_offsets(self, bounding) -> tuple[int, ...]:
        min_x, min_y, cell_x, cell_y, side = self.grid
        qminx, qmaxx, qminy, qmaxy = bounding.GetEnvelope()
        x0, x1 = self._span(qminx, qmaxx, min_x, cell_x, side)
        y0, y1 = self._span(qminy, qmaxy, min_y, cell_y, side)
        found = set()
        for row in range(y0, y1 + 1):
            base = row * side
            for column in range(x0, x1 + 1):
                found.update(self.cells[base + column])
        return tuple(sorted(found))


@dataclass(frozen=True)
class CellScene:
    """An immutable whole-cell scene, versioned by the caller's key."""

    path: str
    version: tuple
    records: tuple[FeatureRecord, ...]
    layers: tuple[LayerCandidates, ...]
    nbytes: int

    def __len__(self) -> int:
        return len(self.records)

    def query(self, bounding, cancelled=None):
        """Return intersecting records in source order, or ``None`` if cancelled.

        Grid candidates are confirmed with the same exact ``Intersects`` test an
        OGR spatial filter applies; a cancelled query never returns a partial
        tuple.
        """
        if bounding is None:
            raise ValueError("query requires a bounding geometry")
        if _cancelled(cancelled):
            return None
        qminx, qmaxx, qminy, qmaxy = bounding.GetEnvelope()
        found: list[FeatureRecord] = []
        for layer in self.layers:
            if _cancelled(cancelled):
                return None
            for position, offset in enumerate(layer.candidate_offsets(bounding)):
                if position % _CANCEL_STRIDE == 0 and _cancelled(cancelled):
                    return None
                eminx, emaxx, eminy, emaxy = layer.envelopes[offset]
                if emaxx < qminx or eminx > qmaxx or emaxy < qminy or eminy > qmaxy:
                    continue
                record = self.records[layer.start + offset]
                geometry = ogr.CreateGeometryFromWkb(record.wkb)
                if geometry is not None and geometry.Intersects(bounding):
                    found.append(record)
        return tuple(found)


def _make_grid(envelopes) -> tuple[tuple[float, float, float, float, int], tuple]:
    """Build a coarse uniform grid that only prunes; exact tests still decide."""
    min_x = min(envelope[0] for envelope in envelopes)
    max_x = max(envelope[1] for envelope in envelopes)
    min_y = min(envelope[2] for envelope in envelopes)
    max_y = max(envelope[3] for envelope in envelopes)
    side = max(1, min(_GRID_MAX_SIDE, int(len(envelopes) ** 0.5)))
    span_x = max_x - min_x
    span_y = max_y - min_y
    cell_x = span_x / side if span_x > 0 else 1.0
    cell_y = span_y / side if span_y > 0 else 1.0
    cells = [[] for _ in range(side * side)]
    for offset, envelope in enumerate(envelopes):
        x0 = max(0, min(side - 1, int((envelope[0] - min_x) / cell_x)))
        x1 = max(0, min(side - 1, int((envelope[1] - min_x) / cell_x)))
        y0 = max(0, min(side - 1, int((envelope[2] - min_y) / cell_y)))
        y1 = max(0, min(side - 1, int((envelope[3] - min_y) / cell_y)))
        for row in range(y0, y1 + 1):
            base = row * side
            for column in range(x0, x1 + 1):
                cells[base + column].append(offset)
    return (min_x, min_y, cell_x, cell_y, side), tuple(tuple(cell) for cell in cells)


def build_cell_scene(datasource, path, version, cancelled=None, timings=None):
    """Build a whole-cell scene from an open OGR datasource.

    Portrayed layers use the drawer's skip rules; inherited spatial and
    attribute filters are cleared.  Returns ``None`` if cancelled, never a
    partial scene.  ``timings`` is an opt-in diagnostic mapping: it receives
    ``"ogr_scan"`` seconds, the cost of reading and indexing this cell's
    features.  ``None`` (the default) records nothing.
    """
    if datasource is None:
        raise ValueError("build_cell_scene requires an open datasource")
    started = time.perf_counter()
    scene = _build_cell_scene(datasource, path, version, cancelled)
    _stage(timings, "ogr_scan", started)
    return scene


def _build_cell_scene(datasource, path, version, cancelled=None):
    """The unattributed build behind :func:`build_cell_scene`."""
    resolved = os.path.abspath(os.fspath(path))
    if _cancelled(cancelled):
        return None
    records: list[FeatureRecord] = []
    layers: list[LayerCandidates] = []
    sequence = 0
    for layer_index in range(datasource.GetLayerCount()):
        if _cancelled(cancelled):
            return None
        layer = datasource.GetLayerByIndex(layer_index)
        name = layer.GetName()
        definition = layer.GetLayerDefn()
        if name in SKIP_LAYERS or definition.GetGeomFieldCount() == 0:
            continue
        fields = ATTRIBUTE_FIELDS.get(name, ())
        field_indexes = [(key, definition.GetFieldIndex(key)) for key in fields]
        layer.SetSpatialFilter(None)
        layer.SetAttributeFilter(None)
        layer.ResetReading()
        start = len(records)
        envelopes: list[tuple[float, float, float, float]] = []
        for feature in layer:
            if _cancelled(cancelled):
                return None
            geometry = feature.GetGeometryRef()
            if geometry is None:
                continue
            wkb = geometry.ExportToWkb()
            if wkb is None:
                continue
            envelope = tuple(geometry.GetEnvelope())
            attributes = tuple(
                (key, _freeze_value(feature.GetField(index)))
                for key, index in field_indexes
                if index >= 0
            )
            records.append(
                FeatureRecord(
                    layer_index=layer_index,
                    sequence=sequence,
                    fid=feature.GetFID(),
                    source_class=name,
                    attributes=attributes,
                    envelope=envelope,
                    wkb=bytes(wkb),
                )
            )
            envelopes.append(envelope)
            sequence += 1
        if envelopes:
            grid, cells = _make_grid(envelopes)
            layers.append(
                LayerCandidates(
                    layer_index=layer_index,
                    source_class=name,
                    start=start,
                    envelopes=tuple(envelopes),
                    grid=grid,
                    cells=cells,
                )
            )
    records = tuple(records)
    layers = tuple(layers)
    seen = set()
    return CellScene(
        path=resolved,
        version=version,
        records=records,
        layers=layers,
        nbytes=_deep_nbytes(records, seen) + _deep_nbytes(layers, seen),
    )


def open_cell_scene(path, version, cancelled=None, timings=None):
    """Open a cell read-only and build its scene.

    The caller's version must match before and after the read; a scene read
    across a change is rejected rather than returned under the wrong version.
    ``timings`` is an opt-in diagnostic mapping receiving ``"ogr_open"`` (the
    GDAL dataset open) and ``"ogr_scan"`` (the feature read through
    :func:`build_cell_scene`), so a store miss can be told apart from a store
    hit.  ``None`` (the default) records nothing.
    """
    actual = _bound_version(path, version)
    if _cancelled(cancelled):
        return None
    started = time.perf_counter()
    datasource = ogr.Open(os.fspath(path), 0)
    _stage(timings, "ogr_open", started)
    if datasource is None:
        raise OSError(f"cannot open ENC cell: {path}")
    try:
        scene = build_cell_scene(datasource, path, actual, cancelled, timings=timings)
    finally:
        datasource = None
    if scene is None or _cancelled(cancelled):
        return None
    if cell_version(path) != actual:
        raise ValueError(f"cell changed while it was being read: {path}")
    return scene


class SceneCache:
    """A small bounded LRU cache of whole-cell scenes, accounted by bytes.

    Single-worker ownership: do not share across threads/processes; no lock.
    An oversized scene is refused, marked negatively and not rebuilt for that
    version.  A current version purges older entries and markers for the path.
    """

    def __init__(self, max_bytes):
        max_bytes = int(max_bytes)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self._entries: OrderedDict[tuple[str, tuple], CellScene] = OrderedDict()
        self._oversized: OrderedDict[tuple[str, tuple], None] = OrderedDict()
        self._bytes = 0
        self.hits = self.misses = self.evictions = 0
        #: Evictions that chose an unpinned victim instead of a pinned one.
        self.pinned_evictions_avoided = 0
        self.oversized = self.stale = 0

    @staticmethod
    def _key(path, version) -> tuple[str, tuple]:
        return (os.path.abspath(os.fspath(path)), version)

    @staticmethod
    def key(path, version) -> tuple[str, tuple]:
        """The cache identity of one cell, for callers that pin entries."""
        return SceneCache._key(path, version)

    @property
    def resident_bytes(self) -> int:
        return self._bytes

    def is_oversized(self, path, version) -> bool:
        return self._key(path, version) in self._oversized

    def _purge_path(self, path) -> None:
        for entry in [key for key in self._entries if key[0] == path]:
            self._bytes -= self._entries.pop(entry).nbytes
        for marker in [key for key in self._oversized if key[0] == path]:
            self._oversized.pop(marker, None)

    def get(self, path, version):
        key = self._key(path, version)
        scene = self._entries.get(key)
        if scene is None:
            self.misses += 1
            return None
        self.hits += 1
        self._entries.move_to_end(key)
        return scene

    def put(self, scene: CellScene, pin=None) -> bool:
        """Store one cell scene, evicting the least recently used entries.

        ``pin`` is an optional container of :meth:`key` values the caller
        still needs for the view it is assembling.  Pinned entries are passed
        over, so a large cell inserted halfway through a build cannot evict a
        cell another planned entry is about to use.  The cap still wins: when
        every resident entry is pinned the oldest pinned entry is evicted
        rather than leaving the cache over budget.
        """
        pinned = pin if pin is not None else ()
        key = self._key(scene.path, scene.version)
        try:
            current = cell_version(scene.path)
        except OSError:
            current = None
        if current is not None and tuple(scene.version) != current:
            self.stale += 1
            return False
        self._purge_path(key[0])
        if scene.nbytes > self.max_bytes:
            self.oversized += 1
            self._oversized[key] = None
            while len(self._oversized) > _OVERSIZED_MARKERS:
                self._oversized.popitem(last=False)
            return False
        while self._bytes + scene.nbytes > self.max_bytes and self._entries:
            victim = next((key for key in self._entries if key not in pinned), None)
            if victim is None:
                # Every resident entry is pinned: the cap still wins, so the
                # oldest pinned entry goes rather than the cache growing.
                victim = next(iter(self._entries))
            elif pinned:
                self.pinned_evictions_avoided += 1
            self._bytes -= self._entries.pop(victim).nbytes
            self.evictions += 1
        self._entries[key] = scene
        self._bytes += scene.nbytes
        return True

    def get_or_build(
        self, path, version, loader, cancelled=None, pin=None, timings=None
    ):
        """Return a cached scene or build one with ``loader(cancelled)``.

        Only a complete, current-version scene is published; cancellation or a
        cell change returns ``None``.  An oversized scene returns ``None`` and
        leaves a bounded marker so that version is not rebuilt indefinitely.
        ``pin`` is forwarded to :meth:`put`, so the cell being built cannot
        evict another cell the same plan still needs.  ``timings`` is an opt-in
        diagnostic mapping receiving ``"scene_cache"`` (the resident lookup) and
        ``"scene_load"`` (the loader, which includes the persisted store read
        and a fresh OGR decode on a miss), so a resident scene, a store hit and
        an OGR rebuild report different stages.
        """
        actual = _bound_version(path, version)
        if _cancelled(cancelled):
            return None
        started = time.perf_counter()
        scene = self.get(path, version)
        _stage(timings, "scene_cache", started)
        if scene is not None:
            return scene
        if self.is_oversized(path, actual):
            return None
        if _cancelled(cancelled):
            return None
        self._purge_path(self._key(path, actual)[0])
        started = time.perf_counter()
        built = loader(cancelled)
        _stage(timings, "scene_load", started)
        if built is None or _cancelled(cancelled):
            return None
        expected_path, expected_version = self._key(path, actual)
        if (built.path, built.version) != (expected_path, expected_version):
            raise ValueError("built scene does not match the requested cell version")
        if cell_version(path) != actual:
            raise ValueError("cell changed during build; refusing a stale scene")
        if not self.put(built, pin=pin):
            return None
        return built
