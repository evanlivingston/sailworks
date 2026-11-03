"""Quick scene-graph layer for cached ICOADS ship observations."""

from PySide6.QtCore import QObject, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtQuick import QQuickItem, QQuickPaintedItem
from datetime import datetime, timezone


class IcoadsQuickItem(QQuickPaintedItem):
    """Paint neutral report markers; reports are observations, not live AIS."""

    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setAcceptHoverEvents(True)
        self.setAntialiasing(True)

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
        painter.setFont(QFont("Sans Serif", 9))
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
        nearest = min(
            self._positions(),
            key=lambda item: (item[0] - x) ** 2 + (item[1] - y) ** 2,
            default=None,
        )
        if nearest and (nearest[0] - x) ** 2 + (nearest[1] - y) ** 2 <= 100:
            return nearest[2]
        return None

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

    def __init__(self, root, parent):
        super().__init__(parent)
        self.root = root
        self.item = IcoadsQuickItem(root)
        self.item.setSize(root.size())
        self.item.setVisible(False)
        barbs = root.findChild(QQuickItem, "weatherBarbs")
        if barbs is not None:
            self.item.stackBefore(barbs)
        self._closed = False

    def present(self, reports, visible, camera):
        if self._closed:
            return
        if not self.item._padding:
            self.item.setSize(self.root.size())
        self.item.set_reports(reports, camera)
        self.item.setVisible(bool(visible and reports))

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

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.item.setVisible(False)
        self.item.set_reports(())
