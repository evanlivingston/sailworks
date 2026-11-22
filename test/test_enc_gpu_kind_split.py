"""The fill/line/marker split that lets GPU lines paint under ENC symbols.

The GPU line payload is drawn by a native sibling item, so the scene mesh has
to stop carrying the extracted line classes while still drawing the chart
fills, the remaining CPU line classes, the point symbols and the sounding
text.  These tests pin the four pieces that keep that split honest:

* the kind filter packs vertices and cell spans without changing source order;
* the marker item is the only one that carries markers and soundings;
* the single-item fallback keeps every kind and text;
* ``EncSceneLayers`` exposes the viewport's whole item surface and keeps the
  line item out of placement writes, with the QML slots deciding paint order.
"""

import inspect
import os
from builtins import property as builtin_property
from pathlib import Path

# Must be set before PySide6 is imported so the offscreen plugin is selected.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402

from sailworks.qt.encgpu import (  # noqa: E402
    FILL_KINDS,
    MARKER_KINDS,
    EncMeshItem,
    EncSceneLayers,
    filter_view_scene,
)
from sailworks.qt.gribviewport import enc_scene_item  # noqa: E402
from sailworks.rendering.encview import CellSpan, ViewScene  # noqa: E402
from sailworks.rendering.vectordrawer import encmesh, encsounding  # noqa: E402
from sailworks.rendering.vectordrawer.encmesh import MeshPart  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = QGuiApplication.instance()
    if application is None:
        application = QGuiApplication([])
    return application


def _scene():
    """Two cells: A is fill + marker, B is a line + an area outline."""
    positions = np.array(
        [
            (0, 0),
            (8, 0),
            (0, 8),
            (20, 0),
            (28, 0),
            (20, 8),
            (40, 0),
            (48, 0),
            (40, 8),
            (60, 0),
            (68, 0),
            (60, 8),
        ],
        dtype=np.float64,
    )
    parts = (
        MeshPart(0, "DEPARE", 0, 1, encmesh.AREA_FILL, 0, 3, (10, 20, 30, 255)),
        MeshPart(0, "SOUNDG", 0, 2, encmesh.MARKER, 3, 3, (200, 100, 50, 255)),
        MeshPart(1, "DEPCNT", 0, 3, encmesh.LINE, 6, 3, (5, 250, 15, 255)),
        MeshPart(1, "RESARE", 0, 4, encmesh.AREA_OUTLINE, 9, 3, (90, 40, 10, 128)),
    )
    cells = (
        CellSpan("A.000", None, 0, 2, 0, 6, "none"),
        CellSpan("B.000", None, 2, 2, 6, 6, "none"),
    )
    return ViewScene(
        positions=positions,
        parts=parts,
        cells=cells,
        unsupported=(),
        skipped=(),
        accounted_bytes=123,
        soundings=np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float64),
        colours=np.arange(48, dtype=np.uint8).reshape(12, 4),
    )


def _kinds(scene):
    return tuple(part.kind for part in scene.parts)


def test_filter_packs_fill_vertices_and_rebuilds_cell_spans():
    scene = _scene()

    fill = filter_view_scene(scene, FILL_KINDS, keep_soundings=False)

    assert _kinds(fill) == (encmesh.AREA_FILL, encmesh.LINE, encmesh.AREA_OUTLINE)
    # The marker's three vertices are gone; the remaining ranges are packed.
    assert fill.positions.shape == (9, 2)
    assert [part.first_vertex for part in fill.parts] == [0, 3, 6]
    assert [cell.vertex_count for cell in fill.cells] == [3, 6]
    assert [cell.first_part for cell in fill.cells] == [0, 1]
    assert [cell.part_count for cell in fill.cells] == [1, 2]
    assert [cell.first_vertex for cell in fill.cells] == [0, 3]
    # A source order is preserved exactly, not sorted by kind.
    assert fill.positions[0].tolist() == [0.0, 0.0]
    assert fill.positions[3].tolist() == [40.0, 0.0]
    # Colours follow their vertices, and the fill copy carries no soundings.
    assert fill.colours.shape == (9, 4)
    assert fill.colours[3].tolist() == scene.colours[6].tolist()
    assert fill.soundings.shape[0] == 0
    # The assembled source scene is untouched.
    assert _kinds(scene) == (
        encmesh.AREA_FILL,
        encmesh.MARKER,
        encmesh.LINE,
        encmesh.AREA_OUTLINE,
    )


