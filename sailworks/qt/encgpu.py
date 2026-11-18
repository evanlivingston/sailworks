"""Opt-in scene-graph upload of an :mod:`encmesh` triangle mesh.

Stage 2 slice: one ``QQuickItem`` whose single ``QSGGeometryNode`` draws a
cell's area and line triangles with ``DrawTriangles``.  There is no Cairo, no
image upload and no raster readback on this path.  This is a diagnostic
adapter, not a styled viewport: it draws one flat colour and makes no claim
about matching the raster renderer's per-feature style.

Threading and scheduling boundary
---------------------------------
``set_mesh``/``set_projection``/``set_origin``/``set_color`` are callers' side
of the boundary and must run on the GUI thread.  They only record state and
call ``update()``; they mark at most one dirty flag.  ``updatePaintNode`` runs
on Qt's render thread and performs the projection/upload **only** when one of
those flags is set (or when Qt has recreated the node).  An unchanged frame
therefore runs no Python projection and copies no vertex data.  A projection
callable passed to ``set_projection`` runs on the render thread, once per
changed frame, so it must be cheap, pure and thread-safe; precompute an
affine transform on the GUI thread instead of doing scene-graph queries inside
it.

Scene-graph lifetime
--------------------
Qt owns any node returned from ``updatePaintNode`` and may delete it, together
with the geometry and material it owns, when the item leaves the scene graph.
The item therefore never trusts a previously created wrapper: when Qt passes
``None`` (or a node that is not the one last returned, or wrappers that have
been invalidated) it rebuilds a fresh ``QSGGeometryNode``/``QSGGeometry``/
``QSGFlatColorMaterial`` and marks the node as owning its geometry and
material.  Ownership flags are set in exactly one place, on every rebuild, so
Qt frees the buffers exactly once.

``QQuickItem.releaseResources`` is the GUI-thread hook Qt runs when the item is
removed from a window or the scene graph is invalidated.  The override drops
the Python references (and marks geometry and material dirty) *before* calling
``super()``, so the next attach starts from no usable children and builds fresh
ones.  It performs no explicit ``delete`` and touches no node, geometry or
material method: Qt still owns and frees those objects, and the item never
reaches a wrapper that teardown may have deleted.

Precision
---------
Source positions are promoted to ``float64`` before the optional local origin
subtraction and the projection, and only the final item-local result is cast
to ``float32``.  Subtracting the camera origin in ``float64`` keeps
scene-scale coordinates from quantising to the client's float32 grid before
they are made local.

Empty meshes allocate zero vertices, so a cleared or clipped-away cell leaves
no stale triangles behind.  Unsupported features stay visible through
``unsupported_count`` rather than being drawn wrong.

Static frame (default for a presented view scene)
-------------------------------------------------
A camera pan changes only the projection callable, and re-projecting every
vertex for it cost 6.3 ms of a 8.5 ms frame at 106k vertices.  The vertices are
therefore projected **once** per scene change into a *reference screen frame*:
the float64 projection runs at upload, the result is stored as ``float32``
offsets from the scene's own bbox centre, and each contributing cell becomes
one child ``QSGGeometryNode``.  A single ``QSGTransformNode`` then carries a
``QMatrix4x4`` from the reference frame to the current item pixels, recomputed
on the CPU from three probe vertices per frame (pan and zoom are affine in
screen pixels, so the matrix is exact: the Mercator warp happened once, at
upload, in ``float64``).  A fourth probe validates the map; a projection that
is not affine for this scene rebuilds the reference frame instead of drawing an
approximation, which is the only way the picture can differ from the CPU path.

Per frame the cost is four probe projections, one matrix write and one bbox
test per cell — independent of the vertex count.  ``ENC_LEGACY_CPU_PROJECT=1``
restores the original per-frame float64 re-projection for A/B comparison.

Colour mode
-----------
``colored=True`` uploads ``QSGGeometry.defaultAttributes_ColoredPoint2D()``
vertices: ``x``/``y`` as ``float32`` plus four unsigned RGBA bytes, 12 bytes
per vertex.  Part colours come from :mod:`encpalette` (``"cm93"`` depth bands,
land/sea area styles and the flat line colours) and are expanded in the mesh's
existing part order, so one ordered node draws all of them.  Triangulation and
node count never change with feature count.  A base ``QSGVertexColorMaterial``
with ``QSGMaterial.Flag.Blending`` set enables blending for the alpha channel.

Only colour is matched.  Line width, joins and dash patterns are not
represented by per-vertex colour, and Qt's premultiplied scene-graph blend
makes translucent fills approximate rather than Cairo-identical.  Symbols are
flat marker triangles; sounding text is a second textured glyph node built
from :class:`sailworks.qt.glyphatlas.GlyphAtlas`, not QML text items.

:meth:`EncMeshItem.set_view_scene` accepts an assembled
:class:`encview.ViewScene` (positions, parts, per-part colours, cell spans) and
uploads it through the same single coloured node, so a multi-cell scene costs
one geometry buffer and one node rather than one per feature or per cell.

Nothing creates this item by default: no QML file imports it and the viewport
does not use it.
"""

from __future__ import annotations

import ctypes
import os

import numpy as np
from PySide6.QtGui import QColor, QMatrix4x4
from PySide6.QtQuick import (
    QQuickItem,
    QSGFlatColorMaterial,
    QSGGeometry,
    QSGGeometryNode,
    QSGMaterial,
    QSGNode,
    QSGTextureMaterial,
    QSGTransformNode,
    QSGVertexColorMaterial,
)

from sailworks.rendering.vectordrawer import encsounding
from sailworks.rendering.vectordrawer.encmesh import EMPTY_MESH, SceneMesh
from sailworks.rendering.vectordrawer.encpalette import NEUTRAL, vertex_colours
from sailworks.qt.glyphatlas import GlyphAtlas

try:  # PySide6 ships shiboken6; the guard keeps a stale C++ wrapper detectable.
    import shiboken6
    from shiboken6 import isValid as _wrapper_is_valid
except ImportError:  # pragma: no cover - only when PySide6 is not installed
    shiboken6 = None

    def _wrapper_is_valid(obj) -> bool:
        return obj is not None


def _vertex_address(geometry: QSGGeometry) -> int:
    """Return ``QSGGeometry``'s vertex buffer as a plain integer address.

    PySide6 versions differ: newer releases hand back a
    ``shiboken6.Shiboken.VoidPtr``, older ones a plain ``int``.  On PySide6
    6.12 ``int(VoidPtr)`` is the real buffer address (it matches the pointer in
    ``repr``), so the conversion does not truncate.  A null address means Qt
    gave us no writable buffer; writing through it would fault inside
    ``memmove`` on the render thread, so it is rejected here instead.
    """
    data = geometry.vertexData()
    address = data if isinstance(data, int) else int(data)
    if address <= 0:
        raise RuntimeError("QSGGeometry has no vertex buffer to upload into")
    return address


#: Byte stride of each vertex layout this module writes.  ``Point2D`` is x/y
#: float32 (8 bytes), ``ColoredPoint2D`` adds four straight RGBA bytes (12),
#: ``TexturedPoint2D`` is x/y/u/v float32 (16).
#:
#: ``QSGGeometry`` stores the raw ``AttributeSet`` pointer it is handed; it
#: does not copy the set and does not keep it alive.  A temporary passed at
#: construction dies with the Python local that produced it, and every later
#: ``attributeCount()``/``sizeOfVertex()`` read then touches freed memory,
#: which is why those accessors looked like they returned garbage.  The sets
#: below are therefore built once and cached for the lifetime of the process,
#: so a geometry built from them reports a stable layout for as long as Qt
#: keeps it.
POINT_STRIDE = 8

#: ``AttributeSet`` objects held for the process lifetime, keyed by layout.
#: See the lifetime note above: nothing else keeps these alive.
_PINNED_ATTRIBUTE_SETS = {}


