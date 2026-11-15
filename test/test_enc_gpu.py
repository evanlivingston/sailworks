"""Focused tests for the opt-in Qt scene-graph adapter (``sailworks.qt.encgpu``).

These exercise the real PySide6 scene-graph objects: vertex upload, the
unchanged-frame fast path and node recreation after teardown.  They do not
render anything or measure presentation.
"""

from __future__ import annotations

import ctypes
import gc
import os
from types import SimpleNamespace

import numpy as np
import pytest

# Must precede any PySide6 import so the headless platform is chosen before
# Qt initialises its platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor as QtGuiColor  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQuick import (  # noqa: E402
    QSGFlatColorMaterial,
    QSGGeometry,
    QSGGeometryNode,
    QSGMaterial,
    QSGNode,
    QSGVertexColorMaterial,
)

from sailworks.qt import encgpu  # noqa: E402
from sailworks.rendering.vectordrawer.encmesh import (  # noqa: E402
    AREA_FILL,
    LINE,
    MeshPart,
    SceneMesh,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QGuiApplication.instance()
    if app is None:
        try:
            app = QGuiApplication([])
        except Exception as exc:  # pragma: no cover - headless without a platform
            pytest.skip(f"Qt platform unavailable: {exc}")
    return app


def _triangle_mesh(offset=(0.0, 0.0), dtype=np.float32) -> SceneMesh:
    ox, oy = offset
    positions = np.array(
        [[ox, oy], [ox + 10.0, oy], [ox, oy + 10.0]],
        dtype=dtype,
    )
    return SceneMesh(
        positions=positions,
        parts=(),
        unsupported=(),
        bounds=None,
        coverage_applied=True,
    )


def _read_vertices(geometry: QSGGeometry) -> np.ndarray:
    count = geometry.vertexCount()
    data = geometry.vertexData()
    address = data if isinstance(data, int) else int(data)
    raw = ctypes.string_at(address, count * 2 * 4)
    return np.frombuffer(raw, dtype=np.float32).reshape(count, 2).copy()


def _colored_mesh() -> SceneMesh:
    positions = np.array(
        [[0.0, 0.0], [10.0, 0.0], [0.0, 10.0], [20.0, 0.0], [30.0, 0.0], [20.0, 10.0]],
        dtype=np.float64,
    )
    parts = (
        MeshPart(0, "DEPARE", 0, 1000, AREA_FILL, 0, 3, (1, 2, 3, 255)),
        MeshPart(0, "COALNE", 1, 1001, LINE, 3, 3, (4, 5, 6, 128)),
    )
    return SceneMesh(
        positions=positions,
        parts=parts,
        unsupported=(),
        bounds=(0.0, 0.0, 30.0, 10.0),
        coverage_applied=True,
    )


def _read_colored(geometry: QSGGeometry) -> np.ndarray:
    count = geometry.vertexCount()
    raw = ctypes.string_at(
        encgpu._vertex_address(geometry), count * encgpu.VERTEX_DTYPE.itemsize
    )
    return np.frombuffer(raw, dtype=encgpu.VERTEX_DTYPE).copy()


def test_qcolor_imported_from_qtgui(qt_app):
    # QColor lives in QtGui; importing it from QtCore is an ImportError at
    # import time, so this also guards module importability.
    assert encgpu.QColor is QtGuiColor


def test_apply_mesh_subtracts_origin_in_float64(qt_app):
    base = 1.0e8
    mesh = SimpleNamespace(
        positions=np.array(
            [[base + 0.25, base], [base + 0.75, base], [base, base + 0.25]],
            dtype=np.float64,
        ),
        vertex_count=3,
    )
    geometry = QSGGeometry(encgpu._point2d_attributes(), 0)

    assert encgpu.apply_mesh(geometry, mesh, origin=(base, base)) == 3
    assert geometry.vertexCount() == 3
    vertices = _read_vertices(geometry)
    assert vertices.dtype == np.float32
    np.testing.assert_allclose(
        vertices,
        np.array([[0.25, 0.0], [0.75, 0.0], [0.0, 0.25]], dtype=np.float32),
    )


def test_apply_mesh_projection_receives_float64_source(qt_app):
    mesh = SimpleNamespace(
        positions=np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]], dtype=np.float32),
        vertex_count=3,
    )
    geometry = QSGGeometry(encgpu._point2d_attributes(), 0)
    seen = []

    def projection(source):
        seen.append(source.dtype)
        return source * 2.0

    encgpu.apply_mesh(geometry, mesh, projection, origin=(1.0, 1.0))
    assert seen == [np.dtype(np.float64)]
    assert _read_vertices(geometry).dtype == np.float32
    np.testing.assert_allclose(
        _read_vertices(geometry),
        np.array([[0.0, 2.0], [4.0, 6.0], [8.0, 10.0]], dtype=np.float32),
    )


