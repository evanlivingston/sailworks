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

Fixed-width strokes
-------------------
S-52 line widths are screen constants, but a retained scene is zoomed by one
affine matrix, which scales every baked stroke width with it.  The coastline
(``COALNE``, 1.5 logical px) and depth contour (``DEPCNT``, 0.5 px) classes
are therefore kept at their logical width by rewriting their vertices in
place.  At frame adoption the intact per-segment quads of those classes are
parsed out of the baked soup -- each segment is exactly six vertices, so
centreline, unit normal and width are recovered exactly -- and on every
presented camera change those six-vertex runs are recomputed and written
back into the very cell buffers they came from: the centreline stays the
baked one, which the shared matrix maps exactly like the cells, and the
half-width is re-laid perpendicular to the transformed segment at the
class's logical pixels, mapped back through the matrix's linear part so the
values land in the cells' reference-frame coordinates.  Per-cell draw order
and the per-cell cull mask are untouched: no second node exists, and a
culled cell's runs are not rewritten while it is culled.  At the published
camera the matrix is the identity and the buffers already hold the parsed
corners bit for bit, so the settled picture is unchanged.  A frame whose
eligible segments exceed :data:`RESTROKE_MAX_SEGMENTS`, or whose quads do
not parse (clipped fragments, a foreign soup layout, a run crossing a cell
boundary), keeps the previous rendering for the whole frame instead of a
partial mixture.  The write touches only the stroke runs, but marking a
cell's geometry dirty makes Qt re-upload that cell's whole buffer, so a
camera change pays one buffer upload per visible restroke-bearing cell.

Fixed-size point symbols
------------------------
Point symbols are flat marker triangles baked into the same cell buffers at
the adopting camera's scale, so the affine matrix scales every symbol with
the zoom.  Every ``MARKER`` part is kept at its logical size the same way
the strokes are: at frame adoption each part's layout is recovered exactly --
the mesh records no marker metadata, so the :mod:`encmarkers` template is
identified by shape-matching its staged vertices, which yields the original
feature anchor of every point (the template origin, never a centroid) and
the template offsets around it.  On every presented camera change those
vertices are rewritten in place inside their cell buffers: the anchor stays
the baked one (the shared matrix maps it exactly like the cells, so the
symbol keeps its ground position) and the offsets are mapped back through
the matrix's linear part, so the symbol renders at its adopted logical
extent whatever the zoom.  A part whose layout does not resolve to an
:class:`encmarkers` template, whose vertices exceed
:data:`MARKER_MAX_VERTICES`, or that straddles a cell boundary keeps the
previous rendering for the whole frame -- every symbol scales as before --
rather than a half-patched mixture.  Per-cell draw order and the per-cell
cull mask are untouched: no second node exists, and a culled cell's markers
are not rewritten while it is culled.

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

from sailworks.rendering.vectordrawer import encmarkers, encsounding
from sailworks.rendering.vectordrawer.encmesh import (
    EMPTY_MESH,
    LINE,
    MARKER,
    SceneMesh,
)
from sailworks.rendering.vectordrawer.encpalette import NEUTRAL, vertex_colours
from sailworks.qt import enc_trace
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

#: Vertex bytes one replaced frame may keep resident for a return pan.
#: Exactly one frame is kept: the one presented immediately before the
#: current one, so an A->B->A pan at one zoom re-attaches its already-built
#: nodes instead of rebuilding and re-uploading them.  The retained nodes
#: stay detached from the scene graph, so they draw nothing while another
#: scene is presented, and they are released on the render thread when a
#: later frame replaces them.
RETAINED_FRAME_VERTEX_BYTES = 64 * 1024 * 1024


def _frame_vertex_bytes(frame) -> int:
    """The uploaded vertex bytes of a static frame: the retention budget."""
    if frame is None:
        return 0
    return sum(int(cell.vertices.nbytes) for cell in frame.cells)


#: Most per-cell rows one ``enc_cull`` trace record carries.  A record is
#: written once per cull pass, not once per cell, so the trace stays small.
TRACE_CULL_LIMIT = 8

#: A culled bbox separated from the clip rect by no more than this many logical
#: pixels is recorded as merely touching it: that is how an undersized
#: ``_compose_margin`` shows up, a cell of the real viewport culled by a clip
#: that ends just short of it.
TRACE_TOUCH_PX = 1.0


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

#: Samples per axis used to prove the presented cells cover the viewport.
#: Coverage suppresses the raster, so the test has to be about the whole view,
#: not about whether *a* cell reaches it: a single cell in one corner satisfies
#: an overlap test and paints a hole where the rest of the chart should be.
#: A five-point grid per axis (edges included) is 25 point-in-box tests, which
#: is free next to the per-frame matrix solve it sits behind.
COVERAGE_SAMPLES = 5

#: Slack, in item pixels, added to a cell's projected extent while proving
#: coverage.  The stored vertices are ``float32`` offsets (ulp ``~2e-4`` px),
#: so this absorbs rounding without letting a real gap through.
COVERAGE_TOLERANCE = 0.5

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


def coverage_boxes_cover(boxes, width: float, height: float) -> bool:
    """Whether ``boxes`` cover the ``width`` x ``height`` item rect.

    ``boxes`` are the item-pixel extents of the cells the frame actually
    draws.  Every sample point of a :data:`COVERAGE_SAMPLES` grid, edge points
    included, must fall inside at least one box: a subset of the planned cells
    covers some of the view, never all of it, which is exactly the state that
    must not be reported as coverage.
    """
    if not boxes or width <= 0.0 or height <= 0.0:
        return False
    steps = COVERAGE_SAMPLES - 1
    half = COVERAGE_TOLERANCE
    for row in range(COVERAGE_SAMPLES):
        y = height * row / steps
        for column in range(COVERAGE_SAMPLES):
            x = width * column / steps
            if not any(
                box[0] - half <= x <= box[2] + half
                and box[1] - half <= y <= box[3] + half
                for box in boxes
            ):
                return False
    return True


def coverage_boxes_reach(boxes, width: float, height: float) -> bool:
    """Whether ``boxes`` overlap the ``width`` x ``height`` item rect at all.

    The weaker companion of :func:`coverage_boxes_cover`: this is the
    retention probe, not the raster's gate.  A frame whose cells still land in
    the viewport is the chart content that view has, even when those cells
    cover only part of it; a frame whose cells map entirely outside draws
    nothing a user can see, and only then may a partial take its place.
    """
    if not boxes or width <= 0.0 or height <= 0.0:
        return False
    half = COVERAGE_TOLERANCE
    return any(
        box[0] - half <= width
        and box[2] + half >= 0.0
        and box[1] - half <= height
        and box[3] + half >= 0.0
        for box in boxes
    )


