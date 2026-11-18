"""Scene-graph lifecycle gate for :class:`EncMeshItem` in a real Quick window.

The item is attached to an offscreen ``QQuickWindow``, render progress is
observed through ``afterRendering`` (never by grabbing the framebuffer), the
source mesh and colour are changed, the item is detached with the window's
resources released, and the same Python item is reattached to a fresh window.

This asserts attach/update/detach/reattach semantics only: no frame timing,
no FPS and no pixel readback.  Qt-owned wrappers are never inspected after
teardown and no pointers are compared across it; the ``releaseResources`` test
asserts only that the Python references became ``None``, never that a wrapper
survived.
"""

import os

# Must be set before PySide6 is imported so the offscreen plugin is selected.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402
import ctypes  # noqa: E402
from threading import Lock  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import QSizeF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQuick import QQuickWindow, QSGGeometry  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sailworks.qt.encgpu import EncMeshItem, _vertex_address  # noqa: E402
from sailworks.rendering.encview import (  # noqa: E402
    CellSpan,
    SkippedCell,
    ViewScene,
)
from sailworks.rendering.vectordrawer import encmesh  # noqa: E402
from sailworks.rendering.vectordrawer.encmesh import (  # noqa: E402
    EMPTY_MESH,
    MeshPart,
    SceneMesh,
)

TIMEOUT = 5.0


@pytest.fixture(scope="module")
def app():
    yield QGuiApplication.instance() or QGuiApplication([])


class _Frames:
    """Render-thread frame counter; only a lock-protected integer is shared."""

    def __init__(self):
        self._lock = Lock()
        self._rendered = 0

    def on_rendered(self):
        with self._lock:
            self._rendered += 1

    def rendered(self):
        with self._lock:
            return self._rendered


def _mesh(positions):
    return SceneMesh(
        positions=np.asarray(positions, dtype=np.float64),
        parts=(),
        unsupported=(),
        bounds=None,
    )


def _identity(positions):
    """Render-thread-safe projection: source units are already item-local."""
    return positions


def _offset(positions):
    """A camera change that moves the whole frame off the item's viewport."""
    return np.asarray(positions, dtype=np.float64) + np.array((1000.0, 1000.0))


#: Mirrors the ColoredPoint2D layout the coloured item uploads: two float32
#: coordinates followed by four premultiplied RGBA bytes, 12 bytes per vertex.
_COLORED_VERTEX = np.dtype(
    {"names": ["x", "y", "rgba"], "formats": ["<f4", "<f4", "4u1"], "itemsize": 12}
)


def _premultiplied(colours):
    """Premultiply straight RGBA bytes the way the Qt uploader must.

    Qt Quick's scene-graph blend mode is premultiplied, so the coloured vertex
    upload stores ``rgb * a // 255`` (integer division, truncating) and leaves
    alpha unchanged.  ``MeshPart.colour`` itself stays straight.
    """
    colours = np.asarray(colours, dtype=np.uint8).copy()
    colours[..., :3] = (
        colours[..., :3].astype(np.uint16) * colours[..., 3:4].astype(np.uint16) // 255
    ).astype(np.uint8)
    return colours


def _two_cell_view_scene():
    """Two contributing cells tiling a 64x64 item: six vertices, then three.

    The cells must reach the item the lifecycle test sizes them to, because
    coverage is now a containment claim over the item rather than an overlap
    test: a scene that paints one corner reports no coverage.
    """
    positions = np.array(
        [
            (0, 0),
            (40, 0),
            (0, 64),
            (40, 0),
            (40, 64),
            (0, 64),
            (40, 0),
            (64, 0),
            (40, 64),
        ],
        dtype=np.float64,
    )
    parts = (
        MeshPart(0, "DEPARE", 0, 11, encmesh.AREA_FILL, 0, 3, (10, 20, 30, 255)),
        MeshPart(0, "DEPARE", 1, 11, encmesh.AREA_OUTLINE, 3, 3, (200, 100, 50, 128)),
        MeshPart(1, "DEPCNT", 0, 21, encmesh.LINE, 6, 3, (5, 250, 15, 255)),
    )
    cells = (
        CellSpan("A.000", None, 0, 2, 0, 6, "none"),
        CellSpan("B.000", None, 2, 1, 6, 3, "none"),
    )
    return ViewScene(
        positions=positions,
        parts=parts,
        cells=cells,
        unsupported=(),
        skipped=(),
        accounted_bytes=123,
    )