def _pinned_attribute_set(name, build):
    """Return the one process-lifetime ``AttributeSet`` for ``name``.

    ``build`` runs at most once.  The result is cached in
    :data:`_PINNED_ATTRIBUTE_SETS`, outliving every caller and therefore every
    ``QSGGeometry`` constructed with it.
    """
    existing = _PINNED_ATTRIBUTE_SETS.get(name)
    if existing is None:
        existing = build()
        _PINNED_ATTRIBUTE_SETS[name] = existing
    return existing


def _point2d_attributes():
    """Return the pinned ``defaultAttributes_Point2D()`` layout (8 bytes).

    Every plain-mesh geometry must be built from this exact object, never from
    a fresh temporary; see :data:`_PINNED_ATTRIBUTE_SETS`.
    """

    def build():
        factory = getattr(QSGGeometry, "defaultAttributes_Point2D", None)
        if factory is None:
            raise RuntimeError(
                "plain ENC meshes require QSGGeometry.defaultAttributes_Point2D()"
            )
        return factory()

    return _pinned_attribute_set("Point2D", build)


def _vertex_row_bytes(array: np.ndarray) -> int:
    """Return the bytes one vertex occupies in ``array``.

    A structured ``(N,)`` array carries the whole layout in ``dtype.itemsize``;
    a plain ``(N, 2)`` float32 array carries one coordinate per item, so its
    row is ``itemsize`` times the trailing dimensions (8 bytes for
    ``Point2D``).  Comparing ``dtype.itemsize`` alone reported 4 bytes for that
    case and rejected every valid ``(N, 2)`` upload.
    """
    trailing = int(np.prod(array.shape[1:], dtype=np.intp))
    return array.dtype.itemsize * trailing


def _upload_vertices(geometry: QSGGeometry, vertices: np.ndarray, stride: int) -> int:
    """Copy ``vertices`` into ``geometry``'s buffer after checking the layout.

    The copy length is derived from the vertex count and the layout stride the
    caller built the geometry with, never from whatever the array happens to
    contain: a 12-byte ``ColoredPoint2D`` array written into an 8-byte
    ``Point2D`` buffer would overrun it by half its length and corrupt the
    scene graph's heap.  ``allocate`` is expected to have run already; a
    capacity that does not match the source is rejected rather than copied.
    Every check runs before ``memmove`` so a rejected upload leaves the buffer
    untouched.
    """
    array = np.asarray(vertices)
    if array.ndim < 1:
        raise ValueError("vertex array must have at least one dimension")
    if not array.flags["C_CONTIGUOUS"]:
        raise ValueError(
            "vertex array must be C-contiguous; a strided view cannot be "
            "copied row for row"
        )
    row_bytes = _vertex_row_bytes(array)
    if row_bytes != stride:
        raise ValueError(
            f"vertex array is {row_bytes} bytes per vertex; "
            f"this geometry uses a {stride}-byte layout"
        )
    count = int(array.shape[0])
    if count == 0:
        return 0
    if array.nbytes != count * row_bytes:
        raise ValueError(
            f"vertex array holds {array.nbytes} bytes but {count} vertices of "
            f"{row_bytes} bytes were prepared"
        )
    allocated = int(geometry.vertexCount())
    if allocated != count:
        raise ValueError(
            f"geometry allocated {allocated} vertices but {count} were prepared"
        )
    # Reliable now that the attribute set is pinned for the process lifetime;
    # it catches a geometry built with a different layout than this upload.
    geometry_stride = int(geometry.sizeOfVertex())
    if geometry_stride != stride:
        raise ValueError(
            f"geometry vertex stride is {geometry_stride} bytes; this upload "
            f"expects a {stride}-byte layout"
        )
    ctypes.memmove(
        ctypes.c_void_p(_vertex_address(geometry)),
        ctypes.c_void_p(array.ctypes.data),
        count * stride,
    )
    return count


#: ``QSGGeometry.defaultAttributes_ColoredPoint2D()`` layout: x/y float32 plus
#: four unsigned bytes of straight RGBA, 12 bytes per vertex.
VERTEX_DTYPE = np.dtype([("x", "<f4"), ("y", "<f4"), ("rgba", "4u1")])

_QCOLOR_TO_RGBA = ("red", "green", "blue", "alpha")


def _blended_vertex_color_material() -> QSGVertexColorMaterial:
    """Return a base ``QSGVertexColorMaterial`` with alpha blending enabled.

    ``QSGMaterial.setFlag`` mutates the C++ flag word, so a plain base material
    is enough and Qt's node ownership stays authoritative.  A Python subclass
    would add a wrapper whose virtual ``flags()`` could outlive the C++ object
    during scene-graph teardown, which is the lifecycle hazard this adapter
    avoids elsewhere.

    Qt's scene-graph blend mode is premultiplied while the mesh stores straight
    RGBA, so translucent parts are approximate compared with the Cairo path.
    That is recorded as a limitation, not claimed as exact matching.
    """
    material = QSGVertexColorMaterial()
    material.setFlag(QSGMaterial.Flag.Blending, True)
    return material


def _colored_attributes():
    """Return the pinned ``defaultAttributes_ColoredPoint2D()`` layout (12 bytes)."""

    def build():
        factory = getattr(QSGGeometry, "defaultAttributes_ColoredPoint2D", None)
        if factory is None:
            raise RuntimeError(
                "coloured ENC meshes require PySide6 6.12 "
                "QSGGeometry.defaultAttributes_ColoredPoint2D()"
            )
        return factory()

    return _pinned_attribute_set("ColoredPoint2D", build)


def _project_positions(mesh, projection, origin) -> np.ndarray:
    """Validate ``mesh.positions`` and return float64 item-local positions."""
    positions = np.asarray(mesh.positions)
    if positions.ndim != 2 or positions.shape[1] != 2:
        raise ValueError("mesh positions must be an (N, 2) array")
    if positions.shape[0] == 0:
        return np.empty((0, 2), dtype=np.float64)
    if projection is None and origin is None:
        return np.asarray(positions, dtype=np.float64)

    source = np.asarray(positions, dtype=np.float64)
    if origin is not None:
        offset = np.asarray(origin, dtype=np.float64).reshape(1, 2)
        source = source - offset
    if projection is not None:
        source = np.asarray(projection(source), dtype=np.float64)
        if source.shape != (mesh.vertex_count, 2):
            raise ValueError("projection must return an (N, 2) array")
    return source


#: How far past the item rect a triangle may reach and still be uploaded.
#: A stroke is one logical pixel wide and a marker is a few; soundings are a
#: separate glyph node.  A triangle whose bounding box misses the padded rect
#: draws nothing on the item, so keeping it only costs projection and upload.
CLIP_PAD = 32.0

#: Default logical-pixel padding the ENC scene is composed over.  The scene
#: layer plans and meshes from a viewport padded by
#: :func:`sailworks.qt.viewporttransform.enc_compose_margin`, which never goes
#: below :data:`sailworks.rendering.overlayrenderer.MARGIN`; the item adopts the
#: exact figure from the published scene through :meth:`EncMeshItem.set_compose_margin`.
SCENE_MARGIN = 256.0


def _visible_triangle_mask(positions: np.ndarray, clip) -> np.ndarray:
    """Mask over triangle-soup vertices: keep triangles near the item rect.

    ``positions`` is an ``(N, 2)`` float array with three vertices per
    triangle, and ``clip`` is ``(x0, y0, x1, y1)`` in item pixels.  A triangle
    is kept when its bounding box intersects the rect, so a triangle too large
    to have a vertex inside but still covering the item is never dropped.  The
    mask preserves order and triangle boundaries, so what the buffer draws is
    unchanged; the dropped vertices were entirely outside the item.
    """
    x0, y0, x1, y1 = (float(value) for value in clip)
    if positions.shape[0] % 3:
        raise ValueError("a triangle soup has three vertices per triangle")
    triangles = positions.reshape(-1, 3, 2)
    xs = triangles[:, :, 0]
    ys = triangles[:, :, 1]
    inside = (
        (xs.max(axis=1) >= x0)
        & (xs.min(axis=1) <= x1)
        & (ys.max(axis=1) >= y0)
        & (ys.min(axis=1) <= y1)
    )
    return np.repeat(inside, 3)


