"""Static-frame matrix path: exact parity with the CPU projection.

The item's vertices are projected once into a reference screen frame and a
``QMatrix4x4`` moves them each frame.  These tests assert that the matrix path
reproduces :func:`encgpu._project_positions` (the legacy CPU path) to well under
``0.01`` item pixels, including after a pan, after a zoom step, with a
non-default device pixel ratio and with a non-zero origin, and that a pan does
not re-upload a single vertex.
"""

from __future__ import annotations

import json
import math
from types import MethodType, SimpleNamespace

import numpy as np
import pytest

from PySide6.QtWidgets import QApplication

from sailworks.qt import enc_trace
from sailworks.qt import encgpu
from sailworks.qt import viewporttransform as _vt
from sailworks.qt.gribviewport import (
    _viewport_widget_class,
    enc_scene_covers_view,
    scene_presents_chart,
)

from sailworks.qt.viewporttransform import (
    ENC_COMPOSE_TRIGGER,
    PanReference,
    enc_compose_margin,
    enc_scene_offset,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Camera:
    """The camera surface ``ChartCamera`` reads."""

    def __init__(self, lon=5.5, lat=48.1125, zoom=14.0, width=710, height=743):
        self.center_lon = lon
        self.center_lat = lat
        self.zoom = zoom
        self.width = width
        self.height = height

    def stamp(self):
        return 1.0


class _Span:
    """The bits of ``encview.CellSpan`` the static frame reads."""

    def __init__(self, first_vertex, vertex_count):
        self.first_vertex = first_vertex
        self.vertex_count = vertex_count


class _Scene:
    """A minimal multi-cell scene: positions, no parts, explicit cell spans."""

    def __init__(self, positions, cells):
        self.positions = np.ascontiguousarray(positions, dtype=np.float64)
        self.parts = ()
        self.cells = tuple(cells)

    @property
    def vertex_count(self):
        return int(self.positions.shape[0])


def _projection(camera, dpr=1):
    """The projection the scene layer publishes, at a given device ratio."""
    from sailworks.core.mapviewport import MapViewport
    from sailworks.qt.encscenelayer import projection_for
    from sailworks.qt.gribviewportchart import ChartCamera

    return projection_for(MapViewport(ChartCamera(camera, dpr), 0, lambda: None))


def _visible_degrees(camera, span=0.75):
    """Scene points spanning the visible area (and a little past its edges)."""
    rows = []
    columns = 41
    for i in range(columns):
        fx = -0.1 + 1.2 * i / (columns - 1)
        for j in range(columns):
            fy = -0.1 + 1.2 * j / (columns - 1)
            rows.append(
                (
                    camera.center_lon + span * fx,
                    camera.center_lat + span * fy * camera.height / camera.width,
                )
            )
    points = np.asarray(rows, dtype=np.float64)
    return np.repeat(points, 3, axis=0)  # a triangle soup, three per triangle


def _split_cells(positions, cells=3):
    step = positions.shape[0] // cells
    spans = []
    first = 0
    for index in range(cells):
        length = step if index < cells - 1 else positions.shape[0] - first
        spans.append(_Span(first, length))
        first += length
    return spans


def _matrix_positions(item):
    """Map every static vertex through the item's current matrix.

    Returns ``(N, 2)`` float64 item-pixel positions in scene order, which is
    the same order :func:`encgpu._project_positions` returns.
    """
    matrix = item._content_node.matrix()
    rows = []
    for cell, node in zip(item._static.cells, item._cell_nodes):
        vertices = cell.vertices
        if not vertices.shape[0]:
            continue
        local = np.column_stack(
            (
                vertices["x"].astype(np.float64),
                vertices["y"].astype(np.float64),
                np.zeros(vertices.shape[0]),
                np.ones(vertices.shape[0]),
            )
        )
        mapped = (
            local
            @ np.asarray(
                [[matrix[i, j] for j in range(4)] for i in range(4)], dtype=np.float64
            ).T
        )
        rows.append(mapped[:, :2])
    if not rows:
        return np.empty((0, 2), dtype=np.float64)
    return np.vstack(rows)


@pytest.fixture(autouse=True)
def _the_layer_owns_its_pan(monkeypatch):
    """Measure the layer's own pan placement, not the container experiment.

    ``CONTAINER_CARRIES_PAN`` is covered in ``test/test_gribviewport.py``;
    these checks model the placement it replaces.
    """
    from sailworks.qt import gribviewport as _gv

    monkeypatch.setattr(_gv, "CONTAINER_CARRIES_PAN", False)


def _presented(app, camera, scene, projection, origin=None, legacy=None, margin=None):
    kwargs = {} if legacy is None else {"legacy_project": legacy}
    item = encgpu.EncMeshItem(text_enabled=False, **kwargs)
    item.setWidth(camera.width)
    item.setHeight(camera.height)
    if origin is not None:
        item.set_origin(origin)
    if margin is not None:
        item.set_compose_margin(margin)
    item.set_projection(projection)
    item.set_view_scene(scene)
    node = item.updatePaintNode(None, None)
    return item, node


def assert_parity(item, scene, projection, origin=None, bound=0.01):
    """Assert the matrix path matches the float64 CPU projection."""
    assert item.static_mode is True
    legacy = encgpu._project_positions(scene, projection, origin)
    matrix = _matrix_positions(item)
    assert matrix.shape == legacy.shape
    deviation = float(np.abs(matrix - legacy).max())
    assert deviation < bound, f"max deviation {deviation:.6f} px"
    return deviation


# --------------------------------------------------------------------- parity
def test_matrix_matches_cpu_projection_after_a_pan(app):
    camera = _Camera()
    scene = _Scene(_visible_degrees(camera), _split_cells(_visible_degrees(camera)))
    projection = _projection(camera)
    item, node = _presented(app, camera, scene, projection)
    assert_parity(item, scene, projection)

    moved = _Camera(lon=camera.center_lon + 0.01, lat=camera.center_lat - 0.004)
    panned = _projection(moved)
    item.set_projection(panned)
    assert item.updatePaintNode(node, None) is node
    deviation = assert_parity(item, scene, panned)
    assert deviation < 0.01
    # The vertices were not touched: one upload for the whole presentation.
    assert item.upload_count == 1
    assert item.matrix_update_count >= 2


def test_matrix_matches_cpu_projection_after_a_zoom(app):
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    item, node = _presented(app, camera, scene, _projection(camera))

    for zoom in (14.5, 15.0, 14.25):
        zoomed = _Camera(zoom=zoom)
        projection = _projection(zoomed)
        item.set_projection(projection)
        assert item.updatePaintNode(node, None) is node
        assert_parity(item, scene, projection)
    assert item.upload_count == 1


def test_matrix_matches_cpu_projection_at_scale_two(app):
    """A hidpi canvas: the projection emits device pixels, both paths agree."""
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    projection = _projection(camera, dpr=2)
    item, node = _presented(app, camera, scene, projection)
    assert_parity(item, scene, projection)

    moved = _Camera(lon=camera.center_lon - 0.002, lat=camera.center_lat + 0.003)
    panned = _projection(moved, dpr=2)
    item.set_projection(panned)
    item.updatePaintNode(node, None)
    assert_parity(item, scene, panned)


def test_matrix_matches_cpu_projection_with_a_nonzero_origin(app):
    camera = _Camera()
    positions = _visible_degrees(camera)
    origin = (camera.center_lon, camera.center_lat)
    # With an origin the projection sees origin-relative degrees, so a plain
    # viewport closure is not the production case; shift the input back so the
    # comparison still runs against a real camera projection.
    base = _projection(camera)
    projection = lambda points: base(np.asarray(points, dtype=np.float64) + origin)

    scene = _Scene(positions, _split_cells(positions))
    item, node = _presented(app, camera, scene, projection, origin=origin)
    assert_parity(item, scene, projection, origin=origin)

    moved = _Camera(lat=camera.center_lat + 0.002)
    moved_projection = lambda points: _projection(moved)(
        np.asarray(points, dtype=np.float64) + origin
    )
    item.set_projection(moved_projection)
    item.updatePaintNode(node, None)
    assert_parity(item, scene, moved_projection, origin=origin)


def test_cells_outside_the_item_are_culled(app):
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    item, node = _presented(app, camera, scene, _projection(camera))
    # The scene spans well past the item, so some cells are already off it.
    assert item.visible_cells and any(item.visible_cells)
    children = list(item._cell_nodes)
    attached = item._content_node.childCount()

    far = _Camera(lon=camera.center_lon + 5.0)
    item.set_projection(_projection(far))
    item.updatePaintNode(node, None)
    assert item.visible_cells and not any(item.visible_cells)
    # Culling changes draw counts only.  The child list is never rewritten, so
    # a grab that renders the recorded tree cannot race a detach.
    assert item._cell_nodes == children
    assert item._content_node.childCount() == attached == len(children)
    for cell_node in children:
        assert cell_node.geometry().vertexCount() == 0
        assert cell_node.parent() == item._content_node

    item.set_projection(_projection(camera))
    item.updatePaintNode(node, None)
    assert item.visible_cells and any(item.visible_cells)
    assert item._cell_nodes == children
    for cell_node, count, visible in zip(
        children, item._cell_vertex_counts, item._cell_visible
    ):
        assert cell_node.geometry().vertexCount() == (count if visible else 0)


class _SpyGeometry:
    """Records the vertex-count writes a culled node receives."""

    def __init__(self, count):
        self._count = int(count)
        self.changes = []

    def vertexCount(self):
        return self._count

    def setVertexCount(self, count):
        self.changes.append(int(count))
        self._count = int(count)


class _SpyNode:
    """Stands in for a cell node; records the dirty bits it is sent."""

    def __init__(self, count):
        self._geometry = _SpyGeometry(count)
        self.dirty = []

    def geometry(self):
        return self._geometry

    def markDirty(self, bits):
        self.dirty.append(bits)


def test_culling_marks_a_changed_cell_geometry_dirty(app):
    """A changed vertex count must dirty the node for the renderer to see it.

    ``setVertexCount`` only writes the Python-side value; QSGBatchRenderer
    re-reads a geometry node's element count only when the node is dirty.  A
    cell culled at the item edge therefore stayed blank when the pan returned
    — the blinking-tile report — and an unchanged count must stay clean.
    """
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    item, node = _presented(app, camera, scene, _projection(camera))
    counts = list(item._cell_vertex_counts)
    visible0 = item._cell_visible
    assert counts and any(item.visible_cells)
    spies = [
        _SpyNode(count if visible else 0)
        for count, visible in zip(counts, item._cell_visible)
    ]
    # Only the Python-side list is swapped: the real nodes stay attached to
    # the content node, and the mask length still matches the frame's cells.
    item._cell_nodes = spies
    dirty_bit = encgpu.QSGNode.DirtyStateBit.DirtyGeometry

    far = _Camera(lon=camera.center_lon + 5.0)
    item.set_projection(_projection(far))
    item.updatePaintNode(node, None)
    assert item.visible_cells and not any(item.visible_cells)
    changed = 0
    for spy, count, was_visible in zip(spies, counts, visible0):
        if was_visible and count:
            # Culled now: the count change is written and the node is dirtied.
            assert spy.geometry().changes == [0]
            assert spy.dirty == [dirty_bit]
            changed += 1
        else:
            # Already drew nothing: no write and, crucially, no dirty.
            assert spy.geometry().changes == []
            assert spy.dirty == []
    assert changed

    item.set_projection(_projection(camera))
    item.updatePaintNode(node, None)
    assert item.visible_cells and any(item.visible_cells)
    for spy, count, visible in zip(spies, counts, item._cell_visible):
        applied = count if visible else 0
        # Every write is paired with exactly one dirty, and the spy's count came
        # back to what the mask says should be drawn.
        assert len(spy.dirty) == len(spy.geometry().changes)
        assert all(bits == dirty_bit for bits in spy.dirty)
        assert spy.geometry().vertexCount() == applied

    # An unchanged mask is the steady state: no write, no dirty.
    before = [(list(spy.geometry().changes), list(spy.dirty)) for spy in spies]
    item.updatePaintNode(node, None)
    assert [(list(spy.geometry().changes), list(spy.dirty)) for spy in spies] == before


def test_culling_restores_a_geometry_zeroed_under_an_unchanged_mask(app):
    """A cell zeroed while the mask is unchanged must be drawn again.

    The cull pass used to return early when the freshly computed mask equalled
    the cached one, so a cell whose geometry had been zeroed (by a stale node,
    a re-used scene graph, or anything else holding the buffer) stayed blank
    with no camera change: the "chart rectangles disappear and reappear"
    report.  The mask is a diagnostic now, not proof the geometries are right.
    """
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    item, _ = _presented(app, camera, scene, _projection(camera))
    counts = list(item._cell_vertex_counts)
    visible = tuple(item._cell_visible)
    assert counts and any(visible)

    # Same camera, same mask — but every geometry now reports zero vertices.
    spies = [_SpyNode(0) for _ in counts]
    item._cell_nodes = spies
    coeffs = item._frame_coeffs()
    assert coeffs is not None
    item._cull_cells(coeffs)

    dirty_bit = encgpu.QSGNode.DirtyStateBit.DirtyGeometry
    for spy, count, is_visible in zip(spies, counts, visible):
        applied = count if is_visible else 0
        assert spy.geometry().vertexCount() == applied
        if applied:
            # The lost geometry is rewritten and the node is dirtied so the
            # renderer re-reads the element count.
            assert spy.geometry().changes == [applied]
            assert spy.dirty == [dirty_bit]
        else:
            # Already drew nothing, so there is still nothing to write.
            assert spy.geometry().changes == []
            assert spy.dirty == []


def test_culling_is_write_free_when_counts_already_match(app):
    """Reconciling every frame must not turn the steady state into a write."""
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    item, _ = _presented(app, camera, scene, _projection(camera))
    counts = list(item._cell_vertex_counts)
    spies = [
        _SpyNode(count if is_visible else 0)
        for count, is_visible in zip(counts, item._cell_visible)
    ]
    item._cell_nodes = spies
    coeffs = item._frame_coeffs()
    assert coeffs is not None

    item._cull_cells(coeffs)

    for spy in spies:
        assert spy.geometry().changes == []
        assert spy.dirty == []


# ---------------------------------------------------------------- cull tracing
def _trace_records(path):
    """Every record a trace file holds; an absent file holds none."""
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def trace_path(tmp_path, monkeypatch):
    """Point the opt-in trace at a file and forget the cached environment."""
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(path))
    enc_trace.reset()
    try:
        yield path
    finally:
        enc_trace.reset()