def test_apply_mesh_empty_allocates_zero_vertices(qt_app):
    geometry = QSGGeometry(encgpu._point2d_attributes(), 3)
    mesh = SimpleNamespace(positions=np.empty((0, 2), dtype=np.float32), vertex_count=0)
    assert encgpu.apply_mesh(geometry, mesh) == 0
    assert geometry.vertexCount() == 0


def test_apply_mesh_rejects_wrong_projection_shape(qt_app):
    mesh = SimpleNamespace(
        positions=np.zeros((3, 2), dtype=np.float32),
        vertex_count=3,
    )
    geometry = QSGGeometry(encgpu._point2d_attributes(), 0)
    with pytest.raises(ValueError):
        encgpu.apply_mesh(geometry, mesh, lambda source: source[:2])


def test_unchanged_frame_skips_projection_and_upload(qt_app):
    item = encgpu.EncMeshItem()
    calls = []

    def projection(source):
        calls.append(source.shape)
        return source + 1.0

    assert item.set_projection(projection)
    mesh = _triangle_mesh()
    assert item.set_mesh(mesh)
    assert item.generation == 1

    node = item.updatePaintNode(None, None)
    assert isinstance(node, QSGGeometryNode)
    assert item.upload_count == 1
    assert item._geometry.vertexCount() == 3
    assert len(calls) == 1

    # Same mesh, same projection, same origin: Qt re-runs updatePaintNode but
    # nothing changed, so no Python projection and no vertex copy happen.
    for _ in range(3):
        assert item.updatePaintNode(node, None) is node
    assert item.upload_count == 1
    assert len(calls) == 1
    assert item._geometry.vertexCount() == 3

    # A colour change only touches the material.
    assert item.set_color(QtGuiColor(10, 20, 30, 255))
    assert item.updatePaintNode(node, None) is node
    assert item.upload_count == 1
    assert len(calls) == 1

    # A transform change re-uploads exactly once.
    assert item.set_projection(None)
    assert item.updatePaintNode(node, None) is node
    assert item.upload_count == 2
    np.testing.assert_allclose(
        _read_vertices(item._geometry),
        np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]], dtype=np.float32),
    )

    # Changing the local origin also re-uploads.
    assert item.set_origin((1.0, 2.0))
    item.updatePaintNode(node, None)
    assert item.upload_count == 3
    np.testing.assert_allclose(
        _read_vertices(item._geometry),
        np.array([[-1.0, -2.0], [9.0, -2.0], [-1.0, 8.0]], dtype=np.float32),
    )


def test_cleared_mesh_uploads_zero_vertices(qt_app):
    item = encgpu.EncMeshItem()
    item.set_mesh(_triangle_mesh())
    node = item.updatePaintNode(None, None)
    assert item._geometry.vertexCount() == 3

    assert item.clear_mesh()
    item.updatePaintNode(node, None)
    assert item.upload_count == 2
    assert item._geometry.vertexCount() == 0


