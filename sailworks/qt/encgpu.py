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

import numpy as np
from PySide6.QtGui import QColor
from PySide6.QtQuick import (
    QQuickItem,
    QSGFlatColorMaterial,
    QSGGeometry,
    QSGGeometryNode,
    QSGMaterial,
    QSGNode,
    QSGTextureMaterial,
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
    geometry: QSGGeometry, mesh: SceneMesh, projection=None, *, origin=None
) -> int:
    """Copy ``mesh`` triangles into ``geometry`` and return the vertex count.

    ``positions`` are promoted to ``float64`` and, when given, the local
    ``origin`` is subtracted before ``projection`` runs.  Only the final
    ``(N, 2)`` item-local array is cast to ``float32``.  ``projection`` maps an
    ``(N, 2)`` ``float64`` source-position array to item-local positions.  A
    mesh with no vertices allocates zero vertices, which draws nothing and
    cannot keep a previous frame's content.
    """
    projected = _project_positions(mesh, projection, origin)
    if projected.shape[0] == 0:
        geometry.allocate(0)
        return geometry.vertexCount()

    output = np.ascontiguousarray(projected, dtype=np.float32)
    if output.shape != (mesh.vertex_count, 2):
        raise ValueError("positions must be an (N, 2) array")
    geometry.allocate(output.shape[0])
    _upload_vertices(geometry, output, POINT_STRIDE)
    return geometry.vertexCount()


def apply_colored_mesh(
    geometry: QSGGeometry,
    mesh: SceneMesh,
    projection=None,
    *,
    origin=None,
    default_color=NEUTRAL,
) -> int:
    """Write 12-byte ``ColoredPoint2D`` vertices for one coloured mesh.

    Part colours from :class:`encmesh.MeshPart` are expanded in part order, so
    the vertex buffer preserves the area-then-line (or line-first) encounter
    order and a single node can draw every feature without one node per
    feature.  Positions go through the same float64 origin/projection path as
    :func:`apply_mesh`.
    """
    projected = _project_positions(mesh, projection, origin)
    if projected.shape[0] == 0:
        geometry.allocate(0)
        return geometry.vertexCount()

    positions = np.ascontiguousarray(projected, dtype=np.float32)
    pieces = getattr(mesh, "parts", ())
    colors = vertex_colours(pieces, positions.shape[0], default_color)
    vertices = np.empty(positions.shape[0], dtype=VERTEX_DTYPE)
    vertices["x"] = positions[:, 0]
    vertices["y"] = positions[:, 1]
    vertices["rgba"] = colors
    geometry.allocate(vertices.shape[0])
    _upload_vertices(geometry, vertices, VERTEX_DTYPE.itemsize)
    return geometry.vertexCount()


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
        text_enabled=False,
    ):
        super().__init__(parent)
        self.setFlag(QQuickItem.Flag.ItemHasContents, True)
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
        #: Sounding labels are opt-in until the textured-node teardown is
        #: proven stable.  With this off the item draws triangles and markers
        #: only and never builds a ``QSGTextureMaterial``; the glyph atlas and
        #: the Qt-free sounding collection remain available and tested.
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
            self._geometry_dirty = True
        else:
            self._material_dirty = True
        self.update()
        return True

    def set_projection(self, projection) -> bool:
        if projection is self._projection:
            return False
        self._projection = projection
        self._geometry_dirty = True
        self._text_dirty = True
        self.update()
        return True

    def set_origin(self, origin) -> bool:
        """Set the local camera origin subtracted before projection.

        Changing it re-uploads on the next frame; the subtraction itself runs
        in ``float64`` so scene-scale coordinates keep their precision.
        """
        if origin is not None:
            origin = (float(origin[0]), float(origin[1]))
        if origin == self._origin:
            return False
        self._origin = origin
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

    def _forget_scene_graph(self) -> None:
        """Drop Python references to scene-graph objects Qt may have deleted.

        Used on teardown and before a rebuild.  The incoming node is never
        reused, so an invalid wrapper cannot reach a Qt call.
        """
        self._node = None
        self._geometry = None
        self._material = None
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

    def _children_are_usable(self, node) -> bool:
        return (
            self._node is node
            and self._built_colored == self._colored
            and self._geometry is not None
            and self._material is not None
            and _wrapper_is_valid(self._geometry)
            and _wrapper_is_valid(self._material)
        )

    def updatePaintNode(self, node, _data):  # noqa: N802 - Qt override
        if node is None:
            node = QSGGeometryNode()
            self._forget_scene_graph()

        if not self._children_are_usable(node):
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
            self._geometry = geometry
            self._material = material
            self._built_colored = self._colored

        if self._material_dirty:
            # QSGVertexColorMaterial has no uniform colour to update.
            if not self._built_colored:
                self._material.setColor(self._color)
            node.markDirty(QSGNode.DirtyStateBit.DirtyMaterial)
            self._material_dirty = False

        if self._geometry_dirty:
            if self._built_colored:
                apply_colored_mesh(
                    self._geometry,
                    self._mesh,
                    self._projection,
                    origin=self._origin,
                    default_color=self._default_rgba(),
                )
            else:
                apply_mesh(
                    self._geometry, self._mesh, self._projection, origin=self._origin
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
