"""Offscreen smoke for the opt-in GPU ENC scene in the Qt viewport.

The wiring pass made three guesses that nothing loaded: that
``findChild(QQuickItem, "encSceneLayer")`` resolves the QML container, that an
``EncMeshItem`` parented to that container is sized by the wiring and presents
a published :class:`ViewScene`, and that the default path (the scene flag off)
has nothing to show.  This loads the real ``ViewportChartScene.qml`` offscreen
and checks all three against real Qt objects.

A frame is observed through ``afterRendering`` only: no pixel readback, no
frame timing and no FPS claim.  The item's construction goes through the
viewport's own :func:`enc_scene_item`, so the test exercises the production
wiring instead of a copy of it.
"""

import os

# Must be set before PySide6 is imported so the offscreen plugin is selected.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402
from pathlib import Path  # noqa: E402
from threading import Lock  # noqa: E402
from types import MethodType, SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import QObject, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQuick import QQuickItem, QQuickView  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sailworks.qt.gribviewport import (  # noqa: E402
    _viewport_widget_class,
    enc_scene_item,
    viewport_layer_flags,
)
from sailworks.rendering.encview import CellSpan, ViewScene  # noqa: E402
from sailworks.rendering.vectordrawer import encmesh  # noqa: E402
from sailworks.rendering.vectordrawer.encmesh import MeshPart  # noqa: E402

pytestmark = pytest.mark.qt

SHADERS = Path(__file__).resolve().parents[1] / "sailworks/qt/shaders"
SCENE = SHADERS / "ViewportChartScene.qml"
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


class _Harness:
    """The viewport's own ENC-scene slot methods, without the rest of it."""

    def __init__(self, item):
        self._enc_scene_item = item
        self._enc_scene_state = "idle"
        self._enc_scene_generation = None
        self._enc_scene_camera = None
        # A publication resolves any deferred raster composition against the
        # widget's camera; no chart composition runs in this harness.
        self._camera = None
        self._chart_timer = None
        # No retained pan reference in this headless harness, so the published
        # scene is placed at the item origin.
        self._map_pan_reference = None
        self._camera_dpr = None
        self._disposed = False
        self.status_updates = 0


def _harness(item):
    harness = _Harness(item)
    cls = _viewport_widget_class()
    for name in (
        "_withdraw_enc_scene",
        "_enc_scene_published",
        "_enc_scene_state_changed",
        "_place_enc_scene",
    ):
        setattr(harness, name, MethodType(getattr(cls, name), harness))
    harness._update_enc_status = lambda: setattr(
        harness, "status_updates", harness.status_updates + 1
    )
    return harness


def _wait_for(predicate, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(5)
    return predicate()


def _wait_frames(frames, start, needed):
    return _wait_for(lambda: frames.rendered() - start >= needed)


def _load_scene():
    """Load the real viewport scene offscreen, returning its ENC container."""
    view = QQuickView()
    view.setSource(QUrl.fromLocalFile(str(SCENE)))
    assert not view.errors(), [str(error.toString()) for error in view.errors()]
    root = view.rootObject()
    assert root is not None, "ViewportChartScene.qml has no root object"
    view.resize(96, 96)
    view.show()
    # A QQuickView root follows the view, but the offscreen plugin may
    # deliver that resize after the first polish; set it explicitly too.
    root.setProperty("width", 96)
    root.setProperty("height", 96)
    container = root.findChild(QQuickItem, "encSceneLayer")
    assert container is not None, "ViewportChartScene.qml has no encSceneLayer"
    assert _wait_for(
        lambda: container.width() > 0 and container.height() > 0
    ), f"encSceneLayer never took the scene's size ({container.size()})"
    return view, root, container


def _view_scene():
    """One small synthetic scene: two cells, nine vertices, in plan order."""
    positions = np.array(
        [(0, 0), (8, 0), (0, 8), (8, 0), (8, 8), (0, 8), (20, 0), (28, 0), (20, 8)],
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


def _release(view):
    view.close()
    try:
        view.releaseResources()
    except RuntimeError:
        pass


def test_the_enc_scene_container_resolves_from_the_real_scene(app):
    """The ``findChild`` lookup the viewport depends on, on the real QML."""
    view, root, container = _load_scene()
    try:
        # A QML ``Item`` is a QQuickItem; both lookups must find the same one.
        assert root.findChild(QObject, "encSceneLayer") is container
        assert isinstance(container, QQuickItem)
        assert container.objectName() == "encSceneLayer"
        # It fills the map band, and it holds nothing until the viewport
        # parents the scene item there.
        assert container.width() == 96 and container.height() == 96
        assert container.childItems() == []
    finally:
        _release(view)


def test_a_published_scene_reaches_the_render_thread(app):
    """The item is sized by the wiring, then drawn once a scene is published."""
    view, _root, container = _load_scene()
    frames = _Frames()
    handler = frames.on_rendered
    view.afterRendering.connect(handler, Qt.ConnectionType.DirectConnection)
    item = enc_scene_item(container)
    harness = _harness(item)
    try:
        assert not item.isVisible(), "the item must start hidden"
        assert item.width() == container.width() > 0
        assert item.height() == container.height() > 0
        assert container.isVisible(), "a parented item must show its container"

        scene = _view_scene()
        start = frames.rendered()
        harness._enc_scene_published((1, scene, {}))
        assert item.isVisible()
        assert item.view_scene is scene
        assert harness._enc_scene_generation == 1

        view.update()
        assert _wait_frames(frames, start, 1), (
            "afterRendering never fired for the published scene "
            f"(sceneGraphError={view.sceneGraphErrors() if hasattr(view, 'sceneGraphErrors') else 'n/a'})"
        )
        assert item._built_colored and item._built_static
        # The static path uploads one geometry node per contributing cell under
        # one transform node: six vertices for A's two triangles, three for B's
        # single triangle.  A dropped cell loses a node, a truncated one loses
        # vertices, and either breaks the sum below.
        cell_vertices = [node.geometry().vertexCount() for node in item._cell_nodes]
        assert len(cell_vertices) == len(scene.cells) == 2
        assert cell_vertices == [6, 3]
        assert item._content_node is not None
        assert item._content_node.childCount() == len(cell_vertices)
        assert sum(cell_vertices) == scene.vertex_count == 9
        assert sum(cell_vertices) // 3 == len(scene.positions) // 3 == 3

        # A layer state that leaves nothing current keeps the last complete
        # frame: it is a better report than a blank chart.  The scene the item
        # already draws stays presented.
        harness._enc_scene_state_changed("unavailable")
        assert harness._enc_scene_state == "unavailable"
        assert item.isVisible()
        assert item.view_scene is scene
    finally:
        view.afterRendering.disconnect(handler)
        item.setParentItem(None)
        _release(view)


def test_the_default_path_creates_no_item_and_hides_the_container(app):
    """With the scene flag off the viewport never parents an item."""
    flags = viewport_layer_flags(
        SimpleNamespace(chart_layer=False, enc_chart_layer=True, enc_scene_layer=False)
    )
    # The raster ENC layer implies the chart layer, exactly as the launcher
    # did before the flags were resolved in one place.
    assert flags == (True, True, False)
    # The scene layer implies the chart layer and supersedes the raster
    # source, so the flag off is the only path that parents no item.
    scene_flags = viewport_layer_flags(
        SimpleNamespace(chart_layer=False, enc_chart_layer=True, enc_scene_layer=True)
    )
    assert scene_flags == (True, False, True)

    view, _root, container = _load_scene()
    try:
        assert container.childItems() == []
        assert not container.isVisible(), "an empty container must stay hidden"
    finally:
        _release(view)