#: ``QSGGeometry.defaultAttributes_TexturedPoint2D()`` layout: x/y and u/v as
#: float32, 16 bytes per vertex.
TEXT_VERTEX_DTYPE = np.dtype([("x", "<f4"), ("y", "<f4"), ("u", "<f4"), ("v", "<f4")])


def _textured_attributes():
    """Return the pinned ``defaultAttributes_TexturedPoint2D()`` layout (16 bytes)."""

    def build():
        factory = getattr(QSGGeometry, "defaultAttributes_TexturedPoint2D", None)
        if factory is None:
            raise RuntimeError(
                "textured glyph quads require "
                "QSGGeometry.defaultAttributes_TexturedPoint2D()"
            )
        return factory()

    return _pinned_attribute_set("TexturedPoint2D", build)


def glyph_quads(labels, atlas) -> np.ndarray:
    """Lay out ``labels`` as textured quads, ``(6 * glyphs, 4)`` float64.

    Each label's ``anchor`` is baseline-left, matching the drawing origin the
    Cairo drawer passes to ``show_text``: the first glyph starts at
    ``(label.x, label.y - atlas.ascent)`` and advances by that glyph's
    ``QFontMetricsF`` advance.  Rows are two triangles per glyph in label then
    text order.  Unknown characters are skipped, never substituted.
    """
    quads = []
    for label in labels:
        top = float(label.y) - atlas.ascent
        bottom = top + atlas.cell_height
        cursor = float(label.x)
        for character in label.text:
            glyph = atlas.glyph(character)
            if glyph is None:
                continue
            x0 = cursor
            x1 = cursor + glyph.advance
            u0, v0, u1, v1 = glyph.uv
            quads.append(((x0, top, u0, v0), (x1, top, u1, v0), (x0, bottom, u0, v1)))
            quads.append(
                ((x1, top, u1, v0), (x1, bottom, u1, v1), (x0, bottom, u0, v1))
            )
            cursor = x1
    if not quads:
        return np.empty((0, 4), dtype=np.float64)
    return np.asarray(quads, dtype=np.float64).reshape(-1, 4)


def apply_glyph_labels(geometry: QSGGeometry, labels, atlas) -> int:
    """Write ``labels`` into ``geometry`` as textured quads; return the count."""
    vertices = glyph_quads(labels, atlas).astype(np.float32)
    count = int(vertices.shape[0])
    geometry.allocate(count)
    if not count:
        return 0
    return _upload_vertices(geometry, vertices, TEXT_VERTEX_DTYPE.itemsize)


def apply_mesh(
    geometry: QSGGeometry, mesh: SceneMesh, projection=None, *, origin=None, clip=None
) -> int:
    """Copy ``mesh`` triangles into ``geometry`` and return the vertex count.

    ``positions`` are promoted to ``float64`` and, when given, the local
    ``origin`` is subtracted before ``projection`` runs.  Only the final
    ``(N, 2)`` item-local array is cast to ``float32``.  ``projection`` maps an
    ``(N, 2)`` ``float64`` source-position array to item-local positions.  A
    mesh with no vertices allocates zero vertices, which draws nothing and
    cannot keep a previous frame's content.

    ``clip`` is an optional ``(x0, y0, x1, y1)`` item rect: triangles whose
    bounding box misses it are dropped before the upload.  They draw nothing
    on the item, so this changes the buffer, never the picture.
    """
    projected = _project_positions(mesh, projection, origin)
    if projected.shape[0] == 0:
        geometry.allocate(0)
        return geometry.vertexCount()

    output = np.ascontiguousarray(projected, dtype=np.float32)
    if output.shape != (mesh.vertex_count, 2):
        raise ValueError("positions must be an (N, 2) array")
    if clip is not None:
        output = output[_visible_triangle_mask(output, clip)]
    geometry.allocate(output.shape[0])
    _upload_vertices(geometry, output, POINT_STRIDE)
    return geometry.vertexCount()


def _premultiplied_colours(colors: np.ndarray) -> np.ndarray:
    """Return a premultiplied copy of straight ``(N, 4)`` uint8 RGBA.

    Qt Quick's vertex colours are premultiplied.  Straight RGBA makes every
    translucent fill additive, and a chart's overlapping areas then saturate to
    white.
    """
    colors = np.array(colors, dtype=np.uint8, copy=True)
    colors[:, :3] = (
        colors[:, :3].astype(np.uint16) * colors[:, 3:4].astype(np.uint16) // 255
    ).astype(np.uint8)
    return colors


def apply_colored_mesh(
    geometry: QSGGeometry,
    mesh: SceneMesh,
    projection=None,
    *,
    origin=None,
    default_color=NEUTRAL,
    clip=None,
) -> int:
    """Write 12-byte ``ColoredPoint2D`` vertices for one coloured mesh.

    Part colours from :class:`encmesh.MeshPart` are expanded in part order, so
    the vertex buffer preserves the area-then-line (or line-first) encounter
    order and a single node can draw every feature without one node per
    feature.  Positions go through the same float64 origin/projection path as
    :func:`apply_mesh`.

    ``clip`` is :func:`apply_mesh`'s optional item rect.  The plan and the
    meshes stay padded, so dropping the off-item triangles here is what keeps
    a pan from projecting, colouring and uploading the whole composed frame
    for the half of it the item cannot show.
    """
    projected = _project_positions(mesh, projection, origin)
    if projected.shape[0] == 0:
        geometry.allocate(0)
        return geometry.vertexCount()

    positions = np.ascontiguousarray(projected, dtype=np.float32)
    pieces = getattr(mesh, "parts", ())
    colors = _premultiplied_colours(
        vertex_colours(pieces, positions.shape[0], default_color)
    )
    vertices = np.empty(positions.shape[0], dtype=VERTEX_DTYPE)
    vertices["x"] = positions[:, 0]
    vertices["y"] = positions[:, 1]
    vertices["rgba"] = colors
    if clip is not None:
        # Fancy indexing returns a compacted copy, which ``_upload_vertices``
        # requires; the triangle order and its colours are preserved.
        vertices = vertices[_visible_triangle_mask(positions, clip)]
    geometry.allocate(vertices.shape[0])
    _upload_vertices(geometry, vertices, VERTEX_DTYPE.itemsize)
    return geometry.vertexCount()


#: ``ENC_LEGACY_CPU_PROJECT=1`` keeps the original per-frame ``float64``
#: re-projection for the presented view-scene path, so the two paths can be
#: A/B compared and the old behaviour stays reachable.
LEGACY_CPU_PROJECT = os.environ.get("ENC_LEGACY_CPU_PROJECT", "").strip() not in (
    "",
    "0",
)

#: Largest disagreement, in item pixels, allowed between the reference-frame
#: matrix and the ``float64`` CPU projection before the frame is re-projected
#: instead.  Pan and zoom are affine in screen pixels, so a healthy frame
#: agrees to ``float64`` rounding (``~1e-9``); anything above this means the
#: projection is not affine over the scene and a matrix would be wrong.
AFFINE_TOLERANCE = 0.005

#: Process-wide count of non-affine fallback activations.  The static-frame
#: path normally transforms one reference frame with a matrix; when
#: :meth:`EncMeshItem._frame_coeffs` returns ``None`` the item rebuilds and
#: re-uploads the scene instead.  Captures report this so a good frame time is
#: never mistaken for the matrix path being used.
_FALLBACK_ACTIVATIONS = 0


def fallback_activations() -> int:
    """Non-affine re-projections performed since start (or the last reset)."""
    return _FALLBACK_ACTIVATIONS


def reset_fallback_activations() -> None:
    """Zero the process-wide fallback counter (diagnostics only)."""
    global _FALLBACK_ACTIVATIONS
    _FALLBACK_ACTIVATIONS = 0


