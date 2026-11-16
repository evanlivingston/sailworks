"""Backend-neutral GPU-ready mesh for ENC area fills and line strokes.

Stage 2 slice under the #213 GPU-first roadmap, reusing the Stage 1 whole-cell
source scene in :mod:`encscene`.  The builder turns real ENC polygons (with
holes and multipolygons) and linear features into plain triangle lists that a
scene-graph backend can upload directly: no Cairo, no raster readback, and one
``DrawTriangles`` call for the whole cell.

Correctness rules:

* Areas are triangulated by ear clipping with the interior rings passed as
  holes, never by an unconstrained centroid-only triangulation, so no triangle
  edge may cross a hole or an outer ring.  Triangles whose centroid is outside
  the polygon interior are dropped and the kept area is checked against the
  polygon area.  A feature whose triangulation cannot be proven exact is
  reported as unsupported instead of being drawn wrong.
* Source and feature order are preserved: parts appear in
  ``CellScene.records`` order, a polygon's fill precedes its outline, and a
  mixed ``GeometryCollection`` keeps its polygonal and lineal parts (the first
  occurring kind is emitted first).  Point parts become procedural marker runs
  from :mod:`encmarkers`, emitted before the area/line runs when the point came
  first, otherwise after them.  Unknown point classes get the generic dot; only
  non-point "other" geometry is reported as unsupported.
* M_COVR coverage follows ``gdalvectorchart.read_coverage`` exactly: only
  ``CATCOV == 1`` declares usable coverage, ``CATCOV == 2`` is subtracted, and
  a feature with absent ``CATCOV`` says nothing either way.  A cell whose
  features are all absent or ``CATCOV == 2`` therefore has *unknown* coverage,
  not full coverage.
* Clipping is opt-in context, not a silent default.  ``clip=True`` (quilt
  context) requires an authoritative coverage polygon and raises
  :class:`UnsupportedGeometry` when none was supplied, so a failed coverage
  read can never masquerade as a full-cell draw.  ``clip=False`` is the
  explicit full-cell / non-quilt case.
* Optional ``simplify`` simplifies source geometry with
  ``SimplifyPreserveTopology`` before clipping and triangulation, using the
  Cairo drawer's half-pixel, power-of-two tolerance.  The simplification error
  is bounded by that tolerance; topology is preserved.
* Line and area-outline strokes are expanded to quads *before* the region
  intersection, so a stroke that crosses the region edge is clipped rather
  than bleeding outside it.
* Width is never assumed screen-constant.  A source-unit ``stroke_width`` is a
  caller-chosen approximation; callers that need a screen-constant width pass
  ``stroke_pixels`` plus the same ``projection`` the backend will use, and the
  width is derived per segment from that projection.
* World/source coordinates stay ``float64`` in :class:`SceneMesh`; the cast to
  the backend's ``float32`` happens in the projection/copy step, so seam
  precision is not lost inside the builder.

Not rendered here: upstream scale ranking (S-52 display priority / cell
``rank`` ordering), Difference(covered) removal, conditional symbology,
soundings, text and textured symbol atlases.  Markers are flat procedural
triangles, so ``scale`` must be supplied per camera for the generic-dot gate.
This is areas plus lines plus point markers in source order, not a
complete quilt or a complete chart, and it makes no performance or fidelity
claim.

Nothing in this module is wired into the default renderer or the viewport.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)

from . import _earcut, encmarkers
from .encpalette import (
    ROLE_FILL,
    ROLE_LINE,
    ROLE_OUTLINE,
    marker_colour,
    part_colour,
)
from .encstroke import UnsupportedGeometry, stroke_lines

AREA_FILL = "area-fill"
AREA_OUTLINE = "area-outline"
LINE = "line"
MARKER = "marker"

#: Geometry types this slice can draw.  Everything else is reported, not dropped.
SUPPORTED_GEOMETRIES = (
    "Polygon",
    "MultiPolygon",
    "LineString",
    "MultiLineString",
    "Point",
    "MultiPoint",
)
#: Present in a cell but deliberately left to the retained CPU/symbol path.
NOT_RENDERED = ("symbols", "text", "soundings")
#: Documented limitation of the stroke mesh.
STROKE_LIMITATION = (
    "segment quads with butt ends; no miter or round joins; "
    "width is screen-constant only when stroke_pixels and projection are supplied"
)

_TRIANGLE_TOLERANCE = 1e-6

#: Ring-vertex count above which an area part is triangulated by GEOS rather
#: than by the pure-Python ear clipper.  Measured on this workload, the Python
#: ear clipper costs about 60 us for an 8-vertex ring against 30 us for GEOS,
#: and the gap widens with size (500 us vs 100 us at 67 vertices, 2.5 ms vs
#: 0.2 ms at 150), so it is kept for small parts only.  Both paths satisfy the
#: same area-reconciliation gate below.
_EARCLIP_MAX_VERTICES = 32

#: Environment variable that selects the area triangulator.  Unset, empty or
#: ``"hybrid"`` keeps the default ear-clip/GEOS split described above; the
#: opt-in ``"native-earcut"`` compiled backend triangulates every part,
#: whatever its size.  Selection never silently falls back: a missing
#: dependency or an unknown value is an explicit error.
_TRIANGULATOR_ENV = "SAILWORKS_ENC_TRIANGULATOR"
TRIANGULATOR_HYBRID = "hybrid"
TRIANGULATOR_NATIVE_EARCUT = "native-earcut"

#: Polygon parts triangulated by the compiled backend in this process, so a
#: measurement harness can prove the opt-in backend actually ran.
_NATIVE_EARCUT_CALLS = 0


def triangulator_backend() -> str:
    """The active area-triangulation backend, from the environment.

    ``SAILWORKS_ENC_TRIANGULATOR`` is read on every call so a process can pick
    a backend before building a scene.  Unknown values raise instead of being
    treated as the default, so a typo cannot silently measure the wrong path.
    """
    raw = os.environ.get(_TRIANGULATOR_ENV)
    if raw is None:
        return TRIANGULATOR_HYBRID
    value = raw.strip().lower()
    if value in ("", TRIANGULATOR_HYBRID):
        return TRIANGULATOR_HYBRID
    if value == TRIANGULATOR_NATIVE_EARCUT:
        return TRIANGULATOR_NATIVE_EARCUT
    raise UnsupportedGeometry(
        f"unknown {_TRIANGULATOR_ENV}={raw!r}; expected {TRIANGULATOR_HYBRID!r}"
        f" (default) or {TRIANGULATOR_NATIVE_EARCUT!r}"
    )


def native_earcut_calls() -> int:
    """Polygon parts triangulated by ``mapbox_earcut`` in this process."""
    return _NATIVE_EARCUT_CALLS


def _load_native_earcut():
    """Import the optional compiled backend, or explain how to provide it."""
    try:
        import mapbox_earcut
    except ImportError as exc:
        raise UnsupportedGeometry(
            f"{_TRIANGULATOR_ENV}={TRIANGULATOR_NATIVE_EARCUT} needs the "
            "compiled 'mapbox_earcut' package, which cannot be imported. "
            "Install it (for example `pip install mapbox_earcut`) or unset "
            f"{_TRIANGULATOR_ENV} to use the default {TRIANGULATOR_HYBRID} "
            "ear-clip/GEOS triangulator."
        ) from exc
    if not hasattr(mapbox_earcut, "triangulate_float64"):
        raise UnsupportedGeometry(
            "the installed 'mapbox_earcut' package has no "
            "triangulate_float64(); install a version that provides it, or "
            f"unset {_TRIANGULATOR_ENV} to use the default triangulator."
        )
    return mapbox_earcut


def _stage(timings, name, started):
    """Accumulate one opt-in stage duration; a no-op when ``timings`` is None."""
    if timings is not None:
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started)


def quantize_tolerance(tolerance: float) -> float:
    """Round a half-pixel tolerance DOWN to a power of two.

    This is the Cairo drawer's rule (:meth:`S57ChartDrawer.draw`): the tolerance
    is half a pixel in source degrees and is floored to a power of two, so
    simplified geometry never exceeds half a pixel of error and the tolerance
    is a small, stable set of buckets that a cache can key on.
    """
    tolerance = float(tolerance)
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        return 0.0
    return 2.0 ** math.floor(math.log2(tolerance))


def view_tolerance(
    lon_span: float,
    lat_span: float,
    width_pixels: float,
    height_pixels: float,
) -> float:
    """The drawer's simplified-geometry tolerance for a view, in degrees.

    ``min(lon_span / width, lat_span / height) * 0.5``, quantised down by
    :func:`quantize_tolerance`.  The drawer uses the allocated viewport size;
    the scene path uses the projected span of the region it is meshing, which
    is the same degrees-per-pixel at the same camera.
    """
    width_pixels = float(width_pixels)
    height_pixels = float(height_pixels)
    if width_pixels <= 0.0 or height_pixels <= 0.0:
        return 0.0
    return quantize_tolerance(
        min(float(lon_span) / width_pixels, float(lat_span) / height_pixels) * 0.5
    )


def _polygons(geometry) -> tuple:
    """Polygon parts of a geometry, in source order."""
    if geometry is None or geometry.is_empty:
        return ()
    if isinstance(geometry, Polygon):
        return (geometry,)
    if hasattr(geometry, "geoms"):  # MultiPolygon, GeometryCollection, ...
        parts = []
        for part in geometry.geoms:
            parts.extend(_polygons(part))
        return tuple(parts)
    return ()


def _lines(geometry) -> tuple:
    """Lineal parts of a geometry, in source order."""
    if geometry is None or geometry.is_empty:
        return ()
    if isinstance(geometry, LineString):
        return (geometry,)
    if isinstance(geometry, MultiLineString):
        return tuple(geometry.geoms)
    if hasattr(geometry, "geoms"):
        parts = []
        for part in geometry.geoms:
            parts.extend(_lines(part))
        return tuple(parts)
    return ()


def _boundary_lines(polygons) -> tuple:
    """Outer and inner rings of every polygon part, in source order."""
    rings = []
    for polygon in polygons:
        rings.append(polygon.exterior)
        rings.extend(polygon.interiors)
    return tuple(rings)


def _ordered_parts(geometry) -> tuple:
    """``(kind, part)`` pairs in source order for a (possibly mixed) geometry.

    ``kind`` is ``"area"`` or ``"line"``.  Point parts (and every point of a
    multipoint) are returned with kind ``"other"`` so the builder can emit a
    marker run for them; non-point other parts are reported as unsupported
    instead of dropping them.
    """
    if geometry is None or geometry.is_empty:
        return ()
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return tuple(("area", part) for part in _polygons(geometry))
    if isinstance(geometry, (LineString, MultiLineString)):
        return tuple(("line", part) for part in _lines(geometry))
    if hasattr(geometry, "geoms"):
        parts = []
        for part in geometry.geoms:
            parts.extend(_ordered_parts(part))
        return tuple(parts)
    return (("other", geometry),)


#: Per-class line style: ``(width in logical pixels, dash (on, off) in
#: logical pixels)``.  Mirrors ``CM93ChartDrawer._render_line``: land lines are
#: heavier, routes are dashed at 4.0/4.0, contours and cables are 0.5 hairlines.
#: Cairo strokes in user units on a surface with ``set_device_scale(dpr)``, so
#: its ``line_width`` and ``set_dash([4.0])`` are logical pixels too; the scene
#: is assembled at ``dpr=1`` and Qt applies the device ratio.  The units match.
LINE_STYLES = {
    "COALNE": (1.5, None),
    "SLCONS": (1.0, None),
    "DEPCNT": (0.5, None),
    "CBLSUB": (0.5, None),
    "FERYRT": (0.8, (4.0, 4.0)),
    "NAVLNE": (0.8, (4.0, 4.0)),
    "TSELNE": (0.8, (4.0, 4.0)),
}

#: Cairo's ``DEFAULT_LINE``: every unlisted line class is a 0.5 px hairline.
DEFAULT_LINE_STYLE = (0.5, None)

#: Per-class area outline style: ``(width, dash)`` in logical pixels,
#: mirroring ``CM93ChartDrawer._get_area_styles``.  ``land_stroke`` carries no
#: Cairo ``line_width``, so ``LNDARE``/``BUAARE``/``BUISGL`` inherit whatever
#: the context last set; they keep the caller's stroke width here.
AREA_OUTLINE_STYLES = {
    "DEPARE": (0.5, None),
    "DRGARE": (0.5, None),
    "LAKARE": (0.5, None),
    "RIVERS": (0.5, None),
    "ACHARE": (0.5, None),
    "ANCBRT": (0.5, None),
    "RESARE": (0.5, (3.0, 3.0)),
    "CTNARE": (0.5, (3.0, 3.0)),
}

#: Area paint order, mirroring ``S57ChartDrawer.AREA_ORDER``: depth areas
#: first, then dredged areas, then land, then every other area class.
AREA_ORDER = {"DEPARE": 0, "DRGARE": 1, "LNDARE": 2}

#: The polygon classes the Cairo drawer fills, mirroring
#: ``S57ChartDrawer.AREA_LAYERS``.  Every other polygon record is administrative
#: or meta geometry (``ADMARE``, ``SEAARE``, ``MAGVAR``, ``M_NPUB``) and must not
#: be filled: an ``ADMARE`` covers the whole cell and paints over the chart.
AREA_LAYERS = {
    "LNDARE",
    "DEPARE",
    "DRGARE",
    "BUAARE",
    "BUISGL",
    "LAKARE",
    "RIVERS",
    "ACHARE",
    "ANCBRT",
    "RESARE",
    "CTNARE",
}


def _geometry_group(geometry) -> int:
    """0 for an area record, 1 for a line record, 2 for everything else.

    The Cairo drawer paints every area, then every line, then every point, so a
    mesh must group the same way.  Emitted in encounter order, a large water
    fill painted after the detail it contains reads as a flat wash.  The group
    comes from the parsed geometry type, which simplification preserves.
    """
    kind = geometry.geom_type
    if kind in ("Polygon", "MultiPolygon"):
        return 0
    if kind in ("LineString", "MultiLineString", "LinearRing"):
        return 1
    if kind == "GeometryCollection":
        kinds = {part.geom_type for part in geometry.geoms}
        if kinds & {"Polygon", "MultiPolygon"}:
            return 0
        if kinds & {"LineString", "MultiLineString"}:
            return 1
    return 2


def _ccw(points: np.ndarray) -> np.ndarray:
    """Return the three points counter-clockwise, so winding is deterministic."""
    ax, ay = points[0]
    bx, by = points[1]
    cx, cy = points[2]
    if (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0.0:
        return points[[0, 2, 1]]
    return points


def _open_ring(ring: np.ndarray) -> np.ndarray:
    """Ring vertices without the duplicated closing vertex."""
    if ring.shape[0] > 1 and np.array_equal(ring[0], ring[-1]):
        return ring[:-1]
    return ring


def _rings_for_earcut(polygon):
    """Flat ``(M, 2)`` ring vertices and hole start indices (in vertices).

    Holes are passed to ear clipping as holes rather than as separate parts, so
    no triangle edge can span a hole or an outer ring.
    """
    exterior = _open_ring(np.asarray(polygon.exterior.coords, dtype=np.float64))
    if exterior.shape[0] < 3:
        return np.empty((0, 2), dtype=np.float64), []
    rings = [exterior]
    for interior in polygon.interiors:
        ring = _open_ring(np.asarray(interior.coords, dtype=np.float64))
        if ring.shape[0] >= 3:
            rings.append(ring)
    hole_indices = []
    total = rings[0].shape[0]
    for ring in rings[1:]:
        hole_indices.append(total)
        total += ring.shape[0]
    return np.concatenate(rings), hole_indices


def _triangulate_earcut(rings: np.ndarray, hole_indices) -> np.ndarray:
    """Ear-clip one polygon part, returning ``(K, 3, 2)`` triangles."""
    indices = _earcut.earcut(rings.ravel().tolist(), hole_indices, 2)
    if not indices:
        return np.empty((0, 3, 2), dtype=np.float64)
    return rings[np.asarray(indices, dtype=np.intp).reshape(-1, 3)]


def _triangulate_native_earcut(rings: np.ndarray, hole_indices) -> np.ndarray:
    """Compiled Earcut of one part, returning ``(K, 3, 2)`` triangles.

    ``_rings_for_earcut`` reports hole *start* offsets (in vertices), whereas
    Earcut wants cumulative ring *end* offsets, so the final entry is the total
    vertex count.  Mapping the starts straight through would make the compiler
    read a hole as the end of the previous ring, which is the ring-mapping
    regression the focused tests pin down.
    """
    global _NATIVE_EARCUT_CALLS
    module = _load_native_earcut()
    ends = np.asarray([*hole_indices, rings.shape[0]], dtype=np.uint32)
    vertices = np.ascontiguousarray(rings, dtype=np.float64)
    try:
        indices = module.triangulate_float64(vertices, ends)
        indices = np.asarray(indices, dtype=np.intp).reshape(-1, 3)
    except Exception as exc:  # the compiled backend rejected this part
        raise UnsupportedGeometry(
            f"native earcut failed for a polygon part: {exc}"
        ) from exc
    _NATIVE_EARCUT_CALLS += 1
    if indices.size == 0:
        return np.empty((0, 3, 2), dtype=np.float64)
    return rings[indices]


def _triangulate_cdt(polygon) -> np.ndarray:
    """GEOS constrained Delaunay of one part, as ``(K, 3, 2)`` triangles."""
    if not hasattr(shapely, "constrained_delaunay_triangles"):
        rings, hole_indices = _rings_for_earcut(polygon)
        return _triangulate_earcut(rings, hole_indices)
    triangulation = shapely.constrained_delaunay_triangles(polygon)
    if triangulation.is_empty:
        return np.empty((0, 3, 2), dtype=np.float64)
    coordinates = shapely.get_coordinates(triangulation)
    # GEOS polygon rings are closed, so every triangle contributes four pairs.
    if coordinates.shape[0] % 4 != 0:
        return np.empty((0, 3, 2), dtype=np.float64)
    return coordinates.reshape(-1, 4, 2)[:, :3]


def triangulate_area(geometry) -> np.ndarray:
    """Triangulation of a polygonal geometry as an ``(N, 2)`` triangle list.

    Each polygon part is triangulated with its interior rings passed as holes,
    so no triangle edge crosses a hole or an outer ring.  Small parts use the
    vendored ear-clipping port (:mod:`._earcut`); larger parts use GEOS'
    constrained Delaunay, because the Python ear clipper's per-vertex cost
    overtakes the GEOS implementation once a ring has more than a few dozen
    vertices (see ``_EARCLIP_MAX_VERTICES``).  Either way the triangles are
    normalised counter-clockwise, degenerate triangles are dropped, a triangle
    whose centroid falls outside the target is discarded, and the kept area is
    reconciled against the polygon area.  Setting
    ``SAILWORKS_ENC_TRIANGULATOR=native-earcut`` replaces both backends with
    the compiled :mod:`mapbox_earcut` implementation for every part.

    Raises :class:`UnsupportedGeometry` when the triangulation cannot be shown
    to cover exactly the polygon interior, so no caller ever draws triangles
    that cross a hole or a coastline.
    """
    backend = triangulator_backend()
    target = geometry if shapely.is_valid(geometry) else shapely.make_valid(geometry)
    polygons = _polygons(target)
    if not polygons:
        raise UnsupportedGeometry("no polygonal area")
    area = sum(polygon.area for polygon in polygons)
    if area <= 0.0:
        raise UnsupportedGeometry("zero-area polygon")

    parts: list[np.ndarray] = []
    # A GEOS constrained-Delaunay result is filtered by centroid: a repair may
    # leave pieces outside the original area.  Ear-clipped triangles are inside
    # their ring by construction, so they only need the degenerate drop; the
    # area gate below still validates them.
    needs_interior: list[np.ndarray] = []
    for polygon in polygons:
        rings, hole_indices = _rings_for_earcut(polygon)
        if rings.shape[0] < 3:
            continue
        if backend == TRIANGULATOR_NATIVE_EARCUT:
            triangles = _triangulate_native_earcut(rings, hole_indices)
            interior = False
        elif rings.shape[0] <= _EARCLIP_MAX_VERTICES:
            triangles = _triangulate_earcut(rings, hole_indices)
            interior = False
        else:
            triangles = _triangulate_cdt(polygon)
            interior = True
        if triangles.shape[0]:
            parts.append(triangles)
            needs_interior.append(np.full(triangles.shape[0], interior, dtype=bool))
    if not parts:
        raise UnsupportedGeometry("triangulation produced no interior triangles")
    triangles = np.concatenate(parts)
    interior_filter = (
        np.concatenate(needs_interior) if len(needs_interior) > 1 else needs_interior[0]
    )

    # One vectorised filter for every part: drop degenerate triangles and any
    # CDT triangle whose centroid falls outside the target.
    a = triangles[:, 0]
    b = triangles[:, 1]
    c = triangles[:, 2]
    abx = b[:, 0] - a[:, 0]
    aby = b[:, 1] - a[:, 1]
    acx = c[:, 0] - a[:, 0]
    acy = c[:, 1] - a[:, 1]
    doubled = abx * acy - aby * acx
    keep = doubled != 0.0  # degenerate sliver, adds no area
    if interior_filter.any():
        subset = np.flatnonzero(interior_filter)
        centroid_x = (a[subset, 0] + b[subset, 0] + c[subset, 0]) / 3.0
        centroid_y = (a[subset, 1] + b[subset, 1] + c[subset, 1]) / 3.0
        keep[subset] &= shapely.contains_xy(target, centroid_x, centroid_y)
    if not keep.any():
        raise UnsupportedGeometry("triangulation produced no interior triangles")
    sign = doubled[keep]
    triangles = np.where(
        (sign < 0.0)[:, None, None], triangles[keep][:, [0, 2, 1]], triangles[keep]
    )
    covered = float(np.abs(sign).sum()) / 2.0
    if abs(covered - area) > _TRIANGLE_TOLERANCE * max(1.0, area):
        raise UnsupportedGeometry(
            "triangulation does not match the polygon area "
            f"({covered:.6g} of {area:.6g})"
        )
    return triangles.reshape(-1, 2)


@dataclass(frozen=True)
class MeshPart:
    """A contiguous vertex run for one feature, in source order.

    ``colour`` is straight RGBA bytes when the mesh was built with a palette,
    otherwise ``None``.  It is style *metadata*: the positions stay
    backend-neutral, and only colour is carried, never width or dash.
    """

    layer_index: int
    source_class: str
    sequence: int
    fid: int
    kind: str
    first_vertex: int
    vertex_count: int
    colour: tuple[int, int, int, int] | None = None

    @property
    def triangle_count(self) -> int:
        return self.vertex_count // 3


#: Part kinds as palette style roles.  Kept local so the mesh module does not
#: depend on palette internals beyond :func:`encpalette.part_colour`.
_STYLE_ROLES = {AREA_FILL: ROLE_FILL, AREA_OUTLINE: ROLE_OUTLINE, LINE: ROLE_LINE}


def _record_field(record, name) -> object:
    """A feature attribute from a real or test record, or ``None``."""
    getter = getattr(record, "attribute", None)
    if callable(getter):
        return getter(name)
    try:
        attributes = dict(getattr(record, "attributes", ()))
    except (TypeError, ValueError):
        return None
    return attributes.get(name)


@dataclass(frozen=True)
class UnsupportedPart:
    """A feature this slice refused to draw, with the reason to show the user."""

    layer_index: int
    source_class: str
    sequence: int
    fid: int
    reason: str


def _unsupported(record, reason) -> UnsupportedPart:
    return UnsupportedPart(
        record.layer_index, record.source_class, record.sequence, record.fid, reason
    )


@dataclass(frozen=True)
class SceneMesh:
    """One cell's triangle soup plus its parts, unsupported features and status."""

    positions: np.ndarray  # (N, 2) float64 source units; cast at projection
    parts: tuple
    unsupported: tuple
    bounds: tuple | None
    coverage_applied: bool = False
    #: "applied" (authoritative M_COVR clipped), "unknown" (no authoritative
    #: region) or "none" (clipping not requested / explicitly disabled).
    coverage_state: str = "none"

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
        """False when any feature is unsupported, so no caller claims a full chart."""
        return not self.unsupported

    def part_positions(self, part: MeshPart) -> np.ndarray:
        return self.positions[part.first_vertex : part.first_vertex + part.vertex_count]


