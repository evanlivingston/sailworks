"""Static-frame matrix path: exact parity with the CPU projection.

The item's vertices are projected once into a reference screen frame and a
``QMatrix4x4`` moves them each frame.  These tests assert that the matrix path
reproduces :func:`encgpu._project_positions` (the legacy CPU path) to well under
``0.01`` item pixels, including after a pan, after a zoom step, with a
non-default device pixel ratio and with a non-zero origin, and that a pan does
not re-upload a single vertex.
"""

from __future__ import annotations

import numpy as np
import pytest

from PySide6.QtWidgets import QApplication

from sailworks.qt import encgpu


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


def _presented(app, camera, scene, projection, origin=None, legacy=None):
    kwargs = {} if legacy is None else {"legacy_project": legacy}
    item = encgpu.EncMeshItem(text_enabled=False, **kwargs)
    item.setWidth(camera.width)
    item.setHeight(camera.height)
    if origin is not None:
        item.set_origin(origin)
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

    far = _Camera(lon=camera.center_lon + 5.0)
    item.set_projection(_projection(far))
    item.updatePaintNode(node, None)
    assert item.visible_cells and not any(item.visible_cells)


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
    uploaded = sum(
        cell_node.geometry().vertexCount() for cell_node in static_item._cell_nodes
    )
    assert uploaded == sum(cell.vertices.shape[0] for cell in static_item._static.cells)
