"""Focused regression for fixed-size ENC point symbols and sounding labels.

The user-approved scope: during a retained-scene zoom the GPU ENC point
symbols keep the screen size they were adopted at, anchored on their
original feature positions, while every other cell content follows the same
affine matrix; and the sounding labels keep rendering from the glyph atlas
at their logical pixel size under the unscaled root -- they never join the
matrix-scaled content node.  The marker patch rewrites the parsed parts'
vertices inside the cell buffers they came from, so per-cell draw order and
culling stay the baked behaviour's.  These checks exercise the template
recovery, the per-cell grouping, the per-frame row derivation and the
item-level zoom wiring directly; the lifecycle tests in :mod:`test_enc_gpu`
cover the rest of the render-thread state machine.
"""

from __future__ import annotations

import ctypes
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

pytest.importorskip("numpy")
pytest.importorskip("PySide6")

from PySide6.QtGui import QGuiApplication, QMatrix4x4  # noqa: E402

from sailworks.qt import encgpu  # noqa: E402
from sailworks.rendering.encview import ViewScene  # noqa: E402
from sailworks.rendering.vectordrawer import encmarkers  # noqa: E402
from sailworks.rendering.vectordrawer.encmesh import (  # noqa: E402
    LINE,
    MARKER,
    MeshPart,
    SceneMesh,
)

#: Isotropic stand-in for the world-pixel projection: 100 px per degree on
#: both axes, so a symbol staged with ``marker_scale`` degrees per template
#: pixel comes out exactly that many reference-frame pixels across, like the
#: real Mercator projection does at the adopting camera.
_PROJECTION_SCALE = 100.0

_ZOOM_IN = (2.0, 0.0, 5.0, 0.0, 2.0, -3.0)
_ZOOM_OUT = (0.5, 0.0, 1.0, 0.0, 0.5, 2.0)
#: Anisotropic case with shear, so the inverse linear part is not a uniform
#: scale and the offset re-lay has to work for real.
_STRETCH = (2.0, 0.25, -1.0, 0.0, 1.0, 0.5)

#: Two beacon feature anchors in source degrees, small enough that the
#: symbol stays inside the test item's rect at the projection above.  The
#: beacon triangle is asymmetric about its anchor, so an anchor recovered as
#: a triangle centroid would sit measurably off.
_ANCHORS = ((0.5, 0.5), (1.5, 0.8))
_MARKER_SCALE = 0.05  # source degrees per template pixel


def _project(points):
    points = np.asarray(points, dtype=np.float64)
    return points * _PROJECTION_SCALE


def _staged(kind, anchors, marker_scale):
    """Vertices exactly as :func:`encmesh.build_scene_mesh` stages them."""
    template = (
        np.asarray(encmarkers.triangles(kind), dtype=np.float64) * marker_scale
    ).reshape(-1, 2)
    return np.concatenate([template + np.asarray(anchor) for anchor in anchors])


def _marker_mesh(kind=encmarkers.BEACON, anchors=_ANCHORS):
    """One cell: two staged markers plus one non-marker triangle part."""
    staged = _staged(kind, anchors, _MARKER_SCALE)
    plain = np.array([[9.0, 9.0], [9.5, 9.0], [9.25, 9.5]])
    positions = np.concatenate((staged, plain))
    return SceneMesh(
        positions=positions,
        parts=(
            MeshPart(
                0,
                "BCNCAR",
                0,
                1,
                MARKER,
                0,
                int(staged.shape[0]),
                (10, 20, 30, 255),
            ),
            MeshPart(
                0,
                "SLCONS",
                1,
                2,
                LINE,
                int(staged.shape[0]),
                int(plain.shape[0]),
                (70, 80, 90, 255),
            ),
        ),
        unsupported=(),
        bounds=None,
    )


def _dot_mesh(anchors=((0.5, 0.5),)):
    """One cell holding generic-dot markers (an unknown point class)."""
    staged = _staged(encmarkers.DOT, anchors, _MARKER_SCALE)
    return SceneMesh(
        positions=staged,
        parts=(
            MeshPart(
                0,
                "SEAARE",
                0,
                1,
                MARKER,
                0,
                int(staged.shape[0]),
                (10, 20, 30, 255),
            ),
        ),
        unsupported=(),
        bounds=None,
    )


def _frame(mesh):
    frame = encgpu.build_static_frame(mesh, _project)
    assert frame is not None
    return frame


def _item_positions(points, coeffs):
    """Map reference-frame local positions through the full affine."""
    a, b, c, d, e, f = coeffs
    x = np.asarray(points)[..., 0].astype(np.float64)
    y = np.asarray(points)[..., 1].astype(np.float64)
    return np.stack((a * x + b * y + c, d * x + e * y + f), axis=-1)