def test_tracing_off_changes_no_draw_count_and_writes_nothing(
    app, tmp_path, monkeypatch
):
    """With ``SAILWORKS_ENC_TRACE`` unset the cull and margin paths stay silent."""
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    far = _Camera(lon=camera.center_lon + 5.0)

    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(path))
    enc_trace.reset()
    try:
        traced, traced_node = _presented(app, camera, scene, _projection(camera))
        traced.set_compose_margin(17.0)
        traced.set_projection(_projection(far))
        traced.updatePaintNode(traced_node, None)
        on_counts = [node.geometry().vertexCount() for node in traced._cell_nodes]
        records = _trace_records(path)
        assert records
        margins = [row for row in records if row["event"] == "enc_compose_margin"]
        assert len(margins) == 1
        assert margins[0]["old"] == encgpu.SCENE_MARGIN
        assert margins[0]["new"] == 17.0
    finally:
        enc_trace.reset()
    written = path.read_text(encoding="utf-8")

    monkeypatch.delenv(enc_trace.ENV_PATH, raising=False)
    enc_trace.reset()
    try:
        assert enc_trace.enabled() is False
        plain, plain_node = _presented(app, camera, scene, _projection(camera))
        plain.set_compose_margin(17.0)
        plain.set_projection(_projection(far))
        plain.updatePaintNode(plain_node, None)
        off_counts = [node.geometry().vertexCount() for node in plain._cell_nodes]
    finally:
        enc_trace.reset()
    assert off_counts == on_counts
    assert path.read_text(encoding="utf-8") == written