def _affine_from_probes(reference: np.ndarray, current: np.ndarray):
    """Solve the affine map taking three ``reference`` points to ``current``.

    Both are ``(3, 2)`` ``float64`` arrays.  Returns ``(a, b, c, d, e, f)`` with
    ``x' = a*x + b*y + c`` and ``y' = d*x + e*y + f``, or ``None`` when the
    probes are collinear and the map is not determined.  The solve runs in
    ``float64``; only the resulting ``QMatrix4x4`` is ``float32``.
    """
    reference = np.asarray(reference, dtype=np.float64)
    current = np.asarray(current, dtype=np.float64)
    if reference.shape[0] < 3 or current.shape != reference.shape:
        return None
    design = np.column_stack((reference[:3], np.ones(3)))
    determinant = float(np.linalg.det(design))
    scale = float(np.max(np.abs(reference[:3]))) or 1.0
    if not abs(determinant) > 1e-9 * scale * scale:
        return None
    solution = np.linalg.solve(design, current[:3])
    return (
        float(solution[0, 0]),
        float(solution[1, 0]),
        float(solution[2, 0]),
        float(solution[0, 1]),
        float(solution[1, 1]),
        float(solution[2, 1]),
    )


def _apply_affine(coeffs, points: np.ndarray) -> np.ndarray:
    """Map ``(N, 2)`` points through the ``(a, b, c, d, e, f)`` affine."""
    a, b, c, d, e, f = coeffs
    points = np.asarray(points, dtype=np.float64)
    return np.column_stack(
        (
            a * points[:, 0] + b * points[:, 1] + c,
            d * points[:, 0] + e * points[:, 1] + f,
        )
    )


def _affine_bounds(coeffs, bounds):
    """Map an axis-aligned ``(x0, y0, x1, y1)`` box; return its new box."""
    x0, y0, x1, y1 = bounds
    corners = np.array(((x0, y0), (x1, y0), (x0, y1), (x1, y1)), dtype=np.float64)
    mapped = _apply_affine(coeffs, corners)
    return (
        float(mapped[:, 0].min()),
        float(mapped[:, 1].min()),
        float(mapped[:, 0].max()),
        float(mapped[:, 1].max()),
    )


class _CellGeometry:
    """One static vertex buffer: a cell's vertices and their reference bbox."""

    __slots__ = ("vertices", "bounds")

    def __init__(self, vertices: np.ndarray, bounds):
        self.vertices = vertices
        self.bounds = bounds


class _StaticFrame:
    """A presented scene projected once into a reference screen frame.

    ``cells`` holds one :class:`_CellGeometry` per contributing cell in
    composite order.  ``reference`` holds the reference-frame positions of up
    to four probe vertices and ``samples`` their source positions, which is all
    a later frame needs to recover the pan/zoom matrix.
    """

    __slots__ = ("cells", "reference", "samples")

    def __init__(self, cells, reference, samples):
        self.cells = cells
        self.reference = reference
        self.samples = samples


def build_static_frame(
    mesh, projection=None, *, origin=None, default_color=NEUTRAL
) -> "_StaticFrame | None":
    """Project ``mesh`` once and return its reference-frame vertex buffers.

    The projection (which for ENC is Web Mercator plus the camera offset) runs
    here, in ``float64``, and never again while the scene and camera stay
    related by an affine map.  Vertices are stored as ``float32`` offsets from
    the scene's own bbox centre, so a stored coordinate stays within a few
    thousand pixels: the ``float32`` ulp there is ~``2e-4`` px, well inside the
    ``0.01`` px the matrix path is required to preserve.  Baking the warp at
    upload is what makes the residual transform exactly affine.
    """
    positions = _project_positions(mesh, projection, origin)
    count = int(positions.shape[0])
    if count == 0:
        return None
    colours = _premultiplied_colours(
        vertex_colours(getattr(mesh, "parts", ()), count, default_color)
    )
    base = (positions.min(axis=0) + positions.max(axis=0)) / 2.0
    local = np.ascontiguousarray(positions - base, dtype=np.float32)

    spans = tuple(getattr(mesh, "cells", ()))
    ranges = [(int(span.first_vertex), int(span.vertex_count)) for span in spans] or [
        (0, count)
    ]
    cells = []
    for first, length in ranges:
        if length <= 0:
            cells.append(_CellGeometry(np.empty(0, dtype=VERTEX_DTYPE), None))
            continue
        chunk = np.empty(length, dtype=VERTEX_DTYPE)
        chunk["x"] = local[first : first + length, 0]
        chunk["y"] = local[first : first + length, 1]
        chunk["rgba"] = colours[first : first + length]
        window = local[first : first + length].astype(np.float64)
        cells.append(
            _CellGeometry(
                chunk,
                (
                    float(window[:, 0].min()),
                    float(window[:, 1].min()),
                    float(window[:, 0].max()),
                    float(window[:, 1].max()),
                ),
            )
        )

    # Probes: the extreme vertices of the reference frame, which are the
    # best-conditioned triple for the solve and the widest baseline for the
    # residual check.
    xs, ys = local[:, 0], local[:, 1]
    probes = []
    for index in (
        int(np.argmin(xs)),
        int(np.argmax(xs)),
        int(np.argmin(ys)),
        int(np.argmax(ys)),
        int(np.argmax(xs.astype(np.float64) + ys.astype(np.float64))),
    ):
        if index not in probes:
            probes.append(index)
        if len(probes) == 4:
            break
    reference = (positions[probes] - base).astype(np.float64)
    sources = np.asarray(mesh.positions, dtype=np.float64)[probes]
    return _StaticFrame(tuple(cells), reference, sources)