def test_filter_marker_keeps_soundings_and_empty_fill_cells():
    scene = _scene()

    marker = filter_view_scene(scene, MARKER_KINDS, keep_soundings=True)

    assert _kinds(marker) == (encmesh.MARKER,)
    assert marker.positions.shape == (3, 2)
    assert marker.positions[0].tolist() == [20.0, 0.0]
    assert [cell.vertex_count for cell in marker.cells] == [3, 0]
    assert [cell.first_vertex for cell in marker.cells] == [0, 3]
    assert marker.soundings is scene.soundings


def test_filter_is_identity_when_every_part_is_kept():
    scene = _scene()
    assert filter_view_scene(scene, FILL_KINDS | MARKER_KINDS) is scene
    assert (
        filter_view_scene(scene, FILL_KINDS | MARKER_KINDS, keep_soundings=False)
        is not scene
    )


def test_enc_mesh_item_owns_only_its_kinds(app):
    scene = _scene()

    fill = EncMeshItem(kinds=FILL_KINDS, text_enabled=False)
    marker = EncMeshItem(kinds=MARKER_KINDS, text_enabled=True)

    assert fill.set_view_scene(scene) is True
    assert marker.set_view_scene(scene) is True
    assert _kinds(fill.view_scene) == (
        encmesh.AREA_FILL,
        encmesh.LINE,
        encmesh.AREA_OUTLINE,
    )
    assert _kinds(marker.view_scene) == (encmesh.MARKER,)
    assert fill.view_scene.soundings.shape[0] == 0
    assert marker.view_scene.soundings is scene.soundings
    assert fill._text_enabled is False and marker._text_enabled is True

    # Re-presenting the same source reuses the filtered copy: the identity
    # check is what keeps the retained-frame swap working.
    fill_first = fill.view_scene
    assert fill.set_view_scene(scene) is False
    assert fill.view_scene is fill_first
    assert marker.set_view_scene(scene) is False


class _FakeItem:
    def __init__(self):
        self.calls = []
        self._x = 0.0
        self._y = 0.0
        self._visible = False
        self._shown = 0
        self._covers = False
        self._reaches = False
        self._cell_nodes = []
        self.labels = ()

    def set_view_scene(self, scene):
        self.calls.append(("set_view_scene", scene))
        return True

    def set_projection(self, projection):
        self.calls.append(("set_projection", projection))

    def set_compose_margin(self, margin):
        self.calls.append(("set_compose_margin", margin))

    def set_scale(self, scale):
        self.calls.append(("set_scale", scale))

    def setVisible(self, visible):
        self._visible = bool(visible)
        self.calls.append(("setVisible", visible))

    def isVisible(self):
        return self._visible

    def setWidth(self, width):
        self.calls.append(("setWidth", width))

    def setHeight(self, height):
        self.calls.append(("setHeight", height))

    def setX(self, x):
        self._x = float(x)

    def setY(self, y):
        self._y = float(y)

    def setProperty(self, name, value):
        self.calls.append(("setProperty", name, value))
        if name == "x":
            self._x = float(value)
        elif name == "y":
            self._y = float(value)

    def property(self, name):
        if name == "x":
            return self._x
        if name == "y":
            return self._y
        return None

    def shown_vertex_count(self):
        return self._shown

    def scene_covers_view(self):
        return self._covers

    def scene_reaches_view(self):
        return self._reaches