def test_a_cull_emits_one_capped_record_with_the_clip_and_margin(app, trace_path):
    """One record per cull pass, bounded per cell, carrying the clip in force."""
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions, cells=12))
    margin = 17.0
    item, node = _presented(app, camera, scene, _projection(camera), margin=margin)
    before = len(
        [row for row in _trace_records(trace_path) if row["event"] == "enc_cull"]
    )

    far = _Camera(lon=camera.center_lon + 5.0)
    item.set_projection(_projection(far))
    item.updatePaintNode(node, None)
    assert item.visible_cells and not any(item.visible_cells)

    records = [row for row in _trace_records(trace_path) if row["event"] == "enc_cull"]
    assert len(records) == before + 1
    record = records[-1]
    pad = encgpu.CLIP_PAD
    assert record["clip"] == pytest.approx(
        [
            -(margin + pad),
            -(margin + pad),
            camera.width + margin + pad,
            camera.height + margin + pad,
        ]
    )
    assert record["viewport"] == pytest.approx(
        [float(camera.width), float(camera.height)]
    )
    assert record["margin"] == margin
    assert record["culled"] == len(scene.cells)
    assert record["cells"] == len(scene.cells)
    assert record["capped"] is True
    assert len(record["detail"]) == encgpu.TRACE_CULL_LIMIT
    assert isinstance(record["frame"], int)
    for row in record["detail"]:
        assert 0 <= row["cell"] < len(scene.cells)
        assert len(row["bounds"]) == 4
        assert len(row["bbox"]) == 4
        assert row["gap"] >= 0.0
        assert isinstance(row["touches"], bool)


def test_a_restored_zero_count_emits_its_record(app, trace_path):
    """A geometry zeroed under an unchanged mask reports the restore it made."""
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    item, _ = _presented(app, camera, scene, _projection(camera))
    counts = list(item._cell_vertex_counts)
    visible = tuple(item._cell_visible)
    assert counts and any(visible)

    spies = [_SpyNode(0) for _ in counts]
    item._cell_nodes = spies
    coeffs = item._frame_coeffs()
    assert coeffs is not None
    item._cull_cells(coeffs)

    records = [
        row
        for row in _trace_records(trace_path)
        if row["event"] == "enc_zero_count" and row["frame"] == item._trace_frame
    ]
    restored = [index for index, is_visible in enumerate(visible) if is_visible]
    assert len(records) == len(restored)
    for record, index in zip(records, restored):
        assert record["cell"] == index
        assert record["previous"] == 0
        assert record["current"] == counts[index]
        assert record["count"] == counts[index]
        assert record["mask"] is True
        assert spies[index].geometry().vertexCount() == counts[index]


def test_forgetting_the_scene_graph_drops_the_cell_bookkeeping(app):
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    item, _ = _presented(app, camera, scene, _projection(camera))
    assert item._cell_nodes and item._cell_vertex_counts and item._cell_visible

    item._forget_scene_graph()
    assert item._cell_nodes == []
    assert item._cell_vertex_counts == []
    assert item._cell_visible == ()

    # With no content node the upload returns early, and the stale lists are
    # still dropped so they can never be zipped against a later build.
    item._cell_vertex_counts = [6, 6]
    item._cell_visible = (True, False)
    item._upload_cells()
    assert item._cell_vertex_counts == []
    assert item._cell_visible == ()