def test_node_recreation_uses_fresh_geometry_and_material(qt_app):
    item = encgpu.EncMeshItem()
    item.set_mesh(_triangle_mesh())

    first = item.updatePaintNode(None, None)
    old_geometry = item._geometry
    old_material = item._material
    assert old_geometry.vertexCount() == 3

    # Qt deleted the node when the item left the scene graph and hands back a
    # fresh one: the adapter must build new children, not touch the stale
    # wrappers it still held.
    second = item.updatePaintNode(None, None)
    assert second is not first
    assert item._node is second
    assert item._geometry is not old_geometry
    assert item._material is not old_material
    assert item._geometry.vertexCount() == 3
    assert item.upload_count == 2
    np.testing.assert_allclose(
        _read_vertices(item._geometry),
        np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]], dtype=np.float32),
    )

    flags_getter = getattr(second, "flags", None)
    if flags_getter is not None:  # PySide6 exposes QSGNode::flags()
        flags = flags_getter()
        assert flags & QSGNode.Flag.OwnsGeometry
        assert flags & QSGNode.Flag.OwnsMaterial

    # A node the item no longer recognises is adopted with fresh children
    # rather than reused with a mismatched geometry.
    stale_geometry = item._geometry
    item._node = None
    item._geometry = None
    item._material = None
    adopted = item.updatePaintNode(second, None)
    assert adopted is second
    assert item._node is second
    assert item._geometry is not None
    assert item._geometry is not stale_geometry
    assert item._geometry.vertexCount() == 3
    assert item.upload_count == 3

    # Steady state after the rebuild is still cheap.
    item.updatePaintNode(second, None)
    assert item.upload_count == 3


def test_colored_vertex_layout_is_twelve_bytes(qt_app):
    assert encgpu.VERTEX_DTYPE.itemsize == 12
    geometry = QSGGeometry(encgpu._colored_attributes(), 0)
    assert encgpu.apply_colored_mesh(geometry, _colored_mesh()) == 6
    assert geometry.vertexCount() == 6

    vertices = _read_colored(geometry)
    assert vertices["x"].dtype == np.float32
    np.testing.assert_allclose(vertices["x"], [0, 10, 0, 20, 30, 20])
    np.testing.assert_allclose(vertices["y"], [0, 0, 10, 0, 0, 10])
    # Parts keep their encounter order: fill run first, line run second.
    np.testing.assert_array_equal(vertices["rgba"][:3], [[1, 2, 3, 255]] * 3)
    np.testing.assert_array_equal(vertices["rgba"][3:], [[4, 5, 6, 128]] * 3)


def test_colored_item_uses_blended_vertex_color_material(qt_app):
    item = encgpu.EncMeshItem(colored=True)
    assert item.colored
    item.set_mesh(_colored_mesh())
    node = item.updatePaintNode(None, None)

    assert isinstance(node, QSGGeometryNode)
    # The material must be Qt's own C++ class, not a Python subclass whose
    # virtual flags() could be invoked after the C++ object is gone.
    assert type(item._material) is QSGVertexColorMaterial
    assert item._material.flags() & QSGMaterial.Flag.Blending
    assert item._built_colored is True
    assert item.upload_count == 1

    vertices = _read_colored(item._geometry)
    np.testing.assert_array_equal(vertices["rgba"][0], [1, 2, 3, 255])
    np.testing.assert_array_equal(vertices["rgba"][-1], [4, 5, 6, 128])

    # An unchanged coloured frame does no projection, copy or reorder.
    for _ in range(3):
        assert item.updatePaintNode(node, None) is node
    assert item.upload_count == 1
    rgba = _read_colored(item._geometry)["rgba"][0]
    np.testing.assert_array_equal(rgba, [1, 2, 3, 255])


def test_colored_node_lifecycle_owns_blended_base_material(qt_app):
    item = encgpu.EncMeshItem(colored=True)
    item.set_mesh(_colored_mesh())
    node = item.updatePaintNode(None, None)
    material = item._material

    def _flags(obj):
        flags_getter = getattr(obj, "flags", None)
        return flags_getter() if flags_getter is not None else None

    node_flags = _flags(node)
    if node_flags is not None:  # PySide6 exposes QSGNode::flags()
        assert node_flags & QSGNode.Flag.OwnsGeometry
        assert node_flags & QSGNode.Flag.OwnsMaterial

    assert encgpu._wrapper_is_valid(material)
    assert material.flags() & QSGMaterial.Flag.Blending

    # Qt has deleted the old node and its children and hands back a new one:
    # the adapter builds a fresh base material without probing the stale
    # wrapper.
    rebuilt = item.updatePaintNode(None, None)
    assert rebuilt is not node
    assert item._material is not material
    assert type(item._material) is QSGVertexColorMaterial
    assert item._material.flags() & QSGMaterial.Flag.Blending
    assert encgpu._wrapper_is_valid(item._material)

    rebuilt_flags = _flags(rebuilt)
    if rebuilt_flags is not None:
        assert rebuilt_flags & QSGNode.Flag.OwnsGeometry
        assert rebuilt_flags & QSGNode.Flag.OwnsMaterial