def test_split_layers_delegate_scene_state_and_placement():
    fill = _FakeItem()
    marker = _FakeItem()
    lines = _FakeItem()
    fill._shown = 9
    marker._shown = 3
    fill._covers = True
    marker._reaches = True
    layers = EncSceneLayers.split(fill, marker, lines)
    scene = _scene()

    assert layers.set_view_scene(scene) is True
    assert ("set_view_scene", scene) in fill.calls
    assert ("set_view_scene", scene) in marker.calls

    layers.set_projection("p")
    layers.set_compose_margin(24)
    layers.set_scale(1.5)
    for item in (fill, marker):
        assert ("set_projection", "p") in item.calls
        assert ("set_compose_margin", 24) in item.calls
        assert ("set_scale", 1.5) in item.calls

    layers.set_width(640)
    layers.set_height(480)
    assert ("setWidth", 640) in marker.calls
    assert ("setHeight", 480) in marker.calls

    # Placement belongs to the mesh items; the line item mirrors mapContent
    # itself and must not receive the publication offset.
    layers.set_position(12.0, 8.0)
    assert (fill._x, fill._y) == (12.0, 8.0)
    assert (marker._x, marker._y) == (12.0, 8.0)
    assert layers.read_offset() == (12.0, 8.0)
    assert layers.write_offset(3.0, 4.0) == (3.0, 4.0)
    assert (fill._x, fill._y) == (3.0, 4.0)
    assert (marker._x, marker._y) == (3.0, 4.0)
    assert (lines._x, lines._y) == (0.0, 0.0)
    assert not any(call[0] == "setX" for call in lines.calls)

    # Hiding takes the line layer down; showing never presents stale lines.
    lines._visible = True
    layers.set_visible(False)
    assert (fill._visible, marker._visible, lines._visible) == (False, False, False)
    layers.set_visible(True)
    assert (fill._visible, marker._visible) == (True, True)
    assert lines._visible is False
    assert layers.is_visible() is True

    assert layers.shown_vertex_count() == 12
    assert layers.scene_covers_view() is True
    assert layers.scene_reaches_view() is True


def test_split_layers_report_labels_vertices_and_cells():
    """Diagnostics must not read zero labels when the marker item draws them."""
    fill = _FakeItem()
    marker = _FakeItem()
    fill._shown = 4
    marker._shown = 7
    fill._cell_nodes = [object()]
    marker._cell_nodes = [object(), object()]
    fill.labels = ()
    marker.labels = ("S1", "S2")
    layers = EncSceneLayers.split(fill, marker, _FakeItem())

    assert layers.text_labels == ("S1", "S2")
    assert layers.shown_vertex_count() == 11
    assert layers.cell_node_count == 3
    assert layers.meshes == (fill, marker)


def test_viewport_offset_helpers_use_the_layer_surface():
    """The rebase path reads and writes the layer, not a member's property."""
    from sailworks.qt import gribviewport

    fill = _FakeItem()
    marker = _FakeItem()
    lines = _FakeItem()
    layers = EncSceneLayers.split(fill, marker, lines)

    assert gribviewport._item_offset(layers, "x", "y") == (0.0, 0.0)
    assert gribviewport._shift_item_offset(layers, "x", "y", 2.0, 3.0) == (
        2.0,
        3.0,
    )
    assert (fill._x, fill._y) == (2.0, 3.0)
    assert (marker._x, marker._y) == (2.0, 3.0)
    assert (lines._x, lines._y) == (0.0, 0.0)

    assert (
        gribviewport.place_enc_scene_item(
            layers, 7.0, 11.0, cause="publication", camera=None
        )
        is None
    )
    assert layers.read_offset() == (7.0, 11.0)
    assert (lines._x, lines._y) == (0.0, 0.0)


def test_single_wraps_one_mesh_as_the_same_surface():
    mesh = _FakeItem()
    mesh._shown = 5
    mesh._covers = True
    mesh._reaches = True
    mesh._cell_nodes = [object(), object()]
    mesh.labels = ("DEPTH",)
    mesh.view_scene = "one"
    mesh.generation = 7
    mesh.upload_count = 3
    mesh.unsupported_count = 1
    layers = EncSceneLayers.single(mesh)

    assert layers.fill is mesh
    assert layers.marker is None
    assert layers.lines is None
    assert layers.meshes == (mesh,)
    assert layers.set_view_scene("scene") is True
    layers.set_projection("p")
    layers.set_compose_margin(4)
    layers.set_scale(2.0)
    layers.set_width(320)
    layers.set_height(200)
    layers.set_position(9.0, 11.0)
    assert (mesh._x, mesh._y) == (9.0, 11.0)
    assert layers.read_offset() == (9.0, 11.0)
    assert layers.write_offset(1.0, 2.0) == (1.0, 2.0)
    layers.set_visible(True)
    assert layers.is_visible() is True
    assert layers.text_labels == ("DEPTH",)
    assert layers.shown_vertex_count() == 5
    assert layers.scene_covers_view() is True
    assert layers.scene_reaches_view() is True
    assert layers.cell_node_count == 2
    assert layers.view_scene == "one"
    assert layers.generation == 7
    assert layers.upload_count == 3
    assert layers.unsupported_count == 1
    # No attribute forwarding: the wrapper is not the mesh.
    assert not hasattr(layers, "kinds")


