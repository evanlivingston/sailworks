"""Retained navigation and dashboard image overlays for Qt map hosts."""

import math
import weakref

import cairo
from PySide6.QtGui import QImage


class MapOverlayServices:
    """Reusable construction boundary for non-CPU map overlay renderers."""

    def __init__(self, controller, time_control, marine_visible=True):
        from gweatherrouting.rendering.navigation import NavigationRenderer
        from gweatherrouting.rendering.ais import AISRenderer
        from gweatherrouting.rendering.dashboard import DashboardRenderer
        from gweatherrouting.rendering.isochrones import IsochronesRenderer
        from .marinestate import MarineDisplayState
        from .icoadsstate import IcoadsState

        self.navigation = NavigationRenderer(controller.core)
        self.ais_renderer = AISRenderer()
        self.isochrones = IsochronesRenderer(controller.scheduler.submit)
        self.marine = MarineDisplayState(controller.core.marine_manager, time_control)
        self.ship_observations = IcoadsState()
        self.marine.visible = marine_visible
        self.marine.set_current_density(controller.settings.marineCurrentDensity)
        marine_ref = weakref.ref(self.marine)

        def current_density_changed(value):
            marine = marine_ref()
            if marine is not None:
                marine.set_current_density(value)

        controller.settings.register_on_change(
            "marineCurrentDensity", current_density_changed
        )
        self.dashboard = DashboardRenderer()

    @classmethod
    def create(cls, controller, time_control, marine_visible=True):
        return cls(controller, time_control, marine_visible)

    def forget(self, host):
        """Release retained overlay resources while a map host is hidden."""
        self.isochrones.forget(host)
        self.marine.forget(host)

    def close(self, host):
        """Close terminal overlay resources for a map host."""
        isochrones = getattr(self, "isochrones", None)
        if isochrones is not None:
            try:
                isochrones.forget(host)
            except Exception:
                pass
        marine = getattr(self, "marine", None)
        if marine is not None:
            try:
                marine.close()
            except Exception:
                pass
        ship_observations = getattr(self, "ship_observations", None)
        if ship_observations is not None:
            try:
                ship_observations.close()
            except Exception:
                pass