def _marker_entry(mesh):
    frame = _frame(mesh)
    plan = frame.markers
    assert plan is not None
    assert len(plan.entries) == 1
    return frame, plan.entries[0]


def _reference_base(mesh):
    projected = _project(mesh.positions)
    return projected, (projected.min(axis=0) + projected.max(axis=0)) / 2.0


def test_the_plan_recovers_the_exact_feature_anchors_not_centroids():
    mesh = _marker_mesh()
    frame, entry = _marker_entry(mesh)
    # Two beacons, six triangles each (the hollow 1.5 px stroke ring).
    assert entry.count == 36
    assert entry.block == 18
    assert frame.marker_groups == ((0, 0, 0),)
    projected, base = _reference_base(mesh)
    expected = _project(np.asarray(_ANCHORS)) - base
    # The recovered anchors are the staged template origins -- the original
    # feature positions -- projected through the same float64 path.
    assert np.allclose(entry.anchors, expected, atol=1e-9)
    # ...and they are demonstrably not the baked triangles' centroids.
    blocks = projected[: entry.count].reshape(-1, entry.block, 2) - base
    centroids = blocks.mean(axis=1)
    drift = np.hypot(*(entry.anchors - centroids).T)
    assert np.all(drift > 1.0)


def test_a_dot_disc_is_not_mistaken_for_a_scaled_buoy():
    """A DOT disc matches the BUOY fan at half the scale; the class decides."""
    frame, entry = _marker_entry(_dot_mesh())
    assert entry.kind == encmarkers.DOT
    assert entry.block == len(encmarkers.triangles(encmarkers.DOT)) * 3


def test_metres_per_pixel_for_reads_the_camera_view_scale():
    """The gate quantity is m/px, not the 1:N SCAMIN denominator."""
    from sailworks.core.enccharts import display_scale_denominator
    from sailworks.qt.encscenelayer import display_scale_for, metres_per_pixel_for

    class _View:
        def get_scale(self):
            return 2.5

    class _Camera:
        def view(self):
            return _View()

    assert metres_per_pixel_for(_Camera()) == pytest.approx(2.5)
    assert metres_per_pixel_for(_View()) == pytest.approx(2.5)
    assert display_scale_for(_Camera()) == pytest.approx(display_scale_denominator(2.5))


def test_a_foreign_marker_layout_falls_back_to_the_baked_rendering():
    junk = np.arange(24, dtype=np.float64).reshape(12, 2) * 0.01 + 10.0
    mesh = SceneMesh(
        positions=junk,
        parts=(MeshPart(0, "WRECKS", 0, 1, MARKER, 0, 12, (10, 20, 30, 255)),),
        unsupported=(),
        bounds=None,
    )
    frame = _frame(mesh)
    assert frame.markers is None
    assert frame.marker_groups == ()
    # Nothing blanked: the baked soup keeps its colours and vertices.
    rgba = frame.cells[0].vertices["rgba"]
    assert np.all(rgba[:, 3] == 255)
    assert np.array_equal(rgba[0], (10, 20, 30, 255))


def test_the_vertex_bound_falls_back_to_the_previous_behaviour(monkeypatch):
    monkeypatch.setattr(encgpu, "MARKER_MAX_VERTICES", 5)
    frame = _frame(_marker_mesh())
    assert frame.markers is None


def test_identity_camera_reproduces_the_baked_vertices():
    mesh = _marker_mesh()
    frame, entry = _marker_entry(mesh)
    rows = encgpu._marker_rows(entry, None)
    projected, base = _reference_base(mesh)
    local = (projected - base).astype(np.float32)
    assert np.allclose(rows, local[: entry.count], atol=1e-3)


@pytest.mark.parametrize("coeffs", [_ZOOM_IN, _ZOOM_OUT, _STRETCH])
def test_zoom_keeps_the_symbol_on_its_anchor_at_its_adopted_extent(coeffs):
    mesh = _marker_mesh()
    frame, entry = _marker_entry(mesh)
    _, base = _reference_base(mesh)
    lin = (coeffs[0], coeffs[1], coeffs[3], coeffs[4])
    rows = encgpu._marker_rows(entry, lin)
    item = _item_positions(rows, coeffs).reshape(-1, entry.block, 2)
    # The symbol's ground point is the matrix-mapped original feature
    # anchor, and every vertex sits at the adopted reference-frame template
    # offset around it: fixed size, exact position, at every camera.
    true_anchors = _project(np.asarray(_ANCHORS)) - base
    screen_anchors = _item_positions(true_anchors, coeffs)
    expected = screen_anchors[:, None, :] + entry.offsets
    assert np.allclose(item, expected, atol=1e-4)
    # Screen extent per block equals the reference-frame extent.
    reference_extent = float(
        np.abs(entry.offsets.max(axis=1) - entry.offsets.min(axis=1)).max()
    )
    for block in item:
        extent = float(np.abs(block.max(axis=0) - block.min(axis=0)).max())
        assert extent == pytest.approx(reference_extent, rel=1e-3)


