"""Qt canvas using the shared Mercator projection and a Cairo image bridge."""

import math

import cairo
import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer, Signal, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget, QMenu

from gweatherrouting.core.mapviewport import MercatorProjection, MapPoint
from gweatherrouting.rendering.style import Style
from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.windrenderer import WindOverlayRenderer
from gweatherrouting.rendering.navigation import NavigationRenderer
from gweatherrouting.rendering.ais import AISRenderer
from gweatherrouting.rendering.dashboard import DashboardRenderer
from .windcursor import WindCursorSampler


class TrackMap(QWidget, MercatorProjection):
    route_location = Signal(str, object)
    view_changed = Signal()
    navigation_request = Signal(str, object)
    cursor_readout = Signal(str)

    def __init__(self, controller, parent=None, time_control=None):
        super().__init__(parent)
        self.setMinimumSize(240, 180)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._center_lat, self._center_lon, self._zoom = 48.4, -123.4, 6.0
        self.points = ()
        self.route = ()
        self.route_speeds = ()
        self.selected_route_point = -1
        self.route_plan = ()
        self.route_preview = ()
        self.offsets = ()
        self.position = None
        self.drag = None
        self.background = None
        self.controller = controller
        self.navigation = NavigationRenderer(controller.core)
        self.ais_renderer = AISRenderer()
        self.ais_targets = ()
        self.ais_visible = True
        self.move_callback = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.charts = controller.charts
        self.charts.add_map(self)
        self.wind = WindOverlayRenderer(WindPainter, controller.scheduler.submit)
        self._ticks = {}
        self._next_tick = 0
        self.disposed = False
        self._zoom_animating = False
        self.time_control = time_control or controller.time_control
        self.time_control.connect("time_change", self._time_changed)
        self.dashboard = DashboardRenderer(controller.connections)
        self.dashboard_visible = True
        self.wind_visible = True
        self._instrument_status = None
        self._cursor_context = None
        self._wind_cursor = WindCursorSampler(self, controller.scheduler.submit)
        controller.boat_changed.connect(self._boat_changed)

    def _time_changed(self, _):
        self._cancel_cursor()
        self.queue_draw()

    def _boat_changed(self, _):
        self.update()

    def refresh_instruments(self):
        status = (
            self.controller.connections.polling_active,
            tuple(
                (s["name"], s["connected"], s["packets"])
                for s in self.controller.connections.get_status()
            ),
        )
        if status != self._instrument_status:
            self._instrument_status = status
            self.update()

    def set_dashboard_visible(self, visible):
        self.dashboard_visible = visible
        self.update()

    def set_wind_visible(self, visible):
        self.wind_visible = visible
        if not visible:
            self.wind.forget(self)
        self._cancel_cursor()
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
        self._wind_cursor.close()
        self.cursor_readout.emit("")
        self.controller.boat_changed.disconnect(self._boat_changed)
        self.charts.forget_map(self)
        self.wind.forget(self)
        for token in list(self._ticks):
            self.remove_tick_callback(token)
        self.time_control.disconnect("time_change", self._time_changed)

    def hideEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        self.cancel_move()
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
            if (
                self._cursor_context is not None
                and self._cursor_context != self._weather_context()
            ):
                self._cancel_cursor()
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
        self._cancel_cursor()
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
        self.view_changed.emit()

    def set_route_plan(self, coordinates):
        self.route_plan = tuple(coordinates)
        self.update()

    def set_route(self, coordinates, speeds=()):
        self.route = tuple(coordinates)
        self.route_speeds = tuple(speeds)
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
        self._cancel_cursor()
        self.cancel_move()
        position = self.convert_screen_to_geographic(event.pos().x(), event.pos().y())
        menu = QMenu(self)
        poi = menu.addAction("Add POI here")
        point = menu.addAction("Add track point here")
        menu.addSeparator()
        start = menu.addAction("Set routing start here")
        destination = menu.addAction("Set routing destination here")
        selected = menu.exec(event.globalPos())
        if selected is poi or selected is point:
            self.navigation_request.emit(
                "poi" if selected is poi else "point", position.get_degrees()
            )
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
        painter.drawImage(QPointF(0, 0), self._navigation_image())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active_route = self.controller.core.routingManager.get_by_name(
            self.navigation.hl_routing
        )
        if (
            active_route is not None
            and active_route.visible
            and 0 <= self.selected_route_point < len(active_route)
        ):
            point = active_route.points[self.selected_route_point]
            x, y = self.convert_geographic_to_screen(MapPoint(point[0], point[1]))
            painter.setPen(QPen(QColor("#5295d2"), 2))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(QPointF(x, y), 5, 5)
        self._draw_route(painter, self.route_plan, "#5295d2", dashed=True)
        self._draw_route(painter, self.route_preview, "#ef5a8c", dashed=True)
        if self.route_plan:
            for label, coordinates in zip(("Start", "Destination"), self.route_plan):
                x, y = self.convert_geographic_to_screen(MapPoint(*coordinates))
                painter.setPen(QPen(QColor("#5295d2"), 1))
                painter.setBrush(QColor("#5295d2"))
                painter.drawEllipse(QPointF(x, y), 3, 3)
                text_width = painter.fontMetrics().horizontalAdvance(label)
                text_x = max(5, min(x + 5, self.width() - text_width - 5))
                painter.drawText(
                    QPointF(text_x, max(15, min(y - 5, self.height() - 5))), label
                )
        if self.points:
            xs, ys = self.convert_geographic_to_screen_batch(
                np.array([p.lat for p in self.points]),
                np.array([p.lon for p in self.points]),
            )
            style = Style.Track.LogTrack
            painter.setPen(QPen(QColor.fromRgbF(*style.color), style.line_width or 1))
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
        if self.dashboard_visible:
            painter.drawImage(QPointF(0, 0), self._dashboard_image())
        painter.setPen(QColor("#e4edf2"))
        caption = "Drag to pan · Scroll to zoom"
        if self.move_callback is not None:
            caption = "Click to place the selected point · Esc to cancel"
        if self.controller.chart_errors:
            caption += " · Some saved charts could not be opened"
        caption = painter.fontMetrics().elidedText(
            caption, Qt.TextElideMode.ElideRight, max(1, self.width() - 36)
        )
        caption_width = painter.fontMetrics().horizontalAdvance(caption)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 155))
        caption_y = 8 if self.dashboard_visible else self.height() - 34
        painter.drawRoundedRect(QRectF(8, caption_y, caption_width + 16, 26), 4, 4)
        painter.setPen(QColor("#eeeeec"))
        painter.drawText(16, caption_y + 18, caption)
        painter.end()

    def _navigation_image(self):
        factor = self.get_scale_factor()
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            math.ceil(self.width() * factor),
            math.ceil(self.height() * factor),
        )
        surface.set_device_scale(factor, factor)
        cr = cairo.Context(surface)
        self.navigation.draw_routes(self, cr, self.time_control.time.timestamp())
        self.navigation.draw_navigation(self, cr)
        if self.ais_visible:
            self.ais_renderer.draw_targets(self, cr, self.ais_targets)
        surface.flush()
        image = QImage(
            surface.get_data(),
            surface.get_width(),
            surface.get_height(),
            surface.get_stride(),
            QImage.Format.Format_ARGB32_Premultiplied,
        ).copy()
        image.setDevicePixelRatio(factor)
        return image

    def _dashboard_image(self):
        factor = self.get_scale_factor()
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            math.ceil(self.width() * factor),
            math.ceil(self.height() * factor),
        )
        surface.set_device_scale(factor, factor)
        self.dashboard.boat_info = self.controller.core.boat_info
        self.dashboard._draw_dashboard(self, cairo.Context(surface))
        surface.flush()
        image = QImage(
            surface.get_data(),
            surface.get_width(),
            surface.get_height(),
            surface.get_stride(),
            QImage.Format.Format_ARGB32_Premultiplied,
        ).copy()
        image.setDevicePixelRatio(factor)
        return image

    def _weather_context(self):
        return (
            self.time_control.time,
            tuple(self.controller.core.grib_manager.gribs),
            self._center_lat,
            self._center_lon,
            self._zoom,
            self.width(),
            self.height(),
            self.wind_visible,
        )

    def _cancel_cursor(self):
        self._wind_cursor.cancel()
        self._cursor_context = None
        self.cursor_readout.emit("")

    def _hover(self, position):
        lat, lon = self.convert_screen_to_geographic(
            position.x(), position.y()
        ).get_degrees()
        context = self._weather_context()
        self._cursor_context = context
        text = "Latitude: %f, Longitude: %f" % (lat, lon)
        self.cursor_readout.emit(text)
        if (
            self.drag is not None
            or self._zoom_animating
            or self.move_callback is not None
            or not self.wind_visible
            or not self.isVisible()
        ):
            self._wind_cursor.cancel()
            return

        def show(wind):
            if (
                self.disposed
                or not self.isVisible()
                or self._cursor_context != context
                or context != self._weather_context()
            ):
                return
            self.cursor_readout.emit(
                "Wind %.1f°, %.1f kts - " % (wind[0], wind[1] * 1.94384) + text
                if wind is not None
                else text
            )

        self._wind_cursor.request(context[1], context[0], lat, lon, show)

    def leaveEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        super().leaveEvent(event)

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        super().resizeEvent(event)

    def begin_move(self, callback):
        self._cancel_cursor()
        self.move_callback = callback
        self.drag = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocus()
        self.update()

    def cancel_move(self):
        self.move_callback = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()

    def keyPressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.key() == Qt.Key.Key_Escape and self.move_callback is not None:
            self.cancel_move()
            event.accept()
            return
        super().keyPressEvent(event)

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
            self.time_control.time,
            settings.gribArrowOpacity,
            settings.gribArrowOnGround,
        )
        sources = tuple((grib,) for grib in self.controller.core.grib_manager.gribs)
        if self.wind_visible:
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
        self._cancel_cursor()
        if event.button() == Qt.MouseButton.LeftButton:
            if self.move_callback is not None:
                position = self.convert_screen_to_geographic(
                    event.position().x(), event.position().y()
                )
                callback = self.move_callback
                self.cancel_move()
                callback(position.get_degrees())
                return
            self.drag = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual method name
        if self.drag is None:
            self._hover(event.position())
            return
        delta = event.position() - self.drag
        center = self.convert_screen_to_geographic(
            self.width() / 2 - delta.x(), self.height() / 2 - delta.y()
        )
        self._center_lat, self._center_lon = center.get_degrees()
        self._center_lat = max(-85, min(85, self._center_lat))
        self.drag = event.position()
        self.update()
        self.view_changed.emit()
        self._hover(event.position())

    def mouseReleaseEvent(self, _):  # noqa: N802 - Qt virtual method name
        self.drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.view_changed.emit()

    def wheelEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
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
        self.view_changed.emit()
        event.accept()