def test_the_layer_surface_is_explicit_and_never_shadows_builtins():
    from sailworks.qt import encgpu

    required = (
        "set_view_scene",
        "set_projection",
        "set_compose_margin",
        "set_scale",
        "set_visible",
        "is_visible",
        "set_width",
        "set_height",
        "set_position",
        "read_offset",
        "write_offset",
        "shown_vertex_count",
        "scene_covers_view",
        "scene_reaches_view",
        "cell_node_count",
        "text_labels",
        "view_scene",
    )
    for name in required:
        assert hasattr(EncSceneLayers, name), name

    assert not hasattr(encgpu, "EncScenePair")
    assert "__getattr__" not in vars(EncSceneLayers)
    assert "item_property" not in vars(EncSceneLayers)
    # A method named ``property`` would capture the later decorators in the
    # class body and break the class definition outright.
    assert "property" not in vars(EncSceneLayers)
    assert isinstance(EncSceneLayers.text_labels, builtin_property)
    assert isinstance(EncSceneLayers.cell_node_count, builtin_property)
    assert isinstance(EncSceneLayers.view_scene, builtin_property)
    source = inspect.getsource(EncSceneLayers)
    assert "def __getattr__" not in source
    assert "item_property" not in source


def _slot_container():
    container = QQuickItem()
    slots = {}
    for name in ("encFillSlot", "encLineSlot", "encMarkerSlot"):
        slot = QQuickItem(container)
        slot.setObjectName(name)
        slots[name] = slot
    return container, slots


def test_enc_scene_item_falls_back_to_one_item(app):
    container = QQuickItem()

    layers = enc_scene_item(container)

    assert isinstance(layers, EncSceneLayers)
    assert isinstance(layers.fill, EncMeshItem)
    assert layers.fill.kinds is None
    assert layers.fill._text_enabled is True
    assert layers.marker is None
    assert layers.lines is None
    assert layers.is_visible() is False
    # Requesting the split without a created native item is the fallback too:
    # a mesh pair without its line layer would still draw the CPU stroke.
    fallback = enc_scene_item(container, gpu_lines=True, lines=None)
    assert isinstance(fallback, EncSceneLayers)
    assert isinstance(fallback.fill, EncMeshItem)
    assert fallback.fill.kinds is None


def test_enc_scene_item_parents_each_layer_in_its_slot(app):
    container, slots = _slot_container()
    lines = QQuickItem(container)

    layers = enc_scene_item(container, gpu_lines=True, lines=lines)

    assert isinstance(layers, EncSceneLayers)
    assert layers.fill.parentItem() is slots["encFillSlot"]
    assert layers.marker.parentItem() is slots["encMarkerSlot"]
    assert lines.parentItem() is slots["encLineSlot"]
    assert layers.fill.kinds == FILL_KINDS
    assert layers.marker.kinds == MARKER_KINDS
    assert layers.fill._text_enabled is False
    assert layers.marker._text_enabled is True
    assert layers.is_visible() is False
    assert lines.isVisible() is False

    scene = _scene()
    layers.set_view_scene(scene)
    assert _kinds(layers.fill.view_scene) == (
        encmesh.AREA_FILL,
        encmesh.LINE,
        encmesh.AREA_OUTLINE,
    )
    assert _kinds(layers.marker.view_scene) == (encmesh.MARKER,)
    assert layers.marker.view_scene.soundings is scene.soundings
    assert layers.fill.view_scene.soundings is not scene.soundings


def test_the_scene_qml_declares_the_slots_in_paint_order():
    qml = (
        Path(__file__).resolve().parents[1]
        / "sailworks"
        / "qt"
        / "shaders"
        / "ViewportChartScene.qml"
    ).read_text()
    start = qml.index('objectName: "encSceneLayer"')
    end = qml.index('objectName: "environmentFillBand"', start)
    block = qml[start:end]
    order = [
        block.index('objectName: "encFillSlot"'),
        block.index('objectName: "encLineSlot"'),
        block.index('objectName: "encMarkerSlot"'),
    ]
    assert order == sorted(order), "fill, line, marker must be the paint order"
    assert "hostedItems" in block
    assert "visible: root.encVisible && hostedItems > 0" in block


def test_nothing_orders_the_layers_with_python_z():
    from sailworks.qt import gribviewport

    source = inspect.getsource(gribviewport.enc_scene_item)
    assert "fill.setZ" not in source
    assert "lines.setZ" not in source
    assert "marker.setZ" not in source