def test_a_singular_linear_part_keeps_the_baked_offsets():
    offsets = np.array([[[1.0, 2.0], [3.0, 4.0]]])
    # det = 1*4 - 2*2 = 0: no offset survives the mapping, so the cells'
    # own matrix, whatever it does to the size, keeps the baked values.
    assert np.array_equal(
        encgpu._marker_offsets(offsets, (1.0, 2.0, 2.0, 4.0)), offsets
    )


def test_a_part_split_across_cells_falls_back_to_the_previous_behaviour():
    entry = encgpu._MarkerPart(0, 12, 12, np.zeros((1, 2)), None)
    plan = encgpu._MarkerPlan((entry,))
    # The part's 12 vertices straddle the first cell's 6: patching per cell
    # would draw the symbol's halves at different times, so the plan is
    # rejected and the frame keeps the baked rendering whole.
    assert encgpu._marker_groups(plan, [(0, 6), (6, 6)]) is None


def test_parts_group_into_their_own_cells():
    first = encgpu._MarkerPart(0, 12, 12, np.zeros((1, 2)), None)
    second = encgpu._MarkerPart(12, 12, 12, np.zeros((1, 2)), None)
    plan = encgpu._MarkerPlan((first, second))
    groups = encgpu._marker_groups(plan, [(0, 12), (12, 12)])
    assert groups == ((0, 0, 0), (1, 12, 1))


def _read_cell(node) -> np.ndarray:
    geometry = node.geometry()
    count = geometry.vertexCount()
    vertices = np.empty(count, dtype=encgpu.VERTEX_DTYPE)
    if count:
        data = geometry.vertexData()
        address = data if isinstance(data, int) else int(data)
        ctypes.memmove(vertices.ctypes.data, address, vertices.nbytes)
    return vertices


def _read_text(geometry) -> np.ndarray:
    count = geometry.vertexCount()
    vertices = np.empty(count, dtype=encgpu.TEXT_VERTEX_DTYPE)
    if count:
        data = geometry.vertexData()
        address = data if isinstance(data, int) else int(data)
        ctypes.memmove(vertices.ctypes.data, address, vertices.nbytes)
    return vertices


@pytest.fixture(scope="module")
def qt_app():
    app = QGuiApplication.instance()
    if app is None:
        try:
            app = QGuiApplication([])
        except Exception as exc:  # pragma: no cover - headless without a platform
            pytest.skip(f"Qt platform unavailable: {exc}")
    return app


def _view_scene(mesh):
    return ViewScene(
        positions=mesh.positions,
        parts=mesh.parts,
        cells=(),
        unsupported=(),
        skipped=(),
        soundings=np.empty((0, 3), dtype=np.float64),
    )


def _item(factory, width=220.0, height=160.0):
    item = encgpu.EncMeshItem(colored=True, texture_factory=factory)
    item.setWidth(width)
    item.setHeight(height)
    return item


def test_a_retained_zoom_rewrites_the_marker_vertices_in_their_cells(qt_app):
    calls = []

    def factory(image):
        calls.append(image.size())
        return None

    mesh = _marker_mesh()
    item = _item(factory)
    item.set_view_scene(_view_scene(mesh))
    node = item.updatePaintNode(None, None)

    frame = item._static
    assert frame.markers is not None
    entry = frame.markers.entries[0]
    baked = _read_cell(item._cell_nodes[0])

    item.set_projection(lambda source: source * 0.5)
    assert item.updatePaintNode(node, None) is node

    patched = _read_cell(item._cell_nodes[0])
    lin = (0.5, 0.0, 0.0, 0.5)
    expected = encgpu._marker_rows(entry, lin)
    assert np.allclose(
        np.stack((patched["x"], patched["y"]), axis=-1)[: entry.count],
        expected,
        atol=1e-3,
    )
    # Only the marker runs moved: the colours and the non-marker triangle
    # are exactly as uploaded.
    assert np.array_equal(patched["rgba"], baked["rgba"])
    assert np.array_equal(
        np.stack((patched["x"], patched["y"]), axis=-1)[entry.count :],
        np.stack((baked["x"], baked["y"]), axis=-1)[entry.count :],
    )


