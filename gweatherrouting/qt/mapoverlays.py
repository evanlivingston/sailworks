"""Retained navigation and dashboard image overlays for Qt map hosts."""

import math

import cairo
from PySide6.QtGui import QImage


class MapOverlayServices:
    """Reusable construction boundary for non-CPU map overlay renderers."""

    def __init__(self, controller, time_control, marine_visible=True):
        from gweatherrouting.rendering.navigation import NavigationRenderer
        from gweatherrouting.rendering.ais import AISRenderer
        from gweatherrouting.rendering.dashboard import DashboardRenderer
        from gweatherrouting.rendering.isochrones import IsochronesRenderer
        from .marineoverlay import MarineOverlay

        self.navigation = NavigationRenderer(controller.core)
        self.ais_renderer = AISRenderer()
        self.isochrones = IsochronesRenderer(controller.scheduler.submit)
        self.marine = MarineOverlay(
            controller.core.marine_manager,
            time_control,
            controller.scheduler.submit,
        )
        self.marine.visible = marine_visible
        self.dashboard = DashboardRenderer(controller.connections)

    @classmethod
    def create(cls, controller, time_control, marine_visible=True):
        return cls(controller, time_control, marine_visible)

    def forget(self, host):
        """Release retained overlay resources while a map host is hidden."""
        self.isochrones.forget(host)
        self.marine.forget(host)

    def close(self, host):
        """Close terminal overlay resources for a map host."""
        self.isochrones.forget(host)
        self.marine.close()


class RetainedOverlayMixin:
    """Build retained overlay images from state supplied by the map host."""

    @property
    def navigation(self):
        return self._overlay_services.navigation

    @property
    def ais_renderer(self):
        return self._overlay_services.ais_renderer

    @property
    def isochrones(self):
        return self._overlay_services.isochrones

    @property
    def marine(self):
        return self._overlay_services.marine

    @property
    def dashboard(self):
        return self._overlay_services.dashboard

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