def _uploaded_scene_vertices(item):
    """Read every coloured cell buffer, in scene order, and concatenate it.

    The static path stores one ``QSGGeometryNode`` per contributing cell under
    a single ``QSGTransformNode``; the root node holds no geometry itself and
    ``item._geometry`` stays ``None`` in this mode (it is only the single-node
    legacy buffer, never a cell alias).  Concatenating the children recovers
    the scene order.
    """
    assert item._built_colored
    content = item._content_node
    assert content is not None and item._static is not None
    assert len(item._cell_nodes) == len(item._static.cells)
    # Every cell node is attached to the one transform node: a cell that was
    # built but never appended (dropped) leaves the counts disagreeing.
    assert content.childCount() == len(item._cell_nodes)
    counts = []
    chunks = []
    for cell, node in zip(item._static.cells, item._cell_nodes):
        geometry = node.geometry()
        count = geometry.vertexCount()
        assert count == cell.vertices.shape[0]
        counts.append(count)
        if count:
            raw = ctypes.string_at(
                _vertex_address(geometry), count * _COLORED_VERTEX.itemsize
            )
            chunks.append(np.frombuffer(raw, dtype=_COLORED_VERTEX, count=count).copy())
    if not chunks:
        return counts, np.empty(0, dtype=_COLORED_VERTEX)
    return counts, np.concatenate(chunks)


def _mapped_cell_positions(item, vertices):
    """Map concatenated static buffers through the item's transform matrix.

    The buffers hold reference-frame offsets; the transform node carries the
    pan/zoom matrix back to item pixels, so mapping here yields the positions
    the render thread actually draws.
    """
    matrix = item._content_node.matrix()
    rows = np.asarray(
        [[matrix[i, j] for j in range(4)] for i in range(4)], dtype=np.float64
    )
    local = np.column_stack(
        (
            vertices["x"].astype(np.float64),
            vertices["y"].astype(np.float64),
            np.zeros(vertices.shape[0]),
            np.ones(vertices.shape[0]),
        )
    )
    return (local @ rows.T)[:, :2]


