"""Marine-owned latest-request worker and image-backed Qt Quick portrayal.

The native viewport renders every layer without a Python QQuickItem subclass:
a Python item makes the scene-graph sync take the GIL on the render thread,
which can deadlock the threaded render loop.  Current glyphs and labels are
therefore painted into a QImage on the worker's publish tick and exposed to
QML through a QQuickImageProvider, exactly like the weather and streamline
images.
"""

from math import asinh, ceil, floor, pi, radians, tan
from threading import Condition, Thread

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtQuick import QQuickImageProvider

from sailworks.rendering.marinesnapshot import (
    current_triangles,
    current_labels,
    sample_currents,
)

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


class MarineImageProvider(QQuickImageProvider):
    """Retain the current painted surfaces for the QML Image items.

    Publish happens on the GUI thread; requestImage may run on the scene-graph
    thread.  A couple of superseded tokens stay servable while QML swaps the
    source, then the oldest is discarded.
    """

    def __init__(self, retained=4):
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._images = {}
        self._order = []
        self._retained = max(1, int(retained))
        self._generation = 0

    def publish(self, image):
        token = str(self._generation)
        self._generation += 1
        self._images[token] = image
        self._order.append(token)
        while len(self._order) > self._retained:
            self._images.pop(self._order.pop(0), None)
        return f"image://marine/{token}"

    def requestImage(self, image_id, size, requested_size):  # noqa: N802
        image = QImage(self._images.get(image_id, QImage()))
        if size is not None:
            size.setWidth(image.width())
            size.setHeight(image.height())
        return image

    def clear(self):
        self._images.clear()
        self._order.clear()


class _Surface(QObject):
    """Pure-Python painting surface; no QQuickItem override is involved."""

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self._root = root
        self._visible = False
        self._x = 0.0
        self._y = 0.0
        self._width = 0.0
        self._height = 0.0
        self.image = QImage()

    def parentItem(self):  # noqa: N802 - mirrors the Quick API
        return self._root

    def x(self):
        return self._x

    def y(self):
        return self._y

    def setX(self, value):  # noqa: N802 - mirrors the Quick API
        self._x = float(value)

    def setY(self, value):  # noqa: N802 - mirrors the Quick API
        self._y = float(value)

    def width(self):
        return self._width

    def height(self):
        return self._height

    def setWidth(self, value):  # noqa: N802 - mirrors the Quick API
        self._width = float(value)

    def setHeight(self, value):  # noqa: N802 - mirrors the Quick API
        self._height = float(value)

    def setVisible(self, visible):  # noqa: N802 - mirrors the Quick API
        self._visible = bool(visible)

    def isVisible(self):  # noqa: N802 - mirrors the Quick API
        return self._visible

    def _resize(self, padding):
        size = self._root.size() if self._root is not None else None
        width = size.width() if size is not None else 0.0
        height = size.height() if size is not None else 0.0
        self.setX(-padding)
        self.setY(-padding)
        self.setWidth(width + 2 * padding)
        self.setHeight(height + 2 * padding)