def test_legacy_flag_reprojects_instead_of_moving_a_matrix(app):
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    projection = _projection(camera)
    item, node = _presented(app, camera, scene, projection, legacy=True)
    assert item.static_mode is False
    assert item.upload_count == 1

    moved = _Camera(lon=camera.center_lon + 0.01)
    item.set_projection(_projection(moved))
    item.updatePaintNode(node, None)
    # The legacy path re-uploads for the new projection, as it always did.
    assert item.upload_count == 2
    assert item.matrix_update_count == 0


def test_legacy_and_matrix_paths_agree_on_the_buffer(app):
    """Both paths draw the same triangles: the matrix path is a relocation."""
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    projection = _projection(camera)

    legacy_item, _ = _presented(app, camera, scene, projection, legacy=True)
    static_item, _ = _presented(app, camera, scene, projection)
    legacy = encgpu._project_positions(scene, projection, None)
    matrix = _matrix_positions(static_item)
    assert float(np.abs(matrix - legacy).max()) < 0.01
    # The legacy path clips to the item at upload, so it drops the vertices
    # this scene puts off the item.  The static frame keeps every vertex (it has
    # to: a pan brings them back without re-projecting) and drops whole cells by
    # bbox instead.  Same picture, different bookkeeping.
    assert legacy_item._geometry.vertexCount() < legacy.shape[0]
    # Resident buffers, not live draw counts: a culled cell keeps its vertices
    # and draws zero of them.
    uploaded = sum(static_item._cell_vertex_counts)
    assert uploaded == sum(cell.vertices.shape[0] for cell in static_item._static.cells)


# ----------------------------------------------------------- absolute placement
# The parity tests above drive the item and the expected projection with the
# same closure, so they agree even when both are wrong together.  The tests
# below derive the expected screen position from the *camera* alone and add
# the transform chain the production viewport actually applies: the item's
# matrix plus the pan translation `mapContent` carries.  A camera-absolute
# scene must not move twice for one pan.


def _camera_panned(reference, dx, dy):
    """A camera whose ``mapContent`` translation is exactly ``(dx, dy)``.

    ``translation_to`` is world pixels of the reference minus those of the
    camera, so invert it: the desired translation picks the camera.
    """
    world = _vt.WORLD_PIXELS * 2.0**reference.zoom
    lon = reference.center_lon - dx * 360.0 / world
    y = _vt._world_y(reference.center_lat, reference.zoom) - dy
    mercator = (1.0 - 2.0 * y / world) * math.pi
    lat = math.degrees(2.0 * math.atan(math.exp(mercator)) - math.pi / 2.0)
    return _Camera(
        lon=lon,
        lat=lat,
        zoom=reference.zoom,
        width=reference.width,
        height=reference.height,
    )


def _pan_offset(reference, camera):
    """The ``mapContent`` x/y the viewport sets when the camera moves."""
    return PanReference.from_camera(reference, 1).translation_to(camera, 1)


def _publish(item, node, camera, positions, map_offset):
    """Present a ready scene for ``camera`` as ``_enc_scene_published`` does.

    ``map_offset`` is the translation ``mapContent`` carries at publication;
    the item offsets itself by exactly that much.
    """
    item.set_projection(_projection(camera))
    item.set_view_scene(_Scene(positions, _split_cells(positions)))
    item.updatePaintNode(node, None)
    return enc_scene_offset(map_offset)


def _drawn(item, item_offset, map_offset):
    """Screen position of the first vertex through matrix + parent pan."""
    return _matrix_positions(item)[0] + np.asarray(map_offset) + np.asarray(item_offset)


def _expected(camera, anchor):
    return np.asarray(_projection(camera)(np.asarray([anchor], dtype=np.float64)))[0]


@pytest.mark.parametrize("dx,dy", [(100.0, -60.0), (-37.0, 21.0), (0.0, 140.0)])
def test_pan_law_moves_a_fixed_point_once(app, dx, dy):
    """One pan moves a fixed geographic point by exactly the pan, never twice."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    scene = _Scene(positions, _split_cells(positions))
    item, node = _presented(app, reference, scene, _projection(reference))
    anchor = positions[0]
    before = _drawn(item, (0.0, 0.0), (0.0, 0.0))

    moved = _camera_panned(reference, dx, dy)
    map_offset = _pan_offset(reference, moved)
    assert np.allclose(map_offset, (dx, dy), atol=1e-6)
    item_offset = enc_scene_offset(map_offset)
    item.set_projection(_projection(moved))
    item.set_view_scene(_Scene(positions, _split_cells(positions)))
    item.updatePaintNode(node, None)

    after = _drawn(item, item_offset, map_offset)
    movement = after - before
    error = float(np.abs(movement - np.asarray(map_offset)).max())
    assert error < 1.0, (
        f"pan law broken for d=({dx}, {dy}): drawn movement {movement}, "
        f"expected {map_offset}"
    )
    # The chart itself is also back on its cells, not merely panned once.
    assert float(np.abs(after - _expected(moved, anchor)).max()) < 1.0


def test_absolute_placement_after_each_of_several_pans(app):
    """Repeated pans re-publish; the chart must never accumulate an offset."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )
    for dx, dy in ((30.0, -20.0), (80.0, -50.0), (-120.0, 90.0), (250.0, 5.0)):
        moved = _camera_panned(reference, dx, dy)
        map_offset = _pan_offset(reference, moved)
        item_offset = _publish(item, node, moved, positions, map_offset)
        drawn = _drawn(item, item_offset, map_offset)
        error = float(np.abs(drawn - _expected(moved, anchor)).max())
        assert error < 1.0, f"pan ({dx}, {dy}) left the chart {error:.3f} px off"


