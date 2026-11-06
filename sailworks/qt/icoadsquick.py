"""Image-backed Quick layer for cached ICOADS ship observations.

Reports are painted on the GUI thread into a QImage and published through a
QQuickImageProvider.  Keeping the layer free of Python QQuickItem subclasses
avoids the render-thread GIL lookup that deadlocks the native viewport.
"""

from datetime import datetime, timezone

from PySide6.QtCore import QObject, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QImage,
    QPainter,
    QPen,
)
from PySide6.QtQuick import QQuickImageProvider

DISPLAY_LOD_THRESHOLD = 2000


class IcoadsImageProvider(QQuickImageProvider):
    """Retain the latest painted marker surfaces for the QML Image item."""

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
        return f"image://icoads/{token}"

    def requestImage(self, image_id, size, requested_size):  # noqa: N802
        image = QImage(self._images.get(image_id, QImage()))
        if size is not None:
            size.setWidth(image.width())
            size.setHeight(image.height())
        return image

    def clear(self):
        self._images.clear()
        self._order.clear()


class IcoadsQuickItem(QObject):
    """Paint neutral report markers; reports are observations, not live AIS."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = parent
        self._reports = ()
        self._positions_key = None
        self._positions_cache = ()
        self._positions_camera = None
        self._camera = None
        self._ref_camera = None
        self._offset_x = 0.0
        self._offset_y = 0.0
        self._padding = 0.0
        self._on_select = None
        self._selected_report = None
        self._visible = False
        self._x = 0.0
        self._y = 0.0
        self._width = 0.0
        self._height = 0.0
        self._cursor = QCursor()
        self.image = QImage()
        self._dirty = True

    # -------------------------------------------------- Quick-like geometry
    def parentItem(self):  # noqa: N802 - mirrors the Quick API
        return self._root

    def x(self):
        return self._x

    def y(self):
        return self._y

    def setX(self, value):  # noqa: N802
        self._x = float(value)

    def setY(self, value):  # noqa: N802
        self._y = float(value)

    def width(self):
        return self._width

    def height(self):
        return self._height

    def setWidth(self, value):  # noqa: N802
        self._width = float(value)

    def setHeight(self, value):  # noqa: N802
        self._height = float(value)

    def setSize(self, size):  # noqa: N802
        self.setWidth(size.width())
        self.setHeight(size.height())

    def setVisible(self, visible):  # noqa: N802
        self._visible = bool(visible)

    def isVisible(self):  # noqa: N802
        return self._visible

    def update(self):
        """Geometry changed; the owning layer repaints after set_camera."""
        self._dirty = True

    def cursor(self):
        return self._cursor

    def setCursor(self, cursor):  # noqa: N802
        self._cursor = QCursor(cursor)

    def unsetCursor(self):  # noqa: N802
        self._cursor = QCursor()

    def mapToItem(self, target, point):  # noqa: N802 - mirrors the Quick API
        mapped = QPointF(point.x() + self._x, point.y() + self._y)
        node = self._root
        while node is not None and node is not target:
            try:
                mapped += QPointF(node.x(), node.y())
                node = node.parentItem()
            except (AttributeError, TypeError):
                break
        return mapped

    # ----------------------------------------------------------- portrayal
    def set_reports(self, reports, camera=None, on_select=None):
        self._reports = tuple(reports or ())
        self._positions_key = None
        if camera is not None:
            self._camera = camera
            if self._ref_camera is None:
                self._ref_camera = camera
        self._on_select = on_select
        self.update()

    def set_camera(self, camera, reference=None):
        old_reference = self._ref_camera
        old_padding = self._padding
        if reference is not None:
            self._ref_camera = reference
        elif self._ref_camera is None:
            self._ref_camera = camera
        # Update reference camera if zoom/size changed
        if self._ref_camera is not None and (
            self._ref_camera.zoom != camera.zoom
            or self._ref_camera.width != camera.width
            or self._ref_camera.height != camera.height
        ):
            self._ref_camera = camera
        self._camera = camera
        # Compute pan offset for covered pan
        if self._ref_camera is not camera:
            try:
                from sailworks.qt.viewporttransform import PanReference

                ref_pan = PanReference.from_camera(self._ref_camera)
                dx, dy = ref_pan.translation_to(camera)
                self._offset_x, self._offset_y = dx, dy
            except (ValueError, TypeError):
                self._offset_x, self._offset_y = 0.0, 0.0
        else:
            self._offset_x, self._offset_y = 0.0, 0.0
        # A translated viewport-sized painted texture clips markers before
        # they reach the stationary map clip. Cover the shared pan window.
        self._padding = (
            0.5 * max(camera.width, camera.height) + 40
            if reference is not None and self.parentItem() is not None
            else 0.0
        )
        self.setX(-self._padding)
        self.setY(-self._padding)
        if self._padding:
            self.setWidth(camera.width + 2 * self._padding)
            self.setHeight(camera.height + 2 * self._padding)
        if (
            self._ref_camera != old_reference
            or self._padding != old_padding
            or self._selected_report is not None
        ):
            self.update()

    def _positions(self):
        if self._ref_camera is None:
            return ()
        camera = self._ref_camera
        key = (
            id(self._reports),
            len(self._reports),
            id(camera),
            *(
                getattr(camera, name, None)
                for name in (
                    "center_lat",
                    "center_lon",
                    "zoom",
                    "width",
                    "height",
                    "time",
                )
            ),
            self._padding,
            self.width(),
            self.height(),
            id(self._selected_report),
        )
        if camera is self._positions_camera and key == self._positions_key:
            return self._positions_cache
        from sailworks.core.mapviewport import MapPoint, MercatorProjection

        view = camera.view()
        bounds = None
        if isinstance(view, MercatorProjection):
            top, left = view.convert_screen_to_geographic(
                -self._padding, -self._padding
            ).get_degrees()
            bottom, right = view.convert_screen_to_geographic(
                self.width() - self._padding, self.height() - self._padding
            ).get_degrees()
            # Roundoff in the inverse must never reject an edge marker.
            epsilon = 1e-9
            bounds = (bottom - epsilon, top + epsilon, left - epsilon, right + epsilon)

        result = []
        for report in self._reports:
            try:
                if bounds is not None:
                    bottom, top, left, right = bounds
                    lat = max(-view.MAX_LAT, min(view.MAX_LAT, float(report.latitude)))
                    lon = float(report.longitude)
                    if lat < bottom or lat > top:
                        continue
                    # Include wrapped candidates conservatively. The existing
                    # projection itself is unwrapped, and remains authoritative.
                    if right - left < 360 and not (
                        left <= lon <= right
                        or left <= lon - 360 <= right
                        or left <= lon + 360 <= right
                    ):
                        continue
                x, y = view.convert_geographic_to_screen(
                    MapPoint(report.latitude, report.longitude)
                )
                # Paint the padded reference surface once; the stationary map
                # clip decides which portion is visible as its parent pans.
                local_x, local_y = x + self._padding, y + self._padding
                if 0 <= local_x <= self.width() and 0 <= local_y <= self.height():
                    result.append((local_x, local_y, report))
            except (AttributeError, TypeError, ValueError):
                continue
        if len(result) > DISPLAY_LOD_THRESHOLD:
            stride = (len(result) + DISPLAY_LOD_THRESHOLD - 1) // DISPLAY_LOD_THRESHOLD
            last = len(result) - 1
            result = [
                entry
                for index, entry in enumerate(result)
                if index % stride == 0
                or index == last
                or entry[2] is self._selected_report
            ]
        self._positions_cache = tuple(result)
        self._positions_key = key
        self._positions_camera = camera
        return self._positions_cache

    def paint(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#202a34"), 1.4))
        painter.setBrush(QColor("#f2c14e"))
        for x, y, _report in self._positions():
            painter.drawEllipse(int(x) - 4, int(y) - 4, 8, 8)
        if self._selected_report is not None:
            self._paint_details(
                painter,
                self._selected_report,
                self._padding - self._offset_x,
                self._padding - self._offset_y,
            )

    def render(self):
        image = QImage(
            max(1, int(round(self.width()))),
            max(1, int(round(self.height()))),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            self.paint(painter)
        finally:
            painter.end()
        self.image = image
        self._dirty = False
        return image

    @staticmethod
    def _detail_lines(report):
        age = datetime.now(timezone.utc) - report.observed_at
        return (
            "NOAA ICOADS \u00b7 delayed ship observation",
            f"Observed {report.observed_at:%Y-%m-%d %H:%M UTC}",
            f"Age: {max(0, int(age.total_seconds() // 3600))} h",
            *(f"{key}: {value}" for key, value in report.measurements),
        )

    @classmethod
    def _paint_details(cls, painter, report, offset_x=0, offset_y=0):
        lines = cls._detail_lines(report)
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        padding = 10
        width = max(metrics.horizontalAdvance(line) for line in lines) + 2 * padding
        height = len(lines) * metrics.height() + 2 * padding
        rect = QRectF(12 + offset_x, 12 + offset_y, width, height)
        painter.setPen(QPen(QColor("#e7edf2"), 1))
        painter.setBrush(QColor(20, 32, 40, 238))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(QColor("#f4f6f8"))
        baseline = rect.top() + padding + metrics.ascent()
        for index, line in enumerate(lines):
            painter.drawText(
                rect.left() + padding,
                baseline + index * metrics.height(),
                line,
            )

    def _report_at(self, x, y):
        """Hit-test displayed markers, including the selected marker if visible."""
        nearest = min(
            self._positions(),
            key=lambda item: (item[0] - x) ** 2 + (item[1] - y) ** 2,
            default=None,
        )
        if nearest and (nearest[0] - x) ** 2 + (nearest[1] - y) ** 2 <= 100:
            return nearest[2]
        return None

    def select_at(self, x, y):
        """Select the report under a scene point; return it when it changed."""
        report = self._report_at(x, y)
        if report is None or report is self._selected_report:
            return None
        self._selected_report = report
        if self._on_select is not None:
            self._on_select(report)
        return report

    def hoverMoveEvent(self, event):  # noqa: N802
        try:
            point = event.position()
            report = self._report_at(point.x(), point.y())
            if report is not None:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                if report is not self._selected_report:
                    self._selected_report = report
                    self.update()
            else:
                self.unsetCursor()
        except Exception:
            self.unsetCursor()

    def hoverLeaveEvent(self, event):  # noqa: N802
        self.unsetCursor()

    def mousePressEvent(self, event):
        try:
            point = event.position()
            report = self._report_at(point.x(), point.y())
            if report is not None:
                self._selected_report = report
                self.update()
                if self._on_select is not None:
                    self._on_select(report)
                event.accept()
                return
        except Exception:
            event.ignore()
            return
        event.ignore()


class IcoadsQuickLayer(QObject):
    """Present cached observations independently from forecast weather."""

    def __init__(self, root, parent, provider=None):
        super().__init__(parent)
        self.root = root
        self.provider = provider or IcoadsImageProvider()
        self.item = IcoadsQuickItem(root)
        self._view = self._find("icoadsMarkers")
        self._published = None
        self._closed = False

    def _find(self, name):
        finder = getattr(self.root, "findChild", None)
        if finder is None:
            return None
        try:
            return finder(QObject, name)
        except TypeError:
            return None

    def _publish(self):
        view = self._view
        if view is None:
            return
        item = self.item
        visible = item.isVisible() and not item.image.isNull()
        if visible:
            view.setProperty("x", item.x())
            view.setProperty("y", item.y())
            view.setProperty("width", item.width())
            view.setProperty("height", item.height())
            if self._published is not item.image:
                view.setProperty("source", self.provider.publish(item.image))
                self._published = item.image
        view.setProperty("visible", visible)

    def present(self, reports, visible, camera):
        if self._closed:
            return
        if not self.item._padding:
            self.item.setSize(self.root.size())
        self.item.set_reports(reports, camera)
        self.item.setVisible(bool(visible and reports))
        self.item.render()
        self._publish()

    def set_camera(self, camera):
        if self._closed:
            return
        if not self.item._padding:
            self.item.setSize(self.root.size())
        viewport = self.parent()
        reference = (
            getattr(viewport, "_map_anchor_camera", None)
            if getattr(viewport, "_map_content", None) is not None
            else camera
        )
        self.item.set_camera(camera, reference)
        if self.item._dirty and self.item.isVisible():
            self.item.render()
            self._publish()

    def select_at(self, x, y):
        if self._closed:
            return None
        report = self.item.select_at(x, y)
        if report is not None:
            self.item.render()
            self._publish()
        return report

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.item.setVisible(False)
        self.item.set_reports(())
        self.item.image = QImage()
        self._publish()