def test_an_initial_scale_gate_collapses_dots_on_the_first_frame(qt_app):
    """``set_scale`` before the scene is adopted must reach the first paint."""
    item = _item(lambda image: None)
    item.set_scale(encmarkers.DOT_MAX_SCALE + 1.0)
    item.set_view_scene(_view_scene(_dot_mesh()))
    item.updatePaintNode(None, None)

    entry = item._static.markers.entries[0]
    assert entry.kind == encmarkers.DOT
    vertices = _read_cell(item._cell_nodes[0])
    positions = np.stack((vertices["x"], vertices["y"]), axis=-1)[: entry.count]
    assert np.allclose(positions, encgpu._hidden_marker_rows(entry), atol=1e-3)


def test_releasing_the_scale_gate_restores_the_dot_vertices(qt_app):
    item = _item(lambda image: None)
    item.set_scale(encmarkers.DOT_MAX_SCALE + 1.0)
    item.set_view_scene(_view_scene(_dot_mesh()))
    node = item.updatePaintNode(None, None)

    entry = item._static.markers.entries[0]
    collapsed = _read_cell(item._cell_nodes[0])[: entry.count]
    assert np.allclose(
        np.stack((collapsed["x"], collapsed["y"]), axis=-1),
        encgpu._hidden_marker_rows(entry),
        atol=1e-3,
    )

    # 100 m/px is the gate's inclusive visible side; the retained offsets are
    # rewritten back from the parsed block, not reconstructed from the anchors.
    item.set_scale(encmarkers.DOT_MAX_SCALE)
    assert item.updatePaintNode(node, None) is node
    restored = _read_cell(item._cell_nodes[0])[: entry.count]
    assert np.allclose(
        np.stack((restored["x"], restored["y"]), axis=-1),
        encgpu._marker_rows(entry, None),
        atol=1e-3,
    )


def test_a_retained_restore_reapplies_the_scale_gate(qt_app):
    """Returning to a parked frame reconciles its buffers with the gate."""
    first = _view_scene(_dot_mesh(anchors=((0.5, 0.5),)))
    second = _view_scene(_dot_mesh(anchors=((1.5, 0.8),)))

    item = _item(lambda image: None)
    item.set_scale(encmarkers.DOT_MAX_SCALE)
    item.set_view_scene(first)
    node = item.updatePaintNode(None, None)

    # Adopt another scene while the gate hides dots: the first frame's nodes
    # are parked with their (still visible) buffers.
    item.set_scale(encmarkers.DOT_MAX_SCALE + 1.0)
    item.set_view_scene(second)
    assert item.updatePaintNode(node, None) is node

    # Return to the retained scene.  Its nodes come back without a rebuild,
    # so the collapse has to be re-applied from the current gate.
    item.set_view_scene(first)
    assert item.updatePaintNode(node, None) is node
    assert item._static.markers is not None
    entry = item._static.markers.entries[0]
    assert entry.kind == encmarkers.DOT
    vertices = _read_cell(item._cell_nodes[0])[: entry.count]
    assert np.allclose(
        np.stack((vertices["x"], vertices["y"]), axis=-1),
        encgpu._hidden_marker_rows(entry),
        atol=1e-3,
    )


def test_sounding_labels_stay_on_the_unscaled_root_at_logical_size(qt_app):
    calls = []

    def factory(image):
        calls.append(image.size())
        return None

    item = _item(factory)
    item.set_scale(50.0)
    item.set_view_scene(
        ViewScene(
            positions=np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]]),
            parts=(),
            cells=(),
            unsupported=(),
            skipped=(),
            soundings=np.array([[10.0, 10.0, 3.5]]),
        )
    )
    node = item.updatePaintNode(None, None)
    before = _read_text(item._text_geometry)

    # A retained zoom: the affine matrix scales the chart's content node,
    # the labels are recomputed under the new camera -- and stay on the
    # unscaled root at the atlas's logical glyph size.
    item.set_projection(lambda source: source * 0.5)
    assert item.updatePaintNode(node, None) is node

    after = _read_text(item._text_geometry)
    # The label node is still the root's child, a sibling of the
    # matrix-scaled content node: labels never join the scaled chart.
    assert node.childCount() == 2
    assert node.lastChild() is item.text_node
    assert item.text_node is not item._content_node
    # The chart really did zoom: the content node carries a non-identity
    # matrix, while every glyph quad keeps the atlas's logical cell height.
    assert item._content_node.matrix() != QMatrix4x4()
    assert item.atlas.pixel_size == 11.0
    for vertices in (before, after):
        spans = vertices["y"].reshape(-1, 6)
        assert np.allclose(
            spans.max(axis=1) - spans.min(axis=1), item.atlas.cell_height
        )