def test_absolute_placement_pan_after_zoom(app):
    """A zoom rebases the reference; the pan after it is still cancelled."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )

    zoomed = _Camera(zoom=14.6)
    # A zoom is a reference invalidation: mapContent is placed at zero offset.
    item_offset = _publish(item, node, zoomed, positions, (0.0, 0.0))
    drawn = _drawn(item, item_offset, (0.0, 0.0))
    assert float(np.abs(drawn - _expected(zoomed, anchor)).max()) < 1.0

    panned = _camera_panned(zoomed, -75.0, 40.0)
    map_offset = _pan_offset(zoomed, panned)
    item_offset = _publish(item, node, panned, positions, map_offset)
    drawn = _drawn(item, item_offset, map_offset)
    assert float(np.abs(drawn - _expected(panned, anchor)).max()) < 1.0


def test_absolute_placement_pan_then_zoom(app):
    """A pan followed by a zoom: the rebase clears the pan it accumulated."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )
    panned = _camera_panned(reference, 60.0, 45.0)
    map_offset = _pan_offset(reference, panned)
    item_offset = _publish(item, node, panned, positions, map_offset)
    assert (
        float(
            np.abs(
                _drawn(item, item_offset, map_offset) - _expected(panned, anchor)
            ).max()
        )
        < 1.0
    )

    zoomed = _Camera(lon=panned.center_lon, lat=panned.center_lat, zoom=14.4)
    item_offset = _publish(item, node, zoomed, positions, (0.0, 0.0))
    drawn = _drawn(item, item_offset, (0.0, 0.0))
    assert float(np.abs(drawn - _expected(zoomed, anchor)).max()) < 1.0


def test_absolute_placement_after_a_zoom_sequence(app):
    """Several zooms that settle leave no accumulated translation error."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )
    for zoom in (14.25, 14.5, 14.75, 15.0, 15.25):
        camera = _Camera(zoom=zoom)
        item_offset = _publish(item, node, camera, positions, (0.0, 0.0))
        drawn = _drawn(item, item_offset, (0.0, 0.0))
        error = float(np.abs(drawn - _expected(camera, anchor)).max())
        assert error < 1.0, f"zoom {zoom} settled {error:.3f} px off"


def test_enc_scene_offset_cancels_the_published_pan():
    assert enc_scene_offset((116.5, -69.8)) == (-116.5, 69.8)
    assert enc_scene_offset((0.0, 0.0)) == (0.0, 0.0)


# ------------------------------------------------ settling without a re-publish
# A settled view is the state the user measures: the camera stops moving and no
# replacement scene arrives.  These drive the production placement method on a
# headless double, so the law under test is the viewport's, not a copy of it.


def _placement(item, camera, reference, published, request=None, generation=None):
    """The production ``_place_enc_scene`` bound to a headless double."""
    widget = SimpleNamespace(
        _enc_scene_item=item,
        _enc_scene_camera=published,
        _enc_scene_frame_camera=published,
        _map_pan_reference=reference,
        _camera=camera,
        _camera_dpr=1.0,
    )
    if request is not None:
        widget._request_enc_scene = request
        widget._enc_scene_generation = generation
        widget._mark_enc_scene_updating = lambda: None
    widget._place_enc_scene = MethodType(
        _viewport_widget_class()._place_enc_scene, widget
    )
    return widget


def _settle(widget, item, node, camera, reference):
    """Stop the camera at ``camera`` and present one frame without new data."""
    widget._camera = camera
    widget._map_pan_reference = reference
    uploads = item.upload_count
    fallbacks = item.fallback_activations
    widget._place_enc_scene()
    item.updatePaintNode(node, None)
    return (
        (float(item.x()), float(item.y())),
        item.upload_count - uploads,
        item.fallback_activations - fallbacks,
    )


def _settle_error(item, anchor, camera, offset, map_offset):
    drawn = _drawn(item, offset, map_offset)
    return float(np.abs(drawn - _expected(camera, anchor)).max())


def test_settle_after_a_pan_without_republishing(app):
    """A pan that settles before replacement data arrives stays pinned."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )
    pan_reference = PanReference.from_camera(reference, 1.0)
    widget = _placement(item, reference, pan_reference, reference)

    moved = _camera_panned(reference, 120.0, -55.0)
    map_offset = _pan_offset(reference, moved)
    offset, uploads, fallbacks = _settle(widget, item, node, moved, pan_reference)
    error = _settle_error(item, anchor, moved, offset, map_offset)
    assert (uploads, fallbacks) == (0, 0), "a pan must not re-compose the frame"
    assert error < 1.0, f"a settled pan left the chart {error:.3f} px off"


def test_settle_after_a_zoom_in_without_republishing(app):
    """One zoom step that settles before replacement data arrives."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )
    widget = _placement(
        item, reference, PanReference.from_camera(reference, 1.0), reference
    )

    zoomed = _Camera(lon=reference.center_lon, lat=reference.center_lat, zoom=14.25)
    offset, uploads, fallbacks = _settle(
        widget, item, node, zoomed, PanReference.from_camera(zoomed, 1.0)
    )
    error = _settle_error(item, anchor, zoomed, offset, (0.0, 0.0))
    assert (uploads, fallbacks) == (0, 0), "a zoom step must not re-upload geometry"
    assert error < 1.0, f"a settled zoom-in left the chart {error:.3f} px off"


def test_settle_after_a_zoom_out_without_republishing(app):
    """One zoom-out step that settles before replacement data arrives."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )
    widget = _placement(
        item, reference, PanReference.from_camera(reference, 1.0), reference
    )

    zoomed = _Camera(lon=reference.center_lon, lat=reference.center_lat, zoom=13.75)
    offset, uploads, fallbacks = _settle(
        widget, item, node, zoomed, PanReference.from_camera(zoomed, 1.0)
    )
    error = _settle_error(item, anchor, zoomed, offset, (0.0, 0.0))
    assert (uploads, fallbacks) == (0, 0), "a zoom step must not re-upload geometry"
    assert error < 1.0, f"a settled zoom-out left the chart {error:.3f} px off"


