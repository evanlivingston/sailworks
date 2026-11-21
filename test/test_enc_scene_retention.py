"""Retention for return pans: the scene layer's answer and the GPU nodes.

An A->B->A pan at one zoom must present scene A again without the worker
recomposing it and without the GPU item rebuilding or re-uploading its
vertices.  The layer keeps complete publications, answers a covered return
camera from one whose composed ring covers it, and re-publishes that scene
under a fresh monotonic generation with the *same scene object*; the item
recognises the object and re-attaches the nodes it already built.  While
another scene is presented the retained frame's nodes are detached, so the
two frames are never drawn on top of each other.

The item tests drive ``updatePaintNode`` directly, as
``test_enc_static_matrix.py`` does; the layer tests bind the retention
methods to a stub, as ``test_enc_scene_viewport_retention.py`` does.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

import numpy as np
import pytest

from PySide6.QtWidgets import QApplication

from sailworks.qt import encgpu
from sailworks.qt.encscenelayer import EncSceneLayer


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


def _mapped_positions(item):
    """Map every presented static vertex through the item's current matrix."""
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


def _presented(camera, scene):
    item = encgpu.EncMeshItem(text_enabled=False)
    item.setWidth(camera.width)
    item.setHeight(camera.height)
    item.set_projection(_projection(camera))
    item.set_view_scene(scene)
    node = item.updatePaintNode(None, None)
    return item, node


# ------------------------------------------------------------------ GPU nodes
def test_a_return_pan_restores_the_retained_nodes(app):
    """A->B->A at one zoom re-attaches A's nodes; nothing is rebuilt or layered."""
    camera_a = _Camera()
    positions = _visible_degrees(camera_a)
    scene_a = _Scene(positions, _split_cells(positions))
    camera_b = _Camera(lon=camera_a.center_lon + 0.2)
    other = _visible_degrees(camera_b)
    scene_b = _Scene(other, _split_cells(other))

    item, node = _presented(camera_a, scene_a)
    nodes_a = list(item._cell_nodes)
    assert nodes_a and any(item.visible_cells)
    assert item.upload_count == 1

    # The viewport adopts a publication by setting the frame's own projection
    # before the scene, exactly as the publish handler does.
    item.set_projection(_projection(camera_b))
    item.set_view_scene(scene_b)
    item.updatePaintNode(node, None)
    nodes_b = list(item._cell_nodes)
    assert nodes_b and not (set(nodes_b) & set(nodes_a))
    assert item._retained[0] is scene_a
    # A's nodes are parked out of the graph: B draws alone.
    assert not any(stale.parent() is item._content_node for stale in nodes_a)
    assert all(current.parent() is item._content_node for current in nodes_b)
    assert item.upload_count == 2

    item.set_projection(_projection(camera_a))
    item.set_view_scene(scene_a)  # pan back: identity match, no rebuild
    assert item.updatePaintNode(node, None) is node
    assert item._cell_nodes == nodes_a
    assert all(restored.parent() is item._content_node for restored in nodes_a)
    assert not any(parked.parent() is item._content_node for parked in nodes_b)
    # The retained nodes were re-attached: no third upload for the return.
    assert item.upload_count == 2
    # The frames traded places: the retained frame is B's, the presented
    # frame is A's, so returning to B again is answered the same way.
    assert item._retained[0] is scene_b
    assert item._retained[1] is not item._static
    assert any(item.visible_cells)


def test_the_restored_frame_draws_where_the_scene_says(app):
    """The re-attached frame plus a matrix equals the CPU projection."""
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene_a = _Scene(positions, _split_cells(positions))
    camera_b = _Camera(lon=camera.center_lon + 0.2)
    other = _visible_degrees(camera_b)
    scene_b = _Scene(other, _split_cells(other))

    item, node = _presented(camera, scene_a)
    item.set_projection(_projection(camera_b))
    item.set_view_scene(scene_b)
    item.updatePaintNode(node, None)
    item.set_projection(_projection(camera))
    item.set_view_scene(scene_a)
    item.updatePaintNode(node, None)

    matrix = _mapped_positions(item)
    legacy = encgpu._project_positions(scene_a, _projection(camera), None)
    assert matrix.shape == legacy.shape
    assert float(np.abs(matrix - legacy).max()) < 0.01