EMPTY_MESH = SceneMesh(
    positions=np.empty((0, 2), dtype=np.float64),
    parts=(),
    unsupported=(),
    bounds=None,
    coverage_applied=False,
    coverage_state="none",
)


def coverage_from_datasource(datasource):
    """Union of the cell's authoritative M_COVR areas, or ``None`` when unknown.

    Semantics match :meth:`gdalvectorchart.read_coverage` exactly: only
    ``CATCOV == 1`` declares usable coverage, ``CATCOV == 2`` is subtracted,
    and a feature with an absent ``CATCOV`` says nothing either way.  A cell
    with no ``CATCOV == 1`` area therefore has *unknown* coverage and returns
    ``None``; absent values are never promoted into an invented clip polygon.
    """
    layer = datasource.GetLayerByName("M_COVR")
    if layer is None:
        return None
    layer.SetSpatialFilter(None)
    layer.SetAttributeFilter(None)
    layer.ResetReading()
    catcov = layer.GetLayerDefn().GetFieldIndex("CATCOV")
    available = []
    denied = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        wkb = geometry.ExportToWkb()
        if wkb is None:
            continue
        parsed = shapely.from_wkb(bytes(wkb))
        parts = _polygons(parsed)
        if not parts:
            continue
        value = feature.GetField(catcov) if catcov >= 0 else None
        if value == 1:
            available.extend(parts)
        elif value == 2:
            denied.extend(parts)
        # Any other value (including absent) is unknown: it neither adds nor
        # removes coverage.
    if not available:
        return None
    result = shapely.union_all(available)
    if denied:
        result = result.difference(shapely.union_all(denied))
    return None if result.is_empty else result


