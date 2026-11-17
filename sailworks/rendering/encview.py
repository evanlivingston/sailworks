"""Backend-neutral assembly of an ordered multi-cell ENC view scene.

:func:`build_view_scene` consumes a :func:`encplan.plan_cells` plan and returns
one scene whose parts are the per-cell meshes concatenated in composite order.
It does not draw, does not touch the default renderer and does not decide the
viewport: the caller supplies the projection used for screen-constant stroke
widths and the palette used for flat per-part colours.

Reuse and composite order
-------------------------

Tessellating a cell is the expensive step, and its result does not depend on
the view when the cell's own coverage is known.  Each such mesh is therefore
built once, clipped to the cell's *own* coverage, and kept in a bounded
:class:`MeshCache` keyed by ``(path, version, palette, stroke pixels)``.  A
caller that supplies ``mesh_scale`` (production does: :func:`encscenelayer`
maps every view into one fixed reference zoom) gets geometry that does not
depend on the view at all, so a zoom is a cache hit instead of a full
re-tessellation.  Without that frame the key keeps the view scale and
tolerance, exactly as before.  The ``Difference(covered)`` region from the plan
is deliberately
*not* used to build cached geometry: it changes with every view change and
would defeat the cache.  ``build_scene_mesh`` remains able to clip to an exact
region for callers that want it.

Per-view overlap suppression is instead done at composite time: cells are
drawn worst-ranked first and best-ranked last (the reverse of the plan's
draw order), so the best-ranked cell is painted over the others.  For opaque
fills this reproduces the visible result of the plan's exact region
subtraction.  It is an approximation, and the difference is real: exact
subtraction is not performed, so where two cells overlap

* translucent fills blend twice instead of the upper cell replacing the lower,
  and
* a lower cell's outlines/strokes that fall inside an upper cell's coverage
  are only hidden if the upper fill is opaque and is painted after them.

A cell with unknown coverage has no authoritative region; it keeps the
view-dependent query, is drawn unclipped and is reported with
``coverage_state == "none"`` rather than guessed into a clipped shape.  Such a
cell cannot be reused across view changes, so its mesh is not cached.

The scene keeps the same contracts as :func:`encmesh.build_scene_mesh`:

* parts follow composite order, then source order within each cell, preserving
  the per-record ``area-fill -> area-outline -> line`` (or line-first) emission;
* ``MeshPart.first_vertex`` stays contiguous across the whole concatenation, so
  one vertex buffer is enough for every part;
* a feature the area/line slice cannot draw is reported, never dropped.

Cells that contribute no geometry are reported in :attr:`ViewScene.skipped`
(for example an oversized cell the cache refused to hold).
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

import numpy as np
import shapely
from osgeo import ogr

from sailworks.rendering import encplan
from sailworks.rendering.vectordrawer import encmesh, encpalette, encscene, encsounding


def _stage(timings, name, started):
    """Accumulate one opt-in stage duration; a no-op when ``timings`` is None."""
    if timings is not None:
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started)


def _cancelled(cancelled) -> bool:
    return cancelled is not None and bool(cancelled())


@dataclasses.dataclass(frozen=True)
class CellSpan:
    """Where one plan cell's parts and vertices land in the assembled scene."""

    path: str
    version: tuple | None
    first_part: int
    part_count: int
    first_vertex: int
    vertex_count: int
    coverage_state: str


@dataclasses.dataclass(frozen=True)
class CellUnsupported:
    """An unsupported feature, attributed to the cell that produced it."""

    path: str
    version: tuple | None
    part: encmesh.UnsupportedPart

    @property
    def reason(self) -> str:
        return self.part.reason


@dataclasses.dataclass(frozen=True)
class SkippedCell:
    """A planned cell that contributed no geometry, with the reason why."""

    path: str
    version: tuple | None
    reason: str


@dataclasses.dataclass(frozen=True)
class CellMesh:
    """One cell's reusable tessellation, clipped to its own coverage."""

    positions: np.ndarray
    parts: tuple
    unsupported: tuple
    coverage_state: str
    #: (N, 3) source ``(x, y, depth)`` for the same coverage, so a cache hit
    #: needs neither the cell scene nor its records.
    soundings: np.ndarray
    #: (N, 4) uint8 per-vertex colour expanded with the palette's neutral
    #: default.  The palette is part of the cache key and the colour depends
    #: only on the mesh and palette, so a warm view reuses these bytes instead
    #: of re-expanding every part.  Counted in :attr:`nbytes`.
    colours: np.ndarray
    #: Honest byte estimate: vertex, sounding and colour buffers plus part
    #: overhead.
    nbytes: int


#: Estimated Python overhead of one ``MeshPart``/``UnsupportedPart`` entry.
#: The vertex data is accounted exactly; parts are counted conservatively.
PART_OVERHEAD_BYTES = 256

#: Bounded per-cell mesh cache for direct callers that do not own one.  It has
#: to hold one view's resident meshes: a cached mesh is clipped to the cell's
#: own coverage, so it is larger than the visible slice it replaces.
DEFAULT_MESH_CACHE_BYTES = 256 * 1024 * 1024

_DEFAULT = object()
_default_mesh_cache = None
_default_mesh_cache_lock = threading.Lock()

#: Upper bound on concurrent per-cell mesh builds when the caller does not pass
#: ``workers``.  Shapely 2 releases the GIL for GEOS work, so threads scale to
#: the largest cell; more workers than cores adds contention without making
#: wall time any shorter.
DEFAULT_MESH_WORKERS = 4

#: Environment override for the default worker bound, for experiments and
#: machines where the default is wrong.  Ignored when it is not a positive int.
MESH_WORKERS_ENV = "SAILWORKS_ENC_MESH_WORKERS"

#: The finest chart zoom a reusable mesh is tessellated for: the chart's own
#: ``MAX_ZOOM`` (``sailworks.core.chscharts``).  A caller that supplies
#: ``mesh_scale`` builds every cell once in this fixed reference frame, so the
#: same mesh serves every zoom and a zoom is a cache hit.  The cost is the
#: opposite direction: at a farther zoom the same geometry carries more
#: triangles than that view needs, and the stroke width baked here (one logical
#: pixel at this zoom) is sub-pixel there.  That trade is deliberate - a
#: re-tessellation per zoom step blanks the chart, reuse does not.
MESH_REFERENCE_ZOOM = 17.0


def _default_workers() -> int:
    """The mesh-build worker bound: env override, else ``min(4, cpu_count)``."""
    override = os.environ.get(MESH_WORKERS_ENV, "")
    try:
        configured = int(override)
    except (TypeError, ValueError):
        configured = 0
    if configured > 0:
        return configured
    return max(1, min(DEFAULT_MESH_WORKERS, os.cpu_count() or 1))


def default_mesh_cache() -> "MeshCache":
    """The process-wide bounded mesh cache used when a caller supplies none.

    Production code (:class:`sailworks.qt.encscenelayer.EncSceneLayer`) owns its
    own worker-local cache and passes it explicitly; this default exists so a
    direct ``build_view_scene`` caller still benefits without global plumbing.
    """
    global _default_mesh_cache
    with _default_mesh_cache_lock:
        if _default_mesh_cache is None:
            _default_mesh_cache = MeshCache(DEFAULT_MESH_CACHE_BYTES)
        return _default_mesh_cache