def test_settle_after_three_zooms_in_does_not_accumulate(app):
    """Three zoom steps, each settled with no replacement scene in between."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )
    widget = _placement(
        item, reference, PanReference.from_camera(reference, 1.0), reference
    )

    errors = []
    for zoom in (14.25, 14.5, 14.75):
        camera = _Camera(lon=reference.center_lon, lat=reference.center_lat, zoom=zoom)
        offset, uploads, fallbacks = _settle(
            widget, item, node, camera, PanReference.from_camera(camera, 1.0)
        )
        assert (uploads, fallbacks) == (0, 0), f"zoom {zoom} re-uploaded geometry"
        errors.append(_settle_error(item, anchor, camera, offset, (0.0, 0.0)))
    assert max(errors) < 1.0, f"zoom steps settled off by {errors}"
    assert errors[-1] <= errors[0] + 0.5, f"zoom error accumulated: {errors}"


def test_settle_after_a_zoom_then_a_pan_without_republishing(app):
    """A pan after a settled zoom, still with no replacement data."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )
    widget = _placement(
        item, reference, PanReference.from_camera(reference, 1.0), reference
    )

    zoomed = _Camera(lon=reference.center_lon, lat=reference.center_lat, zoom=14.25)
    zoom_reference = PanReference.from_camera(zoomed, 1.0)
    offset, uploads, fallbacks = _settle(widget, item, node, zoomed, zoom_reference)
    assert (uploads, fallbacks) == (0, 0)
    assert _settle_error(item, anchor, zoomed, offset, (0.0, 0.0)) < 1.0

    moved = _camera_panned(zoomed, 90.0, -40.0)
    map_offset = _pan_offset(zoomed, moved)
    offset, uploads, fallbacks = _settle(widget, item, node, moved, zoom_reference)
    error = _settle_error(item, anchor, moved, offset, map_offset)
    assert (uploads, fallbacks) == (0, 0), "a pan must not re-compose the frame"
    assert error < 1.0, f"a pan after a zoom settled {error:.3f} px off"


def test_settle_after_a_four_x_zoom_without_republishing(app):
    """A scale far from the composed one is still a transform, not a rebuild."""
    reference = _Camera()
    positions = _visible_degrees(reference)
    anchor = positions[0]
    item, node = _presented(
        app,
        reference,
        _Scene(positions, _split_cells(positions)),
        _projection(reference),
    )
    widget = _placement(
        item, reference, PanReference.from_camera(reference, 1.0), reference
    )

    zoomed = _Camera(lon=reference.center_lon, lat=reference.center_lat, zoom=16.0)
    offset, uploads, fallbacks = _settle(
        widget, item, node, zoomed, PanReference.from_camera(zoomed, 1.0)
    )
    error = _settle_error(item, anchor, zoomed, offset, (0.0, 0.0))
    assert (uploads, fallbacks) == (0, 0), "a 4x zoom must not re-upload geometry"
    assert error < 1.0, f"a settled 4x zoom left the chart {error:.3f} px off"


# ------------------------------------------------- covering the view through a pan
# The scene layer composes over a viewport padded by ``MARGIN``, and culling
# runs in the item frame.  A pan translates the item, so a clip on the item rect
# is a clip on the compose-time viewport: cells the composition supplied out in
# the padding get detached and never come back, which is the hard-edged
# rectangle the user sees.  These build cells in that padding and check that
# they stay attached and that the uncovered strip is bounded by a recompose.


def _column_scene(camera, columns=8, rows=5, margin=None):
    """Cells filling the padded composition, in contiguous column blocks."""
    margin = enc_compose_margin(camera) if margin is None else float(margin)
    left = -margin - camera.width / 2.0
    right = camera.width / 2.0 + margin
    top = -margin - camera.height / 2.0
    bottom = camera.height / 2.0 + margin
    step = (right - left) / columns
    points = []
    for index in range(columns):
        for edge in (0.0, 0.5, 1.0):
            for row in range(rows):
                px = left + (index + edge) * step
                py = top + (bottom - top) * row / (rows - 1)
                centre = _camera_panned(camera, px, py)
                points.append((centre.center_lon, centre.center_lat))
    positions = np.asarray(points, dtype=np.float64)
    return positions, _split_cells(positions, cells=columns)


def _attached_cover(item, dx):
    """Union of the attached cells' x-extent on screen, as (start, end)."""
    matrix = item._content_node.matrix()
    tx = float(matrix[0, 3]) + dx
    spans = []
    for cell, attached in zip(item._static.cells, item._cell_visible):
        if not attached or cell.bounds is None:
            continue
        spans.append((cell.bounds[0] + tx, cell.bounds[2] + tx))
    spans.sort()
    start, end = spans[0]
    for low, high in spans[1:]:
        if low > end:
            break
        end = max(end, high)
    return start, end


def test_culling_keeps_the_composed_padding_attached(app):
    """Cells the composition supplied outside the item must not be detached."""
    reference = _Camera()
    margin = enc_compose_margin(reference)
    positions, cells = _column_scene(reference, margin=margin)
    item, node = _presented(
        app,
        reference,
        _Scene(positions, cells),
        _projection(reference),
        margin=margin,
    )
    attached = list(item._cell_visible)
    assert len(attached) == 8
    assert all(attached), (
        "cells composed into the padded view were culled: "
        f"{[i for i, ok in enumerate(attached) if not ok]}"
    )

    start, end = _attached_cover(item, 200.0)
    assert start <= 0.5 and end >= reference.width - 0.5, (
        f"after a 200 px pan the attached cells cover [{start:.1f}, {end:.1f}], "
        f"not the viewport [0, {reference.width}]"
    )


def test_stale_cover_requests_one_recompose_per_generation(app):
    """Half the composed margin consumed asks once, not once per frame."""
    from unittest import mock

    reference = _Camera()
    margin = enc_compose_margin(reference)
    trigger = ENC_COMPOSE_TRIGGER * margin
    positions, cells = _column_scene(reference, margin=margin)
    item, node = _presented(
        app,
        reference,
        _Scene(positions, cells),
        _projection(reference),
        margin=margin,
    )
    request = mock.Mock()
    pan_reference = PanReference.from_camera(reference, 1.0)
    widget = _placement(
        item, reference, pan_reference, reference, request=request, generation=4
    )

    for dx in (100.0, trigger - 20.0):
        moved = _camera_panned(reference, dx, 0.0)
        widget._camera = moved
        widget._place_enc_scene()
        assert not request.called, f"a {dx:.1f} px pan is inside half the margin"

    for dx in (trigger + 10.0, 400.0, 700.0, 900.0):
        moved = _camera_panned(reference, dx, 0.0)
        widget._camera = moved
        widget._place_enc_scene()
    assert (
        request.call_count == 1
    ), f"one replacement per presented generation, got {request.call_count}"

    widget._enc_scene_generation = 5
    widget._place_enc_scene()
    assert request.call_count == 2, "a new generation may ask again"


def test_zoom_out_requests_a_recompose(app):
    """A zoom-out enlarges the view past what the composition covers."""
    from unittest import mock

    reference = _Camera()
    margin = enc_compose_margin(reference)
    positions, cells = _column_scene(reference, margin=margin)
    item, node = _presented(
        app,
        reference,
        _Scene(positions, cells),
        _projection(reference),
        margin=margin,
    )
    request = mock.Mock()
    widget = _placement(
        item,
        reference,
        PanReference.from_camera(reference, 1.0),
        reference,
        request=request,
        generation=1,
    )

    zoomed = _Camera(lon=reference.center_lon, lat=reference.center_lat, zoom=13.75)
    widget._camera = zoomed
    widget._map_pan_reference = PanReference.from_camera(zoomed, 1.0)
    widget._place_enc_scene()
    assert request.call_count == 1, "a zoom-out must ask for a scene at the new scale"
    assert widget._enc_scene_frame_camera is zoomed


