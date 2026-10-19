"""Qt canvas using the shared Mercator projection and a Cairo image bridge."""

import math
import sys
import time
from typing import Optional, cast

import cairo
import numpy as np
from PySide6.QtCore import QEvent, QPointF, Qt, QTimer, Signal, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget, QMenu, QLabel, QPushButton

from gweatherrouting.core.mapviewport import MercatorProjection, MapPoint, MapViewport
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
from gweatherrouting.rendering.panprefetch import pan_prefetch_offset
from gweatherrouting.rendering.overlayrenderer import MARGIN as WEATHER_MARGIN
from .basecharts import BaseChartRenderer


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
    _ZOOM_PREFETCH_MS = 100
    # Per-tick zoom easing rate and snap distance for the smooth animation.
    _ZOOM_EASE_RATE = 14.0
    _ZOOM_SNAP = 0.004
    # Cap for the log-track polyline; denser logs are subsampled uniformly,
    # which only drops sub-pixel detail while keeping every paint cheap.
    _MAX_TRACK_POLYLINE = 1500
    _FETCH_BUTTON_STYLE = (
        "background: rgba(30, 33, 36, 220); color: #9fc3e0; "
        "padding: 6px 10px; border-radius: 4px; border: 1px solid #5295d2;"
    )
    # Suppress a repeat Fetch for a view/time/selection whose gaps survived
    # the last fetch (domain-edge points no re-download can fill). Newer
    # model cycles arrive within hours, so the offer returns after this.
    _FETCH_SUPPRESS_S = 6 * 3600

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
        self._gpu = None
        self._gpu_sequence = 0
        self._gpu_geometry = None
        self._gpu_batches = ()
        self._gpu_wind_raster: Optional[QImage] = None
        self._gpu_raster_key = None
        self._gpu_wind_mode = None
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
        self._release_velocity: tuple | None = None
        self._trackpad_velocity: tuple = (0.0, 0.0)
        self._trackpad_last_event: float | None = None
        self._trackpad_last_speculation: float = 0.0
        self._inertia: Optional[PanInertia] = None
        self._inertia_tick_token = None
        self._inertia_last_tick = 0.0
        self._track_proj = None  # (data id, w, h, zoom, lat, lon, xs, ys, poly)
        self._ais_targets_state: tuple = ()
        self._ais_visible_state = True
        self._zoom_settle = QTimer(self)
        self._zoom_settle.setSingleShot(True)
        self._zoom_settle.timeout.connect(self._zoom_settle_timeout)
        self._zoom_prefetch = QTimer(self)
        self._zoom_prefetch.setSingleShot(True)
        self._zoom_prefetch.timeout.connect(self._prefetch_zoom_target)
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
        self._base_chart_renderer = BaseChartRenderer(controller.scheduler.submit)
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
        self._cursor_parts = {"wind": "", "current": "", "depth": ""}
        self._cursor_coords = ""
        self._cursor_position = None
        self._wind_cursor = WindCursorSampler(self, controller.scheduler.submit)
        controller.data.changed.connect(self._cancel_cursor)
        self.coverage_notice = QLabel(self)
        self.coverage_notice.setWordWrap(True)
        self.coverage_notice.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.coverage_notice.setStyleSheet(
            "background: rgba(30, 33, 36, 220); color: #e5c17c; padding: 8px; border-radius: 4px;"
        )
        self.coverage_notice.hide()
        self.coverage_fetch = QPushButton("Fetch", self)
        self.coverage_fetch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.coverage_fetch.setStyleSheet(self._FETCH_BUTTON_STYLE)
        self.coverage_fetch.hide()
        self.coverage_fetch.clicked.connect(
            lambda _checked=False: self._fetch_missing_coverage()
        )
        self.coverage_fetch_buttons = {}
        self._coverage_gaps = None
        self._coverage_text = ""
        self._fetch_awaiting = {}
        self._fetch_done = {}
        self._fetch_initiator = None
        controller.data.task.changed.connect(self._download_progress)
        controller.data.forecast_loaded.connect(self._note_installed_forecast)
        controller.data.task.finished.connect(self._fetch_finished)
        self.weather_coverage = WeatherCoverage(self, self._show_coverage)
        controller.boat_changed.connect(self._boat_changed)
        controller.settings.register_on_change("smoothZoom", self._set_smooth_zoom)
        controller.settings.register_on_change("panInertia", self._set_pan_inertia)
        if getattr(controller, "map_renderer", "cpu") == "gpu":
            try:
                from .gpupresenter import GpuPresenter

                self._gpu = GpuPresenter(self)
                if self._gpu.active:
                    controller.set_renderer_status(self, "pending")
            except (ImportError, RuntimeError, OSError) as exc:
                import logging

                logging.getLogger("gweatherrouting").warning(
                    "GPU map unavailable; using CPU: %s", exc
                )
                controller.set_renderer_status(self, "cpu", reason=str(exc))
        elif hasattr(controller, "set_renderer_status"):
            controller.set_renderer_status(self, "cpu")

    def update(self, *args):
        super().update(*args)
        gpu = getattr(self, "_gpu", None)
        if gpu is not None and gpu.active:
            gpu.request()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self._gpu is not None and self._gpu.active:
            self._gpu.request()

    def _time_changed(self, _):
        self._cancel_cursor()
        gpu = getattr(self, "_gpu", None)
        stager = getattr(gpu, "weather", None)
        if (
            gpu is not None
            and gpu.active
            and stager is not None
            and not self._marine_time_dependent()
            and stager.request(self) is not None
        ):
            # The numerical field is already staged. CPU fill/isobar workers
            # still receive exact-time requests in _gpu_frame, and queue_draw
            # invalidates the base when a completed background arrives. Do
            # not upload the same cached background for every uniform change.
            self.invalidate_navigation()
            self.update()
            return
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
        if self._gpu is not None:
            self._gpu.close()
        if hasattr(self.controller, "forget_renderer"):
            self.controller.forget_renderer(self)
        self._wind_cursor.close()
        self.controller.data.changed.disconnect(self._cancel_cursor)
        self.weather_coverage.close()
        self._disconnect_optional(
            self.controller.data.task.changed, self._download_progress
        )
        self._disconnect_optional(
            self.controller.data.forecast_loaded, self._note_installed_forecast
        )
        self._disconnect_optional(
            self.controller.data.task.finished, self._fetch_finished
        )
        self.cursor_readout.emit("")
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.controller.boat_changed.disconnect(self._boat_changed)
        self.charts.forget_map(self)
        # Terminal teardown: close the renderers so their retained handles and
        # prepared data are released, even if a queued completion callback
        # never runs. Hide/show keeps the reusable forget path.
        self._base_chart_renderer.close()
        self.wind.close()
        self.isochrones.forget(self)
        self.marine.close()
        for token in list(self._ticks):
            self.remove_tick_callback(token)
        self.time_control.disconnect("time_change", self._time_changed)

    def _disconnect_optional(self, signal, slot):
        """Disconnect a signal, tolerating a slot that was never connected."""
        try:
            signal.disconnect(slot)
        except (RuntimeError, TypeError):
            pass

    def hideEvent(self, event):  # noqa: N802 - Qt virtual method name
        if self._gpu is not None:
            self._gpu.suspend()
        self._gpu_geometry = None
        self._gpu_batches = ()
        self._gpu_wind_raster = None
        self._gpu_raster_key = None
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.drag = None
        self.cancel_move()
        self.charts._chs_renderer.forget(self)
        self._base_chart_renderer.forget(self)
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

    def invalidate_chart_sources(self):
        """Apply explicit source switches even while a gesture preview is retained."""
        self._cancel_cursor()
        self._invalidate_base()
        self.queue_draw()

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
            getattr(settings, "gribWindStyle", "barbs"),
            (
                getattr(settings, "gribBarbDensity", "standard")
                if getattr(settings, "gribWindStyle", "barbs") == "streamlines"
                else "standard"
            ),
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

    def _marine_time_dependent(self):
        sources = self.marine.sources
        return self.marine.visible and bool(
            sources.fields or sources.levels.datasets or sources.currents.datasets
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
        cache, target = self._resolve_reusable_layer(cache, key, build)
        self._paint_layer(painter, cache[4], target)
        return cache

    def _resolve_reusable_layer(self, cache, key, build):
        """Resolve a cache and its transform independently of its presenter."""
        width, height, _ = self._view_pixel_size()
        if cache is not None and cache[0] == key:
            _, cached_lat, cached_lon, cached_zoom, image = cache
            size = image.deviceIndependentSize()
            pad_x, pad_y = (size.width() - width) / 2, (size.height() - height) / 2
            if cached_zoom == self._zoom:
                offset_x, offset_y = self._pan_offset(cached_lat, cached_lon)
                margin = min(self._REUSE_MARGIN, max(0, min(pad_x, pad_y) * 2 / 3))
                if max(abs(offset_x), abs(offset_y)) <= margin:
                    return cache, QRectF(
                        offset_x - pad_x,
                        offset_y - pad_y,
                        size.width(),
                        size.height(),
                    )
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
                        return cache, target
        image = self._build_padded_layer(build)
        size = image.deviceIndependentSize()
        target = QRectF(
            (width - size.width()) / 2,
            (height - size.height()) / 2,
            size.width(),
            size.height(),
        )
        return (key, self._center_lat, self._center_lon, self._zoom, image), target

    def _gpu_frame(self):
        from .mapframe import ImageFrame, MapFrame, prepare_wind

        numerical_wind = None
        stager = getattr(self._gpu, "weather", None)
        if stager is not None:
            numerical_wind = stager.request(self)
        base_key = self._base_key()
        if numerical_wind is not None and not self._marine_time_dependent():
            # Queue asynchronous exact-time fill/isobars without allocating a
            # new Cairo raster or uploading unchanged chart pixels. Completion
            # still invalidates the base through the existing queue_draw hook.
            self._build_padded_layer(
                lambda: self._draw_weather_background(
                    cairo.Context(
                        cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None)
                    )
                )
            )
            state = self.wind._states.get(self)
            published = state.frame if state is not None else None
            wind_key = base_key[4]
            base_key = base_key[:4] + ((id(published),) + wind_key[1:],) + base_key[5:]
        previous_base = self._base_cache
        self._base_cache, base_target = self._resolve_reusable_layer(
            self._base_cache, base_key, self._base_image
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
        self._nav_cache, nav_target = self._resolve_reusable_layer(
            self._nav_cache, self._nav_key(), self._navigation_image
        )
        geometry, opacity = ({}, 0.0)
        weather_item = getattr(getattr(self._gpu, "scene", None), "weather", None)
        shader_ready = weather_item is not None and weather_item.property("ready")
        if numerical_wind is not None and shader_ready:
            opacity = float(self.controller.settings.gribArrowOpacity)
        elif self.wind_visible:
            geometry, opacity = self.wind.live_geometry_for_view(
                self,
                self.width(),
                self.height(),
                self._center_lat,
                self._center_lon,
                self._zoom,
                density=getattr(
                    self.controller.settings, "gribBarbDensity", "standard"
                ),
            )
        if numerical_wind is not None:
            opacity = float(self.controller.settings.gribArrowOpacity)
        ratio = float(self.devicePixelRatioF())
        raster_wind = None
        if ratio < 2:
            key = (id(geometry), opacity, self.width(), self.height(), ratio)
            if geometry and key != self._gpu_raster_key:
                self._gpu_wind_raster = self._wind_raster(geometry, opacity, ratio)
                self._gpu_raster_key = key
            if geometry:
                # The raster was rendered above whenever the geometry exists.
                image = cast(QImage, self._gpu_wind_raster)
                raster_wind = ImageFrame(
                    image, (0, 0, image.width() / ratio, image.height() / ratio)
                )
            self._gpu_batches = ()
            self._gpu_wind_mode = "cpu"
        else:
            if geometry is not self._gpu_geometry or self._gpu_wind_mode != "gpu":
                self._gpu_batches = prepare_wind(geometry)
            self._gpu_wind_raster = None
            self._gpu_raster_key = None
            self._gpu_wind_mode = "gpu"
        self._gpu_geometry = geometry
        self._gpu_sequence += 1

        def image_frame(cache, target):
            return ImageFrame(
                cache[4],
                (target.x(), target.y(), target.width(), target.height()),
            )

        frame = MapFrame(
            self._gpu_sequence,
            self.width(),
            self.height(),
            ratio,
            self._sea_color().getRgbF(),
            image_frame(self._base_cache, base_target),
            image_frame(self._nav_cache, nav_target),
            self._gpu_batches,
            opacity,
            raster_wind,
            numerical_wind,
        )
        self._check_weather_coverage()
        return frame

    def is_moving(self):
        return (
            self.drag is not None
            or self._inertia is not None
            or self._zoom_animating
            or self._native_gesture
            or self._trackpad_panning
        )

    def event(self, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            gpu = getattr(self, "_gpu", None)
            if gpu is not None and gpu.active:
                gpu.request()
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
                self._schedule_zoom_prefetch()
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
            self._trackpad_velocity = (0.0, 0.0)
            self._trackpad_last_event = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        # Qt has already applied the system natural-scroll preference. Native
        # momentum arrives as further pixel deltas; never add our drag inertia.
        now = time.monotonic()
        if self._trackpad_last_event is not None and now > self._trackpad_last_event:
            elapsed = now - self._trackpad_last_event
            instant = (pixels.x() / elapsed, pixels.y() / elapsed)
            previous = self._trackpad_velocity
            self._trackpad_velocity = (
                0.7 * previous[0] + 0.3 * instant[0],
                0.7 * previous[1] + 0.3 * instant[1],
            )
        self._trackpad_last_event = now
        self._pan_pixels(pixels.x(), pixels.y())
        if now - self._trackpad_last_speculation >= 0.5:
            if self._prefetch_along(*self._trackpad_velocity):
                self._trackpad_last_speculation = now
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
        self._zoom_prefetch.stop()
        if self._zoom_tick_token is not None:
            self.remove_tick_callback(self._zoom_tick_token)
            self._zoom_tick_token = None
        self._zoom_target = None
        self._zoom_animating = False
        self._zoom_anchor = None

    def _schedule_zoom_prefetch(self):
        if not getattr(self, "_zoom_prefetch_enabled", True):
            return
        # Every changed input target invalidates unfinished speculation; only
        # a quiet target starts a new job, not every notch in a wheel stream.
        self.wind.cancel_speculation(self)
        self._zoom_prefetch.start(self._ZOOM_PREFETCH_MS)

    def _prefetch_zoom_target(self):
        if self.disposed or not self.isVisible() or not self._zoom_animating:
            return False
        target = self._zoom_target if self._zoom_target is not None else self._zoom
        view = MapViewport(self)
        view._zoom = target
        if self._zoom_anchor is not None:
            lat, lon, px, py = self._zoom_anchor
            x, y = view.convert_geographic_to_screen(MapPoint(lat, lon))
            center = view.convert_screen_to_geographic(
                view.width / 2 + x - px, view.height / 2 + y - py
            )
            latitude, longitude = center.get_degrees()
        else:
            latitude, longitude = self._center_lat, self._center_lon
        return self.wind.prefetch_at(self, latitude, longitude, zoom=target)

    def _refresh_after_gesture(self):
        if self._overlay_pending:
            self._overlay_pending = False
            self._invalidate_base()
            self.invalidate_navigation()
        self._prefetch_weather_ahead()
        self.update()

    def _prefetch_weather_ahead(self):
        """Queue one speculative weather frame in the release direction."""
        vector = self._release_velocity
        self._release_velocity = None
        if vector is None or self.disposed:
            return
        self._prefetch_along(vector[0], vector[1])

    def _prefetch_along(self, vx, vy):
        """Queue one speculative frame along a pixel velocity vector."""
        if self.disposed:
            return False
        predictive = getattr(self, "_adaptive_pan_prefetch", True)
        seconds = self.wind.pan_lookahead(self) if predictive else 1.0
        # Stay within the worker's margin so predicted work can serve visible
        # demand before completion, instead of being cancelled on approach.
        offset = pan_prefetch_offset(
            vx,
            vy,
            self.width(),
            self.height(),
            seconds,
            WEATHER_MARGIN if predictive else None,
        )
        if offset is None:
            return False
        center = self.convert_screen_to_geographic(
            self.width() / 2 - offset[0], self.height() / 2 - offset[1]
        )
        latitude, longitude = center.get_degrees()
        return self.wind.prefetch_at(self, latitude, longitude, predictive=predictive)

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
        vx, vy = self._drag_velocity.release(time.monotonic(), pointer.x(), pointer.y())
        self._release_velocity = (vx, vy)
        if not self._pan_inertia_enabled:
            return False
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
        if self._gpu is not None and self._gpu.active:
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
        TrackMap._paint_wind_geometry(painter, geometry, opacity)

    def _wind_raster(self, geometry, opacity, ratio):
        image = QImage(
            math.ceil(self.width() * ratio),
            math.ceil(self.height() * ratio),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setClipRect(self.rect())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            TrackMap._paint_wind_geometry(painter, geometry, opacity)
        finally:
            painter.end()
        return image

    @staticmethod
    def _paint_wind_geometry(painter, geometry, opacity):
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

    def _show_coverage(self, text, gaps=None):
        if self.disposed:
            return
        if gaps is not None:
            self._coverage_gaps = gaps
        elif text != self.coverage_notice.text():
            self._coverage_gaps = None
        for model in list(self._fetch_done):
            if model not in (self._coverage_gaps or {}):
                del self._fetch_done[model]
        self._coverage_text = text
        suppressed = self._fetch_suppressed()
        if text and suppressed and self._coverage_gaps:
            lines = []
            for model, missing in self._coverage_gaps.items():
                short = model.removeprefix("NOAA ").removeprefix("CMC ")
                status = ", ".join(missing)
                if model in suppressed:
                    status += " (downloaded; gaps remain)"
                lines.append(f"{short}: {status}")
            text = "\n".join(lines)
        self.coverage_notice.setText(text)
        visible = bool(text) and self.isVisible()
        self.coverage_notice.setFixedWidth(max(80, min(360, self.width() - 24)))
        self.coverage_notice.adjustSize()
        self.coverage_notice.setVisible(visible)
        self._refresh_fetch_buttons()
        self.coverage_notice.raise_()
        for button in self._visible_fetch_buttons():
            button.raise_()

    def _layout_coverage(self):
        self.coverage_notice.move(
            self.width() - self.coverage_notice.width() - 12,
            self.height() - self.coverage_notice.height() - 12,
        )
        buttons = self._visible_fetch_buttons()
        if not buttons:
            return
        for button in buttons:
            button.adjustSize()
        width = max(button.width() for button in buttons)
        spacing = 6
        total = sum(button.height() for button in buttons) + spacing * (
            len(buttons) - 1
        )
        x = self.coverage_notice.x() - width - 8
        if x < 8:
            x = self.width() - width - 12
            y = self.coverage_notice.y() - total - 6
        else:
            y = self.coverage_notice.y() + self.coverage_notice.height() - total
        y = max(8, y)
        for button in buttons:
            button.move(x + width - button.width(), y)
            y += button.height() + spacing

    def _visible_fetch_buttons(self):
        """Visible fetch buttons: per-model in notice order, Fetch all last."""
        gaps = self._coverage_gaps or {}
        ordered = [
            self.coverage_fetch_buttons[model]
            for model in gaps
            if model in self.coverage_fetch_buttons
            and self.coverage_fetch_buttons[model].isVisible()
        ]
        if self.coverage_fetch.isVisible():
            ordered.append(self.coverage_fetch)
        return ordered

    def _ensure_fetch_button(self, model):
        button = self.coverage_fetch_buttons.get(model)
        if button is None:
            button = QPushButton(model.removeprefix("NOAA ").removeprefix("CMC "), self)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(self._FETCH_BUTTON_STYLE)
            button.hide()
            button.clicked.connect(
                lambda _checked=False, m=model: self._fetch_missing_coverage((m,))
            )
            self.coverage_fetch_buttons[model] = button
        return button

    def _refresh_fetch_buttons(self, estimate=True):
        """Rebuild Fetch-all plus per-model button visibility, labels, layout."""
        fresh = self._fetch_models()
        downloading = self._forecast_downloading()
        sizes = (
            self._estimate_models_coverage(fresh)
            if estimate and not downloading
            else {}
        )
        bounds_ok = self._view_download_bounds() is not None
        base = (
            bool(self.coverage_notice.text())
            and self.coverage_notice.isVisible()
            and bounds_ok
            and (downloading or not self._download_busy())
        )
        if downloading:
            percent = self.controller.data.task.percent
            progress = f"Fetching… {percent}%" if percent >= 0 else "Fetching…"
        else:
            progress = ""
        # Exactly one progress readout: the button that started the download,
        # or Fetch all for downloads started elsewhere.
        initiator = self._fetch_initiator if downloading else None
        for model in fresh:
            button = self._ensure_fetch_button(model)
            short = model.removeprefix("NOAA ").removeprefix("CMC ")
            if downloading:
                show = bool(base and model == initiator)
                button.setText(f"{short} · {progress}" if show else short)
            else:
                show = bool(base)
                size = sizes.get(model)
                button.setText(f"{short} ({size})" if size else short)
            button.setVisible(show)
            button.setEnabled(bool(show and not downloading))
        for model, button in self.coverage_fetch_buttons.items():
            if model not in fresh:
                button.hide()
        # Fetch all reads out downloads it started, or ones started elsewhere
        # that cannot be attributed to a model button.
        show_all = bool(
            base
            and fresh
            and (not downloading or initiator == "all" or not self._fetch_awaiting)
        )
        if downloading:
            self.coverage_fetch.setText(progress)
        elif len(fresh) == 1:
            size = sizes.get(fresh[0])
            self.coverage_fetch.setText(f"Fetch ({size})" if size else "Fetch")
        else:
            self.coverage_fetch.setText("Fetch all")
        self.coverage_fetch.setVisible(show_all)
        self.coverage_fetch.setEnabled(bool(show_all and not downloading))
        self._layout_coverage()
        for button in self._visible_fetch_buttons():
            button.raise_()

    def _fetch_models(self):
        suppressed = set(self._fetch_suppressed())
        from gweatherrouting.core.modeldownloads import PROVIDERS

        gaps = self._coverage_gaps or {}
        selected = tuple(self.controller.data.selection.models)
        return tuple(
            m for m in gaps if m in PROVIDERS and m in selected and m not in suppressed
        )

    def _fetch_suppressed(self):
        """Gap models already fetched for this view/time/selection.

        A repeat download would return the same run, so it cannot fill the
        remaining (domain-edge) gaps. Offered again once the view, time, or
        selection moves, a newer run lands, or the window lapses.
        """
        from gweatherrouting.core.modeldownloads import PROVIDERS

        gaps = self._coverage_gaps or {}
        selected = tuple(self.controller.data.selection.models)
        models = tuple(m for m in gaps if m in PROVIDERS and m in selected)
        context = self._coverage_context()
        now = time.monotonic()
        return tuple(
            model
            for model in models
            if (record := self._fetch_done.get(model)) is not None
            and context is not None
            and record[0] == context
            and record[1] is not None
            and self._model_max_start(model) == record[1]
            and now - record[2] < self._FETCH_SUPPRESS_S
        )

    def _coverage_context(self):
        """Coverage request inputs minus the installed-sources snapshot."""
        key = self.weather_coverage.key
        return key[:4] if key else None

    def _model_max_start(self, model):
        from gweatherrouting.core.forecastselection import forecast_source

        try:
            starts = [
                grib.start_time
                for grib in self.controller.core.grib_manager.gribs
                if forecast_source(grib) == model
                and getattr(grib, "start_time", None) is not None
            ]
            return max(starts) if starts else None
        except TypeError:
            return None

    def _note_installed_forecast(self, forecast):
        from gweatherrouting.core.forecastselection import forecast_source

        try:
            model = forecast_source(forecast)
        except Exception:
            return
        context = self._fetch_awaiting.pop(model, None)
        if context is None:
            return
        self._fetch_done[model] = (
            context,
            self._model_max_start(model),
            time.monotonic(),
        )

    def _fetch_finished(self, _result, _error):
        if getattr(self.controller.data, "operation", "") == "forecasts":
            # Failed downloads never reach forecast_loaded; drop them so a
            # retry stays offered.
            self._fetch_awaiting.clear()
            self._fetch_initiator = None

    def _download_busy(self):
        # Mirror DataController.run()'s refusal conditions so the Fetch button
        # is never shown enabled when a click would silently no-op.
        data = self.controller.data
        task = getattr(data, "task", None)
        if task is not None and (task.busy or task.closed):
            return True
        owner = getattr(data, "owner", None) or self.controller
        if getattr(getattr(owner, "routing", None), "busy", False):
            return True
        for name in ("polars", "map_data"):
            subtask = getattr(getattr(owner, name, None), "task", None)
            if getattr(subtask, "busy", False):
                return True
        return False

    def _forecast_downloading(self):
        """A Fetch-style model download started by download_selected is running."""
        data = self.controller.data
        task = getattr(data, "task", None)
        return bool(task is not None and task.busy and data.operation == "forecasts")

    def _download_progress(self):
        """Mirror the running model download on the Fetch buttons, text only."""
        if self.disposed:
            return
        if not self._forecast_downloading():
            if any(
                "Fetching" in button.text()
                for button in (
                    self.coverage_fetch,
                    *self.coverage_fetch_buttons.values(),
                )
                if button.isVisible()
            ):
                # Finished or failed without new coverage data; rebuild the
                # buttons instead of leaving a stale progress label.
                self._show_coverage(self._coverage_text, self._coverage_gaps)
            return
        if self.is_moving():
            return
        # Text/geometry only: never recompute the (possibly network-backed)
        # size estimate on a progress tick.
        self._refresh_fetch_buttons(estimate=False)

    def _view_download_bounds(self):
        try:
            top_left, bottom_right = self.get_bbox()
        except Exception:
            return None
        north, west = top_left.get_degrees()
        south, east = bottom_right.get_degrees()
        if east - west >= 360:
            west, east = -180, 180
        else:
            west, east = (west + 180) % 360 - 180, (east + 180) % 360 - 180
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            return None
        # NOMADS subsets retain grid nodes inside the requested box. Without
        # a halo, edge queries lack the surrounding nodes for interpolation
        # (a zoomed-in GFS request can otherwise be only 3 by 2 nodes).
        # Two GFS 0.25-degree cells also comfortably cover HRRR's 3-km grid.
        south, north = max(-90, south - 0.5), min(90, north + 0.5)
        west, east = max(-180, west - 0.5), min(180, east + 0.5)
        return south, west, north, east

    def _estimate_models_coverage(self, models):
        from gweatherrouting.core.modeldownloads import estimate_missing_download

        gaps = self._coverage_gaps or {}
        if not models:
            return {}
        bounds = self._view_download_bounds()
        if bounds is None:
            return {}
        selection = self.controller.data.selection
        sizes = {}
        for model in models:
            try:
                sizes[model] = estimate_missing_download(
                    {model: gaps[model]}, bounds, selection
                )
            except Exception:
                sizes[model] = None
        return sizes

    def _fetch_missing_coverage(self, models=None):
        fresh = self._fetch_models()
        requested = tuple(models) if models is not None else fresh
        requested = tuple(model for model in requested if model in fresh)
        if not requested or self._download_busy():
            return
        bounds = self._view_download_bounds()
        if bounds is None:
            return
        self._record_fetch_context(requested)
        self._fetch_initiator = "all" if models is None else requested[0]
        widgets = [self.coverage_fetch] + [
            self._ensure_fetch_button(model) for model in requested
        ]
        self._set_fetch_widgets_enabled(widgets, False)
        try:
            started = self.controller.data.download_selected(bounds, requested)
        except Exception:
            self._fetch_initiator = None
            self._set_fetch_widgets_enabled(widgets, True)
            raise
        if started:
            # A download is running; every other fetch would refuse while busy.
            self._set_fetch_widgets_enabled(
                [self.coverage_fetch, *self.coverage_fetch_buttons.values()], False
            )
        else:
            # Another task is busy; keep the buttons usable for a later click.
            self._fetch_initiator = None
            self._set_fetch_widgets_enabled(widgets, True)

    def _record_fetch_context(self, requested):
        """Remember the view context each requested model is fetched for."""
        context = self._coverage_context()
        if context is not None:
            for model in requested:
                self._fetch_awaiting[model] = context

    def _set_fetch_widgets_enabled(self, widgets, enabled):
        """Enable or disable the given fetch buttons in order."""
        for widget in widgets:
            widget.setEnabled(enabled)

    def _check_weather_coverage(self):
        if self.disposed or self.is_moving():
            self.coverage_notice.hide()
            self.coverage_fetch.hide()
            for button in self.coverage_fetch_buttons.values():
                button.hide()
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
            self._show_coverage(self._coverage_text, self._coverage_gaps)

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
        caption = ""
        if self.move_callback is not None:
            caption = "Click to place the selected point · Esc to cancel"
        if self.controller.chart_errors:
            if caption:
                caption += " · "
            caption += "Some saved charts could not be opened"
        if not caption:
            return
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
        self._cursor_position = None
        self._cursor_parts = {"wind": "", "current": "", "depth": ""}
        self.cursor_readout.emit("")

    def _emit_cursor_readout(self):
        parts = [
            part
            for part in (
                self._cursor_parts["wind"],
                self._cursor_parts["current"],
                self._cursor_parts["depth"],
            )
            if part
        ]
        if self._cursor_coords:
            parts.append(self._cursor_coords)
        self.cursor_readout.emit("  ·  ".join(parts))

    def _sample_depth(self):
        if (
            self.disposed
            or not self.isVisible()
            or self._cursor_position is None
            or self._cursor_context != self._weather_context()
        ):
            return
        lat, lon = self._cursor_position
        try:
            depth = self.charts.depth_at(lat, lon, self.get_scale())
        except Exception:
            depth = None
        text = ""
        if depth:
            if "sounding" in depth:
                text = "Sounding %.1f m (%.0f m away)" % (
                    depth["sounding"],
                    depth["distance_m"],
                )
            elif depth.get("area") and all(
                value is not None for value in depth["area"]
            ):
                low, high = sorted(depth["area"])
                text = "Depth %g–%g m" % (low, high)
        self._cursor_parts["depth"] = text

    def _hover(self, position):
        lat, lon = self.convert_screen_to_geographic(
            position.x(), position.y()
        ).get_degrees()
        context = self._weather_context()
        self._cursor_context = context
        self._cursor_position = (lat, lon)
        self._cursor_parts = {"wind": "", "current": "", "depth": ""}
        self._cursor_coords = "Lat %.6f°  Lon %.6f°" % (lat, lon)
        if not self.is_moving() and self.move_callback is None:
            marine = self.marine.cursor_text(lat, lon)
            if marine:
                self._cursor_parts["current"] = marine
        self._sample_depth()
        self._emit_cursor_readout()
        if self.is_moving() or self.move_callback is not None or not self.isVisible():
            self._wind_cursor.cancel()
            return
        if not self.wind_visible:
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
            self._cursor_parts["wind"] = (
                "Wind %.1f°, %.1f kts" % (wind[0], wind[1] * 1.94384)
                if wind is not None
                else ""
            )
            self._emit_cursor_readout()

        self._wind_cursor.request(context[1], context[0], lat, lon, show)

    def leaveEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        super().leaveEvent(event)

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.drag = None
        if self.coverage_notice.isVisible():
            self.coverage_notice.setFixedWidth(max(80, min(360, self.width() - 24)))
            self.coverage_notice.adjustSize()
            self._layout_coverage()
        super().resizeEvent(event)
        if self._gpu is not None and self._gpu.active:
            self._gpu.request()

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
        self._draw_weather_background(cr)
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

    def _draw_weather_background(self, cr):
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
            getattr(settings, "gribWindStyle", "barbs"),
            getattr(settings, "gribBarbDensity", "standard"),
        )
        sources = tuple((grib,) for grib in self.controller.core.grib_manager.gribs)
        if self.wind_visible or getattr(settings, "gribIsobarsVisible", True):
            # Fill/isobars stay in the cached base image (smooth when scaled);
            # barbs paint crisp in paintEvent via _paint_wind_barbs().
            self.wind.draw(self, cr, sources, style, live_barbs=False)

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
            self._release_velocity = None
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
        self._schedule_zoom_prefetch()
        self._ensure_zoom_tick()
        self._zoom_settle.start(self._ZOOM_SETTLE_MS)
        self.update()
        self.view_changed.emit()
        event.accept()