def reset_default_mesh_cache() -> None:
    """Drop the process-wide default cache (tests, teardown)."""
    global _default_mesh_cache
    with _default_mesh_cache_lock:
        _default_mesh_cache = None


class MeshCache:
    """A small bounded LRU cache of per-cell meshes, accounted by bytes.

    Ownership is one view build, not one thread: :func:`build_view_scene` with
    ``workers`` above one puts and gets from several mesh threads at once, so
    every read and write of the entries, the byte total and the counters is
    guarded by a lock.  The budget is separate from the whole-cell scene cache
    so a warm mesh does not evict the source scene it was built from.  An entry
    larger than the whole budget is refused instead of evicting everything
    else.
    """

    def __init__(self, max_bytes):
        max_bytes = int(max_bytes)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self._entries: OrderedDict[tuple, CellMesh] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self.hits = self.misses = self.evictions = self.refused = 0
        #: Evictions that chose an unpinned victim instead of a pinned one.
        self.pinned_evictions_avoided = 0
        #: Tessellations actually performed through this cache.
        self.triangulations = 0
        #: The quantised rectangle each entry was clipped to, for distance
        #: eviction.  Kept beside the entries, not in the mesh.
        self._granules: dict[tuple, tuple] = {}
        #: ``(lon, lat, span_degrees)`` of the newest view: granules farther
        #: than twice this span are dropped before anything nearer, so a pan
        #: keeps its margin of reused meshes and a zoom-out does not.
        self._focus = None

    def focus(self, lon, lat, span_degrees) -> None:
        """Record the current view, for distance-based eviction."""
        try:
            span = float(span_degrees)
        except (TypeError, ValueError):
            return
        if not np.isfinite(span) or span <= 0.0:
            return
        with self._lock:
            self._focus = (float(lon), float(lat), span)

    def _far(self, granule) -> bool:
        """Whether ``granule`` is beyond twice the current view's span."""
        focus = self._focus
        if focus is None or granule is None:
            return False
        lon, lat, span = focus
        x0, y0, x1, y1 = granule
        distance = max(abs(0.5 * (x0 + x1) - lon), abs(0.5 * (y0 + y1) - lat))
        return distance > 2.0 * span

    @staticmethod
    def key(
        path,
        version,
        scale_bucket,
        tolerance,
        palette_key,
        stroke_pixels,
        granule=None,
        trim=None,
    ) -> tuple:
        """Identity of one reusable cell mesh.

        ``scale_bucket`` and ``tolerance`` are ``None`` for a mesh built in a
        fixed reference frame (``build_view_scene(mesh_scale=...)``): such a
        mesh does not depend on the view, so neither the display scale nor the
        simplification tolerance may be part of its identity - if they were,
        every zoom step would miss and re-tessellate every cell.

        ``granule`` is the quantised rectangle the mesh was clipped to (see
        :func:`_granule_region`).  It is part of the key because the geometry
        really does depend on it, but it changes only on a grid step, so a pan
        smaller than a step keeps the key and reuses the mesh.

        ``trim`` is :func:`_trim_digest` of the ground a better-ranked cell
        subtracted from this one, and ``None`` when nothing was subtracted.  A
        trimmed mesh is clipped to the cell's *remaining* coverage, so the key
        must change when the better-cell set changes even though the cell,
        its granule and its style have not: the digest is exactly that
        identity, and equal digests mean equal geometry.
        """
        return (
            os.path.abspath(os.fspath(path)),
            None if version is None else tuple(version),
            scale_bucket,
            None if tolerance is None else float(tolerance),
            palette_key,
            float(stroke_pixels),
            granule,
            trim,
        )

    @property
    def resident_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def note_triangulation(self) -> None:
        """Count one real tessellation; concurrent builders share the counter."""
        with self._lock:
            self.triangulations += 1

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
            self._entries.move_to_end(key)
            return entry

    def put(self, key, entry: CellMesh, granule=None, pin=None) -> bool:
        """Store one mesh, evicting to stay inside the byte cap.

        ``pin`` is an optional container of keys the caller still needs for
        the view it is building.  Pinned entries are passed over by the
        distance heuristic and by the LRU fallback, so a large cell inserted
        halfway through a build cannot evict a mesh another planned cell just
        built.  The cap still wins: when every resident entry is pinned the
        oldest pinned entry is evicted rather than refusing the insert, which
        keeps the cache bounded at the cost of a rebuild later in the build.
        """
        pinned = pin if pin is not None else ()
        nbytes = int(entry.nbytes)
        with self._lock:
            if nbytes > self.max_bytes:
                self.refused += 1
                return False
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous.nbytes
            while self._bytes + nbytes > self.max_bytes and self._entries:
                victim = next(
                    (
                        candidate
                        for candidate, stored in self._granules.items()
                        if candidate in self._entries
                        and candidate not in pinned
                        and self._far(stored)
                    ),
                    None,
                )
                if victim is None:
                    victim = next(
                        (
                            candidate
                            for candidate in self._entries
                            if candidate not in pinned
                        ),
                        None,
                    )
                if victim is None:
                    # Every resident entry is pinned: enforcing the cap takes
                    # priority over the working set.
                    victim = next(iter(self._entries))
                elif pinned:
                    self.pinned_evictions_avoided += 1
                self._bytes -= self._entries.pop(victim).nbytes
                self._granules.pop(victim, None)
                self.evictions += 1
            self._entries[key] = entry
            if granule is not None:
                self._granules[key] = tuple(granule)
            self._bytes += nbytes
            return True


@dataclasses.dataclass(frozen=True)
class ViewScene:
    """An ordered, multi-cell scene ready for a backend to upload."""

    #: (N, 2) float64 source units; one contiguous buffer for every part.
    positions: np.ndarray
    parts: tuple
    #: One entry per contributing cell, in composite order (worst-ranked
    #: first, best-ranked last).
    cells: tuple
    #: Cell-attributed :class:`encmesh.UnsupportedPart` entries.
    unsupported: tuple
    #: Planned cells that produced no geometry, and why.
    skipped: tuple
    #: Sum of the contributing cells' accounted whole-scene bytes, once each.
    accounted_bytes: int = 0
    #: Bytes resident in the mesh cache after this assembly.
    mesh_bytes: int = 0
    #: Mesh cache hits and misses for this assembly.
    mesh_hits: int = 0
    mesh_misses: int = 0
    #: Meshes actually tessellated for this assembly (cache misses that were
    #: built, not misses that were refused or cancelled).
    mesh_built: int = 0
    #: (N, 3) float64 source ``(x, y, depth)`` for every SOUNDG point, in plan
    #: then source order.  Kept unmaterialised as screen labels: the sounding
    #: scale gate and the 32x16 dedupe are per camera and must not be baked in.
    soundings: np.ndarray = dataclasses.field(
        default_factory=lambda: encsounding.EMPTY_SOUNDINGS
    )
    #: (N, 4) uint8 expanded per-vertex colour, assembled from the cells'
    #: cached arrays.  Reused by :meth:`vertex_colours` for the neutral
    #: default, so a view never re-expands the per-part colours.
    colours: np.ndarray = dataclasses.field(
        default_factory=lambda: np.empty((0, 4), dtype=np.uint8)
    )

    @property
    def is_empty(self) -> bool:
        return self.positions.shape[0] == 0

    @property
    def vertex_count(self) -> int:
        return int(self.positions.shape[0])

    @property
    def triangle_count(self) -> int:
        return self.vertex_count // 3

    @property
    def is_complete(self) -> bool:
        """False when anything was unsupported or skipped, so no full-chart claim."""
        return not self.unsupported and not self.skipped

    def part_positions(self, part: encmesh.MeshPart) -> np.ndarray:
        return self.positions[part.first_vertex : part.first_vertex + part.vertex_count]

    def vertex_colours(self, default=encpalette.NEUTRAL) -> np.ndarray:
        """Expanded per-vertex colours, reused from the cached cell meshes.

        The cached buffers were expanded with :data:`encpalette.NEUTRAL` when
        each cell was meshed, and the palette is part of the mesh cache key, so
        the assembled array can be returned as-is for that default.  A caller
        that asks for a different default still gets a fresh expansion, because
        the cached buffer cannot stand in for another colour.
        """
        neutral = tuple(int(channel) for channel in encpalette.NEUTRAL[:4])
        if (
            self.colours.shape[0] == self.vertex_count
            and tuple(int(channel) for channel in default[:4]) == neutral
        ):
            return self.colours
        return encpalette.vertex_colours(self.parts, self.vertex_count, default)