def test_colored_item_fills_uncolored_parts_with_its_default(qt_app):
    plain = SceneMesh(
        positions=np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]], dtype=np.float64),
        parts=(MeshPart(0, "UNKNOWN", 0, 1, AREA_FILL, 0, 3),),
        unsupported=(),
        bounds=None,
        coverage_applied=True,
    )
    item = encgpu.EncMeshItem(colored=True)
    item.set_mesh(plain)
    item.updatePaintNode(None, None)
    np.testing.assert_array_equal(
        _read_colored(item._geometry)["rgba"][0], [120, 155, 190, 255]
    )


def test_colored_origin_is_subtracted_before_the_float32_cast(qt_app):
    base = 1.0e8
    mesh = SceneMesh(
        positions=np.array(
            [[base + 0.25, base], [base + 0.75, base], [base, base + 0.25]],
            dtype=np.float64,
        ),
        parts=(MeshPart(0, "DEPARE", 0, 1, AREA_FILL, 0, 3, (9, 8, 7, 255)),),
        unsupported=(),
        bounds=None,
        coverage_applied=True,
    )
    item = encgpu.EncMeshItem(colored=True)
    item.set_origin((base, base))
    item.set_mesh(mesh)
    item.updatePaintNode(None, None)
    vertices = _read_colored(item._geometry)
    np.testing.assert_allclose(vertices["x"], [0.25, 0.75, 0.0])
    np.testing.assert_allclose(vertices["y"], [0.0, 0.0, 0.25])
    np.testing.assert_array_equal(vertices["rgba"][0], [9, 8, 7, 255])


def test_switching_color_mode_rebuilds_the_attribute_layout(qt_app):
    item = encgpu.EncMeshItem(colored=True)
    item.set_mesh(_colored_mesh())
    node = item.updatePaintNode(None, None)
    colored_geometry = item._geometry
    assert item.upload_count == 1

    assert item.set_colored(False)
    assert item.updatePaintNode(node, None) is node
    assert item._built_colored is False
    assert item._geometry is not colored_geometry
    assert isinstance(item._material, QSGFlatColorMaterial)
    assert item.upload_count == 2
    # Flat mode writes 8-byte Point2D vertices again.
    assert item._geometry.vertexCount() == 6
    assert _read_vertices(item._geometry).shape == (6, 2)

    # Steady state in flat mode stays upload-free.
    item.updatePaintNode(node, None)
    assert item.upload_count == 2


def _large_positions(count: int) -> np.ndarray:
    xs = np.arange(count, dtype=np.float64)
    return np.column_stack((xs, -xs))


def _large_mesh(count: int) -> SceneMesh:
    return SceneMesh(
        positions=_large_positions(count),
        parts=(),
        unsupported=(),
        bounds=None,
        coverage_applied=True,
    )


def _fresh_geometry(attributes=None) -> QSGGeometry:
    """Build a dynamic-triangle geometry on a pinned, process-lifetime layout.

    ``QSGGeometry`` keeps only the raw ``AttributeSet`` pointer it is handed,
    so a temporary built inline here would be freed with this frame and the
    geometry's ``attributeCount()``/``sizeOfVertex()`` would read freed
    memory.  Callers therefore pass one of the pinned sets from ``encgpu``.
    """
    if attributes is None:
        attributes = encgpu._point2d_attributes()
    geometry = QSGGeometry(attributes, 0)
    geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
    geometry.setVertexDataPattern(QSGGeometry.DataPattern.DynamicPattern)
    return geometry


def test_large_flat_upload_copies_full_capacity(qt_app):
    """A >1MB flat upload lands byte-exact in the real Qt vertex buffer."""
    count = 200_000  # 1.6 MB at the 8-byte Point2D stride.
    geometry = _fresh_geometry(encgpu._point2d_attributes())

    uploaded = encgpu.apply_mesh(geometry, _large_mesh(count))

    assert uploaded == count == geometry.vertexCount()
    assert encgpu._vertex_address(geometry) > 0
    raw = ctypes.string_at(encgpu._vertex_address(geometry), uploaded * 2 * 4)
    vertices = np.frombuffer(raw, dtype=np.float32).reshape(uploaded, 2)
    xs = np.arange(count, dtype=np.float32)
    assert np.array_equal(vertices, np.column_stack((xs, -xs)))


