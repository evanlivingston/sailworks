"""Shared sizing and invalidation operations for Qt map hosts."""

import math

from PySide6.QtCore import QRectF


class MapHostMixin:
    """Small QWidget-facing helpers used by map state and renderer services."""

    _REUSE_MARGIN = 128
    _RENDER_PADDING = 192
    _PADDING_BYTES = 16 * 1024 * 1024

    def _pan_offset(self, cached_lat, cached_lon):
        """Screen offset that re-aligns a cached layer with the current view."""
        return (
            self._lon_to_x(cached_lon) - self._lon_to_x(self._center_lon),
            self._lat_to_y(cached_lat) - self._lat_to_y(self._center_lat),
        )

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
        from gweatherrouting.core.mapviewport import MercatorProjection

        lat = max(-MercatorProjection.MAX_LAT, min(MercatorProjection.MAX_LAT, lat))
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
        # Keep async renderers' canvas identity and restore state on failure.
        padding, zooming = self._render_padding, self._zoom_animating
        self._render_padding = self._layer_padding()
        self._zoom_animating = False
        try:
            return build()
        finally:
            self._render_padding, self._zoom_animating = padding, zooming

    def _paint_layer(self, painter, image, target):
        # Sample only visible pixels, including ancestor-surface redirection.
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
                        -pad_x * ratio,
                        -pad_y * ratio,
                        pad_x * ratio,
                        pad_y * ratio,
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

    def update(self, *args):
        super().update(*args)
        presenter = getattr(self, "_weather_presenter", None)
        if presenter is not None:
            presenter.request()
        gpu = getattr(self, "_gpu", None)
        if gpu is not None and gpu.active:
            gpu.request()

    def queue_draw(self):
        if self.disposed:
            return
        if (
            self._cursor_context is not None
            and self._cursor_context != self._weather_context()
        ):
            self._cancel_cursor()
        if self.is_moving():
            self._overlay_pending = True
            self.update()
            return
        self._invalidate_base()
        self.invalidate_navigation()
        self.update()

    map_redraw_idle = queue_draw

    def get_attribution_bounds(self):
        return self._render_padding, self._render_padding, self.width(), self.height()

    def _boat_changed(self, _):
        self.update()

    def _invalidate_dashboard(self):
        self._dash_cache = None

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

    def get_scale_factor(self):
        return math.ceil(self.devicePixelRatioF())

    def get_allocated_width(self):
        return self.width() + 2 * self._render_padding

    def get_allocated_height(self):
        return self.height() + 2 * self._render_padding

    def get_attribution_bounds(self):
        return self._render_padding, self._render_padding, self.width(), self.height()

    def _invalidate_base(self):
        self._base_cache = None
        self._environment_cache = None
        self.background = None

    def invalidate_navigation(self):
        """Drop the cached navigation overlay (data changed, view kept)."""
        self._nav_cache = None

    def _view_pixel_size(self):
        return (
            self.width(),
            self.height(),
            round(float(self.devicePixelRatioF()), 3),
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