EMPTY_VIEW_SCENE = ViewScene(
    positions=np.empty((0, 2), dtype=np.float64),
    parts=(),
    cells=(),
    unsupported=(),
    skipped=(),
    accounted_bytes=0,
    soundings=encsounding.EMPTY_SOUNDINGS,
)


@dataclasses.dataclass(frozen=True)
class _ScopedRecords:
    """The queried subset of a cell, in the shape ``build_scene_mesh`` reads."""

    records: tuple


def _view_region(plan, bounding):
    """The region to query cells with: a caller bbox, or the plan's own view."""
    if bounding is None:
        clips = [cell.clip for cell in plan if cell.clip is not None]
        if not clips:
            return None
        region = shapely.union_all(clips)
        return None if region.is_empty else region
    if hasattr(bounding, "GetEnvelope"):
        return bounding
    if hasattr(bounding, "geom_type"):
        return bounding
    if getattr(bounding, "get_bbox", None) is not None:
        # A viewport already knows how to turn its extent into lon/lat.
        return encplan._bounding(bounding)[0]
    raise TypeError(
        "bounding must be an OGR or shapely geometry, a viewport with "
        "get_bbox(), or None to use the plan's own view region"
    )


def _ogr_geometry(region):
    if region is None or hasattr(region, "GetEnvelope"):
        return region
    return ogr.CreateGeometryFromWkb(shapely.to_wkb(region))


def _oversized(scene_cache, path, version) -> bool:
    is_oversized = getattr(scene_cache, "is_oversized", None)
    return bool(is_oversized(path, version)) if is_oversized is not None else False


def _mesh_nbytes(positions, soundings, parts, unsupported, colours) -> int:
    """Account a mesh honestly: exact buffers plus per-part Python overhead."""
    return (
        int(positions.nbytes)
        + int(soundings.nbytes)
        + int(colours.nbytes)
        + (len(parts) + len(unsupported)) * PART_OVERHEAD_BYTES
    )


def _palette_key(palette):
    """A stable palette identity: a name, never an object address."""
    if palette is None:
        return None
    if isinstance(palette, str):
        return palette
    name = getattr(palette, "name", None)
    return name if isinstance(name, str) else type(palette).__qualname__


def _scale_bucket(projection, geometry):
    """Quantised display scale for a clipped cell region, or ``None``.

    Stroke width is screen-constant, so a mesh depends on display scale.  The
    bucket is the log2 of the region's projected diagonal: panning leaves it
    unchanged, zooming changes it, and no cache entry is reused across buckets.
    ``None`` means the projection could not be probed, so nothing is cached.
    """
    try:
        min_x, min_y, max_x, max_y = geometry.bounds
    except (AttributeError, TypeError):
        return None
    probe = np.array([[min_x, min_y], [max_x, max_y]], dtype=np.float64)
    try:
        projected = np.asarray(projection(probe), dtype=np.float64)
    except Exception:  # noqa: BLE001 - an unprobeable projection just skips cache
        return None
    if projected.shape != (2, 2):
        return None
    span = float(
        np.hypot(
            projected[1, 0] - projected[0, 0],
            projected[1, 1] - projected[0, 1],
        )
    )
    if not np.isfinite(span) or span <= 0.0:
        return None
    return round(float(np.log2(span)), 3)


def _reference_projection(projection, mesh_scale):
    """The same projection expressed in the mesh's fixed reference frame.

    ``mesh_scale`` is the factor that turns the caller's projection (world
    pixels at the *current* zoom) into world pixels at
    :data:`MESH_REFERENCE_ZOOM`.  Mercator world pixels scale linearly with
    ``2**zoom`` about the same origin, so one multiplier is exact.  ``None``
    means the caller did not ask for a fixed frame: the mesh stays
    view-dependent and its cache key keeps the view's scale and tolerance.
    """
    if mesh_scale is None:
        return None
    factor = float(mesh_scale)
    if not np.isfinite(factor) or factor <= 0.0:
        return None
    if factor == 1.0:
        # The caller's own frame: the mesh is still view-dependent (tolerance
        # and stroke width are this zoom's), so it must keep the view's scale
        # and tolerance in its key.  Measuring this beat the reference frame:
        # meshing at MESH_REFERENCE_ZOOM costs 3x assembly time at chart zooms.
        return None

    def project(points) -> np.ndarray:
        projected = np.asarray(projection(points), dtype=np.float64)
        return projected * factor

    return project


def _bounds(geometry):
    """Source-space ``(min_x, min_y, max_x, max_y)`` for shapely or OGR."""
    if geometry is None:
        return None
    if hasattr(geometry, "GetEnvelope"):
        min_x, max_x, min_y, max_y = geometry.GetEnvelope()
        return (float(min_x), float(min_y), float(max_x), float(max_y))
    try:
        return tuple(float(value) for value in geometry.bounds)
    except (AttributeError, TypeError):
        return None


def _tolerance_bucket(projection, geometry):
    """The drawer's quantised half-pixel simplification tolerance, in degrees.

    Mirrors ``S57ChartDrawer.draw``: degrees per pixel from the region's
    projected span, halved, then floored to a power of two.  The projected
    span is what makes the bucket stable while panning and different when
    zooming, exactly like :func:`_scale_bucket`; the quantisation keeps the
    tolerance to a small set of values so cached geometry can be shared across
    a pan.  ``None`` means the projection could not be probed, so the mesh must
    not be cached against a tolerance it cannot reproduce.
    """
    bounds = _bounds(geometry)
    if bounds is None:
        return None
    min_x, min_y, max_x, max_y = bounds
    probe = np.array([[min_x, min_y], [max_x, max_y]], dtype=np.float64)
    try:
        projected = np.asarray(projection(probe), dtype=np.float64)
    except Exception:  # noqa: BLE001 - an unprobeable projection just skips cache
        return None
    if projected.shape != (2, 2):
        return None
    width_pixels = abs(float(projected[1, 0] - projected[0, 0]))
    height_pixels = abs(float(projected[1, 1] - projected[0, 1]))
    if not np.isfinite(width_pixels) or not np.isfinite(height_pixels):
        return None
    if width_pixels <= 0.0 or height_pixels <= 0.0:
        return None
    return encmesh.view_tolerance(
        max_x - min_x, max_y - min_y, width_pixels, height_pixels
    )