class RetainedOverlayMixin:
    """Build retained overlay images from state supplied by the map host."""

    @property
    def navigation(self):
        services = getattr(self, "_overlay_services", None)
        if services is not None:
            return services.navigation
        return getattr(self, "_navigation", None)

    @navigation.setter
    def navigation(self, value):
        self._navigation = value

    @property
    def ais_renderer(self):
        services = getattr(self, "_overlay_services", None)
        if services is not None:
            return services.ais_renderer
        return getattr(self, "_ais_renderer", None)

    @ais_renderer.setter
    def ais_renderer(self, value):
        self._ais_renderer = value

    @property
    def isochrones(self):
        services = getattr(self, "_overlay_services", None)
        if services is not None:
            return services.isochrones
        return getattr(self, "_isochrones", None)

    @isochrones.setter
    def isochrones(self, value):
        self._isochrones = value

    @property
    def marine(self):
        services = getattr(self, "_overlay_services", None)
        if services is not None:
            return services.marine
        return getattr(self, "_marine", None)

    @marine.setter
    def marine(self, value):
        self._marine = value

    @property
    def ship_observations(self):
        services = getattr(self, "_overlay_services", None)
        if services is not None:
            return services.ship_observations
        return getattr(self, "_ship_observations", None)

    @ship_observations.setter
    def ship_observations(self, value):
        self._ship_observations = value

    @property
    def dashboard(self):
        services = getattr(self, "_overlay_services", None)
        if services is not None:
            return services.dashboard
        return getattr(self, "_dashboard", None)

    @dashboard.setter
    def dashboard(self, value):
        self._dashboard = value

    # Aliases for the retained static race-geometry cache. All names share one
    # store so callers can use any of the conventional spellings.
    @property
    def _race_geometry_cache(self):
        return getattr(self, "_race_cache_store", None)

    @_race_geometry_cache.setter
    def _race_geometry_cache(self, value):
        self._race_cache_store = value

    @property
    def _race_tracks_cache(self):
        return getattr(self, "_race_cache_store", None)

    @_race_tracks_cache.setter
    def _race_tracks_cache(self, value):
        self._race_cache_store = value

    @property
    def _race_cache(self):
        return getattr(self, "_race_cache_store", None)

    @_race_cache.setter
    def _race_cache(self, value):
        self._race_cache_store = value

    @property
    def _static_race_cache(self):
        return getattr(self, "_race_cache_store", None)

    @_static_race_cache.setter
    def _static_race_cache(self, value):
        self._race_cache_store = value

    def _race_geometry_key(self):
        core = self.controller.core
        width, height, ratio = self._view_pixel_size()
        # Include the current view camera so pan/zoom invalidates the static
        # race geometry, while marker-only scrubbing (which does not change the
        # view) can reuse the cached image.
        try:
            view = (self._center_lat, self._center_lon, self._zoom)
        except Exception:
            view = None
        return (
            width,
            height,
            ratio,
            view,
            self._collection_snapshot(core.trackManager),
            self._collection_snapshot(core.poiManager),
        )

    def _race_tracks_key(self):
        return self._race_geometry_key()

    def _race_geometry_image(self):
        key = self._race_geometry_key()
        cache = self._race_geometry_cache
        if isinstance(cache, tuple) and len(cache) >= 2 and cache[0] == key:
            return cache[1]

        import cairo
        from PySide6.QtGui import QImage

        factor = self.get_scale_factor()
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            math.ceil(self.get_allocated_width() * factor),
            math.ceil(self.get_allocated_height() * factor),
        )
        surface.set_device_scale(factor, factor)
        cr = cairo.Context(surface)
        self.navigation.draw_race_tracks(self, cr)
        surface.flush()
        image = QImage(
            surface.get_data(),
            surface.get_width(),
            surface.get_height(),
            surface.get_stride(),
            QImage.Format.Format_ARGB32_Premultiplied,
        ).copy()
        image.setDevicePixelRatio(factor)
        self._race_geometry_cache = (key, image)
        return image

    def _race_tracks_image(self):
        return self._race_geometry_image()

    def invalidate_race_geometry(self):
        self._race_cache_store = None

    def invalidate_race_tracks(self):
        self.invalidate_race_geometry()

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
            tuple(
                getattr(self, "race_boat_positions", ())
                or getattr(self, "race_positions", ())
                or ()
            ),
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
        )

    def _navigation_image(self):
        from types import SimpleNamespace

        factor = self.get_scale_factor()
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            math.ceil(self.get_allocated_width() * factor),
            math.ceil(self.get_allocated_height() * factor),
        )
        surface.set_device_scale(factor, factor)
        cr = cairo.Context(surface)
        isochrone_frame = getattr(
            self, "isochrone_frame", SimpleNamespace(isochrones=())
        )
        frames = (
            ((isochrone_frame,),) if getattr(isochrone_frame, "isochrones", ()) else ()
        )
        isochrones = getattr(self, "isochrones", None)
        if isochrones is not None:
            try:
                isochrones.draw(self, cr, frames, "routing-live")
            except Exception:
                pass
        navigation = getattr(self, "navigation", None)
        if navigation is not None:
            try:
                navigation.draw_routes(self, cr, self.time_control.time.timestamp())
            except Exception:
                pass
            try:
                navigation.draw_navigation(self, cr)
            except Exception:
                pass
        # Compose the cached race geometry instead of redrawing race tracks on
        # every navigation image. The cache is keyed on view/track state, so
        # marker-only scrubbing reuses the previously rendered geometry.
        race_geometry_image = getattr(self, "_race_geometry_image", None)
        if callable(race_geometry_image):
            cached_race_image = race_geometry_image()
            if isinstance(cached_race_image, QImage) and not cached_race_image.isNull():
                # Keep the Qt image and its buffer alive for the lifetime of the
                # Cairo surface that wraps them; Cairo does not take ownership of
                # the external buffer.
                cached_bits = cached_race_image.bits()
                if cached_bits is None:
                    raise RuntimeError("cached race image has no backing buffer")
                cached_width = cached_race_image.width()
                cached_height = cached_race_image.height()
                cached_stride = cached_race_image.bytesPerLine()
                if cached_width <= 0 or cached_height <= 0 or cached_stride <= 0:
                    raise ValueError("cached race image has invalid dimensions")
                cached_surface = cairo.ImageSurface.create_for_data(
                    cached_bits,
                    cairo.FORMAT_ARGB32,
                    cached_width,
                    cached_height,
                    cached_stride,
                )
                # Retain references in locals so the wrapped buffer cannot be
                # collected while Cairo reads it during painting.
                _race_composition_refs = (
                    cached_race_image,
                    cached_bits,
                    cached_surface,
                )
                cached_surface.set_device_scale(factor, factor)
                cr.set_source_surface(cached_surface, 0, 0)
                cr.paint()
                cached_surface.flush()
        elif navigation is not None and hasattr(navigation, "draw_race_tracks"):
            navigation.draw_race_tracks(self, cr)
        if getattr(self, "ais_visible", False):
            ais_renderer = getattr(self, "ais_renderer", None)
            ais_targets = getattr(self, "ais_targets", ())
            if ais_renderer is not None:
                try:
                    ais_renderer.draw_targets(self, cr, ais_targets)
                except Exception:
                    pass
        from gweatherrouting.core.mapviewport import MapPoint

        positions = getattr(self, "race_boat_positions", ()) or getattr(
            self, "race_positions", ()
        )
        if positions:
            for position in positions:
                try:
                    lat, lon = position
                except Exception:
                    continue
                try:
                    x, y = self.convert_geographic_to_screen(
                        MapPoint.new_degrees(float(lat), float(lon))
                    )
                except Exception:
                    continue
                cr.save()
                cr.set_source_rgba(0.95, 0.2, 0.1, 1.0)
                cr.arc(x, y, 7, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
                cr.set_line_width(1.5)
                cr.arc(x, y, 7, 0, 2 * math.pi)
                cr.stroke()
                cr.restore()
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
