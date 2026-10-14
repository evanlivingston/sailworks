"""Qt canvas using the shared Mercator projection and a Cairo image bridge."""

import math

import cairo
import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget, QMenu

from gweatherrouting.core.mapviewport import MercatorProjection, MapPoint
from gweatherrouting.rendering.style import Style
from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.windrenderer import WindOverlayRenderer


class TrackMap(QWidget, MercatorProjection):
    route_location = Signal(str, object)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setMinimumSize(240, 180)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._center_lat, self._center_lon, self._zoom = 48.4, -123.4, 6.0
        self.points = ()
        self.route = ()
        self.selected_route_point = -1
        self.route_plan = ()
        self.route_preview = ()
        self.offsets = ()
        self.position = None
        self.drag = None
        self.background = None
        self.controller = controller
        self.charts = controller.charts
        self.charts.add_map(self)
        self.wind = WindOverlayRenderer(WindPainter, controller.scheduler.submit)
        self._ticks = {}
        self._next_tick = 0
        self.disposed = False
        self._zoom_animating = False
        controller.time_control.connect("time_change", self._time_changed)

    def _time_changed(self, _):
        self.queue_draw()

    def get_scale_factor(self):
        return math.ceil(self.devicePixelRatioF())

    def add_tick_callback(self, callback):
        self._next_tick += 1
        token = self._next_tick
        timer = QTimer(self)
        timer.setInterval(16)

        def tick():
            if not callback(self, None):
                self.remove_tick_callback(token)

        timer.timeout.connect(tick)
        self._ticks[token] = timer
        timer.start()
        return token

    def remove_tick_callback(self, token):
        timer = self._ticks.pop(token, None)
        if timer:
            timer.stop()
            timer.deleteLater()

    def dispose(self):
        if self.disposed:
            return
        self.disposed = True
        self.charts.forget_map(self)
        self.wind.forget(self)
        for token in list(self._ticks):
            self.remove_tick_callback(token)
        self.controller.time_control.disconnect("time_change", self._time_changed)

    def hideEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.charts._chs_renderer.forget(self)
        self.wind.forget(self)
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.dispose()
        super().closeEvent(event)

    def get_allocated_width(self):
        return self.width()

    def get_allocated_height(self):
        return self.height()

    def queue_draw(self):
        if not self.disposed:
            self.background = None
            self.update()

    map_redraw_idle = queue_draw

    def set_points(self, points, offsets, fit=False):
        self.points = tuple(points)
        self.offsets = tuple(offsets)
        if fit and points:
            self.fit_track()
        self.update()

    def fit_track(self):
        if not self.points:
            return
        self.fit_coordinates([(point.lat, point.lon) for point in self.points])

    def fit_coordinates(self, coordinates):
        if not coordinates:
            return
        lats, lons = zip(*coordinates)
        self._center_lat = (min(lats) + max(lats)) / 2
        self._center_lon = (min(lons) + max(lons)) / 2
        self._zoom = 0
        xs, ys = self.convert_geographic_to_screen_batch(np.array(lats), np.array(lons))
        spread_x, spread_y = max(np.ptp(xs), 0.0001), max(np.ptp(ys), 0.0001)
        self._zoom = max(
            1,
            min(
                14,
                math.log2(
                    min(
                        max(1, self.width() - 80) / spread_x,
                        max(1, self.height() - 80) / spread_y,
                    )
                ),
            ),
        )
        self.background = None
        self.update()

    def set_route_plan(self, coordinates):
        self.route_plan = tuple(coordinates)
        self.update()

    def set_route(self, coordinates):
        self.route = tuple(coordinates)
        self.selected_route_point = -1
        self.update()

    def select_route_point(self, row):
        self.selected_route_point = row
        self.update()

    def set_route_preview(self, coordinates):
        self.route_preview = tuple(coordinates)
        self.update()

    def fit_route(self):
        self.fit_coordinates((*self.route, *self.route_plan))

    def contextMenuEvent(self, event):  # noqa: N802 - Qt virtual method name
        position = self.convert_screen_to_geographic(event.pos().x(), event.pos().y())
        menu = QMenu(self)
        start = menu.addAction("Set routing start here")
        destination = menu.addAction("Set routing destination here")
        selected = menu.exec(event.globalPos())
        if selected is start or selected is destination:
            self.route_location.emit(
                "start" if selected is start else "destination", position.get_degrees()
            )

    def _draw_route(self, painter, coordinates, color, dashed=False):
        if not coordinates:
            return
        lats, lons = zip(*coordinates)
        xs, ys = self.convert_geographic_to_screen_batch(np.array(lats), np.array(lons))
        pen = QPen(QColor(color), 2)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in zip(xs, ys)]))

    def set_position(self, position):
        self.position = position
        self.update()

    def paintEvent(self, _):  # noqa: N802 - Qt virtual method name
        key = (
            self.width(),
            self.height(),
            self._center_lat,
            self._center_lon,
            self._zoom,
            self.devicePixelRatioF(),
        )
        if self.background is None or self.background[0] != key:
            self.background = (key, self._base_image())
        painter = QPainter(self)
        painter.drawImage(QPointF(0, 0), self.background[1])
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_route(painter, self.route_plan, "#5295d2", dashed=True)
        self._draw_route(painter, self.route, "#26aa70")
        self._draw_route(painter, self.route_preview, "#ef5a8c", dashed=True)
        if self.route:
            lats, lons = zip(*self.route)
            rx, ry = self.convert_geographic_to_screen_batch(
                np.array(lats), np.array(lons)
            )
            painter.setPen(QPen(QColor("#26aa70"), 1))
            for index, (x, y) in enumerate(zip(rx, ry)):
                selected = index == self.selected_route_point
                painter.setBrush(QColor("#ffffff" if selected else "#26aa70"))
                radius = 5 if selected else 2
                painter.drawEllipse(QPointF(x, y), radius, radius)
        if self.route_plan:
            for label, coordinates in zip(("Start", "Destination"), self.route_plan):
                x, y = self.convert_geographic_to_screen(MapPoint(*coordinates))
                painter.setPen(QColor("#5295d2"))
                painter.drawText(QPointF(x + 5, y - 5), label)
        if self.points:
            xs, ys = self.convert_geographic_to_screen_batch(
                np.array([p.lat for p in self.points]),
                np.array([p.lon for p in self.points]),
            )
            painter.setPen(QPen(QColor("#f2be62"), 2))
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in zip(xs, ys)]))
            index = len(self.points) - 1
            if self.position is not None and self.offsets:
                index = max(
                    0,
                    min(
                        index,
                        int(np.searchsorted(self.offsets, self.position, side="right"))
                        - 1,
                    ),
                )
            painter.setBrush(QColor("#ffffff"))
            painter.setPen(QPen(QColor("#d8851b"), 2))
            painter.drawEllipse(QPointF(xs[index], ys[index]), 5, 5)
        painter.setPen(QColor("#e4edf2"))
        caption = "Drag to pan · Scroll to zoom"
        if self.controller.chart_errors:
            caption += " · Some saved charts could not be opened"
        painter.drawText(12, self.height() - 12, caption)
        painter.end()

    def _base_image(self):
        factor = self.get_scale_factor()
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            math.ceil(self.width() * factor),
            math.ceil(self.height() * factor),
        )
        surface.set_device_scale(factor, factor)
        cr = cairo.Context(surface)
        Style.chart_palettes[self.controller.settings.chartPalette].sea.apply(cr)
        cr.paint()
        self.charts.do_draw(self, cr)
        settings = self.controller.settings
        style = (
            self.controller.time_control.time,
            settings.gribArrowOpacity,
            settings.gribArrowOnGround,
        )
        sources = tuple((grib,) for grib in self.controller.core.grib_manager.gribs)
        self.wind.draw(self, cr, sources, style)
        surface.flush()
        # copy() transfers ownership; Cairo's buffer can then be released.
        image = QImage(
            surface.get_data(),
            surface.get_width(),
            surface.get_height(),
            surface.get_stride(),
            QImage.Format.Format_ARGB32_Premultiplied,
        ).copy()
        image.setDevicePixelRatio(factor)
        return image

    def mousePressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual method name
        if self.drag is None:
            return
        delta = event.position() - self.drag
        center = self.convert_screen_to_geographic(
            self.width() / 2 - delta.x(), self.height() / 2 - delta.y()
        )
        self._center_lat, self._center_lon = center.get_degrees()
        self._center_lat = max(-85, min(85, self._center_lat))
        self.drag = event.position()
        self.update()

    def mouseReleaseEvent(self, _):  # noqa: N802 - Qt virtual method name
        self.drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event):  # noqa: N802 - Qt virtual method name
        pointer = event.position()
        anchor = self.convert_screen_to_geographic(pointer.x(), pointer.y())
        delta = event.angleDelta().y() / 120 or event.pixelDelta().y() / 60
        self._zoom = max(1, min(20, self._zoom + delta * 0.25))
        x, y = self.convert_geographic_to_screen(anchor)
        center = self.convert_screen_to_geographic(
            self.width() / 2 + x - pointer.x(), self.height() / 2 + y - pointer.y()
        )
        self._center_lat, self._center_lon = center.get_degrees()
        self.update()
        event.accept()