@dataclasses.dataclass(frozen=True)
class _CellContribution:
    """One resolved cell's ready-to-concatenate mesh, relative to itself."""

    cell: object
    #: ``(abspath, version)`` of the whole-cell scene it came from.
    scene_key: tuple
    scene_nbytes: int
    positions: np.ndarray
    parts: tuple
    colours: np.ndarray
    soundings: np.ndarray
    unsupported: tuple
    coverage_state: str


def _assemble(
    contributions, skipped, owner, hits_before, misses_before, built_before=0
):
    """One :class:`ViewScene` from the completed cells, worst-ranked first.

    ``contributions`` arrive in plan order (best-ranked first); reversing them
    here is what makes the best-ranked cell the last one drawn.  A subset of
    the plan therefore assembles into a scene with the same relative order as
    the full one, which is what progressive publication relies on.  Vertex
    offsets are recomputed per assembly, so no part ever points outside its
    own buffer.
    """
    chunks: list[np.ndarray] = []
    colour_chunks: list[np.ndarray] = []
    parts: list[encmesh.MeshPart] = []
    cells: list[CellSpan] = []
    unsupported: list[CellUnsupported] = []
    sounding_chunks: list[np.ndarray] = []
    accounted: dict[tuple[str, tuple], int] = {}
    offset = 0
    for contribution in reversed(contributions):
        first_part = len(parts)
        first_vertex = offset
        for part in contribution.parts:
            parts.append(
                dataclasses.replace(part, first_vertex=part.first_vertex + offset)
            )
        vertex_count = int(contribution.positions.shape[0])
        if vertex_count:
            chunks.append(contribution.positions)
            offset += vertex_count
        if contribution.colours.shape[0]:
            colour_chunks.append(contribution.colours)
        if contribution.soundings.shape[0]:
            sounding_chunks.append(contribution.soundings)
        cells.append(
            CellSpan(
                path=contribution.cell.path,
                version=contribution.cell.version,
                first_part=first_part,
                part_count=len(contribution.parts),
                first_vertex=first_vertex,
                vertex_count=vertex_count,
                coverage_state=contribution.coverage_state,
            )
        )
        unsupported.extend(
            CellUnsupported(contribution.cell.path, contribution.cell.version, part)
            for part in contribution.unsupported
        )
        # Distinct cells can share one whole-cell scene only by path/version
        # equality, so the whole-scene bytes are counted once each.
        accounted.setdefault(contribution.scene_key, contribution.scene_nbytes)

    positions = np.concatenate(chunks) if chunks else np.empty((0, 2), dtype=np.float64)
    colours = (
        np.concatenate(colour_chunks)
        if colour_chunks
        else np.empty((0, 4), dtype=np.uint8)
    )
    soundings = (
        np.concatenate(sounding_chunks)
        if sounding_chunks
        else encsounding.EMPTY_SOUNDINGS
    )
    return ViewScene(
        positions=positions,
        parts=tuple(parts),
        cells=tuple(cells),
        unsupported=tuple(unsupported),
        skipped=tuple(skipped),
        accounted_bytes=sum(accounted.values()),
        mesh_bytes=owner.resident_bytes if owner is not None else 0,
        mesh_hits=owner.hits - hits_before if owner is not None else 0,
        mesh_misses=owner.misses - misses_before if owner is not None else 0,
        mesh_built=(owner.triangulations - built_before if owner is not None else 0),
        soundings=soundings,
        colours=colours,
    )


@dataclasses.dataclass(frozen=True)
class _CellTask:
    """One cell's view-independent build inputs, resolved on the calling thread.

    Everything here is either immutable or owned by this cell alone, so a
    worker may read it without a lock.  ``query_region`` is a shapely geometry
    (converted to OGR inside the worker) so two cells never share one mutable
    OGR handle.
    """

    index: int
    cell: object
    clip_region: object
    query_region: object
    known: bool
    tolerance: float | None
    key: tuple | None
    #: The projection this cell is meshed with: the fixed reference frame when
    #: one was requested, else the view's own projection.
    projection: object = None
    #: The region this cell's point features must fall inside: its own visible
    #: coverage, which is wider than ``clip_region`` wherever a better-ranked
    #: cell owns the ground.  ``None`` falls back to the fill clip.
    point_region: object = None
    #: The quantised rectangle this mesh was clipped to, when it is cacheable.
    granule: tuple | None = None


@dataclasses.dataclass(frozen=True)
class _CellOutcome:
    """What one cell produced: a contribution, a skip, or a cancellation."""

    contribution: object = None
    skipped: "SkippedCell | None" = None
    cancelled: bool = False
    #: Stages measured on the worker thread; merged by the caller, so the
    #: shared ``timings`` mapping is only ever written from one thread.
    timings: dict | None = None


def _same_extent(left, right):
    """Whether two regions cover the same extent, to source precision.

    The cache-safe case for a per-cell mesh: a cell wholly inside the view has
    the same clip as coverage, so its mesh does not depend on the view.
    """
    try:
        lx0, ly0, lx1, ly1 = left.bounds
        rx0, ry0, rx1, ry1 = right.bounds
    except (AttributeError, TypeError):
        return False
    return (
        abs(lx0 - rx0) <= 1e-9
        and abs(ly0 - ry0) <= 1e-9
        and abs(lx1 - rx1) <= 1e-9
        and abs(ly1 - ry1) <= 1e-9
    )


#: The planned view is snapped outward onto a grid this many times coarser than
#: itself, so a pan smaller than one step reuses the same granule.
GRANULE_STEPS = 4

#: Source degrees a granule bound is rounded to, so two cameras on the same grid
#: line produce the same key.
GRANULE_DECIMALS = 9


def _granule_region(region):
    """The quantised rectangle a reusable mesh is clipped to.

    The planned view snapped *outward* onto a grid a quarter of its own span, so
    the granule always contains the planned view: clipping to it can never omit
    a visible pixel.  Two cameras that land on the same grid lines share the
    granule, so a pan of a few percent of the view keeps the same key and its
    meshes stay resident.  ``None`` when ``region`` has no usable extent.
    """
    bounds = _bounds(region)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    step_x = (x1 - x0) / GRANULE_STEPS
    step_y = (y1 - y0) / GRANULE_STEPS
    if not (step_x > 0.0 and step_y > 0.0):
        return None
    if not np.isfinite([x0, y0, x1, y1, step_x, step_y]).all():
        return None
    return shapely.box(
        round(float(np.floor(x0 / step_x)) * step_x, GRANULE_DECIMALS),
        round(float(np.floor(y0 / step_y)) * step_y, GRANULE_DECIMALS),
        round(float(np.ceil(x1 / step_x)) * step_x, GRANULE_DECIMALS),
        round(float(np.ceil(y1 / step_y)) * step_y, GRANULE_DECIMALS),
    )


def _granule_key(granule) -> tuple:
    """The granule's identity in a mesh cache key."""
    return tuple(round(float(value), GRANULE_DECIMALS) for value in granule.bounds)