def test_representing_the_presented_scene_is_still_a_no_op(app):
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene = _Scene(positions, _split_cells(positions))
    item, node = _presented(camera, scene)
    assert item.upload_count == 1
    assert item.set_view_scene(scene) is False
    assert item.updatePaintNode(node, None) is node
    assert item.upload_count == 1


def test_a_palette_change_drops_the_retained_frame(app):
    """Baked vertex colours belong to the old palette; the frame cannot answer."""
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene_a = _Scene(positions, _split_cells(positions))
    camera_b = _Camera(lon=camera.center_lon + 0.2)
    other = _visible_degrees(camera_b)
    scene_b = _Scene(other, _split_cells(other))

    item, node = _presented(camera, scene_a)
    nodes_a = list(item._cell_nodes)
    item.set_projection(_projection(camera_b))
    item.set_view_scene(scene_b)
    item.updatePaintNode(node, None)
    assert item._retained[0] is scene_a

    item.set_colored(False)
    assert item._retained is None
    assert item._retained_dead  # parked, not freed on the GUI thread
    item.set_colored(True)
    item.set_view_scene(scene_a)
    item.updatePaintNode(node, None)
    # A scene the item no longer holds nodes for is rebuilt and re-uploaded.
    assert item._cell_nodes != nodes_a
    assert item.upload_count == 3
    assert item._retained_dead == []  # released by the paint, render side


def test_hiding_the_item_drops_the_retained_frame(app):
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene_a = _Scene(positions, _split_cells(positions))
    camera_b = _Camera(lon=camera.center_lon + 0.2)
    other = _visible_degrees(camera_b)
    scene_b = _Scene(other, _split_cells(other))

    item, node = _presented(camera, scene_a)
    item.set_projection(_projection(camera_b))
    item.set_view_scene(scene_b)
    item.updatePaintNode(node, None)
    assert item._retained[0] is scene_a

    item.setVisible(False)
    assert item._retained is None
    assert item._retained_restore is False
    # The parked nodes are released by the next paint, on the render thread.
    item.setVisible(True)
    camera_c = _Camera(lon=camera.center_lon - 0.2)
    more = _visible_degrees(camera_c)
    scene_c = _Scene(more, _split_cells(more))
    item.set_projection(_projection(camera_c))
    item.set_view_scene(scene_c)
    item.updatePaintNode(node, None)
    assert item._retained_dead == []


def test_a_frame_over_the_retention_budget_is_not_retained(app, monkeypatch):
    monkeypatch.setattr(encgpu, "RETAINED_FRAME_VERTEX_BYTES", 1)
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene_a = _Scene(positions, _split_cells(positions))
    camera_b = _Camera(lon=camera.center_lon + 0.2)
    other = _visible_degrees(camera_b)
    scene_b = _Scene(other, _split_cells(other))

    item, node = _presented(camera, scene_a)
    nodes_a = list(item._cell_nodes)
    item.set_projection(_projection(camera_b))
    item.set_view_scene(scene_b)
    item.updatePaintNode(node, None)
    assert item._retained is None
    # The outgoing nodes were dropped exactly as before the retention work.
    assert not any(stale.parent() is item._content_node for stale in nodes_a)


def test_a_restore_is_refused_after_the_graph_was_forgotten(app):
    """A stale retention entry cannot reach dead nodes; the demand rebuilds."""
    camera = _Camera()
    positions = _visible_degrees(camera)
    scene_a = _Scene(positions, _split_cells(positions))
    camera_b = _Camera(lon=camera.center_lon + 0.2)
    other = _visible_degrees(camera_b)
    scene_b = _Scene(other, _split_cells(other))

    item, _node = _presented(camera, scene_a)
    nodes_a = list(item._cell_nodes)
    item.set_projection(_projection(camera_b))
    item.set_view_scene(scene_b)
    item.updatePaintNode(None, None)
    assert item._retained[0] is scene_a

    # As a window teardown would: the graph the retained nodes lived in is
    # gone, so the entry must not be restored into a fresh one.
    item._forget_scene_graph()
    assert item._retained is None
    item.set_projection(_projection(camera))
    item.set_view_scene(scene_a)
    item.updatePaintNode(None, None)
    assert item._cell_nodes != nodes_a
    assert item.upload_count == 3


# --------------------------------------------------------------- scene layer
class _ViewCamera:
    """The camera surface the layer's retention checks read."""

    def __init__(self, lon=5.5, lat=48.0, zoom=14.0, width=710, height=743):
        self.center_lon = lon
        self.center_lat = lat
        self.zoom = zoom
        self.width = width
        self.height = height

    def stamp(self):
        return 1.0