def _wait_frames(frames, start, needed, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    while frames.rendered() - start < needed and time.monotonic() < deadline:
        QTest.qWait(5)
    return frames.rendered() - start >= needed


def _attach(item, frames):
    window = QQuickWindow()
    errors = []
    signal = getattr(window, "sceneGraphError", None)
    if signal is not None:
        signal.connect(lambda _api, message: errors.append(str(message)))
    handler = frames.on_rendered
    window.afterRendering.connect(handler, Qt.ConnectionType.DirectConnection)
    item.setParentItem(window.contentItem())
    window.resize(64, 64)
    window.show()
    return window, errors, handler


def _release(item, window, handler):
    """Disconnect, unparent, close and release; safe to call twice."""
    try:
        window.afterRendering.disconnect(handler)
    except (RuntimeError, TypeError):
        pass
    item.setParentItem(None)
    window.close()
    try:
        window.releaseResources()
    except RuntimeError:
        pass


def _render_unchanged(window, frames, count):
    """Render ``count`` frames of an unchanged item, one window.update() each.

    The offscreen window renders on demand, so it will not emit further
    ``afterRendering`` events just because time passed.  Each desired frame is
    therefore requested explicitly.  The counter is captured *before*
    ``update()`` so a frame that lands immediately is not missed.
    """
    for _ in range(count):
        start = frames.rendered()
        window.update()
        if not _wait_frames(frames, start, 1):
            return False
    return True


def _skip_if_stalled(frames, start, errors, stage):
    if _wait_frames(frames, start, 1):
        return
    pytest.skip(
        f"offscreen QQuickWindow never rendered the {stage} attach "
        f"(afterRendering did not fire within {TIMEOUT:.0f}s; "
        f"sceneGraphError={errors})"
    )


def test_enc_mesh_item_attach_update_detach_reattach(app):
    frames = _Frames()
    item = EncMeshItem()
    item.setSize(QSizeF(64, 64))
    item.set_projection(_identity)
    first = _mesh([(0, 0), (8, 0), (0, 8)])
    second = _mesh([(0, 0), (16, 0), (16, 16), (0, 16), (8, 8), (2, 14)])
    item.set_mesh(first)
    source_positions = second.positions.copy()

    # No projection and no vertex copy happens before a scene graph exists.
    assert item.upload_count == 0
    assert item.generation == 1
    assert item.mesh is first

    window, errors, handler = _attach(item, frames)
    try:
        _skip_if_stalled(frames, 0, errors, "first")
        assert item.upload_count >= 1, "first frame never uploaded vertices"

        # An unchanged item must not rebuild or re-upload on later frames.
        settled = item.upload_count
        assert _render_unchanged(window, frames, 2), "unchanged frame never requested"
        assert item.upload_count == settled, "unchanged item re-uploaded vertices"

        # A new source mesh uploads exactly once and keeps the source intact.
        assert item.set_mesh(second) is True
        assert item.generation == 2
        assert item.set_mesh(second) is False, "identical source re-marked dirty"
        before = item.upload_count
        assert _wait_frames(frames, frames.rendered(), 1), "mesh change never rendered"
        assert item.upload_count == before + 1, "mesh change was not uploaded once"
        assert item.mesh is second
        assert item.mesh.positions.dtype == np.float64
        np.testing.assert_array_equal(item.mesh.positions, source_positions)
        assert item.is_complete and item.unsupported_count == 0

        # Flat colour lives in the material, so it must not re-upload geometry.
        colour_before = item.upload_count
        assert item.set_color("#ff0000") is True
        assert item.set_color("#ff0000") is False
        assert _wait_frames(
            frames, frames.rendered(), 1
        ), "colour change never rendered"
        assert item.upload_count == colour_before, "colour change re-uploaded vertices"

        # Detach from the live scene graph and release the window's resources.
        _release(item, window, handler)
        detached_uploads = item.upload_count
        np.testing.assert_array_equal(item.mesh.positions, source_positions)

        # The same Python item must rebuild its Qt-owned objects in a new window.
        window2, errors2, handler2 = _attach(item, frames)
        try:
            start = frames.rendered()
            if not _wait_frames(frames, start, 1):
                pytest.fail(
                    "reattached item never rendered "
                    f"(sceneGraphError={errors2}); uploads={item.upload_count}"
                )
            assert (
                item.upload_count > detached_uploads
            ), "reattached item reused a stale node without a fresh upload"
            resettled = item.upload_count
            assert _render_unchanged(
                window2, frames, 2
            ), "unchanged frame never requested"
            assert (
                item.upload_count == resettled
            ), "reattached item re-uploaded without a change"
            assert item.mesh is second
            np.testing.assert_array_equal(item.mesh.positions, source_positions)

            # Clearing after reattach leaves an empty, still-valid item.
            assert item.clear_mesh() is True
            assert item.mesh is EMPTY_MESH
            assert item.generation == 3
            assert _wait_frames(frames, frames.rendered(), 1), "clear never rendered"
            assert item.upload_count == resettled + 1
        finally:
            _release(item, window2, handler2)
    finally:
        _release(item, window, handler)


def test_enc_mesh_item_release_resources_forgets_wrappers(app, monkeypatch):
    """Detaching must run the hook, drop the wrappers and force a fresh upload.

    The spy is installed on the class rather than the instance because Qt calls
    the virtual through the type, not through a per-object attribute.
    """
    frames = _Frames()
    item = EncMeshItem()
    item.setSize(QSizeF(64, 64))
    item.set_projection(_identity)
    item.set_mesh(_mesh([(0, 0), (8, 0), (0, 8)]))

    released = []
    original_release = EncMeshItem.releaseResources

    def tracking_release(self):
        released.append(True)
        original_release(self)

    monkeypatch.setattr(EncMeshItem, "releaseResources", tracking_release)

    window, errors, handler = _attach(item, frames)
    try:
        _skip_if_stalled(frames, 0, errors, "release-resources")
        assert item.upload_count >= 1, "attach never uploaded vertices"
        assert item._node is not None
        assert item._geometry is not None and item._material is not None
        assert item._geometry_dirty is False and item._material_dirty is False
        uploaded = item.upload_count

        _release(item, window, handler)
        deadline = time.monotonic() + TIMEOUT
        while not released and time.monotonic() < deadline:
            QTest.qWait(5)
        assert released, "Qt never called EncMeshItem.releaseResources on detach"
        # The hook drops references only; None is asserted, no wrapper is used.
        assert item._node is None
        assert item._geometry is None and item._material is None
        assert item._geometry_dirty and item._material_dirty

        window2, errors2, handler2 = _attach(item, frames)
        try:
            if not _wait_frames(frames, frames.rendered(), 1):
                pytest.fail(f"reattached item never rendered: {errors2}")
            assert item.upload_count > uploaded, "reattached item did not re-upload"
            assert item._geometry is not None and item._material is not None
        finally:
            _release(item, window2, handler2)
    finally:
        _release(item, window, handler)


def test_enc_mesh_item_colored_reattach(app):
    if not hasattr(QSGGeometry, "defaultAttributes_ColoredPoint2D"):
        pytest.skip("this PySide6 build lacks defaultAttributes_ColoredPoint2D")

    # Vertex-colour geometry and material are Qt-owned; a fresh window must
    # replace them rather than reuse wrappers Qt already deleted.
    frames = _Frames()
    item = EncMeshItem(colored=True)
    item.setSize(QSizeF(64, 64))
    item.set_projection(_identity)
    item.set_mesh(_mesh([(0, 0), (8, 0), (0, 8), (8, 0), (8, 8), (0, 8)]))
    assert item.colored is True and item.upload_count == 0

    window, errors, handler = _attach(item, frames)
    try:
        _skip_if_stalled(frames, 0, errors, "coloured")
        uploaded = item.upload_count
        assert uploaded >= 1
        _release(item, window, handler)
        window2, errors2, handler2 = _attach(item, frames)
        try:
            if not _wait_frames(frames, frames.rendered(), 1):
                pytest.fail(f"reattached coloured item never rendered: {errors2}")
            assert item.upload_count > uploaded
            settled = item.upload_count
            assert _render_unchanged(
                window2, frames, 2
            ), "unchanged frame never requested"
            assert item.upload_count == settled and item.colored is True
        finally:
            _release(item, window2, handler2)
    finally:
        _release(item, window, handler)


def test_enc_mesh_item_renders_view_scene_in_order(app):
    """A two-cell ViewScene uploads one node per cell, in scene order.

    The static path projects once and gives each contributing cell its own
    ``QSGGeometryNode`` under one ``QSGTransformNode``, so the scene's geometry
    is the concatenation of those children: six vertices for A's two triangles
    and three for B's, three triangles in total.

    Expectations are premultiplied: Qt Quick's scene-graph blend mode is
    premultiplied, so the uploader multiplies each vertex's RGB by its alpha
    and leaves alpha alone.  The palette's ``MeshPart.colour`` stays straight,
    so the two conventions differ only for translucent parts.
    """
    if not hasattr(QSGGeometry, "defaultAttributes_ColoredPoint2D"):
        pytest.skip("this PySide6 build lacks defaultAttributes_ColoredPoint2D")

    scene = _two_cell_view_scene()
    expected_positions = scene.positions.astype(np.float32)
    expected_colours = _premultiplied(scene.vertex_colours())
    source_positions = scene.positions.copy()

    frames = _Frames()
    item = EncMeshItem()
    item.setSize(QSizeF(64, 64))
    item.set_projection(_identity)
    assert item.set_view_scene(scene) is True
    assert item.set_view_scene(scene) is False, "identical scene re-marked dirty"
    assert item.view_scene is scene
    assert item.mesh is scene
    assert item.colored is True
    assert item.generation == 1 and item.upload_count == 0

    window, errors, handler = _attach(item, frames)
    try:
        _skip_if_stalled(frames, 0, errors, "view-scene")
        assert item.upload_count >= 1, "view scene was never uploaded"

        # Parts and cells stay exactly as assembled; nothing is sorted.
        assert item.mesh.parts == scene.parts
        assert tuple(cell.path for cell in item.view_scene.cells) == ("A.000", "B.000")
        assert item.generation == 1

        counts, vertices = _uploaded_scene_vertices(item)
        # One node per contributing cell, six vertices for A and three for B.
        # Nine vertices over three triangles is exactly the source scene, so a
        # dropped cell (a node missing from the transform) or a truncated one
        # (a short buffer) fails the count and the position comparison below.
        assert counts == [cell.vertex_count for cell in scene.cells] == [6, 3]
        assert vertices.shape[0] == scene.vertex_count == 9
        assert vertices.shape[0] // 3 == source_positions.shape[0] // 3 == 3
        # Mapped through the item's transform, the buffers reproduce the
        # source positions in scene order.
        positions = _mapped_cell_positions(item, vertices)
        np.testing.assert_allclose(
            positions[:, 0], expected_positions[:, 0], rtol=0, atol=0.01
        )
        np.testing.assert_allclose(
            positions[:, 1], expected_positions[:, 1], rtol=0, atol=0.01
        )
        np.testing.assert_array_equal(vertices["rgba"], expected_colours)
        # Each part's run keeps its own colour, in part order, with no merge.
        # The opaque runs are unchanged by the premultiply; the translucent
        # outline (200, 100, 50, 128) becomes (100, 50, 25, 128), because
        # 200*128//255 == 100, 100*128//255 == 50, 50*128//255 == 25.
        np.testing.assert_array_equal(
            vertices["rgba"][3:6],
            np.tile(np.array([100, 50, 25, 128], dtype=np.uint8), (3, 1)),
        )
        for part in scene.parts:
            start = part.first_vertex
            stop = start + part.vertex_count
            np.testing.assert_array_equal(
                vertices["rgba"][start:stop],
                np.tile(
                    _premultiplied(np.array(part.colour, dtype=np.uint8)),
                    (part.vertex_count, 1),
                ),
            )

        # The float64 source is untouched by the upload.
        assert item.mesh.positions.dtype == np.float64
        np.testing.assert_array_equal(item.mesh.positions, source_positions)

        settled = item.upload_count
        assert _render_unchanged(window, frames, 2), "unchanged frame never requested"
        assert item.upload_count == settled, "unchanged view scene re-uploaded"

        _release(item, window, handler)
        released = item.upload_count

        # The same item must rebuild its node and re-upload in a new window.
        window2, errors2, handler2 = _attach(item, frames)
        try:
            if not _wait_frames(frames, frames.rendered(), 1):
                pytest.fail(f"reattached view scene never rendered: {errors2}")
            assert item.upload_count > released, "reattach reused a stale node"
            counts, vertices = _uploaded_scene_vertices(item)
            assert counts == [6, 3]
            np.testing.assert_array_equal(vertices["rgba"], expected_colours)
            resettled = item.upload_count
            assert _render_unchanged(
                window2, frames, 2
            ), "unchanged frame never requested"
            assert item.upload_count == resettled

            # An assembly whose only cell was oversized leaves empty geometry,
            # never the previous frame's triangles.
            skipped = SkippedCell("huge.000", None, "oversized")
            empty = ViewScene(
                positions=np.empty((0, 2), dtype=np.float64),
                parts=(),
                cells=(skipped,),
                unsupported=(),
                skipped=(skipped,),
                accounted_bytes=0,
            )
            assert item.set_view_scene(empty) is True
            assert _wait_frames(
                frames, frames.rendered(), 1
            ), "empty scene never rendered"
            assert item.generation == 2
            assert item.view_scene is empty and item.mesh.is_empty
            # With no contributing cell there is nothing to allocate, so no
            # upload is counted; the generation bump proves the scene was
            # applied, and the empty node graph proves nothing stale survives.
            assert item._static is None
            assert item._cell_nodes == []
            assert item._geometry is None
            assert item._content_node is not None
            assert item._content_node.childCount() == 0
        finally:
            _release(item, window2, handler2)
    finally:
        _release(item, window, handler)


def test_a_frame_mapped_outside_the_view_is_reported_blank_not_hidden(app):
    """A retained frame that draws nothing says so; it is never hidden.

    A zoom or a long pan can carry the composed frame entirely outside the
    item.  The item keeps the scene, its buffers and its visibility - dropping
    them would turn a stale picture into a blank chart - and reports zero drawn
    vertices plus no viewport coverage, which is the state the viewport uses to
    draw loading cells instead of waiting for the complete scene.
    """
    if not hasattr(QSGGeometry, "defaultAttributes_ColoredPoint2D"):
        pytest.skip("this PySide6 build lacks defaultAttributes_ColoredPoint2D")

    scene = _two_cell_view_scene()
    frames = _Frames()
    item = EncMeshItem()
    item.setSize(QSizeF(64, 64))
    item.set_projection(_identity)
    item.set_view_scene(scene)
    item.setVisible(True)

    window, errors, handler = _attach(item, frames)
    try:
        _skip_if_stalled(frames, 0, errors, "coverage")
        assert item.scene_covers_view() is True
        assert item.shown_vertex_count() == scene.vertex_count

        # The demanded camera moved the frame off the viewport: the matrix is
        # re-solved, every cell is culled to zero vertices, nothing is dropped.
        assert item.set_projection(_offset) is True
        assert _render_unchanged(window, frames, 1), "camera change never rendered"

        assert item.isVisible() is True
        assert item.view_scene is scene
        assert item.upload_count >= 1
        assert item.scene_covers_view() is False
        assert item.shown_vertex_count() == 0
        assert sum(item._cell_vertex_counts) == scene.vertex_count
    finally:
        _release(item, window, handler)