def _trim_digest(region) -> bytes | None:
    """A stable identity for the ground a trimmed cell keeps.

    The trim is ``coverage`` minus the ground better-ranked cells claim, which
    depends on the planned cell set and ranks but not on the camera, so a
    digest of the trimmed geometry is stable across a pan and changes exactly
    when a neighbour set changes its shape.  Bytes are compared, not hashes of
    a lossy description: equal digests mean equal geometry, so two different
    neighbour sets cannot collide.  ``None`` when the region cannot be encoded,
    and the caller then refuses to cache rather than key it wrongly.
    """
    if region is None or getattr(region, "is_empty", True):
        return None
    try:
        encoded = shapely.to_wkb(region)
    except Exception:  # noqa: BLE001 - an unencodable trim is just not cached
        return None
    return hashlib.blake2b(bytes(encoded), digest_size=16).hexdigest()


def _trimmed_coverages(plan):
    """Each planned cell's coverage minus the ground better cells claim.

    ``plan`` is in the planner's draw order, best-ranked first, so a cell's
    better ranks are exactly the entries before it.  The subtraction uses whole
    coverages, not the view clip: inside the view it is the same set the
    planner subtracted (``(C - U) n V == (C n V) - (U n V)``), and outside it
    it keeps the mesh consistent with the next pan step.  A cell with unknown
    coverage claims nothing - it draws over the view without masking - so it is
    neither trimmed nor does it trim.  The entry for an untrimmed cell is that
    cell's own coverage object, so the caller can tell the two apart by
    identity.
    """
    covered = None
    trimmed: list = []
    for cell in plan:
        own = cell.coverage if getattr(cell, "coverage_known", False) else None
        if own is None:
            trimmed.append(None)
            continue
        if covered is None:
            trimmed.append(own)
            covered = own
            continue
        try:
            trimmed.append(shapely.difference(own, covered))
            covered = shapely.union(covered, own)
        except Exception:  # noqa: BLE001 - untestable trim is not cached
            trimmed.append(None)
    return trimmed


def _granule_owned(base, planned, view, granule) -> bool:
    """Whether ``granule`` can be meshed for this cell without changing pixels.

    Two conditions, and both are needed.  The planned clip must be exactly
    ``base`` intersected with the view, where ``base`` is the cell's own
    coverage or - where better-ranked cells claim ground - that coverage minus
    their claim: only then does a mesh over the granule add nothing the view
    does not need and nothing an upper cell hides.  And the cell must cover
    part of the granule.  The clip itself is then ``base`` intersected with the
    granule: the same geometry the view needs, plus the border the next pan
    step reaches into.
    """
    if not _planned_matches(base, planned, view):
        return False
    try:
        return not shapely.intersection(base, granule).is_empty
    except Exception:  # noqa: BLE001 - an untestable cell is just not cached
        return False


def _planned_matches(base, planned, view) -> bool:
    """Whether ``planned`` is exactly ``base`` intersected with the view.

    ``base`` may have holes - the ground a better-ranked cell keeps - so an
    envelope shortcut is not enough: a hole that reaches the view edge changes
    the intersection's bounds, which is exactly the case a pan produces.  The
    intersection is computed once and compared with a symmetric difference,
    an exact set test at source precision, so a plan that subtracted more,
    less or a different neighbour is refused.
    """
    view_bounds = _bounds(view)
    if view_bounds is None or _bounds(base) is None or _bounds(planned) is None:
        return False
    try:
        expected = shapely.intersection(base, shapely.box(*view_bounds))
        if expected.is_empty:
            return False
        difference = shapely.area(shapely.symmetric_difference(expected, planned))
        return float(difference) <= 1e-9 * max(1.0, float(shapely.area(expected)))
    except Exception:  # noqa: BLE001 - an untestable cell is just not cached
        return False


def _focus_cache(owner, region) -> None:
    """Point the cache's distance eviction at this assembly's view."""
    bounds = _bounds(region)
    if bounds is None:
        return
    x0, y0, x1, y1 = bounds
    owner.focus(
        0.5 * (x0 + x1),
        0.5 * (y0 + y1),
        max(x1 - x0, y1 - y0),
    )


def _prepare_cell(
    cell,
    index,
    region,
    palette_key,
    projection,
    stroke_pixels,
    owner,
    mesh_scale=None,
    reuse_granules=True,
    trimmed=None,
):
    """Resolve a cell's clip, query region and cache key, without touching it."""
    known = bool(cell.coverage_known)
    own = cell.coverage if known else None
    # The ground this cell keeps once better-ranked cells have claimed theirs.
    # ``trimmed`` is the same object as ``own`` when nothing was subtracted, so
    # an untrimmed cell keeps exactly its old key and old clip.
    base = own
    trim_identity = None
    granule = None
    if known and own is not None:
        if trimmed is not None and trimmed is not own:
            trim_identity = _trim_digest(trimmed)
            if trim_identity is not None:
                base = trimmed
        # Mesh the region the view can see, not the whole cell.  Clipping to
        # ``cell.coverage`` makes the mesh view-independent, but at harbour
        # zoom the coverage is orders of magnitude larger than the view
        # (measured: 14 degrees of source meshed for a 0.08 degree view,
        # 122,757 vertices and 1.7 s for one cell), and every one of those
        # triangles is uploaded to draw nothing.  The plan already carries the
        # visible region, so use it - and clip to the *inflated* view's granule
        # wherever this cell owns all of it, so a pan inside the composition
        # margin keeps the same mesh instead of re-tessellating.
        planned = cell.clip if cell.clip is not None else own
        clip_region = planned
        cacheable = False
        if owner is not None and cell.clip is not None:
            if _same_extent(base, cell.clip):
                # The whole cell - or the whole ground it still owns - is
                # visible, so its clip is already view-free: key the mesh on
                # the cell and keep the previous behaviour.
                granule = _granule_region(cell.clip)
                clip_region = cell.clip
                cacheable = granule is not None
            elif reuse_granules and region is not None:
                candidate = _granule_region(region)
                if candidate is not None and _granule_owned(
                    base, planned, region, candidate
                ):
                    granule = candidate
                    clip_region = shapely.intersection(base, candidate)
                    cacheable = not clip_region.is_empty
        query_region = clip_region
    elif known:
        # A hand-built plan without its own coverage: only the view clip is
        # available, so this mesh is view-dependent and is not reused.
        clip_region = cell.clip
        query_region = cell.clip
        cacheable = False
    else:
        # Unknown coverage: unclipped, over the view, and never cached.
        clip_region = None
        query_region = region
        cacheable = False

    # Point features are clipped by this cell's own visible coverage, not by
    # the ground a better-ranked cell owns.  ``clip_region`` is trimmed to what
    # no better cell claims, which loses a point feature only this cell holds:
    # markers are additive symbols, so it is safe to keep them, and a symbol
    # both cells hold is drawn twice at the same position, as Cairo does.
    point_region = None
    if own is not None and region is not None and hasattr(region, "geom_type"):
        point_region = (
            own if _same_extent(own, clip_region) else shapely.intersection(own, region)
        )
    if point_region is not None:
        # Query the whole visible cell, not just the ground it owns: the
        # records for those unique point features have to reach the mesher
        # before it can keep them.  Fills and lines are still clipped to
        # ``clip_region``, so nothing is painted twice.
        query_region = point_region

    # The drawer's half-pixel tolerance.  A known-coverage cell is meshed
    # against its own coverage, so its tolerance is view-independent within a
    # bucket; an unknown-coverage cell falls back to the queried view.  In the
    # fixed reference frame the tolerance is the finest the chart offers, which
    # is what makes the mesh reusable across zoom levels.
    mesh_projection = _reference_projection(projection, mesh_scale)
    tolerance_projection = (
        mesh_projection if mesh_projection is not None else projection
    )
    tolerance_region = clip_region if clip_region is not None else region
    tolerance = _tolerance_bucket(tolerance_projection, tolerance_region)

    key = None
    if cacheable and tolerance is not None and granule is not None:
        granule_key = _granule_key(granule)
        if mesh_projection is not None:
            # The mesh depends only on the cell and its granule, not on the
            # exact view: scale and tolerance are not part of its identity, so
            # a zoom is a hit and a pan inside the granule is a hit.
            key = MeshCache.key(
                cell.path,
                cell.version,
                None,
                None,
                palette_key,
                stroke_pixels,
                granule_key,
                trim_identity,
            )
        else:
            bucket = _scale_bucket(projection, clip_region)
            if bucket is not None:
                key = MeshCache.key(
                    cell.path,
                    cell.version,
                    bucket,
                    tolerance,
                    palette_key,
                    stroke_pixels,
                    granule_key,
                    trim_identity,
                )
    return _CellTask(
        index=index,
        cell=cell,
        clip_region=clip_region,
        query_region=query_region,
        known=known,
        tolerance=tolerance,
        key=key,
        projection=(mesh_projection if mesh_projection is not None else projection),
        point_region=point_region,
        granule=None if granule is None else _granule_key(granule),
    )