def build_scene_mesh(
    scene,
    stroke_width=None,
    coverage=None,
    *,
    clip=None,
    point_clip=None,
    area_outlines=True,
    stroke_pixels=None,
    projection=None,
    palette=None,
    scale=None,
    marker_scale=1.0,
    simplify=None,
    timings=None,
) -> SceneMesh:
    """Mesh a :class:`encscene.CellScene` in source/feature order.

    Stroke width is explicit and never assumed screen-constant.  Supply either
    ``stroke_width`` (source units, a caller-chosen approximation) or
    ``stroke_pixels`` together with ``projection`` (screen-constant width).

    ``coverage`` is the authoritative M_COVR polygon from
    :func:`coverage_from_datasource`.  ``clip`` is explicit context:

    * ``None`` (default) - clip when a coverage polygon is supplied.
    * ``True`` - quilt context: an authoritative region is *required*; a
      ``coverage`` of ``None`` raises :class:`UnsupportedGeometry` rather than
      drawing the full cell.
    * ``False`` - clipping explicitly not requested; the full cell is drawn
      and reported as ``coverage_state == "none"``.

    ``point_clip`` is the region point features must fall inside; ``None``
    (default) uses the fill clip.  A quilted composition passes the cell's own
    visible coverage here, because a point feature may exist only in the
    lower-ranked cell: markers are additive, so keeping the cell's own point
    inside ground a better cell owns draws the symbol the Cairo drawer draws,
    and a symbol both cells hold is drawn twice at the same position.

    Supported but undrawable features become :class:`UnsupportedPart` entries;
    nothing is dropped silently.  Coordinates stay ``float64``.

    ``palette`` is opt-in style metadata.  ``None`` keeps parts uncoloured.
    A palette name from ``Style.chart_palettes`` (for example ``"cm93"``) or a
    :class:`ChartPalette` resolves each part's flat RGBA from its source class,
    its ``DRVAL1``/``DRVAL2`` depth band and the same area/line encounter
    order.  Only colour is resolved; line width, joins and dashes are still
    reported as :data:`STROKE_LIMITATION`.  Marker colours follow the same
    opt-in rule and use :func:`encpalette.marker_colour`.

    Point features emit :data:`MARKER` runs built from
    :func:`encmarkers.triangles`.  Templates are in local pixels, so
    ``marker_scale`` converts them to source units (source units per template
    pixel) and defaults to 1.0.

    ``scale`` is the camera's display scale for the drawer's scale gates, for
    example the generic dot is hidden above :data:`encmarkers.DOT_MAX_SCALE`.
    The builder has no view scale, so ``None`` (default) applies no gates;
    callers must evaluate them per camera and pass the current scale.

    ``simplify`` is an optional simplification tolerance in source degrees,
    applied with ``SimplifyPreserveTopology`` before clipping and
    triangulation, exactly as :class:`S57ChartDrawer` simplifies before it
    draws.  The Cairo drawer derives it as half a view pixel, quantised down to
    a power of two (:func:`view_tolerance` / :func:`quantize_tolerance`); the
    same rule here makes the tessellation cost follow the view scale, and its
    error is bounded by ``simplify`` source degrees (half a pixel) for the same
    features.  Topology is preserved, so a fill keeps its holes and a line
    stays connected; a feature that simplifies to an empty geometry keeps its
    original geometry rather than disappearing.  ``None`` (default) tessellates
    the source geometry unchanged.

    ``timings`` is an opt-in diagnostic accumulator: a mutable mapping whose
    ``"wkb"``, ``"simplify"``, ``"clip"`` and ``"triangulate"`` keys receive
    the seconds spent in each stage, summed over this call.  ``None`` (the
    default) records nothing and keeps the build path unchanged.
    """
    if (stroke_width is None) == (stroke_pixels is None):
        raise ValueError("supply exactly one of stroke_width or stroke_pixels")
    if stroke_pixels is not None:
        if projection is None:
            raise ValueError("stroke_pixels requires a projection")
        if not float(stroke_pixels) > 0.0:
            raise ValueError("stroke_pixels must be positive")
    elif not float(stroke_width) > 0.0:
        raise ValueError("stroke_width must be positive source units")
    if not float(marker_scale) > 0.0:
        raise ValueError("marker_scale must be positive source units per pixel")

    if clip is True and coverage is None:
        raise UnsupportedGeometry(
            "quilt clipping requires an authoritative M_COVR region; "
            "coverage is unknown, refusing to draw the full cell"
        )
    clip_geometry = None
    if clip is not False and coverage is not None:
        clip_geometry = (
            coverage if shapely.is_valid(coverage) else shapely.make_valid(coverage)
        )

    def stroke(geometry, region=None, pixels=None, dash=None):
        return stroke_lines(
            geometry,
            stroke_width,
            stroke_pixels=stroke_pixels if pixels is None else pixels,
            projection=projection,
            region=region,
            dash=dash,
        )

    records = tuple(getattr(scene, "records", ()))
    chunks: list[np.ndarray] = []
    parts: list[MeshPart] = []
    unsupported: list[UnsupportedPart] = []
    offset = 0

    def emit(record, kind, vertices, colour=None) -> None:
        nonlocal offset
        if vertices is None or len(vertices) == 0:
            return
        if colour is None and palette is not None:
            colour = part_colour(
                record.source_class,
                _STYLE_ROLES[kind],
                _record_field(record, "DRVAL1"),
                _record_field(record, "DRVAL2"),
                palette,
            )
        parts.append(
            MeshPart(
                layer_index=record.layer_index,
                source_class=record.source_class,
                sequence=record.sequence,
                fid=record.fid,
                kind=kind,
                first_vertex=offset,
                vertex_count=int(len(vertices)),
                colour=colour,
            )
        )
        chunks.append(vertices)
        offset += int(len(vertices))

    def marker_vertices(points, kind):
        """All points' template triangles, translated to source coordinates."""
        template = np.asarray(encmarkers.triangles(kind), dtype=np.float64) * float(
            marker_scale
        )
        runs = []
        for point in points:
            anchor = np.array([point.x, point.y], dtype=np.float64)
            runs.append(template + anchor)
        return np.concatenate(runs).reshape(-1, 2)

    parsed: list[tuple[int, object, object, int, tuple]] = []
    for index, record in enumerate(records):
        started = time.perf_counter()
        try:
            geometry = shapely.from_wkb(record.wkb)
        except Exception as error:  # noqa: BLE001 - report, never drop silently
            unsupported.append(_unsupported(record, f"unreadable WKB: {error}"))
            continue
        finally:
            _stage(timings, "wkb", started)
        if geometry is None or geometry.is_empty:
            unsupported.append(_unsupported(record, "empty geometry"))
            continue
        parsed.append((index, record, geometry, _geometry_group(geometry)))

    ordered_records = sorted(
        parsed,
        key=lambda item: (
            item[3],
            AREA_ORDER.get(item[1].source_class, 3) if item[3] == 0 else 0,
            item[0],
        ),
    )

    for _index, record, geometry, _group in ordered_records:
        if stroke_pixels is None:
            # LINE_STYLES holds screen-pixel widths and dashes.  A caller that
            # supplied a source-unit ``stroke_width`` has no pixel contract to
            # convert them with - its width is already the caller's own
            # approximation - so the caller's width governs every class.
            line_pixels, line_dash = None, None
        else:
            line_pixels, line_dash = LINE_STYLES.get(
                record.source_class, DEFAULT_LINE_STYLE
            )

        if simplify:
            started = time.perf_counter()
            simplified = shapely.simplify(
                geometry, float(simplify), preserve_topology=True
            )
            _stage(timings, "simplify", started)
            # A sub-pixel feature must not vanish: keep the source geometry
            # when simplification empties it.  The error stays bounded by the
            # tolerance either way.
            if simplified is not None and not simplified.is_empty:
                geometry = simplified

        started = time.perf_counter()
        ordered = _ordered_parts(geometry)
        polygonal = tuple(part for kind, part in ordered if kind == "area")
        if polygonal and record.source_class not in AREA_LAYERS:
            # The drawer fills only its area whitelist; administrative and sea
            # areas cover the cell and would paint over the chart.
            polygonal = ()
        lineal = tuple(part for kind, part in ordered if kind == "line")
        other = tuple(part for kind, part in ordered if kind == "other")
        points = tuple(part for part in other if part.geom_type == "Point")
        # Markers are additive symbols, so a point may be kept in a region the
        # fill clip excludes.  ``point_clip`` is the cell's own visible coverage
        # in a quilt, where ``clip_geometry`` has already been trimmed to the
        # ground no better-ranked cell owns: a cell can hold a point feature the
        # better cell lacks, and clipping it by ground ownership loses it
        # outright.  Duplicating a symbol at the same position costs one run and
        # paints identically, which is what the Cairo drawer does.
        point_geometry = clip_geometry if point_clip is None else point_clip
        if point_geometry is not None:
            points = tuple(point for point in points if point_geometry.covers(point))
        other = tuple(part for part in other if part.geom_type != "Point")
        _stage(timings, "ordered", started)

        # Fill is clipped by exact intersection before triangulation; strokes
        # are clipped after expansion so they cannot bleed past the edge.
        fill_vertices = None
        fill_polygons = ()
        if polygonal:
            area = (
                polygonal[0] if len(polygonal) == 1 else GeometryCollection(polygonal)
            )
            if clip_geometry is not None:
                started = time.perf_counter()
                area = area.intersection(clip_geometry)
                _stage(timings, "clip", started)
            if not area.is_empty:
                fill_polygons = _polygons(area)
                started = time.perf_counter()
                try:
                    fill_vertices = triangulate_area(area)
                except UnsupportedGeometry as error:
                    fill_polygons = ()
                    unsupported.append(_unsupported(record, str(error)))
                finally:
                    _stage(timings, "triangulate", started)

        line_vertices = None
        if lineal:
            line_geometry = GeometryCollection(lineal) if len(lineal) > 1 else lineal[0]
            if clip_geometry is not None:
                started = time.perf_counter()
                line_geometry = line_geometry.intersection(clip_geometry)
                _stage(timings, "clip", started)
            if not line_geometry.is_empty:
                started = time.perf_counter()
                line_vertices = stroke(
                    line_geometry,
                    region=clip_geometry,
                    pixels=line_pixels,
                    dash=line_dash,
                )
                _stage(timings, "stroke_line", started)

        outline_vertices = None
        if area_outlines and fill_polygons:
            started = time.perf_counter()
            rings = _boundary_lines(fill_polygons)
            if rings:
                if stroke_pixels is None:
                    outline_pixels, outline_dash = None, None
                else:
                    outline_pixels, outline_dash = AREA_OUTLINE_STYLES.get(
                        record.source_class, (stroke_pixels, None)
                    )
                outline_vertices = stroke(
                    MultiLineString(rings),
                    region=clip_geometry,
                    pixels=outline_pixels,
                    dash=outline_dash,
                )
            _stage(timings, "stroke_outline", started)

        marker_kind = None
        marker_run = None
        if points:
            marker_kind = encmarkers.classify(
                record.source_class, _record_field(record, "COLOUR")
            )
            # A scale-gated symbol is intentionally hidden, not unsupported.
            if marker_kind is not None and encmarkers.is_visible(marker_kind, scale):
                started = time.perf_counter()
                marker_run = marker_vertices(points, marker_kind)
                _stage(timings, "markers", started)
        marker_colour_value = (
            marker_colour(marker_kind)
            if marker_run is not None and palette is not None
            else None
        )

        # The drawer paints each area before the lines and points of the same
        # feature, whatever order the source geometry lists them in.
        emit(record, AREA_FILL, fill_vertices)
        emit(record, AREA_OUTLINE, outline_vertices)
        emit(record, LINE, line_vertices)
        emit(record, MARKER, marker_run, marker_colour_value)

        if points and marker_kind is None:
            unsupported.append(
                _unsupported(
                    record, "sounding text is not rendered by the marker slice"
                )
            )

        for part in other:
            unsupported.append(
                _unsupported(
                    record, f"{part.geom_type} is not rendered by the area/line slice"
                )
            )
        if not ordered:
            unsupported.append(
                _unsupported(
                    record,
                    f"{geometry.geom_type} is not rendered by the area/line slice",
                )
            )

    if chunks:
        positions = np.concatenate(chunks)
        min_x, min_y = positions.min(axis=0)
        max_x, max_y = positions.max(axis=0)
        bounds = (float(min_x), float(min_y), float(max_x), float(max_y))
    else:
        positions = np.empty((0, 2), dtype=np.float64)
        bounds = None
    return SceneMesh(
        positions=positions,
        parts=tuple(parts),
        unsupported=tuple(unsupported),
        bounds=bounds,
        coverage_applied=clip_geometry is not None,
        coverage_state="applied" if clip_geometry is not None else "none",
    )
