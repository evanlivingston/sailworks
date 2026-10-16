"""Qt canvas using the shared Mercator projection and a Cairo image bridge."""

import math
import sys
import time
from typing import Optional

import cairo
import numpy as np
from PySide6.QtCore import QEvent, QPointF, Qt, QTimer, Signal, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget, QMenu, QLabel

from gweatherrouting.core.mapviewport import MercatorProjection, MapPoint
from gweatherrouting.core.mapmotion import DragVelocity, PanInertia
from gweatherrouting.rendering.style import Style
from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.windpainter import barb_bucket_colors
from gweatherrouting.rendering.windrenderer import WindOverlayRenderer
from gweatherrouting.rendering.navigation import NavigationRenderer
from gweatherrouting.rendering.ais import AISRenderer
from gweatherrouting.rendering.dashboard import DashboardRenderer
from gweatherrouting.rendering.isochrones import IsochroneFrame, IsochronesRenderer
from .windcursor import WindCursorSampler
from .marineoverlay import MarineOverlay
from .weathercoverage import WeatherCoverage


class TrackMap(QWidget, MercatorProjection):
    route_location = Signal(str, object)
    view_changed = Signal()
    navigation_request = Signal(str, object)
    cursor_readout = Signal(str)

    # Pan distance (logical pixels) a cached layer image may be shifted
    # instead of re-rendered. Mirrors the GTK renderers' reuse margins.
    _REUSE_MARGIN = 128
    _RENDER_PADDING = 192
    # Additional image storage per layer, excluding the visible viewport.
    _PADDING_BYTES = 16 * 1024 * 1024
    # Wheel zoom events closer than this settle into one sharp re-render,
    # with a scaled preview painted in between (no overlay job storm).
    _ZOOM_SETTLE_MS = 250
    # Per-tick zoom easing rate and snap distance for the smooth animation.
    _ZOOM_EASE_RATE = 14.0
    _ZOOM_SNAP = 0.004
    # Cap for the log-track polyline; denser logs are subsampled uniformly,
    # which only drops sub-pixel detail while keeping every paint cheap.
    _MAX_TRACK_POLYLINE = 1500

    def __init__(self, controller, parent=None, time_control=None):
        super().__init__(parent)
        self.setMinimumSize(240, 180)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._center_lat, self._center_lon, self._zoom = 48.4, -123.4, 6.0
        self.points: tuple = ()
        self.route: tuple = ()
        self.route_speeds: tuple = ()
        self.selected_route_point = -1
        self.route_plan: tuple = ()
        self.route_preview: tuple = ()
        self.offsets: tuple = ()
        self.position = None
        self.drag = None
        self.background = None
        self._render_padding = 0
        self._base_cache = None  # (key, lat, lon, zoom, QImage)
        self._nav_cache = None  # (key, lat, lon, zoom, QImage)
        self._dash_cache = None  # (key, QImage)
        self._overlay_pending = False
        self._zoom_anchor = None  # (lat, lon, x, y) for zoom previews
        self._zoom_target = None
        self._native_gesture = False
        self._trackpad_panning = False
        self._trackpad_scroll_pans = sys.platform == "darwin"
        self._zoom_tick_token = None
        self._zoom_last_tick = 0.0
        self._smooth_zoom = True
        self._pan_inertia_enabled = True
        self._drag_velocity = DragVelocity()
        self._inertia: Optional[PanInertia] = None
        self._inertia_tick_token = None
        self._inertia_last_tick = 0.0
        self._track_proj = None  # (data id, w, h, zoom, lat, lon, xs, ys, poly)
        self._ais_targets_state: tuple = ()
        self._ais_visible_state = True
        self._zoom_settle = QTimer(self)
        self._zoom_settle.setSingleShot(True)
        self._zoom_settle.timeout.connect(self._zoom_settle_timeout)
        self._scroll_settle = QTimer(self)
        self._scroll_settle.setSingleShot(True)
        self._scroll_settle.timeout.connect(self._finish_trackpad_pan)
        self.controller = controller
        self.navigation = NavigationRenderer(controller.core)
        self.ais_renderer = AISRenderer()
        self.move_callback = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.charts = controller.charts
        self.charts.add_map(self)
        self.wind = WindOverlayRenderer(WindPainter, controller.scheduler.submit)
        self.isochrones = IsochronesRenderer(controller.scheduler.submit)
        self.isochrone_frame = IsochroneFrame((), (), ())
        self._ticks = {}
        self._next_tick = 0
        self.disposed = False
        self._zoom_animating = False
        self.time_control = time_control or controller.time_control
        self.time_control.connect("time_change", self._time_changed)
        self.marine = MarineOverlay(
            controller.core.marine_manager,
            self.time_control,
            controller.scheduler.submit,
        )
        self.marine.visible = time_control is None
        self.dashboard = DashboardRenderer(controller.connections)
        self.dashboard_visible = True
        self.wind_visible = True
        self._instrument_status = None
        self._cursor_context = None
        self._wind_cursor = WindCursorSampler(self, controller.scheduler.submit)
        self.coverage_notice = QLabel(self)
        self.coverage_notice.setWordWrap(True)
        self.coverage_notice.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.coverage_notice.setStyleSheet(
            "background: rgba(30, 33, 36, 220); color: #e5c17c; padding: 8px; border-radius: 4px;"
        )
        self.coverage_notice.hide()
        self.weather_coverage = WeatherCoverage(self, self._show_coverage)
        controller.boat_changed.connect(self._boat_changed)
        controller.settings.register_on_change("smoothZoom", self._set_smooth_zoom)
        controller.settings.register_on_change("panInertia", self._set_pan_inertia)

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
        self.weather_coverage.close()
        self.cursor_readout.emit("")
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.controller.boat_changed.disconnect(self._boat_changed)
        self.charts.forget_map(self)
        self.wind.forget(self)
        self.isochrones.forget(self)
        self.marine.close()
        for token in list(self._ticks):
            self.remove_tick_callback(token)
        self.time_control.disconnect("time_change", self._time_changed)

    def hideEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.drag = None
        self.cancel_move()
        self.charts._chs_renderer.forget(self)
        self.wind.forget(self)
        self.isochrones.forget(self)
        self.marine.forget(self)
        # Forgotten overlay state means the cached pixels may reference
        # frames that will never refresh; drop them while hidden.
        self._invalidate_base()
        self.invalidate_navigation()
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.dispose()
        super().closeEvent(event)

    def get_allocated_width(self):
        return self.width() + 2 * self._render_padding

    def get_allocated_height(self):
        return self.height() + 2 * self._render_padding

    def get_attribution_bounds(self):
        return self._render_padding, self._render_padding, self.width(), self.height()

    def queue_draw(self):
        if self.disposed:
            return
        if (
            self._cursor_context is not None
            and self._cursor_context != self._weather_context()
        ):
            self._cancel_cursor()
        if self.is_moving():
            # Mid-gesture: overlay workers may deliver frames while the view
            # is still moving. Paint shifted previews now and do one sharp
            # rebuild when the gesture settles instead of churning per event.
            self._overlay_pending = True
            self.update()
            return
        self._invalidate_base()
        self.invalidate_navigation()
        self.update()

    map_redraw_idle = queue_draw

    @property
    def ais_targets(self):
        return self._ais_targets_state

    @ais_targets.setter
    def ais_targets(self, targets):
        self._ais_targets_state = tuple(targets)
        self.invalidate_navigation()

    @property
    def ais_visible(self):
        return self._ais_visible_state

    @ais_visible.setter
    def ais_visible(self, visible):
        self._ais_visible_state = bool(visible)
        self.invalidate_navigation()

    def _invalidate_base(self):
        self._base_cache = None
        self.background = None

    def invalidate_navigation(self):
        """Drop the cached navigation overlay (data changed, view kept)."""
        self._nav_cache = None

    def _invalidate_dashboard(self):
        self._dash_cache = None

    def _view_pixel_size(self):
        return (
            self.width(),
            self.height(),
            round(float(self.devicePixelRatioF()), 3),
        )

    def _wind_style_key(self):
        settings = self.controller.settings
        return (
            self.time_control.time,
            settings.gribArrowOpacity,
            settings.gribArrowOnGround,
            getattr(settings, "gribIsobarsVisible", True),
            self.wind_visible,
            getattr(settings, "gribIsobarInterval", 4.0),
            getattr(settings, "gribIsobarOpacity", 0.85),
            getattr(settings, "gribIsobarLabels", True),
            tuple(
                (id(grib), getattr(grib, "name", None))
                for grib in self.controller.core.grib_manager.gribs
            ),
        )

    def _marine_key(self):
        return (
            self.marine.visible,
            self.marine.currents_visible,
            id(self.marine.sources),
        )

    def _base_key(self):
        width, height, ratio = self._view_pixel_size()
        return (
            width,
            height,
            ratio,
            self.controller.settings.chartPalette,
            self._wind_style_key(),
            self._marine_key(),
        )

    @staticmethod
    def _collection_snapshot(manager):
        snapshot = []
        for element in manager:
            position = getattr(element, "position", None)
            try:
                size = len(element)
            except TypeError:
                size = 0
            snapshot.append(
                (
                    element.name,
                    bool(getattr(element, "visible", True)),
                    size,
                    tuple(position) if position is not None else None,
                )
            )
        return tuple(snapshot)

    def _nav_key(self):
        core = self.controller.core
        width, height, ratio = self._view_pixel_size()
        if self._ais_visible_state:
            ais = tuple(
                sorted(
                    (round(target.latitude, 4), round(target.longitude, 4))
                    for target in self._ais_targets_state
                    if target.has_valid_position()
                )
            )
        else:
            ais = None
        return (
            width,
            height,
            ratio,
            self.time_control.time.timestamp(),
            ais,
            self._collection_snapshot(core.routingManager),
            self._collection_snapshot(core.trackManager),
            self._collection_snapshot(core.poiManager),
            self.route_plan,
            self.route_preview,
            self.isochrone_frame,
            self.route,
            self.selected_route_point,
            self.navigation.hl_routing,
            self.navigation.hl_poi,
            self.navigation.hl_track_item,
            id(self.points),
            len(self.points),
            self.position,
            id(self.offsets),
        )

    def _dash_key(self):
        width, height, ratio = self._view_pixel_size()
        boat = self.controller.core.boat_info
        return (
            width,
            height,
            ratio,
            self.dashboard_visible,
            (boat.sog, boat.cog, boat.aws, boat.awa, boat.tws, boat.twa, boat.depth),
            self._instrument_status,
        )

    def _pan_offset(self, cached_lat, cached_lon):
        """Screen offset that re-aligns a cached layer with the current view."""
        return (
            self._lon_to_x(cached_lon) - self._lon_to_x(self._center_lon),
            self._lat_to_y(cached_lat) - self._lat_to_y(self._center_lat),
        )

    def _sea_color(self):
        color = Style.chart_palettes[
            self.controller.settings.chartPalette
        ].sea.color or (0.83, 0.92, 0.93, 1.0)
        return QColor.fromRgbF(*color)

    def _zoom_preview_target(self, cached_lat, cached_lon, cached_zoom, size):
        """Target rect that stretches a cached layer through the zoom anchor."""
        if self._zoom_anchor is None:
            return None
        anchor_lat, anchor_lon, pointer_x, pointer_y = self._zoom_anchor
        width, height = size
        ratio = 2.0 ** (self._zoom - cached_zoom)
        tile = 2.0**cached_zoom * self.TILE_SIZE
        center_x = (cached_lon + 180.0) / 360.0 * tile
        anchor_x = (anchor_lon + 180.0) / 360.0 * tile
        old_x = anchor_x - center_x + width / 2.0
        old_y = (
            self._mercator_y(anchor_lat, tile)
            - self._mercator_y(cached_lat, tile)
            + height / 2.0
        )
        target_width, target_height = width * ratio, height * ratio
        return QRectF(
            pointer_x - old_x * ratio,
            pointer_y - old_y * ratio,
            target_width,
            target_height,
        )

    @staticmethod
    def _mercator_y(lat, tile):
        lat = max(-TrackMap.MAX_LAT, min(TrackMap.MAX_LAT, lat))
        lat_rad = math.radians(lat)
        return (
            (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
            / 2.0
            * tile
        )

    def _layer_padding(self):
        width, height = self.width(), self.height()
        extra_pixels = self._PADDING_BYTES / (4 * self.get_scale_factor() ** 2)
        # (w + 2p)(h + 2p) - wh <= extra_pixels.
        budget = (
            math.sqrt((width + height) ** 2 + 4 * extra_pixels) - width - height
        ) / 4
        return max(0, min(self._RENDER_PADDING, min(width, height) // 3, int(budget)))

    def _build_padded_layer(self, build):
        # Renderers keep this canvas identity for async jobs, but capture frozen
        # MapViewports synchronously. Restore the interactive projection even
        # if a reader fails. A replacement bake needs fresh chart coverage.
        padding, zooming = self._render_padding, self._zoom_animating
        self._render_padding = self._layer_padding()
        self._zoom_animating = False
        try:
            return build()
        finally:
            self._render_padding, self._zoom_animating = padding, zooming

    def _paint_layer(self, painter, image, target):
        # Sample only visible pixels to bound raster work and destination
        # geometry, including when rendering into an ancestor surface.
        viewport = QRectF(0, 0, self.width(), self.height())
        sx, sy = image.width() / target.width(), image.height() / target.height()
        source = QRectF(
            -target.x() * sx,
            -target.y() * sy,
            viewport.width() * sx,
            viewport.height() * sy,
        )
        painter.drawImage(viewport, image, source)

    def _draw_reusable_layer(self, painter, cache, key, build):
        """Reuse padded scene pixels, rebuilding before coverage is exhausted."""
        width, height, _ = self._view_pixel_size()
        if cache is not None and cache[0] == key:
            _, cached_lat, cached_lon, cached_zoom, image = cache
            size = image.deviceIndependentSize()
            pad_x, pad_y = (size.width() - width) / 2, (size.height() - height) / 2
            if cached_zoom == self._zoom:
                offset_x, offset_y = self._pan_offset(cached_lat, cached_lon)
                margin = min(self._REUSE_MARGIN, max(0, min(pad_x, pad_y) * 2 / 3))
                if max(abs(offset_x), abs(offset_y)) <= margin:
                    self._paint_layer(
                        painter,
                        image,
                        QRectF(
                            offset_x - pad_x,
                            offset_y - pad_y,
                            size.width(),
                            size.height(),
                        ),
                    )
                    return cache
            elif self._zoom_animating:
                target = self._zoom_preview_target(
                    cached_lat, cached_lon, cached_zoom, (width, height)
                )
                if target is not None:
                    ratio = 2.0 ** (self._zoom - cached_zoom)
                    target.adjust(
                        -pad_x * ratio, -pad_y * ratio, pad_x * ratio, pad_y * ratio
                    )
                    if target.contains(QRectF(0, 0, width, height)):
                        self._paint_layer(painter, image, target)
                        return cache
        image = self._build_padded_layer(build)
        size = image.deviceIndependentSize()
        self._paint_layer(
            painter,
            image,
            QRectF(
                (width - size.width()) / 2,
                (height - size.height()) / 2,
                size.width(),
                size.height(),
            ),
        )
        return (key, self._center_lat, self._center_lon, self._zoom, image)

    def is_moving(self):
        return (
            self.drag is not None
            or self._inertia is not None
            or self._zoom_animating
            or self._native_gesture
            or self._trackpad_panning
        )

    def event(self, event):
        if event.type() == QEvent.Type.NativeGesture and not self.disposed:
            return self._native_gesture_event(event)
        if event.type() == QEvent.Type.WindowDeactivate and (
            self._native_gesture or self._trackpad_panning
        ):
            self._finish_zoom_silent()
            self._refresh_after_gesture()
            self.view_changed.emit()
        return super().event(event)

    def _begin_native_gesture(self):
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.drag = None
        self._native_gesture = True
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _native_gesture_event(self, event):
        kind = event.gestureType()
        if kind == Qt.NativeGestureType.BeginNativeGesture:
            self._begin_native_gesture()
        elif kind == Qt.NativeGestureType.EndNativeGesture:
            if self._native_gesture:
                self._finish_zoom_silent()
                self._refresh_after_gesture()
                self.view_changed.emit()
        elif kind == Qt.NativeGestureType.ZoomNativeGesture:
            scale = 1.0 + event.value()
            if math.isfinite(scale) and scale > 0:
                if not self._native_gesture:
                    self._begin_native_gesture()
                if self._zoom_anchor is None:
                    pointer = event.position()
                    anchor = self.convert_screen_to_geographic(pointer.x(), pointer.y())
                    self._zoom_anchor = (
                        *anchor.get_degrees(),
                        pointer.x(),
                        pointer.y(),
                    )
                self._zoom_animating = True
                self._zoom = max(1, min(20, self._zoom + math.log2(scale)))
                self._recenter_on_anchor()
                self.update()
                self.view_changed.emit()
        else:
            # A pinch can include rotation events. The chart remains north-up.
            return self._native_gesture
        event.accept()
        return True

    def _finish_trackpad_pan(self):
        if not self._trackpad_panning:
            return
        self._trackpad_panning = False
        self._scroll_settle.stop()
        if not self.disposed:
            self._refresh_after_gesture()
            self.view_changed.emit()

    def _trackpad_pan(self, event):
        phase = getattr(event, "phase", lambda: Qt.ScrollPhase.NoScrollPhase)()
        pixels = event.pixelDelta()
        modifiers = getattr(
            event, "modifiers", lambda: Qt.KeyboardModifier.NoModifier
        )()
        if (
            not self._trackpad_scroll_pans
            or modifiers
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
            or (pixels.isNull() and phase == Qt.ScrollPhase.NoScrollPhase)
        ):
            return False
        if not self._trackpad_panning or phase == Qt.ScrollPhase.ScrollBegin:
            self._cancel_inertia(settle=False)
            self._finish_zoom_silent()
            self.drag = None
            self._trackpad_panning = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        # Qt has already applied the system natural-scroll preference. Native
        # momentum arrives as further pixel deltas; never add our drag inertia.
        self._pan_pixels(pixels.x(), pixels.y())
        if phase == Qt.ScrollPhase.ScrollEnd:
            self._finish_trackpad_pan()
        else:
            if phase == Qt.ScrollPhase.NoScrollPhase:
                self._scroll_settle.start(self._ZOOM_SETTLE_MS)
            self.update()
            self.view_changed.emit()
        event.accept()
        return True

    def _finish_zoom(self):
        was_zooming = self._zoom_animating or self._zoom_target is not None
        self._finish_zoom_silent()
        if self.disposed or not was_zooming:
            return
        self._refresh_after_gesture()

    def _zoom_settle_timeout(self):
        if self.disposed:
            return
        if self._zoom_tick_token is not None:
            # Glide still converging after the last input; give it room
            # instead of freezing mid-animation.
            self._zoom_settle.start(self._ZOOM_SETTLE_MS)
            return
        self._finish_zoom()

    def _finish_zoom_silent(self):
        self._native_gesture = False
        self._trackpad_panning = False
        self._scroll_settle.stop()
        self._zoom_settle.stop()
        if self._zoom_tick_token is not None:
            self.remove_tick_callback(self._zoom_tick_token)
            self._zoom_tick_token = None
        self._zoom_target = None
        self._zoom_animating = False
        self._zoom_anchor = None

    def _refresh_after_gesture(self):
        if self._overlay_pending:
            self._overlay_pending = False
            self._invalidate_base()
            self.invalidate_navigation()
        self.update()

    def _set_smooth_zoom(self, enabled):
        if self.disposed:
            return
        self._smooth_zoom = bool(enabled)
        if not enabled and self._zoom_target is not None:
            self._zoom = self._zoom_target
            self._recenter_on_anchor()
            self._finish_zoom()
            self.view_changed.emit()

    def _set_pan_inertia(self, enabled):
        if self.disposed:
            return
        self._pan_inertia_enabled = bool(enabled)
        if not enabled:
            self._cancel_inertia()

    def _cancel_inertia(self, settle=True):
        moving = self._inertia is not None
        if self._inertia_tick_token is not None:
            self.remove_tick_callback(self._inertia_tick_token)
        self._inertia_tick_token = None
        self._inertia = None
        if moving and settle and not self.disposed:
            self._refresh_after_gesture()

    def _pan_pixels(self, dx, dy):
        center = self.convert_screen_to_geographic(
            self.width() / 2 - dx, self.height() / 2 - dy
        )
        self._center_lat, self._center_lon = center.get_degrees()
        self._center_lat = max(-85, min(85, self._center_lat))

    def _start_inertia(self, pointer):
        if not self._pan_inertia_enabled:
            return False
        vx, vy = self._drag_velocity.release(time.monotonic(), pointer.x(), pointer.y())
        motion = PanInertia(vx, vy)
        if not motion.active:
            return False
        self._inertia = motion
        self._inertia_last_tick = time.monotonic()
        self._inertia_tick_token = self.add_tick_callback(self._inertia_tick)
        return True

    def _inertia_tick(self, canvas, _):
        motion = self._inertia
        if motion is None:
            self._inertia_tick_token = None
            return False
        now = time.monotonic()
        elapsed = min(0.05, max(0.0, now - self._inertia_last_tick))
        self._inertia_last_tick = now
        self._pan_pixels(*motion.step(elapsed))
        self.update()
        self.view_changed.emit()
        if self._inertia is not motion:
            return False
        if not motion.active:
            self._inertia = None
            self._inertia_tick_token = None
            self._refresh_after_gesture()
            return False
        return True

    def _ensure_zoom_tick(self):
        if self._zoom_tick_token is None and not self.disposed:
            self._zoom_last_tick = time.monotonic()
            self._zoom_tick_token = self.add_tick_callback(self._zoom_tick)

    def _zoom_tick(self, canvas, _):
        now = time.monotonic()
        elapsed = min(0.05, max(0.0, now - self._zoom_last_tick))
        self._zoom_last_tick = now
        target = self._zoom_target
        if target is None:
            self._zoom_tick_token = None
            return False
        step = target - self._zoom
        self._zoom += step * min(1.0, elapsed * self._ZOOM_EASE_RATE)
        if abs(target - self._zoom) <= self._ZOOM_SNAP:
            self._zoom = target
        self._recenter_on_anchor()
        if self._zoom == target:
            self._zoom_target = None
            self._zoom_tick_token = None
            self._finish_zoom()
            return False
        self.update()
        return True

    def _recenter_on_anchor(self):
        """Keep the zoom anchor fixed under the cursor while zooming."""
        if self._zoom_anchor is None:
            return
        anchor_lat, anchor_lon, pointer_x, pointer_y = self._zoom_anchor
        x, y = self.convert_geographic_to_screen(MapPoint(anchor_lat, anchor_lon))
        center = self.convert_screen_to_geographic(
            self.width() / 2 + x - pointer_x, self.height() / 2 + y - pointer_y
        )
        self._center_lat, self._center_lon = center.get_degrees()
        self._center_lat = max(-85, min(85, self._center_lat))

    @staticmethod
    def _world_at(lon, lat, zoom):
        tile = 2.0**zoom * TrackMap.TILE_SIZE
        return (lon + 180.0) / 360.0 * tile, TrackMap._mercator_y(lat, tile)

    def set_points(self, points, offsets, fit=False):
        self.points = tuple(points)
        self.offsets = tuple(offsets)
        if fit and points:
            self.fit_track()
        self.invalidate_navigation()
        self.update()

    def fit_track(self):
        if not self.points:
            return
        self.fit_coordinates([(point.lat, point.lon) for point in self.points])

    def fit_coordinates(self, coordinates):
        if not coordinates:
            return
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
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
        self._finish_zoom_silent()
        self._invalidate_base()
        self.invalidate_navigation()
        self.update()
        self.view_changed.emit()

    def set_route_plan(self, coordinates):
        self.route_plan = tuple(coordinates)
        self.invalidate_navigation()
        self.update()

    def set_route(self, coordinates, speeds=()):
        self.route = tuple(coordinates)
        self.route_speeds = tuple(speeds)
        self.selected_route_point = -1
        self.invalidate_navigation()
        self.update()

    def select_route_point(self, row):
        self.selected_route_point = row
        self.invalidate_navigation()
        self.update()

    def set_isochrones(self, frontiers):
        self.isochrone_frame = IsochroneFrame(tuple(frontiers), (), ())
        if not frontiers:
            self.isochrones.forget(self)
        self.invalidate_navigation()
        self.update()

    def set_route_preview(self, coordinates):
        self.route_preview = tuple(coordinates)
        self.invalidate_navigation()
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
        self.invalidate_navigation()
        self.update()

    def paintEvent(self, _):  # noqa: N802 - Qt virtual method name
        if self.width() <= 0 or self.height() <= 0:
            return
        painter = QPainter(self)
        # Expanded images must also stay inside this widget during native
        # QWidget render/grab redirection into an ancestor surface.
        painter.setClipRect(self.rect())
        # Water is the fallback for unavailable chart/overlay source coverage.
        painter.fillRect(0, 0, self.width(), self.height(), self._sea_color())
        previous_base = self._base_cache
        self._base_cache = self._draw_reusable_layer(
            painter, self._base_cache, self._base_key(), self._base_image
        )
        if self._base_cache is not previous_base:
            self.background = (
                (
                    self.width(),
                    self.height(),
                    self._center_lat,
                    self._center_lon,
                    self._zoom,
                    float(self.devicePixelRatioF()),
                ),
                self._base_cache[4],
            )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_wind_barbs(painter)
        self._nav_cache = self._draw_reusable_layer(
            painter, self._nav_cache, self._nav_key(), self._navigation_image
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_route_overlay(painter)
        self._paint_track_overlay(painter)
        self._paint_dashboard(painter)
        self._paint_caption(painter)
        painter.end()
        self._check_weather_coverage()

    def _paint_wind_barbs(self, painter):
        """Crisp fixed-size foreground barbs over the scaled base image."""
        if not self.wind_visible:
            return
        geometry, opacity = self.wind.live_geometry_for_view(
            self,
            self.width(),
            self.height(),
            self._center_lat,
            self._center_lon,
            self._zoom,
            density=getattr(self.controller.settings, "gribBarbDensity", "standard"),
        )
        if not geometry:
            return
        colors = barb_bucket_colors()
        painter.save()
        for bucket in sorted(geometry):
            lines, tris, dots = geometry[bucket]
            r, g, b = colors[bucket]
            color = QColor.fromRgbF(r, g, b, opacity)
            if lines or dots:
                pen = QPen(color, 1)
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                path = QPainterPath()
                for x1, y1, x2, y2 in lines:
                    path.moveTo(x1, y1)
                    path.lineTo(x2, y2)
                for x, y in dots:
                    path.addEllipse(QPointF(x, y), 2.0, 2.0)
                painter.drawPath(path)
            if tris:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                path = QPainterPath()
                path.setFillRule(Qt.FillRule.WindingFill)
                for ax, ay, bx, by, cx, cy in tris:
                    path.moveTo(ax, ay)
                    path.lineTo(bx, by)
                    path.lineTo(cx, cy)
                    path.closeSubpath()
                painter.drawPath(path)
        painter.restore()

    def _show_coverage(self, text):
        if self.disposed:
            return
        self.coverage_notice.setText(text)
        self.coverage_notice.setFixedWidth(max(80, min(360, self.width() - 24)))
        self.coverage_notice.adjustSize()
        self.coverage_notice.move(
            self.width() - self.coverage_notice.width() - 12,
            self.height() - self.coverage_notice.height() - 12,
        )
        self.coverage_notice.setVisible(bool(text) and self.isVisible())
        self.coverage_notice.raise_()

    def _check_weather_coverage(self):
        if self.disposed or self.is_moving():
            self.coverage_notice.hide()
            return
        fields = tuple(
            name
            for name, enabled in (
                ("wind", self.wind_visible),
                ("pressure", self.controller.settings.gribIsobarsVisible),
            )
            if enabled
        )
        points = tuple(
            self.convert_screen_to_geographic(
                self.width() * x, self.height() * y
            ).get_degrees()
            for y in (0.02, 0.5, 0.98)
            for x in (0.02, 0.5, 0.98)
        )
        points = tuple((lat, (lon + 180) % 360 - 180) for lat, lon in points)
        self.weather_coverage.request(
            self.controller.data.selection.models,
            fields,
            self.time_control.time,
            points,
            tuple(self.controller.core.grib_manager.gribs),
        )
        if (
            not self.weather_coverage.running
            and self.weather_coverage.pending is None
            and self.coverage_notice.text()
            and not self.coverage_notice.isVisible()
        ):
            self._show_coverage(self.coverage_notice.text())

    def _paint_route_overlay(self, painter):
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

    def _track_screen_points(self):
        """Projected log-track pixels, remapped exactly when the view moved.

        Screen projection is an affine map of Mercator world pixels, so a
        cached projection can be shifted/scaled into a moved view without
        re-running the per-point transcendental math on every gesture tick.
        Returns (xs, ys, polyline-or-None); the polyline is only reused
        when the view is unchanged.
        """
        width, height, _ = self._view_pixel_size()
        data_id = (id(self.points), len(self.points))
        cache = self._track_proj
        if cache is not None and cache[:3] == (data_id, width, height):
            _, _, _, cached_zoom, cached_lat, cached_lon, xs, ys, poly = cache
            if (
                cached_zoom == self._zoom
                and cached_lat == self._center_lat
                and cached_lon == self._center_lon
            ):
                return xs, ys, poly
            ratio = 2.0 ** (self._zoom - cached_zoom)
            old_x, old_y = self._world_at(cached_lon, cached_lat, cached_zoom)
            new_x, new_y = self._world_at(
                self._center_lon, self._center_lat, cached_zoom
            )
            xs = ratio * (xs + old_x - width / 2.0 - new_x) + width / 2.0
            ys = ratio * (ys + old_y - height / 2.0 - new_y) + height / 2.0
            poly = self._track_polyline(xs, ys)
            self._track_proj = (
                data_id,
                width,
                height,
                self._zoom,
                self._center_lat,
                self._center_lon,
                xs,
                ys,
                poly,
            )
            return xs, ys, poly
        xs, ys = self.convert_geographic_to_screen_batch(
            np.array([p.lat for p in self.points]),
            np.array([p.lon for p in self.points]),
        )
        poly = self._track_polyline(xs, ys)
        self._track_proj = (
            data_id,
            width,
            height,
            self._zoom,
            self._center_lat,
            self._center_lon,
            xs,
            ys,
            poly,
        )
        return xs, ys, poly

    def _track_polyline(self, xs, ys):
        count = len(xs)
        if count > self._MAX_TRACK_POLYLINE:
            stride = -(-count // self._MAX_TRACK_POLYLINE)
            index = np.arange(0, count, stride)
            if index[-1] != count - 1:
                index = np.append(index, count - 1)
            xs, ys = xs[index], ys[index]
        return QPolygonF([QPointF(x, y) for x, y in zip(xs, ys)])

    def _paint_track_overlay(self, painter):
        if not self.points:
            return
        xs, ys, poly = self._track_screen_points()
        style = Style.Track.LogTrack
        painter.setPen(QPen(QColor.fromRgbF(*style.color), style.line_width or 1))
        painter.drawPolyline(poly)
        index = len(self.points) - 1
        if self.position is not None and self.offsets:
            index = max(
                0,
                min(
                    index,
                    int(np.searchsorted(self.offsets, self.position, side="right")) - 1,
                ),
            )
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(QPen(QColor("#d8851b"), 2))
        painter.drawEllipse(QPointF(xs[index], ys[index]), 5, 5)

    def _paint_dashboard(self, painter):
        if not self.dashboard_visible:
            return
        key = self._dash_key()
        if self._dash_cache is None or self._dash_cache[0] != key:
            self._dash_cache = (key, self._dashboard_image())
        painter.drawImage(QPointF(0, 0), self._dash_cache[1])

    def _paint_caption(self, painter):
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

    def _navigation_image(self):
        factor = self.get_scale_factor()
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            math.ceil(self.get_allocated_width() * factor),
            math.ceil(self.get_allocated_height() * factor),
        )
        surface.set_device_scale(factor, factor)
        cr = cairo.Context(surface)
        frames = ((self.isochrone_frame,),) if self.isochrone_frame.isochrones else ()
        self.isochrones.draw(self, cr, frames, "routing-live")
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
            self.marine.sources,
            self.marine.currents_visible,
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
        if not self.is_moving() and self.move_callback is None:
            marine = self.marine.cursor_text(lat, lon)
            if marine:
                text = marine + " - " + text
        self.cursor_readout.emit(text)
        if (
            self.is_moving()
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
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.drag = None
        super().resizeEvent(event)

    def begin_move(self, callback):
        self._cancel_cursor()
        self._cancel_inertia()
        self._finish_zoom_silent()
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
            math.ceil(self.get_allocated_width() * factor),
            math.ceil(self.get_allocated_height() * factor),
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
            getattr(settings, "gribIsobarsVisible", True),
            self.wind_visible,
            getattr(settings, "gribIsobarInterval", 4.0),
            getattr(settings, "gribIsobarOpacity", 0.85),
            getattr(settings, "gribIsobarLabels", True),
        )
        sources = tuple((grib,) for grib in self.controller.core.grib_manager.gribs)
        if self.wind_visible or getattr(settings, "gribIsobarsVisible", True):
            # Fill/isobars stay in the cached base image (smooth when scaled);
            # barbs paint crisp in paintEvent via _paint_wind_barbs().
            self.wind.draw(self, cr, sources, style, live_barbs=False)
        self.marine.do_draw(self, cr)
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
        self._cancel_inertia()
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
            self._drag_velocity = DragVelocity()
            self._drag_velocity.add(time.monotonic(), self.drag.x(), self.drag.y())
            self._finish_zoom_silent()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual method name
        if self.drag is None:
            self._hover(event.position())
            return
        delta = event.position() - self.drag
        self._pan_pixels(delta.x(), delta.y())
        self.drag = event.position()
        self._drag_velocity.add(time.monotonic(), self.drag.x(), self.drag.y())
        self.update()
        self.view_changed.emit()
        self._hover(event.position())

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event is not None and event.button() != Qt.MouseButton.LeftButton:
            return
        pointer = event.position() if event is not None else self.drag
        dragging = self.drag is not None
        self.drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if not (dragging and pointer is not None and self._start_inertia(pointer)):
            # Settle once the drag or its inertial continuation ends.
            self._refresh_after_gesture()
        self.view_changed.emit()

    def wheelEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        if self._native_gesture:
            event.accept()
            return
        if self._trackpad_pan(event):
            return
        self._finish_trackpad_pan()
        pointer = event.position()
        anchor = self.convert_screen_to_geographic(pointer.x(), pointer.y())
        delta = event.angleDelta().y() / 120 or event.pixelDelta().y() / 60
        if not delta:
            event.accept()
            return
        self._cancel_inertia(settle=False)
        # Accumulate into an animated target instead of jumping: trackpad
        # streams keep extending the glide, discrete notches ease over to it.
        # Painting stays on scaled previews until the animation converges.
        base = self._zoom_target if self._zoom_target is not None else self._zoom
        self._zoom_target = max(1, min(20, base + delta * 0.25))
        self._zoom_anchor = (*anchor.get_degrees(), pointer.x(), pointer.y())
        if not self._smooth_zoom:
            self._zoom = self._zoom_target
            self._recenter_on_anchor()
            self._finish_zoom_silent()
            self._refresh_after_gesture()
            self.view_changed.emit()
            event.accept()
            return
        self._zoom_animating = True
        self._ensure_zoom_tick()
        self._zoom_settle.start(self._ZOOM_SETTLE_MS)
        self.update()
        self.view_changed.emit()
        event.accept()