class EncMeshItem(QQuickItem):
    """A QQuickItem that presents one SceneMesh as a single DrawTriangles node.

    ``colored=False`` (the default) is the original flat-colour adapter: a
    ``Point2D`` geometry and a ``QSGFlatColorMaterial``.  ``colored=True``
    switches to the PySide6 6.12 ``ColoredPoint2D`` layout and a
    ``QSGVertexColorMaterial`` whose ``Blending`` flag is set, so the mesh's
    per-part palette colours travel in the vertex buffer and one ordered node
    still draws the whole cell.  Qt may split that node internally; the adapter never reorders
    parts and never creates one node per feature.

    :meth:`set_view_scene` presents an assembled multi-cell
    :class:`encview.ViewScene` through that same ordered coloured node: its
    concatenated positions and per-cell parts upload unchanged, so one node
    still draws every contributing cell.

    Sounding text is a second, child ``QSGGeometryNode`` using a
    ``QSGTextureMaterial`` and one glyph atlas texture.  Its quads are built
    from the view scene's source soundings with the current projection and
    scale, so the 32x16 dedupe stays per camera instead of in cached geometry.
    The child is appended after the triangle node, so labels paint on top.
    """

    _DEFAULT_COLOR = QColor(120, 155, 190, 255)

    def __init__(
        self,
        parent=None,
        *,
        colored=False,
        glyph_pixel_size=11.0,
        texture_factory=None,
        text_enabled=True,
        legacy_project=None,
    ):
        super().__init__(parent)
        self.setFlag(QQuickItem.Flag.ItemHasContents, True)
        #: Per-item override of :data:`LEGACY_CPU_PROJECT`: ``True`` keeps the
        #: per-frame float64 re-projection, ``None`` follows the module setting.
        self._legacy_project = (
            LEGACY_CPU_PROJECT if legacy_project is None else bool(legacy_project)
        )
        self._mesh = EMPTY_MESH
        #: The assembled multi-cell scene currently presented, if any.  Kept by
        #: reference so cell spans and skipped-cell reasons stay inspectable.
        self._view_scene = None
        self._color = QColor(self._DEFAULT_COLOR)
        self._projection = None
        self._origin = None
        self._generation = 0
        self._upload_count = 0
        self._colored = bool(colored)
        #: Attribute layout of the currently built geometry, so toggling the
        #: mode rebuilds the scene-graph children instead of writing the wrong
        #: vertex stride into them.
        self._built_colored = False
        # Python references to the current scene-graph objects.  They are
        # replaced wholesale whenever Qt recreates the node; never reused after
        # the node that owns them is gone.
        self._node = None
        self._geometry = None
        self._material = None
        self._geometry_dirty = True
        self._material_dirty = True
        # Static reference frame: the projected scene, the transform node that
        # carries its matrix, and one geometry node per cell.
        self._static = None
        self._built_static = False
        self._content_node = None
        self._cell_nodes = []
        self._cell_vertex_counts = []
        self._cell_visible = ()
        #: One-shot flag: an explicit empty scene clears the presented cells on
        #: the next upload, while an empty rebuild keeps the last complete
        #: frame on screen.
        self._drop_cells_on_upload = False
        #: How far past the item the presented scene was composed.  Culling
        #: must keep the cells in that ring: they are what a pan brings into
        #: view before the replacement arrives.
        self._compose_margin = SCENE_MARGIN
        self._matrix_count = 0
        #: Times this item re-projected instead of transforming its reference
        #: frame with a matrix (see :meth:`_frame_coeffs`).  Reported by the
        #: app-level capture harness so a matrix-path frame is not confused
        #: with a fallback frame.
        self.fallback_activations = 0

        # Sounding text state.  The atlas image is CPU-side and reusable; the
        # texture and text node belong to whatever node Qt currently owns.
        self._glyph_pixel_size = float(glyph_pixel_size)
        self._texture_factory = texture_factory
        self._scale = None
        self._atlas = None
        self._atlas_dpr = 1.0
        self._texture = None
        #: True once texture creation was attempted for the current atlas, so a
        #: windowless/None result is also cached instead of retried each frame.
        self._texture_ready = False
        self._text_node = None
        self._text_geometry = None
        self._text_material = None
        self._text_geometry_dirty = True
        self._text_dirty = True
        self._text_labels = ()
        self._text_size = None
        self._text_upload_count = 0
        self._texture_upload_count = 0
        #: Sounding labels are on by default: the textured child is detached
        #: with ``removeChildNode`` and its texture owned by exactly one
        #: material, which survives the build/present/teardown cycles the
        #: earlier teardown crash needed (see ``_detach_text_node``).  Turning
        #: it off draws triangles and markers only.
        self._text_enabled = bool(text_enabled)

    @property
    def mesh(self) -> SceneMesh:
        return self._mesh

    @property
    def colored(self) -> bool:
        return self._colored

    @property
    def view_scene(self):
        """The presented ``ViewScene``, or ``None`` on the flat mesh path."""
        return self._view_scene

    def _default_rgba(self) -> tuple[int, int, int, int]:
        return tuple(getattr(self._color, channel)() for channel in _QCOLOR_TO_RGBA)

    @property
    def generation(self) -> int:
        """Bumped on every accepted mesh, so a stale item is detectable."""
        return self._generation

    @property
    def unsupported_count(self) -> int:
        return len(self._mesh.unsupported)

    @property
    def is_complete(self) -> bool:
        return self._mesh.is_complete

    @property
    def upload_count(self) -> int:
        """Number of projection/vertex uploads performed for this item."""
        return self._upload_count

    @property
    def matrix_update_count(self) -> int:
        """Number of matrix writes on the static path (one per presented frame)."""
        return self._matrix_count

    @property
    def static_mode(self) -> bool:
        """True when this item is drawing static buffers through a matrix."""
        return self._built_static

    @property
    def visible_cells(self) -> tuple:
        """Per-cell visibility from the last cull, in composite order."""
        return self._cell_visible

    def shown_vertex_count(self) -> int:
        """Vertices the presented mask currently draws.

        A culled cell keeps its buffer and is drawn with a zero vertex count,
        so the sum of the cell buffers is *not* what is on screen.  This is
        the number a state probe and the adoption policy read: zero means the
        frame is attached but draws nothing.
        """
        counts = self._cell_vertex_counts
        mask = self._cell_visible
        if not counts:
            return 0
        if len(mask) != len(counts):
            return int(sum(counts))
        return int(sum(count for count, shown in zip(counts, mask) if shown))

    def scene_covers_view(self) -> bool:
        """Whether any cell of the presented frame reaches the item's viewport.

        The retained frame draws through a matrix solved for the current
        camera, so its own reference bbox mapped by that matrix is what the
        user actually sees.  A frame composed for a camera a zoom or a long pan
        left behind can map entirely outside the item: keeping it then draws
        exactly nothing, which is indistinguishable from a blank chart.
        """
        if not self._cell_nodes or self._static is None:
            return False
        boxes = [cell.bounds for cell in self._static.cells if cell.bounds is not None]
        if not boxes:
            return False
        bounds = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        width = float(self.width())
        height = float(self.height())
        if width <= 0.0 or height <= 0.0:
            return False
        coeffs = self._frame_coeffs()
        if coeffs is None:
            # The re-projected fallback draws from the current camera; it is
            # not proof of a blank frame, so do not treat it as one.
            return True
        x0, y0, x1, y1 = _affine_bounds(coeffs, bounds)
        return not (x1 < 0.0 or y1 < 0.0 or x0 > width or y0 > height)

    @property
    def text_upload_count(self) -> int:
        """Number of glyph-quad uploads performed for this item."""
        return self._text_upload_count

    @property
    def texture_upload_count(self) -> int:
        """Number of glyph atlas textures created for this item."""
        return self._texture_upload_count

    @property
    def labels(self) -> tuple:
        """The sounding labels laid out for the last presented frame."""
        return self._text_labels

    @property
    def atlas(self):
        """The cached glyph atlas, or ``None`` before the first sounding frame."""
        return self._atlas

    @property
    def text_node(self):
        """The child textured node, or ``None`` when no labels are pending."""
        return self._text_node

    def set_mesh(self, mesh) -> bool:
        mesh = EMPTY_MESH if mesh is None else mesh
        if mesh is self._mesh:
            return False
        self._mesh = mesh
        self._view_scene = None
        # A new mesh replaces the picture, an empty one included: the caller
        # asked for that content, so no cell of the previous frame survives it.
        self._static = None
        self._generation += 1
        self._geometry_dirty = True
        self._text_dirty = True
        self.update()
        return True

    def clear_mesh(self) -> bool:
        return self.set_mesh(None)

    def set_view_scene(self, view_scene) -> bool:
        """Present one assembled multi-cell :class:`encview.ViewScene`.

        The scene's contiguous float64 ``positions`` and ordered ``parts`` are
        uploaded through the existing single batched coloured geometry node, so
        part order is preserved exactly as assembled and per-part colours
        travel in the vertex buffer.  Presenting a view scene enables the
        coloured layout; the flat ``set_mesh``/``set_color`` path is unaffected.
        ``None`` clears to empty geometry.  Re-presenting the scene already on
        display is a no-op, so an unchanged frame does not re-upload.

        An empty scene (for example a plan whose only cells were oversized and
        skipped) allocates zero vertices rather than leaving the previous
        frame's triangles on screen.  The scene reference is kept in
        :attr:`view_scene` so cell attribution and skip reasons remain
        inspectable after upload.
        """
        mesh = EMPTY_MESH if view_scene is None else view_scene
        if view_scene is self._view_scene and mesh is self._mesh:
            return False
        self._view_scene = view_scene
        self._mesh = mesh
        self._colored = True
        # A replacement scene is what the viewport asked for.  A scene with no
        # contributing part is an explicit "nothing to draw" and takes the
        # presented cells down; a scene with parts rebuilds them on the next
        # paint.  Retention is for a rebuild that produces nothing, not for a
        # scene the caller replaced.
        self._static = None
        self._drop_cells_on_upload = not getattr(mesh, "parts", ())
        self._generation += 1
        self._geometry_dirty = True
        self._material_dirty = True
        self._text_dirty = True
        self._prepare_glyphs()
        self.update()
        return True

    def set_colored(self, colored) -> bool:
        """Toggle per-vertex palette colour; rebuilds the attribute layout."""
        colored = bool(colored)
        if colored == self._colored:
            return False
        self._colored = colored
        self._static = None
        self._geometry_dirty = True
        self._material_dirty = True
        self.update()
        return True

    def set_color(self, color) -> bool:
        color = QColor(color)
        if color == self._color:
            return False
        self._color = color
        if self._colored:
            # The flat colour is the fallback baked into vertices for parts
            # without palette metadata, so it is a geometry change here.
            self._static = None
            self._geometry_dirty = True
        else:
            self._material_dirty = True
        self.update()
        return True

    def set_projection(self, projection) -> bool:
        if projection is self._projection:
            return False
        self._projection = projection
        if not self._static_mode():
            # The legacy path bakes the projection into the vertex buffer.  The
            # static frame keeps the vertices and moves a matrix instead, so a
            # pan only costs one uniform write.
            self._geometry_dirty = True
        self._text_dirty = True
        self.update()
        return True

    def set_origin(self, origin) -> bool:
        """Set the local camera origin subtracted before projection.

        Changing it re-uploads on the next frame; the subtraction itself runs
        in ``float64`` so scene-scale coordinates keep their precision.  The
        reference frame stays attached: the matrix path re-projects its sample
        points with the new origin, so a pan costs no rebuild and leaves no
        blank frame behind.
        """
        if origin is not None:
            origin = (float(origin[0]), float(origin[1]))
        if origin == self._origin:
            return False
        self._origin = origin
        # The origin is an argument of the projection, not a screen-space
        # offset, but ``_frame_coeffs`` re-projects the reference frame's own
        # samples with the *current* origin, so the matrix path already handles
        # this exactly.  Keeping ``_static`` attached means a pan never blanks
        # the chart; a non-affine projection still rebuilds in
        # ``_update_static``.
        self._geometry_dirty = True
        self._text_dirty = True
        self.update()
        return True

    def set_scale(self, scale) -> bool:
        """Set the camera's display scale for the sounding gate.

        ``None`` applies no gate, matching :func:`encmesh.build_scene_mesh`.
        The gate lives here, per camera, not in the cached scene soundings.
        """
        if scale is not None:
            scale = float(scale)
        if scale == self._scale:
            return False
        self._scale = scale
        self._text_dirty = True
        self.update()
        return True

    @property
    def scale(self):
        return self._scale

    def _visible_clip(self):
        """The composed scene rect padded for strokes and markers; ``None`` unsized.

        Item-local coordinates: the content node's matrix is solved in the
        reference frame, so a cell's transformed bbox is comparable to a rect
        here.  The rect is the composition's own extent, not the item's:
        ``mapContent`` translates the item, so a clip on the item rect is a
        clip on the *compose-time viewport*, and panning then hides cells that
        are already on screen.  Culling still drops everything the composition
        cannot show, which is all it is for.  ``_compose_margin`` is the ring
        the scene layer actually composed, published with the scene.
        """
        width = float(self.width())
        height = float(self.height())
        if width <= 0.0 or height <= 0.0:
            return None
        margin = self._compose_margin
        return (
            -margin - CLIP_PAD,
            -margin - CLIP_PAD,
            width + margin + CLIP_PAD,
            height + margin + CLIP_PAD,
        )

    def set_compose_margin(self, margin) -> bool:
        """Adopt the padding the presented scene was composed over.

        The scene layer plans and meshes over a viewport grown by
        :func:`sailworks.qt.viewporttransform.enc_compose_margin`; the item
        keeps that ring attached so a pan into it stays drawn.  ``None`` (an
        older or flat scene) falls back to :data:`SCENE_MARGIN`.
        """
        margin = SCENE_MARGIN if margin is None else float(margin)
        if margin == self._compose_margin:
            return False
        self._compose_margin = margin
        # Force the mask to be recomputed and re-applied on the next paint;
        # no buffer is touched.
        self._cell_visible = ()
        self.update()
        return True

    @property
    def compose_margin(self):
        return self._compose_margin

    def _forget_scene_graph(self) -> None:
        """Drop Python references to scene-graph objects Qt may have deleted.

        Used on teardown and before a rebuild.  The incoming node is never
        reused, so an invalid wrapper cannot reach a Qt call.
        """
        self._node = None
        self._geometry = None
        self._material = None
        self._content_node = None
        self._cell_nodes = []
        # The per-cell bookkeeping must go with the nodes: a stale list zipped
        # against the next build's nodes silently truncates the mask.
        self._cell_vertex_counts = []
        self._cell_visible = ()
        self._text_node = None
        self._text_geometry = None
        self._text_material = None
        self._discard_texture()
        self._geometry_dirty = True
        self._material_dirty = True
        self._text_geometry_dirty = True

    def _discard_texture(self) -> None:
        """Drop the glyph texture reference.

        ``QSGTextureMaterial`` takes ownership (``setOwnsTexture(True)``), so
        the texture is freed when the material is destroyed.  Deleting it here
        while a live material still points at it left the material with a
        dangling texture — the crash this replacement avoids.  Dropping the
        Python reference is all this item may do.
        """
        self._texture = None
        self._texture_ready = False

    def _detach_text_node(self, node) -> None:
        """Remove the textured child, letting Qt delete it exactly once."""
        text_node = self._text_node
        if (
            node is not None
            and text_node is not None
            and shiboken6 is not None
            and _wrapper_is_valid(text_node)
        ):
            try:
                node.removeChildNode(text_node)
            except Exception:  # pragma: no cover - Qt already dropped it
                pass
        self._text_node = None
        self._text_geometry = None
        self._text_material = None
        self._text_geometry_dirty = True

    def _prepare_glyphs(self):
        """Build the glyph atlas here, on the GUI thread.

        ``QImage``/``QFontMetricsF`` must not be constructed on the render
        thread, so the atlas is built from the GUI-thread setters and simply
        consumed by ``updatePaintNode``.  A widget without a window yet has no
        device pixel ratio; the atlas is then built on the next setter.
        """
        try:
            self._ensure_atlas()
        except Exception:  # noqa: BLE001 - labels are optional; never break a frame
            self._atlas = None

    def _ensure_atlas(self):
        """Build or reuse the glyph atlas at the window's device pixel ratio."""
        window = self.window()
        dpr = 1.0
        if window is not None:
            try:
                dpr = float(window.devicePixelRatio())
            except Exception:  # pragma: no cover - window without a screen
                dpr = 1.0
        dpr = max(1.0, dpr)
        if self._atlas is None or dpr != self._atlas_dpr:
            if self._atlas is not None:
                self._discard_texture()
            self._atlas = GlyphAtlas(self._glyph_pixel_size, device_pixel_ratio=dpr)
            self._atlas_dpr = dpr
            self._text_geometry_dirty = True
        return self._atlas

    def _ensure_texture(self):
        """Create the atlas texture once per atlas; ``None`` means no texture."""
        if self._texture_ready:
            return self._texture
        # The atlas is built on the GUI thread; creating the texture from its
        # finished image is the only part that belongs to the render thread.
        atlas = self._atlas
        if atlas is None:
            return None
        texture = None
        if self._texture_factory is not None:
            texture = self._texture_factory(atlas.image)
        else:
            window = self.window()
            if window is not None:
                texture = window.createTextureFromImage(atlas.image)
        self._texture = texture
        self._texture_ready = True
        self._texture_upload_count += 1
        return texture

    def _presented_labels(self) -> tuple:
        """Screen-space labels for the scene under the current camera."""
        scene = self._view_scene
        soundings = getattr(scene, "soundings", None)
        if soundings is None or len(soundings) == 0:
            return ()
        screen = encsounding.project_points(soundings, self._projection, self._origin)
        points = (
            (float(x), float(y), float(depth))
            for (x, y), depth in zip(screen, soundings[:, 2])
        )
        return encsounding.collect_soundings(
            points,
            scale=self._scale,
            width=self.width(),
            height=self.height(),
        )

    def _build_text_node(self, node) -> None:
        """Attach a fresh textured child after the triangle node."""
        self._detach_text_node(node)
        text_node = QSGGeometryNode()
        geometry = QSGGeometry(_textured_attributes(), 0)
        geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
        geometry.setVertexDataPattern(QSGGeometry.DataPattern.DynamicPattern)
        material = QSGTextureMaterial()
        # Sounding ink is premultiplied in the atlas image, so alpha blending
        # is what lets the glyph edges read against the chart triangles.
        material.setFlag(QSGMaterial.Flag.Blending, True)
        if self._texture is not None:
            material.setTexture(self._texture)
            # Exactly one owner: the material frees the texture when it dies.
            # The item only ever drops its reference (see ``_discard_texture``).
            try:
                material.setOwnsTexture(True)
            except AttributeError:  # pragma: no cover - older PySide6
                pass
        text_node.setGeometry(geometry)
        text_node.setMaterial(material)
        text_node.setFlag(QSGNode.Flag.OwnsGeometry)
        text_node.setFlag(QSGNode.Flag.OwnsMaterial)
        node.appendChildNode(text_node)
        self._text_node = text_node
        self._text_geometry = geometry
        self._text_material = material
        self._text_geometry_dirty = True

    def releaseResources(self):  # noqa: N802 - Qt override
        """Forget Qt-owned scene-graph wrappers before Qt deletes them.

        Qt calls this on the GUI thread when the item leaves its window or the
        window's scene graph is invalidated.  Dropping the Python references
        and marking both parts dirty means the next ``updatePaintNode`` sees no
        usable children and rebuilds, instead of reusing a wrapper whose C++
        object teardown may already have deleted.  Ownership flags are left
        untouched: Qt still owns the geometry and material it was given and
        frees them exactly once.
        """
        self._forget_scene_graph()
        super().releaseResources()

    def _static_mode(self) -> bool:
        """True when the scene is presented as a static frame plus a matrix.

        Only a presented :class:`encview.ViewScene` on the coloured layout uses
        the reference frame; the flat ``set_mesh`` path keeps its original
        single-node behaviour, and :data:`LEGACY_CPU_PROJECT` (or the item's
        ``legacy_project``) forces the original per-frame re-projection.
        """
        return (
            not self._legacy_project and self._colored and self._view_scene is not None
        )

    def _frame_coeffs(self):
        """The reference frame -> current item-pixel affine, or ``None``.

        Four probe vertices are projected with the *current* projection: three
        solve the map and the fourth measures how far the true projection is
        from affine over this scene.  A healthy pan/zoom agrees to float64
        rounding because the Mercator warp is already baked into the reference
        frame; anything above :data:`AFFINE_TOLERANCE` returns ``None`` and the
        caller re-projects instead.
        """
        frame = self._static
        if frame is None:
            return None
        source = frame.samples
        if self._origin is not None:
            source = source - np.asarray(self._origin, dtype=np.float64).reshape(1, 2)
        if self._projection is None:
            current = source
        else:
            current = np.asarray(self._projection(source), dtype=np.float64)
            if current.shape != frame.reference.shape:
                return None
        coeffs = _affine_from_probes(frame.reference, current)
        if coeffs is None:
            return None
        if current.shape[0] > 3:
            deviation = float(
                np.abs(_apply_affine(coeffs, frame.reference[3:]) - current[3:]).max()
            )
            if not deviation <= AFFINE_TOLERANCE:
                return None
        return coeffs

    def _apply_matrix(self, coeffs) -> None:
        """Write the cell-local -> item-pixel matrix onto the content node."""
        content = self._content_node
        if content is None:
            return
        a, b, c, d, e, f = coeffs
        content.setMatrix(
            QMatrix4x4(
                a,
                b,
                0.0,
                c,
                d,
                e,
                0.0,
                f,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
            )
        )
        self._matrix_count += 1

    def _cull_cells(self, coeffs) -> None:
        """Keep only cells whose transformed bbox reaches the item; O(cells).

        Qt 6 has no ``Invisible`` scene-graph flag, so a culled cell keeps its
        node and buffer in place and is drawn with a zero vertex count.  The
        child list therefore never changes after the first build, which keeps
        the recorded tree valid while an offscreen grab renders it.
        """
        frame = self._static
        content = self._content_node
        if frame is None or content is None:
            return
        clip = self._visible_clip()
        mask = []
        for cell in frame.cells:
            if cell.bounds is None:
                mask.append(False)
                continue
            if clip is None:
                mask.append(True)
                continue
            x0, y0, x1, y1 = _affine_bounds(coeffs, cell.bounds)
            outside = x1 < clip[0] or x0 > clip[2] or y1 < clip[1] or y0 > clip[3]
            mask.append(not outside)
        mask = tuple(mask)
        # The mask is recorded for diagnostics only: it is never trusted as
        # proof that the geometries still draw what it says.  A previous
        # frame (or anything else holding the nodes) can zero a cell's
        # vertex count while this mask is unchanged, and skipping the
        # reconciliation below on "same mask" then left the chart blank
        # until an unrelated scene-graph rebuild reset the cache.
        self._cell_visible = mask
        # The child list is static once built: detaching and re-attaching
        # nodes while an offscreen grab renders the very same subtree faults
        # in native code.  A culled cell is instead drawn with zero vertices,
        # which needs no buffer write, so the resident vertex data is
        # untouched and the tree the renderer recorded stays valid.
        for node, count, visible in zip(
            self._cell_nodes, self._cell_vertex_counts, mask
        ):
            applied = count if visible else 0
            geometry = node.geometry()
            if geometry is None or geometry.vertexCount() == applied:
                # Nothing to tell the renderer, so leave the node clean.  This
                # is the steady-state path: a pan that keeps the same mask
                # never marks anything dirty.
                continue
            geometry.setVertexCount(applied)
            # setVertexCount only writes the Python-side value; QSGBatchRenderer
            # re-reads a geometry node's element count only when the node is
            # dirty.  Without this, a cell culled while it scrolled off stays
            # blank after scrolling back in — the "square tiles blink out"
            # report.  Marked only on an actual change so the steady state
            # stays dirty-free.
            node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)

    def _ensure_content(self) -> bool:
        """Attach the cell transform node once there is a cell to draw."""
        if self._content_node is not None:
            # The recorded node may have been deleted under us; treat a dead
            # wrapper as absent so the cells are built again instead of being
            # appended to a dangling node.
            return _wrapper_is_valid(self._content_node)
        frame = self._static
        if frame is None or not any(cell.vertices.shape[0] for cell in frame.cells):
            return False
        content = QSGTransformNode()
        text_node = self._text_node
        if text_node is not None and _wrapper_is_valid(text_node):
            # Labels paint on top of the chart, never under it.
            self._node.insertChildNodeBefore(content, text_node)
        else:
            self._node.appendChildNode(content)
        self._content_node = content
        return True

    def _drop_presented_cells(self) -> None:
        """Detach the presented cells so an empty scene leaves nothing drawn.

        The content node itself stays: it is the item's transform child and a
        later scene re-uses it.  Only the per-cell nodes and the bookkeeping
        that describes them go, so no stale list can be zipped against the next
        build's nodes.
        """
        content = self._content_node
        if content is not None:
            for child in self._cell_nodes:
                try:
                    content.removeChildNode(child)
                except Exception:  # pragma: no cover - Qt already dropped it
                    pass
        self._cell_nodes = []
        self._cell_vertex_counts = []
        self._cell_visible = ()
        self._geometry = None
        self._material = None

    def _upload_cells(self) -> None:
        """Create one static-pattern geometry node per cell under the content."""
        content = self._content_node
        # Reset the per-cell bookkeeping only when there is no content node, so
        # the lists can never lag behind the nodes they describe.
        if content is None:
            self._cell_vertex_counts = []
            self._cell_visible = ()
            return
        frame = self._static
        if frame is None:
            if self._drop_cells_on_upload:
                # The caller replaced the picture with one that has nothing to
                # draw; the previous cells must not outlive that request.
                self._drop_cells_on_upload = False
                self._drop_presented_cells()
                return
            # No buildable frame (empty mesh): the cells already attached are
            # the last complete picture and stay on screen.
            return
        # Build the replacement nodes before touching the presented ones: an
        # empty or all-skipped frame must not blank a chart that is still
        # drawable.  Nodes are kept one-per-cell, including empty cells, so the
        # cull mask stays aligned with ``frame.cells``.
        nodes = []
        counts = []
        drawn = 0
        for cell in frame.cells:
            geometry = QSGGeometry(_colored_attributes(), 0)
            geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
            # StaticPattern: the buffer is written once per scene change and
            # never in the steady state, so Qt may keep it resident.
            geometry.setVertexDataPattern(QSGGeometry.DataPattern.StaticPattern)
            material = _blended_vertex_color_material()
            cell_node = QSGGeometryNode()
            cell_node.setGeometry(geometry)
            cell_node.setMaterial(material)
            cell_node.setFlag(QSGNode.Flag.OwnsGeometry)
            cell_node.setFlag(QSGNode.Flag.OwnsMaterial)
            count = int(cell.vertices.shape[0])
            if count:
                geometry.allocate(count)
                _upload_vertices(geometry, cell.vertices, VERTEX_DTYPE.itemsize)
                drawn += count
            nodes.append(cell_node)
            counts.append(count)
        if not drawn:
            # Nothing in the new frame draws.  Keep the previous complete frame
            # attached rather than swapping in an empty one mid-motion.
            return
        for child in self._cell_nodes:
            try:
                content.removeChildNode(child)
            except Exception:  # pragma: no cover - Qt already dropped it
                pass
        for cell_node in nodes:
            content.appendChildNode(cell_node)
        self._cell_nodes = nodes
        self._cell_vertex_counts = counts
        self._cell_visible = ()
        # No single "the" geometry or material exists in the multi-cell static
        # scene: aliasing the first cell here made diagnostics read one cell's
        # buffer (or, after a rebuild, a deleted C++ geometry) and report it as
        # the whole scene.  ``_cell_vertex_counts`` is the authoritative
        # per-cell record; ``_geometry``/``_material`` stay None in this mode.
        self._geometry = None
        self._material = None
        self._upload_count += 1

    def _update_static(self) -> None:
        """Present the scene through the reference frame, uploading on change."""
        if self._static is None or not self._cell_nodes:
            self._static = build_static_frame(
                self._mesh,
                self._projection,
                origin=self._origin,
                default_color=self._default_rgba(),
            )
            self._ensure_content()
            self._upload_cells()
        self._geometry_dirty = False
        coeffs = self._frame_coeffs()
        if coeffs is None and self._static is not None:
            # The projection is not affine over this scene: re-project into the
            # current frame rather than draw a warped approximation.  This is
            # the legacy cost, paid only where the matrix would be wrong.
            global _FALLBACK_ACTIVATIONS
            _FALLBACK_ACTIVATIONS += 1
            self.fallback_activations += 1
            self._static = build_static_frame(
                self._mesh,
                self._projection,
                origin=self._origin,
                default_color=self._default_rgba(),
            )
            self._ensure_content()
            self._upload_cells()
            coeffs = self._frame_coeffs()
        if coeffs is None:
            # Nothing to transform (empty scene, or a degenerate probe set that
            # already rebuilt above): the buffers are already current.  Leave a
            # retained frame's matrix alone -- resetting it would move the last
            # complete picture while its replacement assembles.
            if self._content_node is not None and not self._cell_nodes:
                self._content_node.setMatrix(QMatrix4x4())
            return
        self._apply_matrix(coeffs)
        self._cull_cells(coeffs)

    def _children_are_usable(self, node, static: bool) -> bool:
        if node is not self._node or self._built_static != static:
            return False
        if static:
            if self._content_node is None:
                return True
            # A content node without the cells it should carry is not usable:
            # rebuilding is cheap and avoids drawing an empty chart.
            return bool(self._cell_nodes) and _wrapper_is_valid(self._content_node)
        return (
            self._built_colored == self._colored
            and self._geometry is not None
            and self._material is not None
            and _wrapper_is_valid(self._geometry)
            and _wrapper_is_valid(self._material)
        )

    def updatePaintNode(self, node, _data):  # noqa: N802 - Qt override
        static = self._static_mode()
        if node is None:
            node = QSGGeometryNode()
            self._forget_scene_graph()

        if not self._children_are_usable(node, static):
            self._detach_text_node(node)
            self._forget_scene_graph()
            self._node = node
            if self._colored:
                attributes = _colored_attributes()
            else:
                # The pinned set, not a per-frame temporary: ``QSGGeometry``
                # keeps only the pointer and this geometry outlives the call.
                attributes = _point2d_attributes()
            geometry = QSGGeometry(attributes, 0)
            geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
            geometry.setVertexDataPattern(QSGGeometry.DataPattern.DynamicPattern)
            if self._colored:
                material = _blended_vertex_color_material()
            else:
                material = QSGFlatColorMaterial()
                material.setColor(self._color)
            node.setGeometry(geometry)
            node.setMaterial(material)
            # Ownership is declared once per build so Qt frees the buffers it
            # was given and never frees a buffer the item still references.
            node.setFlag(QSGNode.Flag.OwnsGeometry)
            node.setFlag(QSGNode.Flag.OwnsMaterial)
            if not static:
                # The static path records its cells in ``_cell_nodes``; a single
                # aliased buffer here would be deleted with the node and read as
                # the whole scene.
                self._geometry = geometry
                self._material = material
            self._built_colored = self._colored
            self._built_static = static

        if self._material_dirty:
            # QSGVertexColorMaterial has no uniform colour to update.
            if not self._built_colored:
                self._material.setColor(self._color)
            node.markDirty(QSGNode.DirtyStateBit.DirtyMaterial)
            self._material_dirty = False

        if static:
            # Runs every presented frame: four probes and one matrix write,
            # independent of the vertex count.
            self._update_static()
        elif self._geometry_dirty:
            clip = self._visible_clip()
            if self._built_colored:
                apply_colored_mesh(
                    self._geometry,
                    self._mesh,
                    self._projection,
                    origin=self._origin,
                    default_color=self._default_rgba(),
                    clip=clip,
                )
            else:
                apply_mesh(
                    self._geometry,
                    self._mesh,
                    self._projection,
                    origin=self._origin,
                    clip=clip,
                )
            self._upload_count += 1
            node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)
            self._geometry_dirty = False

        # Sounding labels are recomputed from the scene's source points under
        # the current camera.  The gate and the 32x16 dedupe are not part of
        # the cached scene, so a scale or projection change only dirties text.
        size = (float(self.width()), float(self.height()))
        if self._text_dirty or size != self._text_size:
            labels = self._presented_labels()
            if labels != self._text_labels:
                self._text_labels = labels
                self._text_geometry_dirty = True
            self._text_size = size
            self._text_dirty = False

        if self._text_labels and self._text_enabled:
            # The atlas is built on the GUI thread (see ``_prepare_glyphs``):
            # constructing a QImage/QFontMetricsF here would run on the render
            # thread, which Qt does not allow.  Without an atlas the labels are
            # simply not drawn this frame rather than built in the wrong place.
            if self._atlas is None:
                return node
            if self._text_node is None or not _wrapper_is_valid(self._text_node):
                self._ensure_texture()
                self._build_text_node(node)
            if self._text_geometry_dirty:
                self._ensure_texture()
                apply_glyph_labels(self._text_geometry, self._text_labels, self._atlas)
                self._text_upload_count += 1
                self._text_node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)
                self._text_geometry_dirty = False
        elif self._text_node is not None:
            self._detach_text_node(node)

        return node
