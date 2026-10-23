"""Retained chart/weather/marine environment composition for Qt map hosts."""

import math

import cairo
from PySide6.QtGui import QImage


class MapEnvironmentMixin:
    """Build the cached environment layer shared by legacy and viewport hosts."""

    def _wind_style_key(self):
        settings = self.controller.settings
        return (
            self.time_control.time,
            settings.gribArrowOpacity,
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
                for grib in self.controller.display_forecast.sources()
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
            bool(self._weather_presenter and self._weather_presenter.enc_active),
        )

    def _environment_image(self):
        """Existing above-ENC layers, retained between ENC and viewport barbs."""
        factor = self.get_scale_factor()
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            math.ceil(self.get_allocated_width() * factor),
            math.ceil(self.get_allocated_height() * factor),
        )
        surface.set_device_scale(factor, factor)
        cr = cairo.Context(surface)
        self.charts.draw_above_enc(self, cr)
        if not getattr(self, "_desktop_viewport", False):
            self._draw_weather_background(cr)
        self.marine.do_draw(self, cr)
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

    def _draw_weather_background(self, cr):
        settings = self.controller.settings
        style = (
            self.time_control.time,
            settings.gribArrowOpacity,
            getattr(settings, "gribIsobarsVisible", True),
            self.wind_visible,
            getattr(settings, "gribIsobarInterval", 4.0),
            getattr(settings, "gribIsobarOpacity", 0.85),
            getattr(settings, "gribIsobarLabels", True),
            getattr(settings, "gribWindStyle", "barbs"),
            getattr(settings, "gribBarbDensity", "standard"),
        )
        sources = tuple((grib,) for grib in self.controller.display_forecast.sources())
        if self.wind_visible or getattr(settings, "gribIsobarsVisible", True):
            # Fill/isobars stay in the cached base image (smooth when scaled);
            # barbs paint crisp in paintEvent via _paint_wind_barbs().
            self.wind.draw(self, cr, sources, style, live_barbs=False)