def _marker_scale(task, region):
    """Source degrees per template pixel, for marker geometry.

    Marker templates are in pixels; a caller that omits this gives every
    symbol a one-degree radius, which covers the whole chart.  Probed from the
    same projected span as the tolerance, so a symbol keeps its pixel size.
    """
    bounds = _bounds(region)
    if bounds is None:
        return None
    min_x, min_y, max_x, max_y = bounds
    probe = np.array([[min_x, min_y], [max_x, max_y]], dtype=np.float64)
    try:
        projected = np.asarray(task.projection(probe), dtype=np.float64)
    except Exception:  # noqa: BLE001 - an unprobeable projection has no scale
        return None
    if projected.shape != (2, 2):
        return None
    width_pixels = abs(float(projected[1, 0] - projected[0, 0]))
    height_pixels = abs(float(projected[1, 1] - projected[0, 1]))
    per_pixel = []
    for span, pixels in ((max_x - min_x, width_pixels), (max_y - min_y, height_pixels)):
        if pixels > 0.0 and np.isfinite(pixels) and span > 0.0:
            per_pixel.append(span / pixels)
    if not per_pixel:
        return None
    return float(min(per_pixel))


def _soundings_within(records, region):
    """The SOUNDG records inside ``region``; other classes pass through.

    A widened query (see :func:`_prepare_cell`) returns records the fill clip
    excludes.  Sounding labels are not marker geometry and are not clipped
    again downstream, so they keep the narrower clip and only the marker slice
    grows.
    """
    if region is None:
        return records
    bounds = region.bounds
    kept = []
    for record in records:
        if getattr(record, "source_class", None) != encsounding.SOUNDING_CLASS:
            kept.append(record)
            continue
        envelope = getattr(record, "envelope", None)
        if envelope is not None:
            min_x, max_x, min_y, max_y = envelope
            if (
                max_x < bounds[0]
                or min_x > bounds[2]
                or max_y < bounds[1]
                or min_y > bounds[3]
            ):
                continue
        try:
            geometry = shapely.from_wkb(record.wkb)
        except Exception:  # noqa: BLE001 - the collector skips it too
            kept.append(record)
            continue
        if geometry is not None and shapely.intersects(geometry, region):
            kept.append(record)
    return tuple(kept)


def _mesh_cell(
    task,
    scene,
    *,
    stroke_pixels,
    palette,
    owner,
    cancelled,
    timings,
    pin=None,
):
    """Query and mesh one prepared cell against ``scene``.

    Touches only this cell's records, geometry and cache entry, so any number
    of these may run at once.  ``timings`` is the caller's private accumulator
    (or ``None``): the shared diagnostic mapping is never written here.
    ``pin`` is the build's mesh working set, passed to the cache so this insert
    cannot evict another planned cell's mesh.
    """
    if _cancelled(cancelled):
        return _CellOutcome(cancelled=True)

    cell = task.cell
    entry = (
        owner.get(task.key) if (owner is not None and task.key is not None) else None
    )
    if entry is None:
        query_geometry = _ogr_geometry(task.query_region)
        started = time.perf_counter()
        records = (
            () if query_geometry is None else scene.query(query_geometry, cancelled)
        )
        _stage(timings, "query", started)
        if records is None or _cancelled(cancelled):
            return _CellOutcome(cancelled=True)
        marker_scale = _marker_scale(task, task.clip_region or task.query_region)
        if marker_scale is None:
            marker_scale = 2.0 * task.tolerance if task.tolerance else 1e-9
        mesh = encmesh.build_scene_mesh(
            _ScopedRecords(records),
            # Clip to whatever region the plan gave this cell.  Cells without
            # one are already passed unclipped; the case this changes is a
            # known cell whose plan carries no coverage, which used to reach
            # build_scene_mesh as clip=True with coverage=None and raise.
            coverage=task.clip_region,
            clip=True if task.clip_region is not None else None,
            point_clip=task.point_region,
            stroke_pixels=stroke_pixels,
            projection=task.projection,
            palette=palette,
            marker_scale=marker_scale,
            simplify=task.tolerance,
            timings=timings,
        )
        started = time.perf_counter()
        soundings = encsounding.soundings_from_records(
            _soundings_within(records, task.clip_region)
        )
        _stage(timings, "soundings", started)
        started = time.perf_counter()
        colours = encpalette.vertex_colours(
            mesh.parts, mesh.vertex_count, encpalette.NEUTRAL
        )
        _stage(timings, "colour", started)
        started = time.perf_counter()
        nbytes = _mesh_nbytes(
            mesh.positions,
            soundings,
            mesh.parts,
            mesh.unsupported,
            colours,
        )
        _stage(timings, "account", started)
        entry = CellMesh(
            positions=mesh.positions,
            parts=tuple(mesh.parts),
            unsupported=tuple(mesh.unsupported),
            coverage_state=mesh.coverage_state,
            soundings=soundings,
            colours=colours,
            nbytes=nbytes,
        )
        if owner is not None:
            owner.note_triangulation()
            if task.key is not None:
                owner.put(task.key, entry, task.granule, pin=pin)
    if _cancelled(cancelled):
        return _CellOutcome(cancelled=True)

    # A cache hit skips the cell scene's per-record query entirely; the
    # soundings travel with the mesh because they share its coverage.
    return _CellOutcome(
        contribution=_CellContribution(
            cell=cell,
            scene_key=(scene.path, scene.version),
            scene_nbytes=int(scene.nbytes),
            positions=entry.positions,
            parts=tuple(entry.parts),
            colours=entry.colours,
            soundings=entry.soundings,
            unsupported=tuple(entry.unsupported),
            coverage_state=entry.coverage_state,
        )
    )


