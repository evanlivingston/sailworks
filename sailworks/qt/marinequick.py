"""Marine-owned latest-request worker and batched Qt Quick current geometry."""

import ctypes
from math import asinh, ceil, floor, pi, radians, tan
from threading import Condition, Thread

import numpy as np
from shiboken6 import delete
from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtGui import QFont, QPainterPath, QPen
from PySide6.QtQuick import (
    QQuickItem,
    QQuickPaintedItem,
    QSGGeometry,
    QSGGeometryNode,
    QSGNode,
    QSGVertexColorMaterial,
)

from sailworks.rendering.marinesnapshot import (
    current_triangles,
    current_labels,
    sample_currents,
)

_ATTRIBS = QSGGeometry.defaultAttributes_ColoredPoint2D()
_VERTEX = np.dtype([("position", "<f4", 2), ("color", "u1", 4)])


def _membership_key(sources, camera):
    """Identify the current-view candidates without sampling their values.

    This mirrors the bounds in sample_currents. A change in any candidate can
    change the result, even if that candidate later proves unavailable.
    """
    from sailworks.core.mapviewport import MapPoint, MapViewport
    from sailworks.rendering.marinepresentation import current_grid_spacing

    view = camera.view()
    grid = None
    if sources.fields:
        level = max(0, floor(camera.zoom + 0.5))
        cells = max(
            1,
            round(
                MapViewport.TILE_SIZE * 2**level / current_grid_spacing(sources.density)
            ),
        )
        world = MapViewport.TILE_SIZE * 2**camera.zoom
        cx = (camera.center_lon + 180) / 360
        latitude = max(
            -MapViewport.MAX_LAT, min(MapViewport.MAX_LAT, camera.center_lat)
        )
        cy = (1 - asinh(tan(radians(latitude))) / pi) / 2
        left, right = cx - camera.width / (2 * world), cx + camera.width / (2 * world)
        top = max(0, cy - camera.height / (2 * world))
        bottom = min(1, cy + camera.height / (2 * world))
        columns = tuple(
            dict.fromkeys(
                i % cells for i in range(ceil(left * cells), floor(right * cells) + 1)
            )
        )
        grid = (cells, columns, ceil(top * cells), floor(bottom * cells))

    groups = []
    for selection in (sources.currents, sources.levels):
        seen, visible = set(), []
        for dataset in selection.datasets:
            station = dataset.station
            if station.id in seen:
                continue
            seen.add(station.id)
            lon = (
                camera.center_lon
                + (station.longitude - camera.center_lon + 180) % 360
                - 180
            )
            x, y = view.convert_geographic_to_screen(MapPoint(station.latitude, lon))
            if 0 <= x <= camera.width and 0 <= y <= camera.height:
                visible.append(station.id)
        groups.append(tuple(visible))
    return grid, *groups


def _projection_margin(reference, camera, minimum):
    """Include geometry visible after the parent moves the reference frame."""
    if reference is None or camera is None:
        return minimum
    from sailworks.qt.viewporttransform import PanReference

    try:
        dx, dy = PanReference.from_camera(reference).translation_to(camera)
    except (AttributeError, TypeError, ValueError):
        return minimum
    return minimum + max(abs(dx), abs(dy))


class MarineLabelItem(QQuickPaintedItem):
    """Paint station labels in Qt Quick without routing them through Cairo."""

    def __init__(self, parent):
        super().__init__(parent)
        self.labels = ()
        self.setAntialiasing(True)

    def present(self, snapshot, ref_camera, camera=None):
        # Project labels against reference camera; parent mapContent pan handles translation
        if camera is None:
            camera = ref_camera
        padding = _projection_margin(ref_camera, camera, 64)
        labels = (
            current_labels(snapshot, ref_camera, margin=padding)
            if snapshot is not None
            else ()
        )
        self.labels = tuple((x + padding, y + padding, text) for x, y, text in labels)
        self.setX(-padding)
        self.setY(-padding)
        size = self.parentItem().size()
        self.setWidth(size.width() + 2 * padding)
        self.setHeight(size.height() + 2 * padding)
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

    def present(self, snapshot, ref_camera, camera=None):
        # Project geometry against the reference camera and paint it into a
        # padded surface; the shared mapContent parent supplies the pan, and
        # the stationary clip decides what is visible.
        if camera is None:
            camera = ref_camera
        padding = _projection_margin(ref_camera, camera, 40)
        rows = (
            current_triangles(snapshot, ref_camera, margin=padding)
            if snapshot is not None
            else ()
        )
        vertices = np.empty(len(rows), dtype=_VERTEX)
        if rows:
            values = np.asarray(rows)
            vertices["position"] = values[:, :2] + padding
            vertices["color"] = values[:, 2:]
        vertices.setflags(write=False)
        self.vertices = vertices
        self.setX(-padding)
        self.setY(-padding)
        size = self.parentItem().size()
        self.setWidth(size.width() + 2 * padding)
        self.setHeight(size.height() + 2 * padding)
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
                geometry = QSGGeometry(_ATTRIBS, len(self.vertices))
                if geometry.sizeOfVertex() != _VERTEX.itemsize:
                    raise RuntimeError("Unsupported QSG colored vertex layout")
                geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
                geometry.setVertexDataPattern(QSGGeometry.DataPattern.DynamicPattern)
                material = QSGVertexColorMaterial()
                node.setGeometry(geometry)
                node.setMaterial(material)
                node.setFlag(QSGNode.Flag.OwnsGeometry)
                node.setFlag(QSGNode.Flag.OwnsMaterial)
                self.resources = (node, _ATTRIBS, geometry, material)
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
        # Reference camera for shared pan: geometry is projected against this
        # camera and the parent mapContent translation handles pan.
        self._ref_camera = None
        self.item.failed.connect(self._fail)
        self._thread = Thread(target=self._run, name="marine-quick", daemon=True)
        self._thread.start()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._publish)
        self._timer.start()

    def set_camera(self, camera):
        self.camera = camera
        # A chart scene moves under the viewport's map reference. Other
        # scenes are stationary and must project at the current camera.
        viewport = self.parent()
        reference = (
            getattr(viewport, "_map_anchor_camera", None)
            if getattr(viewport, "_map_content", None) is not None
            else None
        )
        old_reference = self._ref_camera
        self._ref_camera = reference or camera
        # Present using reference-local projection
        if old_reference != self._ref_camera or self._content is None:
            self._present(self.snapshot, camera)
        if self._content is not None:
            self.request(*self._content, camera)

    def request(self, sources, instant, visible, camera):
        if self._closed or self.error:
            return False
        content = (sources, instant, visible)
        try:
            membership = _membership_key(sources, camera)
        except (AttributeError, TypeError, ValueError, OverflowError):
            # Unknown source contracts cannot prove selection invariance.
            membership = (camera.center_lat, camera.center_lon)
        key = (
            id(sources),
            instant,
            visible,
            camera.zoom,
            camera.width,
            camera.height,
            membership,
        )
        if key == self._key:
            return True
        # A time-only request keeps the previous completed frame visible while
        # the worker samples its replacement. Source/visibility changes must
        # clear immediately because the previous content is no longer valid.
        same_sources = self._content is not None and self._content[0] is sources
        frame_changed = self._key is not None and self._key[3:6] != key[3:6]
        if frame_changed or (
            self._content != content and (not same_sources or not visible)
        ):
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
            # Use reference camera for projection; parent mapContent handles pan
            ref_camera = self._ref_camera if self._ref_camera else camera
            self.item.present(snapshot, ref_camera, camera)
            self.labels.present(snapshot, ref_camera, camera)
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
