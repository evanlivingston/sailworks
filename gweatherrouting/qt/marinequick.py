"""Marine-owned latest-request worker and batched Qt Quick current geometry."""

import ctypes
from threading import Condition, Thread

import numpy as np
from shiboken6 import delete
from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen
from PySide6.QtQuick import (
    QQuickItem,
    QQuickPaintedItem,
    QSGGeometry,
    QSGGeometryNode,
    QSGNode,
    QSGVertexColorMaterial,
)

from gweatherrouting.rendering.marinesnapshot import (
    current_triangles,
    current_labels,
    sample_currents,
)

_ATTRIBUTES = QSGGeometry.defaultAttributes_ColoredPoint2D()
_VERTEX = np.dtype([("position", "<f4", 2), ("color", "u1", 4)])


class MarineLabelItem(QQuickPaintedItem):
    """Paint station labels in Qt Quick without routing them through Cairo."""

    def __init__(self, parent):
        super().__init__(parent)
        self.labels = ()
        self.setAntialiasing(True)

    def present(self, snapshot, camera):
        self.labels = current_labels(snapshot, camera) if snapshot is not None else ()
        self.setSize(self.parentItem().size())
        self.update()

    def paint(self, painter):
        painter.setFont(QFont("Sans Serif", 10, QFont.Weight.Bold))
        for x, y, text in self.labels:
            path = QPainterPath()
            path.addText(x, y, painter.font(), text)
            painter.setPen(QPen(Qt.GlobalColor.black, 3))
            painter.drawPath(path)
            painter.fillPath(path, Qt.GlobalColor.white)


class MarineCurrentItem(QQuickItem):
    """Only updatePaintNode allocates or mutates scene graph resources."""

    failed = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.setFlag(QQuickItem.Flag.ItemHasContents, True)
        self.vertices = np.empty(0, dtype=_VERTEX)
        self.resources = None

    def present(self, snapshot, camera):
        rows = current_triangles(snapshot, camera) if snapshot is not None else ()
        vertices = np.empty(len(rows), dtype=_VERTEX)
        if rows:
            values = np.asarray(rows)
            vertices["position"] = values[:, :2]
            vertices["color"] = values[:, 2:]
        vertices.setflags(write=False)
        self.vertices = vertices
        self.setSize(self.parentItem().size())
        self.update()

    def updatePaintNode(self, old, _):  # noqa: N802
        if not len(self.vertices):
            self.resources = None
            # Qt hands ownership of old to updatePaintNode. Returning None
            # alone leaves it attached to the scene graph, freezing every
            # previous time layer when loading clears the current geometry.
            if old is not None:
                delete(old)
            return None
        try:
            node = old or QSGGeometryNode()
            if old is None:
                geometry = QSGGeometry(_ATTRIBUTES, len(self.vertices))
                if geometry.sizeOfVertex() != _VERTEX.itemsize:
                    raise RuntimeError("Unsupported QSG colored vertex layout")
                geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
                geometry.setVertexDataPattern(QSGGeometry.DataPattern.DynamicPattern)
                material = QSGVertexColorMaterial()
                node.setGeometry(geometry)
                node.setMaterial(material)
                node.setFlag(QSGNode.Flag.OwnsGeometry)
                node.setFlag(QSGNode.Flag.OwnsMaterial)
                self.resources = (node, _ATTRIBUTES, geometry, material)
            geometry = node.geometry()
            geometry.allocate(len(self.vertices))
            ctypes.memmove(
                int(geometry.vertexData()),
                self.vertices.ctypes.data,
                self.vertices.nbytes,
            )
            node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)
            return node
        except Exception as exc:
            self.failed.emit(str(exc))
            return None