#: S-52 line classes whose strokes keep a fixed logical-pixel width while a
#: retained scene is zoomed: the coastline (1.5 px) and the depth contour
#: (0.5 px) from :data:`sailworks.rendering.vectordrawer.encmesh.LINE_STYLES`.
#: Every other class keeps the baked behaviour, where the affine matrix
#: scales the stroke width with the zoom.
RESTROKE_CLASSES = frozenset({"COALNE", "DEPCNT"})

#: Most segments one frame's restroke may carry.  The per-camera patch
#: re-derives each segment's six vertices on the render thread, so the bound
#: caps that per-frame work and the bytes it copies (~6 vertices and 12 bytes
#: each per segment; Qt re-uploads a touched cell's whole buffer to the GPU).
#: A frame whose eligible segments exceed the bound keeps the previous
#: rendering for the whole frame -- every class scales as before -- rather
#: than drawing a half-parsed mixture.
RESTROKE_MAX_SEGMENTS = 64_000

#: Most marker vertices one frame's fixed-size patch may carry.  The
#: per-camera patch re-derives each part's vertices on the render thread, so
#: the bound caps that per-frame work; a frame whose markers exceed the
#: bound keeps the previous rendering for the whole frame -- every symbol
#: scales as before -- rather than drawing a half-patched mixture.
MARKER_MAX_VERTICES = 64_000

#: The staged marker template geometry per kind, flattened to ``(N, 2)``
#: float64 in emission order -- exactly the arrays
#: :func:`sailworks.rendering.vectordrawer.encmesh.build_scene_mesh` scales
#: by ``marker_scale`` and translates onto the feature anchors.
_MARKER_TEMPLATES = {
    kind: np.asarray(encmarkers.triangles(kind), dtype=np.float64).reshape(-1, 2)
    for kind in encmarkers.KINDS
}

#: The affine that maps a frame onto the camera it was adopted for.
_IDENTITY_AFFINE = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


class _RestrokePlan:
    """Fixed-width stroke data parsed from one adopted static frame.

    ``corners`` holds each eligible segment's four distinct reference-frame
    corners as an ``(S, 4, 2)`` float32 array ordered ``(near_start,
    near_end, far_end, far_start)`` -- the values the baked cell buffers
    carry, so writing them back under the identity matrix is bit-exact with
    the soup the frame replaced.  ``runs`` records where each parsed stretch
    sits: ``(first_vertex, segments)`` per contributing ``LINE`` part, in
    part order, so the per-cell patch can find the runs inside the cell
    buffers without re-parsing the soup.
    """

    __slots__ = ("segments", "corners", "runs")

    def __init__(self, segments, corners, runs):
        self.segments = int(segments)
        self.corners = corners
        self.runs = tuple(runs)


def _restroke_plan(mesh, local):
    """Parse the fixed-width stroke quads out of a mesh's baked soup, or ``None``.

    :func:`sailworks.rendering.vectordrawer.encstroke.stroke_lines` emits
    exactly six vertices per segment, ``(near_start, near_end, far_end,
    near_start, far_end, far_start)``, so an unclipped quad repeats its first
    and third corners bit for bit.  A ``LINE`` part of a
    :data:`RESTROKE_CLASSES` class whose vertices are a whole number of such
    groups therefore yields, per segment, the centreline endpoints, the unit
    normal and the reference-frame width -- everything the per-frame
    derivation needs, with no new field on the mesh or the scene and no
    change to the disk cache identity.

    A clipped stroke fragment loses the six-vertex layout, so parsing stops
    at the first group that does not repeat its corners: the prefix joins
    the restroke and everything from there on keeps the baked rendering,
    exactly as it behaves today.  ``None`` means the frame carries no
    eligible segments, or more than :data:`RESTROKE_MAX_SEGMENTS` of them;
    the caller then renders the frame exactly as before.
    """
    parts = getattr(mesh, "parts", ())
    if not parts:
        return None
    positions = np.asarray(mesh.positions)
    if positions.ndim != 2 or positions.shape[1] != 2:
        return None
    corner_blocks = []
    runs = []
    total = 0
    for part in parts:
        if part.kind != LINE or part.source_class not in RESTROKE_CLASSES:
            continue
        first = int(part.first_vertex)
        count = int(part.vertex_count)
        if count < 6 or count % 6 != 0:
            continue
        if first < 0 or first + count > positions.shape[0]:
            continue
        groups = count // 6
        source_block = positions[first : first + count].reshape(groups, 6, 2)
        reference_block = local[first : first + count].reshape(groups, 6, 2)
        paired = np.all(source_block[:, 0] == source_block[:, 3], axis=1) & np.all(
            source_block[:, 2] == source_block[:, 4], axis=1
        )
        delta = reference_block[:, 0] - reference_block[:, 5]
        widths = np.hypot(delta[:, 0], delta[:, 1])
        step = (reference_block[:, 1] + reference_block[:, 2]) - (
            reference_block[:, 0] + reference_block[:, 3]
        )
        steps = np.hypot(step[:, 0], step[:, 1])
        usable = paired & (widths > 0.0) & (steps > 0.0)
        broken = np.flatnonzero(~usable)
        take = int(broken[0]) if broken.size else groups
        if take == 0:
            continue
        total += take
        if total > RESTROKE_MAX_SEGMENTS:
            return None
        corner_blocks.append(reference_block[:take, [0, 1, 2, 5], :])
        runs.append((first, take))
    if not corner_blocks:
        return None
    corners = np.concatenate(corner_blocks).astype(np.float32)
    return _RestrokePlan(total, corners, runs)