def test_large_colored_upload_copies_full_capacity(qt_app):
    """A >1MB ColoredPoint2D upload keeps every x/y and RGBA byte."""
    count = 200_000  # 2.4 MB at the 12-byte ColoredPoint2D stride.
    color = (9, 8, 7, 200)
    mesh = SceneMesh(
        positions=_large_positions(count),
        parts=(MeshPart(0, "DEPARE", 0, 1000, AREA_FILL, 0, count, color),),
        unsupported=(),
        bounds=None,
        coverage_applied=True,
    )
    geometry = _fresh_geometry(encgpu._colored_attributes())

    uploaded = encgpu.apply_colored_mesh(geometry, mesh)

    assert uploaded == count == geometry.vertexCount()
    raw = ctypes.string_at(
        encgpu._vertex_address(geometry), uploaded * encgpu.VERTEX_DTYPE.itemsize
    )
    vertices = np.frombuffer(raw, dtype=encgpu.VERTEX_DTYPE)
    xs = np.arange(count, dtype=np.float32)
    assert np.array_equal(vertices["x"], xs)
    assert np.array_equal(vertices["y"], -xs)
    rgba = np.tile(np.array(color, np.uint8), (count, 1))
    assert np.array_equal(vertices["rgba"], rgba)


def test_upload_is_independent_of_the_source_array(qt_app):
    """The copied bytes outlive the mesh arrays they came from."""
    count = 50_000
    positions = _large_positions(count)
    mesh = SceneMesh(
        positions=positions,
        parts=(),
        unsupported=(),
        bounds=None,
        coverage_applied=True,
    )
    geometry = _fresh_geometry(encgpu._point2d_attributes())
    encgpu.apply_mesh(geometry, mesh)

    positions[:] = 0.0
    del positions, mesh

    raw = ctypes.string_at(encgpu._vertex_address(geometry), count * 2 * 4)
    vertices = np.frombuffer(raw, dtype=np.float32).reshape(count, 2)
    assert vertices[3, 0] == np.float32(3.0)
    assert vertices[count - 1, 1] == np.float32(-(count - 1))


def test_upload_rejects_a_stride_mismatch(qt_app):
    """A 16-byte array is refused rather than written into an 8-byte layout."""
    geometry = _fresh_geometry(encgpu._point2d_attributes())
    geometry.allocate(4)
    textured = np.zeros((4, 4), dtype=np.float32)

    with pytest.raises(ValueError, match="8-byte layout"):
        encgpu._upload_vertices(geometry, textured, encgpu.POINT_STRIDE)


def test_upload_rejects_a_capacity_mismatch(qt_app):
    """Prepared vertices that exceed the allocated capacity are refused."""
    geometry = _fresh_geometry(encgpu._point2d_attributes())
    geometry.allocate(4)
    vertices = np.zeros((8, 2), dtype=np.float32)

    with pytest.raises(ValueError, match="allocated 4 vertices"):
        encgpu._upload_vertices(geometry, vertices, encgpu.POINT_STRIDE)


def test_vertex_address_rejects_a_null_buffer(qt_app):
    """A null vertexData() raises instead of a null memmove on the render thread."""

    class _NoBuffer:
        def vertexData(self):
            return 0

    with pytest.raises(RuntimeError, match="no vertex buffer"):
        encgpu._vertex_address(_NoBuffer())


#: Stride each pinned layout must report.  The values are the ones Qt defines;
#: they are read back through ``QSGGeometry.sizeOfVertex()`` below.
_PINNED_LAYOUTS = (
    ("_point2d_attributes", encgpu.POINT_STRIDE),
    ("_colored_attributes", encgpu.VERTEX_DTYPE.itemsize),
    ("_textured_attributes", encgpu.TEXT_VERTEX_DTYPE.itemsize),
)


def _layout(geometry) -> tuple[int, int]:
    """Read the two accessors that depend on the attribute set being alive."""
    return (int(geometry.attributeCount()), int(geometry.sizeOfVertex()))