def test_scene_margin_matches_the_composition_padding():
    """The item's default cull ring and the raster composition agree."""
    from sailworks.rendering.overlayrenderer import MARGIN

    assert encgpu.SCENE_MARGIN == MARGIN


def test_compose_margin_is_two_viewports_across(app):
    """The planned frame spans about two viewports, floored at ``MARGIN``."""
    from sailworks.qt.viewporttransform import ENC_COMPOSE_FRACTION

    reference = _Camera()
    assert enc_compose_margin(reference) == pytest.approx(
        ENC_COMPOSE_FRACTION * reference.width
    )
    assert enc_compose_margin(_Camera(width=1200, height=800)) == 400.0
    assert enc_compose_margin(_Camera(width=300, height=400)) == encgpu.SCENE_MARGIN


def _screen_boxes(item, shift):
    """Attached cells' bboxes in the viewport frame, as ``(x0, y0, x1, y1)``."""
    matrix = item._content_node.matrix()
    coeffs = (
        float(matrix[0, 0]),
        float(matrix[0, 1]),
        float(matrix[0, 3]) + shift[0],
        float(matrix[1, 0]),
        float(matrix[1, 1]),
        float(matrix[1, 3]) + shift[1],
    )
    return [
        encgpu._affine_bounds(coeffs, cell.bounds)
        for cell, attached in zip(item._static.cells, item._cell_visible)
        if attached and cell.bounds is not None
    ]


def _uncovered(item, camera, shift, samples=48):
    """Fraction of the viewport no attached cell's box reaches."""
    boxes = _screen_boxes(item, shift)
    if not boxes:
        return 1.0
    covered = 0
    total = 0
    for x in np.linspace(0.0, camera.width, samples + 1):
        for y in np.linspace(0.0, camera.height, samples + 1):
            total += 1
            for x0, y0, x1, y1 in boxes:
                if x0 <= x <= x1 and y0 <= y <= y1:
                    covered += 1
                    break
    return 1.0 - covered / total


def _request(state, widget):
    """Record a replacement request for the camera that asked for it."""
    state.requests += 1
    state.pending = widget._camera
    state.age = 0


def _drive_pan(app, reference, total, step, margin=None, latency=0):
    """Pan ``total`` px in ``step`` steps; publish after ``latency`` frames.

    Returns ``(rows, requests)`` where each row is
    ``(dx, uncovered_fraction, requested_this_frame)``.  A publish replaces the
    presented scene with one composed for the camera that asked for it, which
    is what the asynchronous layer delivers.
    """
    margin = enc_compose_margin(reference) if margin is None else float(margin)
    positions, cells = _column_scene(reference, margin=margin)
    item, node = _presented(
        app,
        reference,
        _Scene(positions, cells),
        _projection(reference),
        margin=margin,
    )
    pan_reference = PanReference.from_camera(reference, 1.0)
    state = SimpleNamespace(pending=None, age=0, requests=0)
    # ``_camera`` in this closure is read by the request callback.
    widget = _placement(
        item,
        reference,
        pan_reference,
        reference,
        request=lambda: _request(state, widget),
        generation=1,
    )

    def publish(camera):
        positions, cells = _column_scene(camera, margin=margin)
        item.set_projection(_projection(camera))
        item.set_compose_margin(margin)
        item.set_view_scene(_Scene(positions, cells))
        widget._enc_scene_camera = camera
        widget._enc_scene_frame_camera = camera
        widget._enc_scene_generation += 1

    rows = []
    dx = 0.0
    while dx <= total + 1e-9:
        moved = _camera_panned(reference, dx, 0.0)
        widget._camera = moved
        before = state.requests
        widget._place_enc_scene()
        if state.pending is not None:
            state.age += 1
            if state.age > latency:
                publish(state.pending)
                state.pending = None
        item.updatePaintNode(node, None)
        offset = _pan_offset(pan_reference, moved)
        shift = (float(item.x()) + offset[0], float(item.y()) + offset[1])
        rows.append((dx, _uncovered(item, reference, shift), state.requests - before))
        dx += step
    return rows, state.requests


def test_long_pan_never_exposes_an_uncovered_region(app):
    """A 900 px drag at 60 fps stays drawn while the composer keeps up."""
    reference = _Camera()
    rows, requests = _drive_pan(app, reference, total=900.0, step=15.0)
    worst = max(row[1] for row in rows)
    assert requests >= 1, "the pan exhausted no margin at all"
    assert worst == 0.0, "uncovered viewport during a scripted pan: " + ", ".join(
        f"{dx:.0f}px={fraction:.3f}" for dx, fraction, _ in rows if fraction
    )


def test_long_pan_residual_is_bounded_when_the_composer_lags(app):
    """A composer that lands ten frames late leaves a bounded strip, no more."""
    from unittest import mock

    from sailworks.qt import gribviewport as _gv

    reference = _Camera()
    # The pre-fix policy: a 256 px frame, asked for only once exhausted, and a
    # composer that takes ten frames to land.  This is the reported gap.
    with (
        mock.patch.object(
            _gv, "enc_compose_margin", lambda camera: encgpu.SCENE_MARGIN
        ),
        mock.patch.object(_vt, "ENC_COMPOSE_TRIGGER", 1.0),
    ):
        slow, _ = _drive_pan(
            app,
            reference,
            total=900.0,
            step=15.0,
            margin=encgpu.SCENE_MARGIN,
            latency=10,
        )
    assert max(row[1] for row in slow) > 0.0, "the pre-fix policy must show a gap"

    # With the shipped policy the same slow composer still stays covered,
    # because the request leaves half the margin as slack.
    fast, requests = _drive_pan(app, reference, total=900.0, step=15.0, latency=10)
    assert requests >= 1
    assert max(row[1] for row in fast) == 0.0