def _restroke_quad_rows(corners, lin=None):
    """Emission-order ``(S, 6, 2)`` float32 positions for parsed segments.

    ``lin is None`` expands the parsed corners into the stroker's six-vertex
    emission order -- the baked cell buffers' own vertices, bit for bit.  If
    ``lin`` is the linear part ``(a, b, d, e)`` of the frame matrix, the
    centreline stays where it is baked (the matrix maps it exactly like the
    cells) and each half-width is re-laid perpendicular to the transformed
    segment at the class's reference-frame pixels, then mapped back through
    the inverse linear part so the values land in the cells' reference-frame
    coordinates and the cell matrix puts them on the intended item pixels.
    A segment whose transformed direction collapses, or a singular linear
    part where no offset survives the mapping, keeps its baked corners: the
    cells' own matrix, whatever it does to the width.
    """
    corners = np.asarray(corners, dtype=np.float32)
    segments = int(corners.shape[0])
    quad = np.empty((segments, 6, 2), dtype=np.float32)
    quad[:, 0] = corners[:, 0]
    quad[:, 1] = corners[:, 1]
    quad[:, 2] = corners[:, 2]
    quad[:, 3] = corners[:, 0]
    quad[:, 4] = corners[:, 2]
    quad[:, 5] = corners[:, 3]
    if lin is None:
        return quad
    a, b, d, e = (float(value) for value in lin)
    det = a * e - b * d
    if det == 0.0:
        return quad
    quads = corners.astype(np.float64)
    centre0 = (quads[:, 0] + quads[:, 3]) * 0.5
    centre1 = (quads[:, 1] + quads[:, 2]) * 0.5
    delta = quads[:, 0] - quads[:, 3]
    width = np.hypot(delta[:, 0], delta[:, 1])
    unit_normal = delta / width[:, None]
    unit_direction = np.column_stack((unit_normal[:, 1], -unit_normal[:, 0]))
    sx = a * unit_direction[:, 0] + b * unit_direction[:, 1]
    sy = d * unit_direction[:, 0] + e * unit_direction[:, 1]
    length = np.hypot(sx, sy)
    usable = length > 0.0
    scale = np.where(usable, length, 1.0)
    half = width * 0.5
    # The item-pixel offset: perpendicular to the transformed segment, at
    # half the class's reference-frame (logical) width.
    off_x = (-sy / scale) * half
    off_y = (sx / scale) * half
    # Back through the inverse linear part, into reference-frame offsets.
    local_x = np.where(usable, (e * off_x - b * off_y) / det, 0.0)
    local_y = np.where(usable, (-d * off_x + a * off_y) / det, 0.0)
    offset = np.column_stack((local_x, local_y))
    near0 = centre0 + offset
    far0 = centre0 - offset
    near1 = centre1 + offset
    far1 = centre1 - offset
    quad[:, 0] = near0
    quad[:, 1] = near1
    quad[:, 2] = far1
    quad[:, 3] = near0
    quad[:, 4] = far1
    quad[:, 5] = far0
    return quad


def _restroke_groups(plan, ranges):
    """Split a plan's runs across the cell spans, or ``None`` if one straddles.

    ``ranges`` are the ``(first_vertex, vertex_count)`` cell spans the cell
    buffers were built from, in composite order.  Each parsed run must sit
    inside exactly one cell: a run split across two cells would have its
    halves patched at different cameras and drawn as a torn quad, so such a
    plan is rejected and the frame keeps the baked rendering whole.  Each
    group is ``(cell_index, cell_first, run_first, corner_start, take)``.
    """
    groups = []
    corner_start = 0
    run_index = 0
    runs = plan.runs
    for cell_index, (first, length) in enumerate(ranges):
        cell_end = first + length
        while run_index < len(runs) and runs[run_index][0] < cell_end:
            run_first, take = runs[run_index]
            run_first = int(run_first)
            take = int(take)
            if run_first < first or run_first + take * 6 > cell_end:
                return None
            groups.append((cell_index, first, run_first, corner_start, take))
            corner_start += take
            run_index += 1
    if run_index != len(runs):
        return None
    return tuple(groups)


class _MarkerPart:
    """One parsed marker part: span, per-point block size, anchors, offsets.

    ``first``/``count`` locate the part's vertices in the composite soup,
    ``block`` is the per-point vertex count of the marker template,
    ``anchors`` are the feature anchors and ``offsets`` the per-block
    template offsets around them.  The parse phase recovers ``anchors`` in
    source units (``offsets`` is ``None``); the reference phase rewrites both
    into reference-frame local pixels.
    """

    __slots__ = ("first", "count", "block", "anchors", "offsets")

    def __init__(self, first, count, block, anchors, offsets):
        self.first = first
        self.count = count
        self.block = block
        self.anchors = anchors
        self.offsets = offsets


class _MarkerPlan:
    """Fixed-size marker data parsed from one adopted static frame.

    ``entries`` holds one :class:`_MarkerPart` per parsed ``MARKER`` part in
    vertex order.  The anchors are the original exact feature positions
    recovered by shape matching, never a centroid; ``None`` entries are
    impossible -- a part that does not resolve keeps the whole frame's baked
    rendering instead.
    """

    __slots__ = ("entries",)

    def __init__(self, entries):
        self.entries = entries


