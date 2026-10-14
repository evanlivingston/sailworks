"""Qt canvas using the shared Mercator projection and a Cairo image bridge."""

import math
from pathlib import Path

import cairo
import numpy as np
from osgeo import ogr
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from gweatherrouting.core.geo.shoreline import ShorelineGeometryCache
from gweatherrouting.core.mapviewport import MapViewport, MercatorProjection
from gweatherrouting.core.storage import DATA_DIR


class TrackMap(QWidget, MercatorProjection):
    def __init__(self, parent=None, coastline_path=None):
        super().__init__(parent)
        self.setMinimumSize(240, 180)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._center_lat, self._center_lon, self._zoom = 48.4, -123.4, 6.0
        self.points = ()
        self.offsets = ()
        self.position = None
        self.drag = None
        self.background = None
        self.shorelines = ShorelineGeometryCache()
        path = (
            Path(coastline_path)
            if coastline_path
            else (Path(DATA_DIR) / "gshhs/GSHHS_shp/l/GSHHS_l_L1.shp")
        )
        self.coastline = ogr.Open(str(path)) if path.is_file() else None

    def get_allocated_width(self):
        return self.width()

    def get_allocated_height(self):
        return self.height()

    def queue_draw(self):
        self.update()

    def set_points(self, points, offsets, fit=False):
        self.points = tuple(points)
        self.offsets = tuple(offsets)
        if fit and points:
            self.fit_track()
        self.update()

    def fit_track(self):
        if not self.points:
            return
        lats = [point.lat for point in self.points]
        lons = [point.lon for point in self.points]
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

    def set_position(self, position):
        self.position = position
        self.update()

    def paintEvent(self, _):
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
        if self.coastline is None:
            caption += " · No installed base map"
        painter.drawText(12, self.height() - 12, caption)
        painter.end()

    def _base_image(self):
        factor = self.devicePixelRatioF()
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            math.ceil(self.width() * factor),
            math.ceil(self.height() * factor),
        )
        surface.set_device_scale(factor, factor)
        cr = cairo.Context(surface)
        cr.set_source_rgb(0.11, 0.21, 0.28)
        cr.paint()
        viewport = MapViewport(self)
        if self.coastline is not None:
            top, bottom = viewport.get_bbox()
            north, west = top.get_degrees()
            south, east = bottom.get_degrees()
            bounds = ogr.CreateGeometryFromWkt(
                f"POLYGON(({west} {south},{east} {south},{east} {north},"
                f"{west} {north},{west} {south}))"
            )
            layer = self.coastline.GetLayer(0)
            layer.SetSpatialFilter(bounds)
            layer.ResetReading()
            for feature in layer:
                geometry = self.shorelines.clip(
                    feature.GetFID(), feature.GetGeometryRef(), bounds
                )
                cr.new_path()
                self._geometry_path(cr, viewport, geometry)
                cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
                cr.set_source_rgb(0.62, 0.65, 0.56)
                cr.fill_preserve()
                cr.set_source_rgb(0.77, 0.79, 0.66)
                cr.set_line_width(0.6)
                cr.stroke()
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

    def _geometry_path(self, cr, viewport, geometry):
        if geometry is None or geometry.IsEmpty():
            return
        if geometry.GetGeometryCount():
            for index in range(geometry.GetGeometryCount()):
                self._geometry_path(cr, viewport, geometry.GetGeometryRef(index))
            return
        points = geometry.GetPoints()
        if not points:
            return
        lons, lats = np.array(points)[:, :2].T
        xs, ys = viewport.convert_geographic_to_screen_batch(lats, lons)
        cr.move_to(xs[0], ys[0])
        for x, y in zip(xs[1:], ys[1:]):
            cr.line_to(x, y)
        cr.close_path()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
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

    def mouseReleaseEvent(self, _):
        self.drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event):
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