def test_zoom_out_step_leaves_no_uncovered_region(app):
    """A zoom-out step re-composes before the enlarged view runs out."""
    from unittest import mock

    reference = _Camera()
    margin = enc_compose_margin(reference)
    positions, cells = _column_scene(reference, margin=margin)
    item, node = _presented(
        app,
        reference,
        _Scene(positions, cells),
        _projection(reference),
        margin=margin,
    )
    request = mock.Mock()
    widget = _placement(
        item,
        reference,
        PanReference.from_camera(reference, 1.0),
        reference,
        request=request,
        generation=1,
    )
    zoomed = _Camera(lon=reference.center_lon, lat=reference.center_lat, zoom=13.5)
    widget._camera = zoomed
    widget._map_pan_reference = PanReference.from_camera(zoomed, 1.0)
    widget._place_enc_scene()
    assert request.call_count == 1
    item.updatePaintNode(node, None)
    assert (
        _uncovered(item, reference, (0.0, 0.0)) == 0.0
    ), "the retained frame no longer covers the zoomed-out viewport"

    positions, cells = _column_scene(zoomed, margin=margin)
    item.set_projection(_projection(zoomed))
    item.set_compose_margin(margin)
    item.set_view_scene(_Scene(positions, cells))
    widget._enc_scene_frame_camera = zoomed
    item.updatePaintNode(node, None)
    assert _uncovered(item, reference, (0.0, 0.0)) == 0.0


def test_a_partial_publication_never_replaces_the_ready_scene(app):
    """Progress for a newer generation must not blank the finished frame."""
    reference = _Camera()
    margin = enc_compose_margin(reference)
    positions, cells = _column_scene(reference, margin=margin)
    ready = _Scene(positions, cells)
    item, node = _presented(
        app,
        reference,
        ready,
        _projection(reference),
        margin=margin,
    )
    shown = SimpleNamespace(
        _enc_scene_item=item,
        _enc_scene_provisional=False,
        _enc_scene_ready_generation=None,
        _enc_scene_generation=None,
        _enc_scene_camera=None,
        _enc_scene_frame_camera=None,
        # The viewport is showing ``reference``: a publication resolves the
        # chart deferral against the camera on screen, so the double carries
        # it exactly as the real widget does from ``__init__``.
        _camera=reference,
        _camera_dpr=1.0,
        _disposed=False,
        _update_enc_status=lambda: None,
        _place_enc_scene=lambda **_: None,
    )
    # The publication path decides retention by consulting two further
    # predicates on ``self``.  Bind the production implementations, as for
    # ``_enc_scene_published``, so the double exercises the real retention
    # law instead of a stubbed verdict.
    widget_class = _viewport_widget_class()
    for name in (
        "_enc_scene_published",
        "_enc_scene_has_presented",
        "_enc_scene_partial_accounts_for_view",
        "_hold_enc_scene_partial",
        "_cancel_enc_scene_partial_hold",
        "_release_held_enc_scene_partial",
    ):
        setattr(shown, name, MethodType(getattr(widget_class, name), shown))
    metadata = {
        "projection": _projection(reference),
        "camera": reference,
        "compose_margin": margin,
    }
    shown._enc_scene_published((1, ready, metadata))
    assert item.view_scene is ready
    assert shown._enc_scene_ready_generation == 1

    panned = _camera_panned(reference, 40.0, 0.0)
    positions, cells = _column_scene(panned, margin=margin)
    partial = _Scene(positions, cells)
    shown._enc_scene_published((2, partial, dict(metadata, partial=True)))
    assert item.view_scene is ready, "a partial scene replaced the finished frame"
    assert shown._enc_scene_provisional is True

    shown._enc_scene_published((2, partial, dict(metadata, camera=panned)))
    assert item.view_scene is partial, "the complete scene must replace it"
    assert shown._enc_scene_ready_generation == 2
    assert item.compose_margin == margin


# ------------------------------------------------------------ coverage claims
# ``scene_covers_view`` is what suppresses the raster layer, so a true answer
# may only describe the cells the frame actually draws, over the whole item.
# A composition whose plan collapsed to a couple of cells still overlaps the
# view; the older overlap test was satisfied by that and let the raster hide
# behind a frame that painted a fraction of the chart.


def _six_cell_cover(camera):
    """A six-cell composition covering the padded view, and its cell spans."""
    margin = enc_compose_margin(camera)
    positions, _ = _column_scene(camera, columns=6, margin=margin)
    return positions, _split_cells(positions, cells=6), margin


def _drawing_item(app, camera, positions, cells, margin):
    item, _ = _presented(
        app,
        camera,
        _Scene(positions, cells),
        _projection(camera),
        margin=margin,
    )
    item.setVisible(True)
    return item


def test_a_full_six_cell_scene_reports_coverage(app):
    camera = _Camera()
    positions, cells, margin = _six_cell_cover(camera)
    item = _drawing_item(app, camera, positions, cells, margin)
    assert len(cells) == 6
    assert item.shown_vertex_count() == int(positions.shape[0])
    assert item.scene_covers_view() is True


def test_a_one_or_two_cell_subset_of_a_six_cell_view_is_not_coverage(app):
    camera = _Camera()
    positions, cells, margin = _six_cell_cover(camera)
    for subset in ((3,), (2, 4)):
        item = _drawing_item(
            app, camera, positions, [cells[index] for index in subset], margin
        )
        assert item.shown_vertex_count() > 0, "the subset still draws cells"
        assert (
            item.scene_covers_view() is False
        ), f"cells {subset} of a six-cell view claimed coverage"


def test_coverage_is_geometric_not_the_planned_cell_set(app):
    """Two cells that tile the view cover it, whatever the plan asked for.

    The claim is about drawn pixels, so a composition that reaches the whole
    item with fewer, larger cells is full coverage and may suppress the
    raster.  Identity with a previous plan is not part of the question: the
    item is never told what that plan was.
    """
    camera = _Camera()
    margin = enc_compose_margin(camera)
    positions, cells = _column_scene(camera, columns=2, margin=margin)
    item = _drawing_item(app, camera, positions, cells, margin)
    assert len(cells) == 2
    assert item.scene_covers_view() is True


def test_the_raster_gate_follows_the_drawn_cell_set(app):
    """A partial cell set keeps the raster available; a full set suppresses it."""
    camera = _Camera()
    positions, cells, margin = _six_cell_cover(camera)
    metadata = {"camera": camera, "complete": True}

    full = _drawing_item(app, camera, positions, cells, margin)
    assert scene_presents_chart(metadata, camera, full) is True

    partial = _drawing_item(app, camera, positions, [cells[2], cells[3]], margin)
    assert enc_scene_covers_view(partial) is False
    assert scene_presents_chart(metadata, camera, partial) is False