def _match_marker_template(block: np.ndarray):
    """Recover ``(block, anchors)`` for one staged marker part, or ``None``.

    :func:`sailworks.rendering.vectordrawer.encmesh.build_scene_mesh` stages
    every point of a ``MARKER`` part as ``anchor +
    encmarkers.triangles(kind) * marker_scale`` in source units, all points
    of the part sharing one template, and records no marker metadata on the
    mesh.  The template is therefore recovered by exact shape matching: a
    candidate kind qualifies only when scaling its flattened template
    reproduces every staged vertex of every point block from one anchor per
    block, inside float64 staging roundoff.  The recovered anchors are the
    original feature anchors -- the template origin, never a vertex
    centroid.

    Kinds whose templates are geometrically identical can tie, and a disc
    template at twice the scale matches a smaller disc; every such tie
    yields the same anchors and the same rendered geometry, so the first
    qualifying kind in :data:`encmarkers.KINDS` order is taken.  ``None``
    means no kind reproduces the layout (a foreign soup, a split fragment)
    and the caller keeps the baked rendering whole.
    """
    count = int(block.shape[0])
    extent = float(np.abs(block).max()) if count else 0.0
    tolerance = 1e-9 * max(extent, 1.0)
    for kind in encmarkers.KINDS:
        template = _MARKER_TEMPLATES[kind]
        size = int(template.shape[0])
        if size == 0 or size > count or count % size:
            continue
        blocks = block.reshape(count // size, size, 2)
        offsets = blocks - blocks[:, :1]
        shape = template - template[:1]
        lengths = np.hypot(shape[:, 0], shape[:, 1])
        index = int(np.argmax(lengths))
        if not lengths[index] > 0.0:
            continue
        block_lengths = np.hypot(offsets[:, index, 0], offsets[:, index, 1])
        if not np.allclose(block_lengths, block_lengths[0], rtol=1e-9, atol=0.0):
            continue
        scale = float(block_lengths[0]) / float(lengths[index])
        if not np.isfinite(scale) or not scale > 0.0:
            continue
        if not np.allclose(offsets, shape * scale, rtol=0.0, atol=tolerance):
            continue
        anchors = blocks[:, 0] - template[:1] * scale
        return size, anchors
    return None


def _marker_plan(mesh):
    """Parse the fixed-size marker runs out of a mesh's parts, or ``None``.

    Every ``MARKER`` part must resolve to one :mod:`encmarkers` template at
    one scale, with one anchor per point block reproduced exactly (see
    :func:`_match_marker_template`); the first part that does not rejects
    the plan, and the frame keeps the baked rendering whole -- all markers
    or none, so a scene never draws a half-patched mixture.  The vertex
    bound mirrors the stroke patch: :data:`MARKER_MAX_VERTICES` caps the
    per-frame rewrite.
    """
    parts = getattr(mesh, "parts", ())
    if not parts:
        return None
    positions = np.asarray(mesh.positions)
    if positions.ndim != 2 or positions.shape[1] != 2:
        return None
    entries = []
    total = 0
    for part in parts:
        if part.kind != MARKER:
            continue
        first = int(part.first_vertex)
        count = int(part.vertex_count)
        if first < 0 or count < 3 or first + count > positions.shape[0]:
            return None
        parsed = _match_marker_template(positions[first : first + count])
        if parsed is None:
            return None
        size, anchors = parsed
        total += count
        if total > MARKER_MAX_VERTICES:
            return None
        entries.append(_MarkerPart(first, count, size, anchors, None))
    if not entries:
        return None
    return _MarkerPlan(tuple(entries))


def _marker_reference(entries, local64, projection, origin, base):
    """Move parsed marker parts into their reference-frame coordinates.

    Each block's anchor is projected through the same float64 path the
    staged vertices took -- origin subtraction, then projection -- and the
    template offsets are the reference-frame block minus that anchor: the
    exact values the baked cell buffers hold around the anchor.
    """
    moved = []
    for entry in entries:
        anchors = np.asarray(entry.anchors, dtype=np.float64)
        if origin is not None:
            anchors = anchors - np.asarray(origin, dtype=np.float64).reshape(1, 2)
        if projection is not None:
            anchors = np.asarray(projection(anchors), dtype=np.float64)
        anchors = anchors - base
        blocks = local64[entry.first : entry.first + entry.count].reshape(
            -1, entry.block, 2
        )
        moved.append(
            _MarkerPart(
                entry.first,
                entry.count,
                entry.block,
                anchors,
                blocks - anchors[:, None, :],
            )
        )
    return _MarkerPlan(tuple(moved))


def _marker_groups(plan, ranges):
    """Split a plan's parts across the cell spans, or ``None`` if one straddles.

    ``ranges`` are the ``(first_vertex, vertex_count)`` cell spans the cell
    buffers were built from, in composite order.  Each parsed part must sit
    inside exactly one cell: a part split across two cells would have its
    halves patched at different cameras and drawn as a torn symbol, so such
    a plan is rejected and the frame keeps the baked rendering whole.  Each
    group is ``(cell_index, cell_first, entry_index)``.
    """
    groups = []
    entry_index = 0
    entries = plan.entries
    for cell_index, (first, length) in enumerate(ranges):
        cell_end = first + length
        while entry_index < len(entries) and entries[entry_index].first < cell_end:
            entry = entries[entry_index]
            if entry.first < first or entry.first + entry.count > cell_end:
                return None
            groups.append((cell_index, first, entry_index))
            entry_index += 1
    if entry_index != len(entries):
        return None
    return tuple(groups)


def _marker_offsets(offsets, lin):
    """Reference-frame offsets that render at their reference-frame extent.

    ``lin`` is the linear part ``(a, b, d, e)`` of the cell matrix.  Each
    template offset is mapped back through the inverse linear part, so the
    matrix renders it at exactly its reference-frame (logical) extent: the
    symbol's screen size is the adopted frame's, whatever the zoom.  A
    singular linear part keeps the baked offsets -- the cells' own matrix,
    whatever it does to the size.
    """
    if lin is None:
        return offsets
    a, b, d, e = (float(value) for value in lin)
    det = a * e - b * d
    if det == 0.0:
        return offsets
    tx, ty = offsets[..., 0], offsets[..., 1]
    return np.stack(((e * tx - b * ty) / det, (-d * tx + a * ty) / det), axis=-1)


def _marker_rows(entry, lin):
    """Emission-order ``(count, 2)`` float64 positions for one marker part.

    ``lin is None`` expands the parsed anchors and offsets into the baked
    cell buffers' own vertices.  With ``lin``, the anchors stay where they
    are baked (the matrix maps them exactly like the cells) and the offsets
    pre-compensate the matrix's linear part (see :func:`_marker_offsets`).
    """
    return (entry.anchors[:, None, :] + _marker_offsets(entry.offsets, lin)).reshape(
        -1, 2
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
    a later frame needs to recover the pan/zoom matrix.  ``restroke`` carries
    the fixed-width stroke patch's data, or ``None`` when the frame keeps
    the previous rendering for every class; ``restroke_groups`` is that
    plan's per-cell split across ``cells``.
    """

    __slots__ = (
        "cells",
        "reference",
        "samples",
        "restroke",
        "restroke_groups",
        "markers",
        "marker_groups",
    )

    def __init__(
        self,
        cells,
        reference,
        samples,
        restroke=None,
        restroke_groups=(),
        markers=None,
        marker_groups=(),
    ):
        self.cells = cells
        self.reference = reference
        self.samples = samples
        self.restroke = restroke
        self.restroke_groups = restroke_groups
        self.markers = markers
        self.marker_groups = marker_groups


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
    straight = vertex_colours(getattr(mesh, "parts", ()), count, default_color)
    base = (positions.min(axis=0) + positions.max(axis=0)) / 2.0
    local64 = positions - base
    local = np.ascontiguousarray(local64, dtype=np.float32)
    try:
        plan = _restroke_plan(mesh, local64)
    except Exception:  # noqa: BLE001 - the fixed-width patch is optional; a
        # parse surprise must never break the frame, and the fallback -- plan
        # None -- renders the frame exactly as the previous behaviour does.
        plan = None
    try:
        markers = _marker_plan(mesh)
    except Exception:  # noqa: BLE001 - the fixed-size patch is optional; a
        # parse surprise must never break the frame, and the fallback -- plan
        # None -- renders the frame exactly as the previous behaviour does.
        markers = None
    colours = _premultiplied_colours(straight)

    spans = tuple(getattr(mesh, "cells", ()))
    ranges = [(int(span.first_vertex), int(span.vertex_count)) for span in spans] or [
        (0, count)
    ]
    groups = ()
    if plan is not None:
        groups = _restroke_groups(plan, ranges)
        if groups is None:
            # A parsed run crosses a cell boundary: patching per cell would
            # split its quad across two buffers drawn at different times, so
            # the frame keeps the baked rendering whole.
            plan = None
            groups = ()
    marker_groups = ()
    if markers is not None:
        marker_groups = _marker_groups(markers, ranges)
        if marker_groups is None:
            # A marker part crosses a cell boundary: patching per cell would
            # split the symbol across two buffers drawn at different times,
            # so the frame keeps the baked rendering whole.
            markers = None
            marker_groups = ()
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

    # Markers join the frame in reference-frame coordinates: the anchors and
    # per-block template offsets the per-camera patch rewrites around.
    if markers is not None:
        markers = _marker_reference(markers.entries, local64, projection, origin, base)

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
    return _StaticFrame(
        tuple(cells), reference, sources, plan, groups, markers, marker_groups
    )


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
        #: Retention for return pans: ``(scene, static frame, cell nodes,
        #: counts, visible mask)`` for the frame presented immediately before
        #: the current one, with its nodes detached from the graph.  One
        #: entry, bounded by :data:`RETAINED_FRAME_VERTEX_BYTES`; nodes are
        #: only mutated on the render thread (see :meth:`_update_static`).
        self._retained = None
        #: Set by :meth:`set_view_scene` when the incoming scene is the
        #: retained one: the next paint re-attaches the retained nodes
        #: instead of rebuilding them.
        self._retained_restore = False
        #: The outgoing ``(scene, static frame)`` a replace may retain,
        #: captured on the GUI thread before ``_static`` is cleared.
        self._outgoing = None
        #: True when the next ``_upload_cells`` should retain the outgoing
        #: frame.
        self._retention_wanted = False
        #: Node objects a GUI-thread invalidation left behind; the render
        #: thread releases them, because QSG nodes may not be deleted on the
        #: GUI thread.
        self._retained_dead = []
        #: How far past the item the presented scene was composed.  Culling
        #: must keep the cells in that ring: they are what a pan brings into
        #: view before the replacement arrives.
        self._compose_margin = SCENE_MARGIN
        self._matrix_count = 0
        #: Cull passes this item has run, counted only while tracing is on.
        #: Every ``enc_cull``/``enc_zero_count`` record carries it so a cull can
        #: be matched to the compose margin that was in force.
        self._trace_frame = 0
        #: Times this item re-projected instead of transforming its reference
        #: frame with a matrix (see :meth:`_frame_coeffs`).  Reported by the
        #: app-level capture harness so a matrix-path frame is not confused
        #: with a fallback frame.
        self.fallback_activations = 0

        #: Fixed-width stroke bookkeeping (see :func:`_restroke_plan`).  The
        #: stroke vertices live in the cell buffers; this records the camera
        #: the patched cells reflect (``_restroke_coeffs``, ``None`` when no
        #: cell is patched), the patched cells' indices, and whether the
        #: buffers are known to hold the baked corners (``_restroke_fresh``
        #: False).  There is no overlay node: the patch rewrites runs inside
        #: the cell buffers, keeping per-cell draw order and culling.
        self._restroke_coeffs = None
        self._restroke_patched = set()
        self._restroke_fresh = False

        #: Fixed-size point-symbol bookkeeping (see :func:`_marker_plan`),
        #: mirroring the stroke patch's: the camera the patched cells
        #: reflect, the patched cells' indices, and whether the buffers are
        #: known to hold the baked vertices (``_marker_fresh`` False).
        self._marker_coeffs = None
        self._marker_patched = set()
        self._marker_fresh = False

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
        """Whether the cells this frame *draws* cover the whole viewport.

        The raster layer is held back on a true result, so this is a claim
        about the pixels on screen and nothing else.  It is answered from the
        presented frame's own cell geometry under the current camera matrix,
        never from the plan that was requested: a composition whose plan
        collapsed (fewer usable cells, cells skipped during assembly, a plan
        built for a narrower camera) carries a cell set that reaches the view
        without covering it.  Mapping that set's union and asking whether it
        *overlaps* the item - the earlier test - is satisfied by one cell in a
        corner, so the raster stayed suppressed and the rest of the chart was
        blank until a full set landed.

        Two independent things are therefore required:

        * the camera matrix exists, so the drawn extent is measurable at all.
          Without one the frame was re-projected for this camera and its
          extent cannot be proven from the retained buffers; the raster then
          composes instead of being suppressed on an assumption;
        * every sample point of the item rect falls inside a cell that is
          actually drawn (:attr:`_cell_visible`), not merely inside a cell the
          frame happens to keep attached with a zero vertex count.

        No extra hysteresis is needed.  Culling runs against the compose rect,
        which is padded by the scene's own margin on every side of the item,
        so a cell that still contributes to the item cannot be culled while
        the item is in view; the mask only changes for cells entirely outside
        the padding, which cannot change the answer here.  Coverage therefore
        flips once, with the drawn cell set, and not per frame.
        """
        if not self._cell_nodes or self._static is None:
            return False
        width = float(self.width())
        height = float(self.height())
        if width <= 0.0 or height <= 0.0:
            return False
        coeffs = self._frame_coeffs()
        if coeffs is None:
            return False
        boxes = self._drawn_cell_boxes(coeffs)
        return coverage_boxes_cover(boxes, width, height)

    def _drawn_cell_boxes(self, coeffs):
        """Item-pixel extents of the cells this frame actually draws.

        A cell that is attached with a zero vertex count draws nothing, so the
        mask, not the attached buffer list, decides what is on screen.
        """
        cells = self._static.cells
        mask = self._cell_visible
        trust_mask = len(mask) == len(cells)
        boxes = []
        for index, cell in enumerate(cells):
            if cell.bounds is None:
                continue
            if trust_mask and not mask[index]:
                continue
            boxes.append(_affine_bounds(coeffs, cell.bounds))
        return boxes

    def scene_reaches_view(self) -> bool:
        """Whether any cell this frame draws still lands in the viewport.

        The adoption policy's retention probe, deliberately weaker than
        :meth:`scene_covers_view`: a frame whose cells still map into the item
        is the chart content this view has, so a newer partial must not take
        the screen from it.  It answers false only when the drawn cells map
        entirely outside the item, which is the one state where keeping the
        frame is indistinguishable from a blank chart.

        A frame that was adopted but has not been built yet is this viewport's
        picture and the next paint draws it.  Answering from an unbuilt frame
        would let the first publication after an adoption replace it with a
        partial.
        """
        width = float(self.width())
        height = float(self.height())
        if width <= 0.0 or height <= 0.0:
            return False
        if self._static is None:
            return bool(getattr(self._mesh, "parts", ()))
        coeffs = self._frame_coeffs()
        if coeffs is None:
            return False
        return coverage_boxes_reach(self._drawn_cell_boxes(coeffs), width, height)

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
        self._invalidate_retention()
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

    def setVisible(self, visible) -> None:  # noqa: N802 - Qt override
        """Present or withdraw the item; hiding drops the retained frame.

        A hidden item draws nothing and its scene-graph children may be
        released while it is hidden, so a frame retained for a return pan is
        not kept across a hidden period (see :meth:`_invalidate_retention`).
        """
        super().setVisible(visible)
        if not visible:
            self._invalidate_retention()

    def _invalidate_retention(self) -> None:
        """Drop retention on the GUI thread; the render thread frees the nodes.

        QSG nodes may not be deleted on the GUI thread, so the detached
        retained nodes are parked for the next ``updatePaintNode`` to release.
        Until then they stay out of the graph and draw nothing.
        """
        slot = self._retained
        self._retained = None
        self._retained_restore = False
        self._outgoing = None
        self._retention_wanted = False
        if slot is not None:
            self._retained_dead.extend(slot[2])

    def _release_dead_retained(self) -> None:
        """Release retained nodes parked by a GUI-thread invalidation.

        Runs on the render thread: dropping the last Python reference deletes
        the C++ nodes here, where QSG node teardown is allowed.
        """
        self._retained_dead.clear()

    def set_view_scene(self, view_scene) -> bool:
        """Present one assembled multi-cell :class:`encview.ViewScene`.

        The scene's contiguous float64 ``positions`` and ordered ``parts`` are
        uploaded through the existing single batched coloured geometry node, so
        part order is preserved exactly as assembled and per-part colours
        travel in the vertex buffer.  Presenting a view scene enables the
        coloured layout; the flat ``set_mesh``/``set_color`` path is unaffected.
        ``None`` clears to empty geometry.  Re-presenting the scene already on
        display is a no-op, so an unchanged frame does not re-upload.
        Re-presenting the *retained* scene -- the one presented immediately
        before the current one, kept bounded by
        :data:`RETAINED_FRAME_VERTEX_BYTES` -- re-attaches the nodes it
        already built on the next paint instead of rebuilding them: a
        same-zoom return pan costs no vertex upload and never draws both
        frames at once (see :meth:`_restore_retained`).

        An empty scene (for example a plan whose only cells were oversized and
        skipped) allocates zero vertices rather than leaving the previous
        frame's triangles on screen.  The scene reference is kept in
        :attr:`view_scene` so cell attribution and skip reasons remain
        inspectable after upload.
        """
        mesh = EMPTY_MESH if view_scene is None else view_scene
        if view_scene is self._view_scene and mesh is self._mesh:
            return False
        outgoing = (self._view_scene, self._static)
        restore = (
            view_scene is not None
            and self._retained is not None
            and view_scene is self._retained[0]
        )
        self._retained_restore = restore
        if restore:
            # A return pan to the retained scene: the outgoing frame is
            # captured for the swap the next paint performs, so the retained
            # and presented frames trade places and the slot stays bounded
            # at one (see :meth:`_restore_retained`).
            self._outgoing = outgoing
            self._retention_wanted = True
        elif (
            self._view_scene is not None
            and view_scene is not None
            and self._static is not None
            and _frame_vertex_bytes(self._static) <= RETAINED_FRAME_VERTEX_BYTES
        ):
            # A replacement scene: the frame being replaced may be retained
            # for the return pan that comes back to it.
            self._outgoing = outgoing
            self._retention_wanted = True
        else:
            self._invalidate_retention()
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
        # The baked vertex colours belong to the old layout, so a retained
        # frame drawn with them cannot answer a demand made after the change.
        self._invalidate_retention()
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
            self._invalidate_retention()
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
        if enc_trace.enabled():
            enc_trace.event(
                "enc_compose_margin",
                frame=self._trace_frame,
                old=self._compose_margin,
                new=margin,
            )
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
        # The parked slot goes with the graph its nodes lived in, but a
        # pending replace-time retention intent survives: it describes the
        # scene change the caller asked for, and the next upload records the
        # entry from it.  An entry whose parked nodes died with this graph is
        # exactly the stale entry ``_restore_retained`` refuses.  A GUI-thread
        # invalidation (:meth:`_invalidate_retention`) cancels the intent
        # outright instead -- the frame it described is no longer presentable.
        slot = self._retained
        self._retained = None
        self._retained_restore = False
        if slot is not None:
            self._retained_dead.extend(slot[2])
        # The cell buffers die with the nodes; a rebuilt graph uploads
        # ``cell.vertices``, which the patch never mutates (the baked
        # corners).  Demand one rewrite anyway: the recorded camera no longer
        # describes anything.
        self._restroke_coeffs = None
        self._restroke_patched = set()
        self._restroke_fresh = True
        self._marker_coeffs = None
        self._marker_patched = set()
        self._marker_fresh = True
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
        self._invalidate_retention()
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

    def _write_restroke_cell(self, cell_index, groups, flat) -> None:
        """Rewrite one cell's stroke runs inside its geometry buffer.

        ``groups`` are that cell's ``(cell_index, cell_first, run_first,
        corner_start, take)`` records and ``flat`` is ``None`` for the baked
        write -- the parsed corners in the stroker's emission order, bit for
        bit -- or an ``(S * 6, 2)`` float32 array covering the cell's
        segments in the same order.  Each run is one ``memmove`` of full
        12-byte rows at its own offset in the cell's buffer, so only the
        stroke vertices are copied: the colours, the triangle order and
        everything around them are exactly as uploaded.  Render thread only;
        the caller decides which cells are written.
        """
        nodes = self._cell_nodes
        if cell_index >= len(nodes):
            return
        node = nodes[cell_index]
        if node is None or not _wrapper_is_valid(node):
            return
        geometry = node.geometry()
        if geometry is None:
            return
        cells = self._static.cells
        buffer = cells[cell_index].vertices
        total = int(buffer.shape[0])
        stride = VERTEX_DTYPE.itemsize
        base = _vertex_address(geometry)
        written = 0
        marked = False
        for _, cell_first, run_first, corner_start, take in groups:
            start = int(run_first) - int(cell_first)
            count = int(take) * 6
            if start < 0 or start + count > total:
                # Defensive: the run was validated against the cell spans at
                # adoption; never write outside the buffer regardless.
                continue
            rows = np.empty(count, dtype=VERTEX_DTYPE)
            if flat is None:
                corners = self._static.restroke.corners[
                    int(corner_start) : int(corner_start) + int(take)
                ]
                flat_run = _restroke_quad_rows(corners).reshape(count, 2)
            else:
                flat_run = flat[written : written + count]
            rows["x"] = flat_run[:, 0]
            rows["y"] = flat_run[:, 1]
            rows["rgba"] = buffer[start : start + count]["rgba"]
            ctypes.memmove(
                ctypes.c_void_p(base + start * stride),
                ctypes.c_void_p(rows.ctypes.data),
                count * stride,
            )
            written += count
            marked = True
        if marked:
            node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)

    def _update_restroke(self, coeffs) -> None:
        """Keep the fixed-width stroke vertices in the cell buffers current.

        Runs on the render thread after the cell matrix is applied.  There is
        no overlay node: the parsed segments' six-vertex runs are rewritten
        inside the very cell buffers they were parsed from, in the cells'
        reference-frame coordinates, so per-cell draw order and the per-cell
        cull mask stay exactly the baked behaviour's.  Nothing runs here when
        the camera has not changed since the last write, so a still viewport
        costs nothing.  At the camera a frame was adopted for the matrix is
        the identity and the buffers already hold the parsed corners bit for
        bit; a return to it restores them the same way.

        A culled cell draws zero vertices, so its runs are not rewritten
        while it stays culled; a cell leaving the patch set is restored to
        its baked corners instead, so a buffer is always either baked or
        current for the recorded camera, whichever camera shows it next.  A
        rebuilt or restored graph starts from ``_restroke_fresh``, which
        reconciles every restroke-bearing cell once.

        Cost, honestly: the write touches only the stroke runs, but marking a
        cell's geometry dirty makes Qt re-upload that cell's whole buffer to
        the GPU, so a camera change pays one buffer upload per visible
        restroke-bearing cell plus one per cell just restored -- bounded by
        the scene, not by :data:`RESTROKE_MAX_SEGMENTS`.
        """
        frame = self._static
        plan = frame.restroke if frame is not None else None
        if plan is None:
            # Nothing parses: the cell buffers hold the baked rendering and
            # nothing touches them, whatever the camera does.
            self._restroke_coeffs = None
            self._restroke_patched = set()
            self._restroke_fresh = False
            return
        groups = frame.restroke_groups
        exact = coeffs is None or coeffs == _IDENTITY_AFFINE
        if self._restroke_fresh:
            pass  # unknown buffer provenance: always reconcile below
        elif exact:
            if not self._restroke_patched:
                return
        elif coeffs == self._restroke_coeffs:
            return
        by_cell = {}
        for group in groups:
            by_cell.setdefault(group[0], []).append(group)
        visible = self._cell_visible
        new_patched = set()
        for cell_index, cell_groups in by_cell.items():
            onscreen = cell_index < len(visible) and bool(visible[cell_index])
            if onscreen and not exact:
                corners = np.concatenate(
                    [
                        plan.corners[int(start) : int(start) + int(take)]
                        for _, _, _, start, take in cell_groups
                    ]
                )
                quad = _restroke_quad_rows(
                    corners, (coeffs[0], coeffs[1], coeffs[3], coeffs[4])
                )
                self._write_restroke_cell(cell_index, cell_groups, quad.reshape(-1, 2))
                new_patched.add(cell_index)
            elif (
                onscreen or cell_index in self._restroke_patched or self._restroke_fresh
            ):
                # An exact camera after a patch, a cell just culled out of
                # the patch set, or buffers of unknown provenance (a rebuilt
                # or restored graph): write the baked corners back.
                self._write_restroke_cell(cell_index, cell_groups, None)
        self._restroke_coeffs = None if exact else coeffs
        self._restroke_patched = new_patched
        self._restroke_fresh = False

    def _write_marker_cell(self, cell_index, cell_groups, lin) -> None:
        """Rewrite one cell's marker runs inside its geometry buffer.

        ``cell_groups`` are that cell's ``(cell_first, entry_index)`` records
        and ``lin`` is the linear part ``(a, b, d, e)`` of the current
        matrix, or ``None`` for the baked write -- the anchors and template
        offsets the buffers were uploaded with.  Each part is one ``memmove``
        of full 12-byte rows at its own offset in the cell's buffer, so only
        the marker vertices are copied: the colours, the triangle order and
        everything around them are exactly as uploaded.  Render thread only;
        the caller decides which cells are written.
        """
        nodes = self._cell_nodes
        if cell_index >= len(nodes):
            return
        node = nodes[cell_index]
        if node is None or not _wrapper_is_valid(node):
            return
        geometry = node.geometry()
        if geometry is None:
            return
        plan = self._static.markers
        buffer = self._static.cells[cell_index].vertices
        total = int(buffer.shape[0])
        stride = VERTEX_DTYPE.itemsize
        base = _vertex_address(geometry)
        marked = False
        for cell_first, entry_index in cell_groups:
            entry = plan.entries[entry_index]
            start = int(entry.first) - int(cell_first)
            count = int(entry.count)
            if start < 0 or start + count > total:
                # Defensive: the part was validated against the cell spans at
                # adoption; never write outside the buffer regardless.
                continue
            rows = np.empty(count, dtype=VERTEX_DTYPE)
            flat = _marker_rows(entry, lin)
            rows["x"] = flat[:, 0]
            rows["y"] = flat[:, 1]
            rows["rgba"] = buffer[start : start + count]["rgba"]
            ctypes.memmove(
                ctypes.c_void_p(base + start * stride),
                ctypes.c_void_p(rows.ctypes.data),
                count * stride,
            )
            marked = True
        if marked:
            node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)

    def _update_markers(self, coeffs) -> None:
        """Keep the fixed-size marker vertices in the cell buffers current.

        Runs on the render thread after the cell matrix is applied, with the
        same in-place discipline as the stroke patch: the parsed parts'
        vertices are rewritten inside the very cell buffers they were parsed
        from, so per-cell draw order and the per-cell cull mask stay exactly
        the baked behaviour's, and nothing runs when the camera has not
        changed since the last write.  Each block's anchor stays the baked
        one -- the shared matrix maps it exactly like the cells, so the
        symbol keeps its ground position -- and the template offsets are
        mapped back through the matrix's linear part, so the symbol renders
        at its adopted logical extent whatever the zoom.  At the camera a
        frame was adopted for the matrix is the identity and the rewrite
        reproduces the baked vertices; a culled cell's parts are not
        rewritten while it stays culled; a rebuilt or restored graph starts
        from ``_marker_fresh``, which reconciles every marker-bearing cell
        once.
        """
        frame = self._static
        plan = frame.markers if frame is not None else None
        if plan is None:
            # Nothing parses: the cell buffers hold the baked rendering and
            # nothing touches them, whatever the camera does.
            self._marker_coeffs = None
            self._marker_patched = set()
            self._marker_fresh = False
            return
        exact = coeffs is None or coeffs == _IDENTITY_AFFINE
        if self._marker_fresh:
            pass  # unknown buffer provenance: always reconcile below
        elif exact:
            if not self._marker_patched:
                return
        elif coeffs == self._marker_coeffs:
            return
        by_cell = {}
        for cell_index, cell_first, entry_index in frame.marker_groups:
            by_cell.setdefault(cell_index, []).append((cell_first, entry_index))
        visible = self._cell_visible
        new_patched = set()
        for cell_index, cell_groups in by_cell.items():
            onscreen = cell_index < len(visible) and bool(visible[cell_index])
            if onscreen and not exact:
                self._write_marker_cell(
                    cell_index,
                    cell_groups,
                    (coeffs[0], coeffs[1], coeffs[3], coeffs[4]),
                )
                new_patched.add(cell_index)
            elif onscreen or cell_index in self._marker_patched or self._marker_fresh:
                # An exact camera after a patch, a cell just culled out of
                # the patch set, or buffers of unknown provenance (a rebuilt
                # or restored graph): write the baked anchors and offsets
                # back.
                self._write_marker_cell(cell_index, cell_groups, None)
        self._marker_coeffs = None if exact else coeffs
        self._marker_patched = new_patched
        self._marker_fresh = False

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
        traced = enc_trace.enabled()
        if traced:
            self._trace_frame += 1
            frame_index = self._trace_frame
            culled_detail = []
            culled_total = 0
        mask = []
        for index, cell in enumerate(frame.cells):
            if cell.bounds is None:
                mask.append(False)
                continue
            if clip is None:
                mask.append(True)
                continue
            x0, y0, x1, y1 = _affine_bounds(coeffs, cell.bounds)
            outside = x1 < clip[0] or x0 > clip[2] or y1 < clip[1] or y0 > clip[3]
            mask.append(not outside)
            if traced and outside:
                culled_total += 1
                if len(culled_detail) < TRACE_CULL_LIMIT:
                    # Distance from the bbox to the clip on the separating axis.
                    gap = max(
                        clip[0] - x1, x0 - clip[2], clip[1] - y1, y0 - clip[3], 0.0
                    )
                    culled_detail.append(
                        {
                            "cell": index,
                            "bounds": [round(float(value), 3) for value in cell.bounds],
                            "bbox": [round(value, 3) for value in (x0, y0, x1, y1)],
                            "gap": round(gap, 3),
                            "touches": gap <= TRACE_TOUCH_PX,
                        }
                    )
        if traced and culled_total:
            enc_trace.event(
                "enc_cull",
                frame=frame_index,
                clip=None if clip is None else [round(value, 3) for value in clip],
                viewport=[float(self.width()), float(self.height())],
                margin=self._compose_margin,
                culled=culled_total,
                cells=len(mask),
                capped=culled_total > TRACE_CULL_LIMIT,
                detail=culled_detail,
            )
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
        for index, (node, count, visible) in enumerate(
            zip(self._cell_nodes, self._cell_vertex_counts, mask)
        ):
            applied = count if visible else 0
            geometry = node.geometry()
            if geometry is None:
                continue
            previous = geometry.vertexCount()
            if previous == applied:
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
            if traced:
                enc_trace.event(
                    "enc_zero_count",
                    frame=frame_index,
                    cell=index,
                    previous=int(previous),
                    current=int(applied),
                    mask=bool(visible),
                    count=int(count),
                )

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

    def _restore_retained(self) -> bool:
        """Re-attach the retained frame's nodes for a return pan.

        Runs on the render thread from :meth:`_update_static`.  The retained
        and presented frames trade places: the presented frame becomes the
        retained one (if it qualifies) and the retained frame's nodes are
        re-attached exactly as they were, so the return costs no vertex
        rebuild or upload, and the two frames are never in the graph at the
        same time.  ``False`` leaves the state untouched when the entry no
        longer matches the scene on display, the graph was rebuilt since, or
        a retained node did not survive.
        """
        slot = self._retained
        if (
            slot is None
            or slot[0] is not self._view_scene
            or not self._static_mode()
            or not self._built_static
            or self._content_node is None
            or not _wrapper_is_valid(self._content_node)
            or not self._cell_nodes
        ):
            return False
        scene, frame, nodes, counts, visible = slot
        if len(nodes) != len(frame.cells) or not nodes:
            return False
        if not all(_wrapper_is_valid(node) for node in nodes):
            self._retained = None
            return False
        outgoing_scene, outgoing_frame = self._outgoing or (None, None)
        self._outgoing = None
        current_nodes = list(self._cell_nodes)
        current_counts = list(self._cell_vertex_counts)
        current_visible = self._cell_visible
        for child in current_nodes:
            try:
                self._content_node.removeChildNode(child)
            except Exception:  # pragma: no cover - Qt already dropped it
                pass
        for child in nodes:
            self._content_node.appendChildNode(child)
        self._retained = None
        if (
            outgoing_scene is not None
            and outgoing_scene is not scene
            and outgoing_frame is not None
            and len(current_nodes) == len(outgoing_frame.cells)
            and _frame_vertex_bytes(outgoing_frame) <= RETAINED_FRAME_VERTEX_BYTES
        ):
            # The presented frame becomes the retained one, so an immediate
            # return to it is answered the same way.  One entry, always.
            self._retained = (
                outgoing_scene,
                outgoing_frame,
                current_nodes,
                current_counts,
                current_visible,
            )
        self._static = frame
        self._cell_nodes = nodes
        self._cell_vertex_counts = list(counts)
        self._cell_visible = visible
        # The restored frame carries its own stroke and marker plans; the
        # rewrites are reconciled from them on the next camera.
        self._restroke_fresh = True
        self._marker_fresh = True
        return True

    def _upload_cells(self) -> None:
        """Create one static-pattern geometry node per cell under the content."""
        # The replace-time retention intent is consumed here whatever the
        # upload does: a frame that ends up dropped or empty must not carry
        # a stale intent into a later, unrelated upload.
        want = self._retention_wanted
        outgoing_scene, outgoing_frame = self._outgoing or (None, None)
        self._retention_wanted = False
        self._outgoing = None
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
        # Retain the outgoing frame, if the replace asked for it and it is
        # inside the budget: its already-uploaded nodes are detached here and
        # parked in the slot, so a return pan to that scene re-attaches them
        # instead of rebuilding and re-uploading (see ``_restore_retained``).
        # Exactly one frame is retained; the slot's previous occupant is
        # released here, on the render thread.
        previous_retained = self._retained
        self._retained = None
        if want and outgoing_scene is not None and outgoing_frame is not None:
            if _frame_vertex_bytes(outgoing_frame) <= RETAINED_FRAME_VERTEX_BYTES:
                self._retained = (
                    outgoing_scene,
                    outgoing_frame,
                    list(self._cell_nodes),
                    list(self._cell_vertex_counts),
                    self._cell_visible,
                )
        if previous_retained is not None:
            self._retained_dead.extend(previous_retained[2])
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
        if self._retained_restore:
            # A return pan asked for the retained scene: try to re-attach its
            # nodes before any rebuild.  A refused restore falls through and
            # rebuilds exactly as a scene the item never saw would.
            self._retained_restore = False
            self._restore_retained()
        if self._static is None or not self._cell_nodes:
            self._static = build_static_frame(
                self._mesh,
                self._projection,
                origin=self._origin,
                default_color=self._default_rgba(),
            )
            self._ensure_content()
            self._upload_cells()
            self._restroke_fresh = True
            self._marker_fresh = True
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
            self._restroke_fresh = True
            self._marker_fresh = True
            coeffs = self._frame_coeffs()
        if coeffs is None:
            # Nothing to transform (empty scene, or a degenerate probe set that
            # already rebuilt above): the buffers are already current.  Leave a
            # retained frame's matrix alone -- resetting it would move the last
            # complete picture while its replacement assembles.
            if self._content_node is not None and not self._cell_nodes:
                self._content_node.setMatrix(QMatrix4x4())
            self._update_restroke(None)
            self._update_markers(None)
            return
        self._apply_matrix(coeffs)
        self._cull_cells(coeffs)
        self._update_restroke(coeffs)
        self._update_markers(coeffs)

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
        # Node wrappers a GUI-thread invalidation parked are released here:
        # this runs on the render thread in every mode, after any rebuild
        # that may have extended the park list.
        self._release_dead_retained()

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
