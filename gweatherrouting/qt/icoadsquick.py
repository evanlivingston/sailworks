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
        self._camera = None
        self._on_select = None
        self._selected_report = None
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setAcceptHoverEvents(True)
        self.setAntialiasing(True)

    def set_reports(self, reports, camera=None, on_select=None):
        self._reports = tuple(reports or ())
        if camera is not None:
            self._camera = camera
        self._on_select = on_select
        self.update()

    def set_camera(self, camera):
        self._camera = camera
        self.update()

    def _positions(self):
        if self._camera is None:
            return ()
        from gweatherrouting.core.mapviewport import MapPoint

        result = []
        for report in self._reports:
            try:
                x, y = self._camera.view().convert_geographic_to_screen(
                    MapPoint(report.latitude, report.longitude)
                )
                if 0 <= x <= self.width() and 0 <= y <= self.height():
                    result.append((x, y, report))
            except (AttributeError, TypeError, ValueError):
                continue
        return tuple(result)

    def paint(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#202a34"), 1.4))
        painter.setBrush(QColor("#f2c14e"))
        for x, y, _report in self._positions():
            painter.drawEllipse(int(x) - 4, int(y) - 4, 8, 8)
        if self._selected_report is not None:
            self._paint_details(painter, self._selected_report)

    @staticmethod
    def _detail_lines(report):
        age = datetime.now(timezone.utc) - report.observed_at
        return (
            "NOAA ICOADS · delayed ship observation",
            f"Observed {report.observed_at:%Y-%m-%d %H:%M UTC}",
            f"Age: {max(0, int(age.total_seconds() // 3600))} h",
            *(f"{key}: {value}" for key, value in report.measurements),
        )

    @classmethod
    def _paint_details(cls, painter, report):
        lines = cls._detail_lines(report)
        painter.setFont(QFont("Sans Serif", 9))
        metrics = painter.fontMetrics()
        padding = 10
        width = max(metrics.horizontalAdvance(line) for line in lines) + 2 * padding
        height = len(lines) * metrics.height() + 2 * padding
        rect = QRectF(12, 12, width, height)
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
        except Exception:
            event.ignore()
            return
        if report is not None:
            self._selected_report = report
            self.update()
            if self._on_select is not None:
                self._on_select(report)
            event.accept()
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
        self.item.setSize(self.root.size())
        self.item.set_reports(reports, camera)
        self.item.setVisible(bool(visible and reports))

    def set_camera(self, camera):
        if self._closed:
            return
        self.item.setSize(self.root.size())
        self.item.set_camera(camera)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.item.setVisible(False)
        self.item.set_reports(())