class MarineQuickLayer(QObject):
    """One active request plus one replaceable latest request, no wind staging.

    Worker results are polled on the GUI thread. Workers receive immutable
    marine selections/cameras only, and never call a QQuickItem method.
    """

    stateChanged = Signal(str)  # noqa: N815

    def __init__(self, root, parent):
        super().__init__(parent)
        self.item = MarineCurrentItem(root)
        self.labels = MarineLabelItem(root)
        barbs = root.findChild(QQuickItem, "weatherBarbs")
        if barbs is not None:
            self.item.stackBefore(barbs)
            self.labels.stackBefore(barbs)
        self.state = "unavailable"
        self.snapshot = None
        self.camera = None
        self._key = None
        self._content = None
        self._condition = Condition()
        self._generation = 0
        self._pending = None
        self._completed = None
        self._closed = False
        self.error = ""
        self.item.failed.connect(self._fail)
        self._thread = Thread(target=self._run, name="marine-quick", daemon=True)
        self._thread.start()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._publish)
        self._timer.start()

    def set_camera(self, camera):
        self.camera = camera
        self._present(self.snapshot, camera)
        if self._content is not None:
            self.request(*self._content, camera)

    def request(self, sources, instant, visible, camera):
        if self._closed or self.error:
            return False
        content = (sources, instant, visible)
        key = (
            id(sources),
            instant,
            visible,
            camera.center_lat,
            camera.center_lon,
            camera.zoom,
            camera.width,
            camera.height,
        )
        if key == self._key:
            return True
        # A time-only request keeps the previous completed frame visible while
        # the worker samples its replacement. Source/visibility changes must
        # clear immediately because the previous content is no longer valid.
        same_sources = self._content is not None and self._content[0] is sources
        if self._content != content and (not same_sources or not visible):
            self.snapshot = None
            self._present(None, camera)
            if self.error:
                return False
        self.camera, self._content, self._key = camera, content, key
        with self._condition:
            self._generation += 1
            self._completed = None
            self._pending = (
                (self._generation, sources, instant, camera) if visible else None
            )
            self._condition.notify()
        showing_previous = same_sources and self.snapshot is not None and visible
        self.state = (
            "loading (showing previous frame)"
            if showing_previous
            else "loading" if visible else "hidden"
        )
        self.stateChanged.emit(self.state)
        if not visible:
            self.snapshot = None
            self._present(None, camera)
        return not self.error

    def _present(self, snapshot, camera):
        if self._closed or self.error:
            return
        try:
            self.item.present(snapshot, camera)
            self.labels.present(snapshot, camera)
        except Exception as exc:
            self._fail(str(exc))

    def _fail(self, reason):
        if self._closed or self.error:
            return
        self.error = reason or "Marine Quick rendering failed"
        with self._condition:
            self._generation += 1
            self._pending = self._completed = None
        self.snapshot = None
        self.item.setVisible(False)
        self.labels.present(None, self.camera)
        self.labels.setVisible(False)
        self.state = "Marine rendering unavailable: " + self.error
        self.stateChanged.emit(self.state)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._closed or self._pending is not None
                )
                if self._closed:
                    return
                generation, sources, instant, camera = self._pending
                self._pending = None

            def cancelled():
                with self._condition:
                    return self._closed or generation != self._generation

            try:
                snapshot = sample_currents(sources, instant, camera, cancelled)
                result = (snapshot, "" if snapshot is None else snapshot.state)
            except Exception as exc:
                result = (None, "unavailable: " + str(exc))
            with self._condition:
                if not cancelled():
                    self._completed = (generation, *result)

    def _publish(self):
        with self._condition:
            result, self._completed = self._completed, None
        if result is None or self._closed or result[0] != self._generation:
            return
        _, self.snapshot, self.state = result
        if self.state.startswith("unavailable:"):
            self._fail(self.state)
            return
        self._present(self.snapshot, self.camera)
        self.stateChanged.emit(self.state)

    def close(self):
        if self._closed:
            return
        self._timer.stop()
        with self._condition:
            self._closed = True
            self._generation += 1
            self._pending = self._completed = None
            self._condition.notify()
        self.snapshot = None
        self.item.setVisible(False)