def test_pinned_attribute_sets_are_stable_across_collection(qt_app):
    """Every call returns the same live ``AttributeSet`` object, after GC."""
    first = tuple(getattr(encgpu, name)() for name, _ in _PINNED_LAYOUTS)

    for _ in range(3):
        gc.collect()
        current = tuple(getattr(encgpu, name)() for name, _ in _PINNED_LAYOUTS)
        assert all(now is kept for now, kept in zip(current, first))


@pytest.mark.parametrize("name, stride", _PINNED_LAYOUTS)
def test_geometry_layout_survives_the_builder_frame(qt_app, name, stride):
    """A geometry keeps a valid count/stride after its builder frame is gone."""
    factory = getattr(encgpu, name)

    def build():
        geometry = QSGGeometry(factory(), 0)
        geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
        geometry.allocate(4)
        return geometry

    geometry = build()
    before = _layout(geometry)
    assert before[1] == stride
    assert before[0] > 0

    for _ in range(3):
        gc.collect()
        assert _layout(geometry) == before
        assert int(geometry.vertexCount()) == 4


def test_paint_node_geometry_layout_survives_the_caller_local(qt_app):
    """The item's real triangle geometry reports 8 bytes/vertex after GC."""
    item = encgpu.EncMeshItem()
    item.set_mesh(_triangle_mesh())
    node = item.updatePaintNode(None, None)
    geometry = item._geometry
    before = _layout(geometry)
    assert before[1] == encgpu.POINT_STRIDE
    assert int(geometry.vertexCount()) == 3

    del node
    for _ in range(3):
        gc.collect()
        assert _layout(geometry) == before
        assert int(geometry.vertexCount()) == 3

    raw = ctypes.string_at(encgpu._vertex_address(geometry), 3 * 2 * 4)
    vertices = np.frombuffer(raw, dtype=np.float32).reshape(3, 2)
    np.testing.assert_array_equal(
        vertices, np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]], dtype=np.float32)
    )


def test_colored_paint_node_geometry_layout_survives_the_caller_local(qt_app):
    """The coloured item keeps its 12-byte layout after its locals are gone."""
    item = encgpu.EncMeshItem()
    item.set_colored(True)
    item.set_mesh(_triangle_mesh())
    node = item.updatePaintNode(None, None)
    geometry = item._geometry
    before = _layout(geometry)
    assert before[1] == encgpu.VERTEX_DTYPE.itemsize

    del node
    for _ in range(3):
        gc.collect()
        assert _layout(geometry) == before


def test_upload_accepts_a_plain_n_by_2_float32_array(qt_app):
    """8 bytes per row comes from the trailing dimension, not ``itemsize``."""
    geometry = _fresh_geometry(encgpu._point2d_attributes())
    geometry.allocate(3)
    vertices = np.arange(6, dtype=np.float32).reshape(3, 2)

    assert encgpu._upload_vertices(geometry, vertices, encgpu.POINT_STRIDE) == 3
    raw = ctypes.string_at(encgpu._vertex_address(geometry), 3 * 2 * 4)
    assert np.array_equal(np.frombuffer(raw, dtype=np.float32).reshape(3, 2), vertices)


def test_upload_rejects_a_non_contiguous_array(qt_app):
    """A strided view is refused before ``memmove`` copies the wrong rows."""
    geometry = _fresh_geometry(encgpu._point2d_attributes())
    geometry.allocate(4)
    padded = np.zeros((4, 4), dtype=np.float32)
    strided = padded[:, :2]
    assert not strided.flags["C_CONTIGUOUS"]

    with pytest.raises(ValueError, match="C-contiguous"):
        encgpu._upload_vertices(geometry, strided, encgpu.POINT_STRIDE)


def test_rejected_upload_leaves_the_buffer_untouched(qt_app):
    """A capacity mismatch is caught before any byte is copied."""
    geometry = _fresh_geometry(encgpu._point2d_attributes())
    geometry.allocate(4)
    byte_count = 4 * encgpu.POINT_STRIDE
    ctypes.memset(encgpu._vertex_address(geometry), 0xAB, byte_count)
    oversized = np.zeros((8, 2), dtype=np.float32)

    with pytest.raises(ValueError, match="allocated 4 vertices"):
        encgpu._upload_vertices(geometry, oversized, encgpu.POINT_STRIDE)

    raw = ctypes.string_at(encgpu._vertex_address(geometry), byte_count)
    assert raw == b"\xab" * byte_count