#: How often a waiting caller re-checks cancellation while a cell is meshing.
CANCEL_POLL_SECONDS = 0.05


def _await(future: Future, cancelled):
    """Wait for ``future``, polling cancellation; ``None`` means cancelled."""
    while True:
        try:
            return future.result(timeout=CANCEL_POLL_SECONDS)
        except FuturesTimeoutError:
            if _cancelled(cancelled):
                return None


def build_view_scene(
    plan,
    scene_cache,
    *,
    projection,
    stroke_pixels,
    palette,
    cancelled=None,
    bounding=None,
    loader=encscene.open_cell_scene,
    mesh_cache=_DEFAULT,
    timings=None,
    on_cell=None,
    workers=None,
    mesh_scale=None,
    reuse_granules=True,
):
    """Assemble one ordered scene from ``plan``.

    Cells are taken from ``scene_cache``; a resident cell is never re-opened.
    ``loader(path, version, cancelled)`` is only called on a cache miss.

    ``mesh_cache`` is a :class:`MeshCache`.  Omitted, the bounded process-wide
    :func:`default_mesh_cache` is used; ``None`` disables mesh reuse.  A cell
    whose plan carries its own coverage is meshed once, clipped to that
    coverage, and reused on later views: a hit never re-tessellates.  The key is
    ``(path, version, palette, stroke pixels)`` for a mesh built in the fixed
    reference frame described below, and additionally the scale bucket and
    simplification tolerance for a view-dependent mesh, so a changed cell
    version, palette, display scale or tolerance cannot return stale geometry.

    ``mesh_scale`` selects that fixed reference frame.  The caller's
    ``projection`` maps degrees to world pixels at the *current* zoom; a
    multiplier puts it at :data:`MESH_REFERENCE_ZOOM`, the finest chart zoom,
    because Mercator world pixels scale linearly with ``2**zoom`` about a fixed
    origin.  Every cell is then meshed once at the finest tolerance the chart
    offers, so a zoom is a mesh-cache hit instead of a full re-tessellation and
    the chart keeps its picture while the view changes.  The trade is honest:
    at a farther zoom the mesh carries more triangles than that view needs, and
    its stroke geometry (one logical pixel at the reference zoom) is sub-pixel.
    ``None`` (the default) keeps the previous view-dependent behaviour, where a
    zoom across a bucket necessarily rebuilds.

    Every cell mesh is simplified with that tolerance before triangulation
    (``SimplifyPreserveTopology``), which bounds the geometric error to the
    tolerance in source degrees and keeps the cold build proportional to the
    view, not to the source vertex count.

    ``reuse_granules`` (default true) is what makes mesh reuse survive a pan.
    When a cell's own coverage contains the whole planned (inflated) view, the
    mesh is clipped to that view quantised onto a grid a quarter of its span -
    a *granule* - and keyed on it.  A camera move smaller than one grid step
    keeps the granule, its meshes and the key, so no cell re-tessellates; a
    move across a step rebuilds only that cell's granule.  A cell trimmed by a
    better-ranked one is reusable on the same terms: the trim is the cell's
    coverage minus the better cells' ground, which is fixed by the planned cell
    set and ranks, not by the camera, so the mesh is clipped to that remainder
    intersected with the granule and keyed on it.  The key also carries a
    digest of the trim geometry, so a changed better-cell set cannot return a
    stale mesh.  ``False`` restores the view-clipped mesh, one per exact
    viewport, which is what the parity check compares against.

    ``bounding`` is the view to query cells with: an OGR or shapely geometry, a
    viewport with ``get_bbox()``, or ``None`` to use the union of the plan's own
    clips (already intersected with the view).  It only decides the query for
    cells with unknown coverage; a known-coverage cell is queried with its own
    coverage so its mesh does not depend on the view.

    Cells are composited worst-ranked first and best-ranked last, the reverse of
    the plan's draw order, so the best-ranked cell wins visually.  Exact
    ``Difference(covered)`` subtraction is not performed; translucent overlaps
    blend twice and can show a lower cell's strokes through an upper fill.

    ``cancelled`` is polled between cells and after each mesh.  A cancelled
    assembly returns ``None``: its return value is never a partial scene.  A
    cell the cache refuses (oversized) or cannot produce is reported in
    :attr:`ViewScene.skipped` instead of being drawn or silently dropped.

    ``workers`` is how many cell meshes may be built at once.  ``None`` (the
    default) is :func:`_default_workers` - ``min(4, cpu_count)``, overridable
    with the ``SAILWORKS_ENC_MESH_WORKERS`` environment variable - and ``1``
    keeps the sequential path, one cell at a time.  Each task reads only its
    own cell's records and geometry and writes only its own cache entry, so no
    lock is needed outside :class:`MeshCache`.  Whole-cell scenes are still
    opened on the calling thread, in plan order, so ``scene_cache`` is never
    shared and GDAL dataset opening stays serialised.  Assembly and progress
    stay in plan order regardless of which cell finishes first, and the result
    is byte-identical for any worker count.  A cell whose build raises is
    reported in :attr:`ViewScene.skipped` with its error and the rest of the
    plan still assembles; the sequential path keeps raising, as it always did.
    Cancelling stops opening new cells and submitting new work and returns
    ``None`` immediately; meshes already running finish on their own threads
    and are discarded.

    ``on_cell(snapshot, done, total)`` is an optional progressive-publication
    callback, invoked after each resolved cell (a skipped cell counts as
    resolved).  ``snapshot`` is a :class:`ViewScene` holding the ``done``
    completed cells out of the ``total`` planned ones, in the same composite
    order the finished scene will have, so it can be drawn while the rest is
    built.  It carries the same ``skipped`` entries as the final scene will.
    ``done`` always counts a prefix of the plan, so it only ever increases and
    never depends on completion order.  The callback must return ``False`` to
    refuse progress: assembly then stops and :func:`build_view_scene` returns
    ``None``.  Any other return value, including ``None`` itself, keeps
    assembly going.  The default ``None`` never publishes.

    ``timings`` is an opt-in diagnostic accumulator (a mutable mapping).  It
    receives ``"cell_open"`` (loader and cache), ``"query"``, ``"wkb"``,
    ``"simplify"``, ``"clip"``, ``"triangulate"``, ``"soundings"`` and
    ``"colour"`` seconds, summed over the cells this call builds.  Inside
    ``"cell_open"`` the loader reports ``"scene_cache"`` (the resident lookup),
    ``"scene_load"`` (the loader itself) and, on a persisted-store hit,
    ``"store_get"``/``"decode_scene"``; a store miss reports ``"store_build"``
    with ``"ogr_open"``/``"ogr_scan"`` - so a resident scene, a cached file and
    a fresh OGR read are three distinguishable cases.  The parent stages
    contain their children and are not additive across that boundary.  A mesh
    cache hit bypasses the geometry stages, so a warm run shows only
    ``"cell_open"``.  ``None`` (the default) records nothing.  Worker stages
    are merged on the calling thread, so the mapping is still written from one
    thread only.
    """
    plan = tuple(plan)
    if _cancelled(cancelled):
        return None
    workers = _default_workers() if workers is None else int(workers)
    if workers < 1:
        raise ValueError("workers must be at least 1")

    owner = default_mesh_cache() if mesh_cache is _DEFAULT else mesh_cache
    region = _view_region(plan, bounding)
    palette_key = _palette_key(palette)
    hits_before = owner.hits if owner is not None else 0
    misses_before = owner.misses if owner is not None else 0
    built_before = owner.triangulations if owner is not None else 0
    if owner is not None:
        _focus_cache(owner, region)

    # What a cell needs is view-independent data plus its own plan entry, so it
    # is resolved here, once, before any worker exists.  The trim for each cell
    # is recomputed from the plan's own order, so a mesh cached for a trimmed
    # cell carries the identity of the ground it kept.
    trimmed_by_index = _trimmed_coverages(plan)
    tasks = [
        _prepare_cell(
            cell,
            index,
            region,
            palette_key,
            projection,
            stroke_pixels,
            owner,
            mesh_scale=mesh_scale,
            reuse_granules=reuse_granules,
            trimmed=trimmed_by_index[index],
        )
        for index, cell in enumerate(plan)
    ]

    # The working set of this build: every mesh and every cell scene the plan
    # names.  Passed to the caches as pins, so a large cell inserted halfway
    # through the build cannot evict a planned cell's freshly built mesh or
    # scene.  The set is derived from the plan, so it is complete before the
    # first insert, and it is dropped with this call: pins describe one build,
    # not a permanent residency claim.
    mesh_pins = frozenset(task.key for task in tasks if task.key is not None)
    scene_pins = (
        frozenset(scene_cache.key(cell.path, cell.version) for cell in plan)
        if scene_cache is not None
        else frozenset()
    )

    contributions: list[_CellContribution] = []
    skipped: list[SkippedCell] = []

    # The plan is best-first and cells are built in plan order, so a usable
    # scene can be published as soon as the first cell resolves.  Composition
    # still paints the worst-ranked cell first: :func:`_assemble` reverses the
    # completed cells, so a later cell never overdraws a better-ranked one.
    def _progress(done) -> bool:
        """Publish the resolved subset; ``False`` means progress was refused.

        The completed cells are assembled exactly as the finished scene will be,
        so a snapshot is a correct scene in the same composite order rather
        than a prefix that could be overdrawn by a later, better-ranked cell.
        """
        if on_cell is None:
            return True
        snapshot = _assemble(
            contributions, skipped, owner, hits_before, misses_before, built_before
        )
        return on_cell(snapshot, done, len(plan)) is not False

    def _open_scene(cell):
        """Open one cell on the calling thread; a skip or ``None`` (cancelled)."""
        started = time.perf_counter()
        build = lambda cancel, path=cell.path, version=cell.version: loader(
            path, version, cancel
        )
        if timings is None:
            scene = scene_cache.get_or_build(
                cell.path,
                cell.version,
                build,
                cancelled,
                pin=scene_pins,
            )
        else:
            try:
                scene = scene_cache.get_or_build(
                    cell.path,
                    cell.version,
                    build,
                    cancelled,
                    pin=scene_pins,
                    timings=timings,
                )
            except TypeError:
                # A cache that predates the diagnostic kwarg still assembles.
                scene = scene_cache.get_or_build(
                    cell.path,
                    cell.version,
                    build,
                    cancelled,
                    pin=scene_pins,
                )
        _stage(timings, "cell_open", started)
        if scene is not None:
            return scene
        if _cancelled(cancelled):
            return None
        reason = (
            "oversized"
            if _oversized(scene_cache, cell.path, cell.version)
            else "unavailable"
        )
        return SkippedCell(cell.path, cell.version, reason)

    if workers == 1:
        # Sequential: one cell at a time, unchanged.
        for task in tasks:
            if _cancelled(cancelled):
                return None
            scene = _open_scene(task.cell)
            if scene is None:
                return None
            if isinstance(scene, SkippedCell):
                skipped.append(scene)
                # A skipped cell is resolved progress, not a contribution.
                if not _progress(len(contributions) + len(skipped)):
                    return None
                continue
            if _cancelled(cancelled):
                return None
            outcome = _mesh_cell(
                task,
                scene,
                stroke_pixels=stroke_pixels,
                palette=palette,
                owner=owner,
                cancelled=cancelled,
                timings=timings,
                pin=mesh_pins,
            )
            if outcome.cancelled:
                return None
            contributions.append(outcome.contribution)
            if not _progress(len(contributions) + len(skipped)):
                return None
    else:
        executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="enc-mesh"
        )

        def _run(task, scene):
            """One submitted cell; never raises, so one cell cannot lose the view."""
            local = {} if timings is not None else None
            try:
                outcome = _mesh_cell(
                    task,
                    scene,
                    stroke_pixels=stroke_pixels,
                    palette=palette,
                    owner=owner,
                    cancelled=cancelled,
                    timings=local,
                    pin=mesh_pins,
                )
            except Exception as exc:
                return _CellOutcome(
                    skipped=SkippedCell(
                        task.cell.path,
                        task.cell.version,
                        f"error: {type(exc).__name__}: {exc}",
                    ),
                    timings=local,
                )
            return _CellOutcome(
                contribution=outcome.contribution,
                skipped=outcome.skipped,
                cancelled=outcome.cancelled,
                timings=local,
            )

        pending: dict[int, Future] = {}
        skipped_slots: dict[int, SkippedCell] = {}
        next_task = 0
        index = 0
        try:
            while index < len(tasks):
                if _cancelled(cancelled):
                    return None
                # Keep the window full.  Nothing is submitted ahead of the
                # window, so a cancelled build leaves cells that never start.
                while next_task < len(tasks) and len(pending) < workers:
                    ahead = tasks[next_task]
                    scene = _open_scene(ahead.cell)
                    if scene is None:
                        return None
                    if isinstance(scene, SkippedCell):
                        skipped_slots[next_task] = scene
                    else:
                        pending[next_task] = executor.submit(_run, ahead, scene)
                    next_task += 1

                if index in skipped_slots:
                    skipped.append(skipped_slots.pop(index))
                else:
                    outcome = _await(pending.pop(index), cancelled)
                    if outcome is None:
                        return None
                    if outcome.timings and timings is not None:
                        # Merged on this thread, so ``timings`` has one writer.
                        for name, value in outcome.timings.items():
                            timings[name] = timings.get(name, 0.0) + value
                    if outcome.cancelled:
                        return None
                    if outcome.skipped is not None:
                        skipped.append(outcome.skipped)
                    else:
                        contributions.append(outcome.contribution)
                # Publication follows the plan, never completion order, so
                # ``done`` is a prefix length that only increases.
                if not _progress(len(contributions) + len(skipped)):
                    return None
                index += 1
        finally:
            # Meshes already inside GEOS finish on their own threads and are
            # discarded; nothing is waited for, so cancellation returns at once.
            executor.shutdown(wait=False, cancel_futures=True)

    if _cancelled(cancelled):
        return None

    started = time.perf_counter()
    result = _assemble(
        contributions, skipped, owner, hits_before, misses_before, built_before
    )
    _stage(timings, "assemble", started)
    return result