def _retained_projection(camera):
    """The frame's own projection: its centre sits at (w/2, h/2)."""
    pixels_per_degree = 100.0

    def project(points):
        points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        return np.column_stack(
            (
                (points[:, 0] - camera.center_lon) * pixels_per_degree
                + camera.width / 2.0,
                (camera.center_lat - points[:, 1]) * pixels_per_degree
                + camera.height / 2.0,
            )
        )

    return project


def _retained_entry(camera, scene="scene-a", content=1, margin=256.0):
    metadata = {
        "time": camera.stamp(),
        "dpr": 1,
        "content": content,
        "camera": camera,
        "projection": _retained_projection(camera),
        "compose_margin": margin,
        "planned_cells": 4,
    }
    return (3, scene, metadata)


def _layer_stub(entries, content_generation=1, generation=7):
    layer = SimpleNamespace(
        _published=None,
        _retained_ready=list(entries),
        _pending=None,
        _active=None,
        _prefetch=None,
        _yielded=None,
        _notify_after_lock=None,
        _generation=generation,
        _dpr=1,
        _state="loading",
        _error="",
        _incomplete="",
        _progress_done=0,
        _progress_total=0,
        content_generation=content_generation,
        counters={
            "reuses": 0,
            "retained_reuses": 0,
            "cancelled": 0,
            "prefetch_cancelled": 0,
        },
    )
    for name in (
        "_reuse",
        "_retained_covering",
        "_retained_covers",
        "_promote_retained",
    ):
        setattr(layer, name, MethodType(getattr(EncSceneLayer, name), layer))
    return layer


def test_a_covered_return_pan_promotes_the_retained_scene():
    camera_a = _ViewCamera()
    entry = _retained_entry(camera_a)
    layer = _layer_stub([entry])

    # A camera one degree away: inside the composed ring (100 px/degree,
    # margin 256 px) and at the same scale the frame was composed for.
    camera_b = _ViewCamera(lon=camera_a.center_lon + 1.0)
    reused = layer._reuse(camera_b, False)

    # The re-publication carries a fresh monotonic generation, never the
    # entry's older one, and the same scene object the item keys nodes on.
    assert reused == 8
    assert layer._generation == 8
    assert layer._published[0] == 8
    assert layer._published[1] is entry[1]
    assert layer._published[2]["retained_reuse"] is True
    assert layer._notify_after_lock is layer._published
    assert layer.counters["retained_reuses"] == 1
    assert layer._state == "ready"


def test_a_stale_content_generation_is_never_promoted():
    layer = _layer_stub(
        [_retained_entry(_ViewCamera(), content=0)], content_generation=1
    )
    assert layer._reuse(_ViewCamera(), False) is None
    assert layer.counters["retained_reuses"] == 0
    assert layer._published is None


def test_a_publication_without_a_content_marker_is_never_promoted():
    entry = _retained_entry(_ViewCamera())
    del entry[2]["content"]
    layer = _layer_stub([entry])
    assert layer._reuse(_ViewCamera(), False) is None
    assert layer._published is None


def test_a_different_zoom_is_never_promoted():
    layer = _layer_stub([_retained_entry(_ViewCamera(zoom=14.0))])
    assert layer._reuse(_ViewCamera(zoom=15.0), False) is None
    assert layer._published is None


def test_a_camera_outside_the_retained_ring_is_never_promoted():
    # The ring is the frame's own projection mapped through its recorded
    # compose margin: 3 degrees at 100 px/degree is 300 px, past the 256 px
    # margin.  Margin arithmetic alone never decides this; the projection does.
    camera_a = _ViewCamera()
    layer = _layer_stub([_retained_entry(camera_a)])
    far = _ViewCamera(lon=camera_a.center_lon + 3.0)
    assert layer._reuse(far, False) is None
    assert layer._published is None


def test_a_forecast_instant_change_is_never_promoted():
    layer = _layer_stub([_retained_entry(_ViewCamera())])
    camera = _ViewCamera()
    camera.stamp = lambda: 2.0
    assert layer._reuse(camera, False) is None
    assert layer._published is None


def test_a_forced_replace_never_promotes():
    layer = _layer_stub([_retained_entry(_ViewCamera())])
    assert layer._reuse(_ViewCamera(), True) is None
    assert layer._published is None