def _surface_image(width, height):
    image = QImage(
        max(1, int(round(width))),
        max(1, int(round(height))),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(Qt.GlobalColor.transparent)
    return image


def paint_currents(rows, width, height):
    """Paint the colored current triangles into one ARGB surface."""
    image = _surface_image(width, height)
    if not len(rows):
        return image
    values = np.asarray(rows)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        # Group by uniform color: each triangle carries one RGBA per vertex.
        paths = {}
        for index in range(0, len(values) - 2, 3):
            corners = values[index : index + 3]
            rgba = tuple(int(channel) for channel in corners[0][2:6])
            path = paths.get(rgba)
            if path is None:
                path = paths[rgba] = QPainterPath()
            path.moveTo(float(corners[0][0]), float(corners[0][1]))
            path.lineTo(float(corners[1][0]), float(corners[1][1]))
            path.lineTo(float(corners[2][0]), float(corners[2][1]))
            path.closeSubpath()
        for (red, green, blue, alpha), path in paths.items():
            painter.fillPath(path, QColor(red, green, blue, alpha))
    finally:
        painter.end()
    return image


def paint_labels(labels, width, height):
    """Paint station labels with the same outline and white fill as before."""
    image = _surface_image(width, height)
    if not labels:
        return image
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = painter.font()
        font.setPointSize(10)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        for x, y, text in labels:
            path = QPainterPath()
            path.addText(float(x), float(y), painter.font(), text)
            painter.setPen(QPen(Qt.GlobalColor.black, 3))
            painter.drawPath(path)
            painter.fillPath(path, Qt.GlobalColor.white)
    finally:
        painter.end()
    return image


class MarineLabelItem(_Surface):
    """Station labels as a painted image surface."""

    def present(self, snapshot, ref_camera, camera=None):
        if camera is None:
            camera = ref_camera
        padding = _projection_margin(ref_camera, camera, 64)
        labels = (
            current_labels(snapshot, ref_camera, margin=padding)
            if snapshot is not None
            else ()
        )
        self.labels = tuple((x + padding, y + padding, text) for x, y, text in labels)
        self._resize(padding)
        self.image = paint_labels(self.labels, self.width(), self.height())
        self.setVisible(snapshot is not None and bool(self.labels))


class MarineCurrentItem(_Surface):
    """Current triangles as a painted image surface."""

    failed = Signal(str)

    def __init__(self, root, parent=None):
        super().__init__(root, parent)
        self.vertices = np.empty(0, dtype=_VERTEX)

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
        self._resize(padding)
        image_rows = np.empty((0, 6), dtype=float)
        if rows:
            image_rows = np.asarray(rows, dtype=float).copy()
            image_rows[:, :2] += padding
        try:
            self.image = paint_currents(image_rows, self.width(), self.height())
        except Exception as exc:
            self.failed.emit(str(exc))
            self.image = QImage()
        self.setVisible(snapshot is not None and len(vertices) > 0)


class MarineQuickLayer(QObject):
    """One active request plus one replaceable latest request, no wind staging.

    Worker results are polled on the GUI thread. Workers receive immutable
    marine selections/cameras only, and never touch a QQuickItem.
    """

    stateChanged = Signal(str)  # noqa: N815

    def __init__(self, root, parent, provider=None):
        super().__init__(parent)
        self.root = root
        self.provider = provider or MarineImageProvider()
        self.item = MarineCurrentItem(root, self)
        self.labels = MarineLabelItem(root, self)
        self._current_view = self._find("marineCurrents")
        self._label_view = self._find("marineLabels")
        self._published = {}
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

    def _find(self, name):
        finder = getattr(self.root, "findChild", None)
        if finder is None:
            return None
        try:
            return finder(QObject, name)
        except TypeError:
            return None

    def _present(self, snapshot, camera):
        if self._closed or self.error:
            return
        try:
            # Use reference camera for projection; parent mapContent handles pan
            ref_camera = self._ref_camera if self._ref_camera else camera
            self.item.present(snapshot, ref_camera, camera)
            self.labels.present(snapshot, ref_camera, camera)
            self._publish_views()
        except Exception as exc:
            self._fail(str(exc))

    def _publish_views(self):
        for surface, view in (
            (self.item, self._current_view),
            (self.labels, self._label_view),
        ):
            if view is None:
                continue
            visible = surface.isVisible() and not surface.image.isNull()
            if visible:
                view.setProperty("x", surface.x())
                view.setProperty("y", surface.y())
                view.setProperty("width", surface.width())
                view.setProperty("height", surface.height())
                if self._published.get(id(surface)) is not surface.image:
                    view.setProperty("source", self.provider.publish(surface.image))
                    self._published[id(surface)] = surface.image
            view.setProperty("visible", visible)

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
        self._publish_views()
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
        self.labels.setVisible(False)
        self._publish_views()
