# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

import logging
import math
import os
from typing import List, Optional
from weakref import WeakKeyDictionary

import cairo

from gweatherrouting.common import resource_path
from gweatherrouting.core.core import LinePointValidityProvider
from gweatherrouting.core.enccharts import chart_scale_rank
from gweatherrouting.core.storage import DATA_DIR
from gweatherrouting.core.settings import SettingsManager
from gweatherrouting.core.scheduling import schedule_idle
from gweatherrouting.core.mapviewport import MapViewport

from .chartlayer import ChartLayer
from .chartclasses import CHART_CLASSES, chart_class_enabled
from .chsrenderer import CHSRenderer
from .gdalrasterchart import GDALRasterChart
from .gdalvectorchart import GDALVectorChart
from .encrenderer import ENCOverlayRenderer
from .mbtilesrenderer import MBTilesChart, MBTilesRenderer
from gweatherrouting.rendering.overlayrenderer import paint_viewport
from .vectordrawer.s57chartdrawer import S57ChartDrawer
from .gshhs import GSHHSVectorChart

logger = logging.getLogger("gweatherrouting")


class ChartCollection:
    def __init__(self, settings_manager, schedule=schedule_idle):
        self.schedule = schedule
        self.charts: List[ChartLayer] = []
        self.gshhsLayer: Optional[GSHHSVectorChart] = None
        self.osmLayer: Optional[GDALVectorChart] = None
        self.basemap_error = ""
        self.settings_manager: SettingsManager = settings_manager
        self.maps: List = []
        self._chart_surfaces: WeakKeyDictionary[
            object, tuple[tuple, MapViewport, cairo.ImageSurface]
        ] = WeakKeyDictionary()
        self._enc_renderer = ENCOverlayRenderer(schedule)
        self._mbtiles_renderer = MBTilesRenderer(schedule)
        self._chs_renderer = CHSRenderer(os.path.join(DATA_DIR, "chs-tiles"), schedule)

        self.settings_manager.register_on_change(
            "chartPalette", self.on_chart_palette_changed
        )
        self.settings_manager.register_on_change(
            "canadianCharts", self.on_canadian_charts_changed
        )
        for _, key, _ in CHART_CLASSES:
            self.settings_manager.register_on_change(
                key, lambda value, key=key: self.on_chart_classes_changed(key, value)
            )

    def add_map(self, canvas):
        self.maps.append(canvas)

    def forget_map(self, canvas):
        self._enc_renderer.forget(canvas)
        self._mbtiles_renderer.forget(canvas)
        self._chs_renderer.forget(canvas)
        self._chart_surfaces.pop(canvas, None)
        if canvas in self.maps:
            self.maps.remove(canvas)

    def on_canadian_charts_changed(self, enabled):
        self._chs_renderer.set_enabled(enabled)
        for canvas in self.maps:
            getattr(canvas, "invalidate_chart_sources", canvas.queue_draw)()

    def on_chart_classes_changed(self, key, enabled):
        self._chart_surfaces.clear()
        for canvas in self.maps:
            if key == "chartENCVisible" and not enabled:
                self._enc_renderer.forget(canvas)
            if key == "chartMBTilesVisible" and not enabled:
                self._mbtiles_renderer.forget(canvas)
            getattr(canvas, "invalidate_chart_sources", canvas.queue_draw)()

    def get_line_point_validity_providers(self):
        lpvp: List[LinePointValidityProvider] = []
        for x in filter(lambda x: x.enabled, self.charts):
            if isinstance(x, LinePointValidityProvider):
                lpvp += [x]
        return lpvp

    def on_chart_palette_changed(self, v):
        self._palette = v
        self._chart_surfaces.clear()
        for m in self.maps:
            m.map_redraw_idle()

    def _load_base_gshhs(self):
        if self.gshhsLayer is not None:
            return
        if not os.path.exists(DATA_DIR + "/gshhs"):
            self.basemap_error = "World coastlines not installed"
            return
        try:
            chart = GSHHSVectorChart(DATA_DIR + "/gshhs", self.settings_manager)
            if chart.lpvFile is None or len(chart.vector_files) != 5:
                raise ValueError("Installed coastline data is incomplete")
            self.gshhsLayer = chart
            self.charts.insert(0, chart)
            self.basemap_error = ""
        except Exception as error:
            self.basemap_error = "Unable to load world coastlines: " + str(error)
            logger.warning("Unable to load base coastlines: %s", error)

    def _load_base_osm(self):
        if self.osmLayer is not None or not os.path.exists(DATA_DIR + "/seamarks.pbf"):
            return
        try:
            chart = GDALVectorChart(DATA_DIR + "/seamarks.pbf", self.settings_manager)
            self.osmLayer = chart
            self.charts.append(chart)
        except Exception as error:
            logger.warning("Unable to load sea marks: %s", error)

    def load_base_chart(self, parent=None):
        """Load installed charts; downloading data belongs to the frontend."""
        self._load_base_gshhs()
        self._load_base_osm()
        fallback = resource_path("gweatherrouting", "data/countries.geojson")
        if self.gshhsLayer is None and not any(c.path == fallback for c in self.charts):
            try:
                self.charts.insert(0, GDALVectorChart(fallback, self.settings_manager))
            except Exception as error:
                self.basemap_error += "; bundled world map unavailable: " + str(error)
                logger.warning("Unable to load bundled world map: %s", error)

    def basemap_status(self, canvas):
        """Explain missing data and worker recovery in the application's map UI."""
        if not self.settings_manager.get("chartWorldVisible", True):
            return "World basemap hidden"
        if self.basemap_error:
            fallback = resource_path("gweatherrouting", "data/countries.geojson")
            detail = (
                " — using bundled low-detail map"
                if any(c.path == fallback and c.enabled for c in self.charts)
                else " — world basemap unavailable"
            )
            return (
                self.basemap_error
                + detail
                + ". See Data Library → Charts → Downloads & base maps."
            )
        renderer = getattr(canvas, "_base_chart_renderer", None)
        if renderer is not None:
            if renderer.last_error:
                return (
                    "World basemap worker failed; legacy recovery: "
                    + renderer.last_error
                )
            state = renderer._states.get(canvas)
            if (
                state is not None
                and state.frame is None
                and state.requested is not None
            ):
                return "World basemap loading…"
        return ""

    def reload_base_chart(self):
        """Attach newly installed base data without replacing existing chart objects."""
        previous_coast = self.gshhsLayer
        self.load_base_chart()
        fallback = resource_path("gweatherrouting", "data/countries.geojson")
        if self.gshhsLayer is not None:
            self.charts = [c for c in self.charts if c.path != fallback]
        detailed = any(
            c.enabled
            and isinstance(c, GDALVectorChart)
            and c not in (self.gshhsLayer, self.osmLayer)
            and c.path != fallback
            and not c.path.lower().endswith(".000")
            for c in self.charts
        )
        if detailed:
            if self.gshhsLayer and previous_coast is None:
                self.gshhsLayer.force_downscale = True
        self._chart_surfaces.clear()

    def load_vector_layer(self, path, metadata=None, enabled=True):
        logger.info("Loading vector chart %s", path)
        chart = GDALVectorChart(
            path, self.settings_manager, metadata=metadata, enabled=enabled
        )
        self.charts += [chart]

        if self.gshhsLayer and enabled and not path.lower().endswith(".000"):
            self.gshhsLayer.force_downscale = True

        return chart

    def detailed_chart_at(self, lat, lon, scale, enabled=None):
        """Return the loaded chart best suited to this point and display scale."""
        settings = getattr(self, "settings_manager", {})
        # Local imagery is an explicit top overlay; bounds are advisory coverage.
        for chart in reversed(self.charts):
            if (
                isinstance(chart, MBTilesChart)
                and (enabled is None or chart.enabled == enabled)
                and chart_class_enabled(settings, chart)
                and chart.info.contains(lat, lon)
            ):
                return chart
        candidates = sorted(
            (
                c
                for c in self.charts
                if (enabled is None or c.enabled == enabled)
                and chart_class_enabled(settings, c)
            ),
            key=lambda c: (
                chart_scale_rank(getattr(c, "_native_scale", 0), scale),
                not c.enabled,
                c.path,
            ),
        )
        for chart in candidates:
            if isinstance(chart, GDALVectorChart) and chart.covers_point(
                lat, lon, scale
            ):
                return chart
            if isinstance(chart, GDALRasterChart):
                for _, bbox, *_ in chart.metadata or []:
                    if (
                        min(bbox[0][0], bbox[1][0])
                        <= lat
                        <= max(bbox[0][0], bbox[1][0])
                    ):
                        if (
                            min(bbox[0][1], bbox[1][1])
                            <= lon
                            <= max(bbox[0][1], bbox[1][1])
                        ):
                            return chart
        return None

    def chart_suggestion_at(self, catalog, lat, lon, scale):
        """Offer a better scale, even when a usable chart already covers the point."""
        active = self.detailed_chart_at(lat, lon, scale, enabled=True)
        disabled = self.detailed_chart_at(lat, lon, scale, enabled=False)
        cell = (
            catalog.chart_at(lat, lon, scale)
            if getattr(self, "settings_manager", {}).get("chartENCVisible", True)
            else None
        )

        def rank(chart):
            return chart_scale_rank(getattr(chart, "_native_scale", 0), scale)

        if disabled and (active is None or rank(disabled) < rank(active)):
            if cell is None or rank(disabled) <= chart_scale_rank(cell.scale, scale):
                return None, disabled
        if cell and (
            active is None or chart_scale_rank(cell.scale, scale) < rank(active)
        ):
            # A catalog cell may already have been imported from another directory.
            imported = next(
                (
                    c
                    for c in self.charts
                    if os.path.splitext(os.path.basename(c.path))[0].upper()
                    == cell.name
                ),
                None,
            )
            if imported and imported.enabled:
                return None, None
            return cell, imported
        return None, None

    def enable_downloaded_chart(self, path):
        chart = next((c for c in self.charts if c.path == path), None)
        if chart is None:
            chart = self.load_vector_layer(path)
        chart.enabled = True
        entries = list(self.settings_manager[chart.ctype + "Charts"] or [])
        entry = next((e for e in entries if e["path"] == path), None)
        if entry:
            entry["enabled"] = True
        else:
            entries.append({"path": path, "metadata": chart.metadata, "enabled": True})
        self.settings_manager[chart.ctype + "Charts"] = entries
        self.settings_manager.save()
        for m in self.maps:
            m.map_redraw_idle()
        return chart

    def load_raster_layer(self, path, metadata=None, enabled=True):
        logger.info("Loading raster chart %s", path)
        chart = GDALRasterChart(
            path,
            self.settings_manager,
            metadata=metadata,
            enabled=enabled,
            schedule=self.schedule,
        )
        self.charts += [chart]

        if self.gshhsLayer and enabled:
            self.gshhsLayer.force_downscale = True
        return chart

    def load_mbtiles_layer(self, path, metadata=None, enabled=True):
        chart = MBTilesChart(path, self.settings_manager, metadata, enabled)
        self.charts.append(chart)
        return chart

    def do_draw(self, gpsmap, cr, include_enc=True, include_mbtiles=True):
        base, enc, mbtiles = [], [], []
        for chart in self.charts:
            if not chart_class_enabled(self.settings_manager, chart):
                continue
            if isinstance(chart, MBTilesChart):
                if chart.enabled:
                    mbtiles.append((chart.info,))
            elif isinstance(chart, GDALVectorChart) and isinstance(
                chart.drawer, S57ChartDrawer
            ):
                if chart.enabled:
                    enc.append(
                        (chart.path, chart._native_scale, chart._coverage_envelopes)
                    )
            else:
                base.append(chart)
        worker = getattr(gpsmap, "_base_chart_renderer", None)
        if worker is None or not worker.draw_base(
            gpsmap, cr, base, self._palette, fallback=self._draw_base
        ):
            self._draw_base(gpsmap, cr, base)
        self._chs_renderer.draw(gpsmap, cr)
        if include_enc:
            self._enc_renderer.draw(gpsmap, cr, tuple(enc), self._palette)
        else:
            self._enc_renderer.forget(gpsmap)
        if include_mbtiles:
            self._mbtiles_renderer.draw(gpsmap, cr, tuple(mbtiles), self._palette)

    def draw_above_enc(self, gpsmap, cr):
        """Retain the app's MBTiles-above-ENC ordering when ENC is a Quick image."""
        charts = tuple(
            (chart.info,)
            for chart in self.charts
            if isinstance(chart, MBTilesChart)
            and chart.enabled
            and chart_class_enabled(self.settings_manager, chart)
        )
        self._mbtiles_renderer.draw(gpsmap, cr, charts, self._palette)

    def _draw_base(self, gpsmap, cr, charts):
        # Chart pixels are static while instruments, weather and routes repaint.
        # A margin also lets small pans reuse the same image at a shifted origin.
        margin = 128
        width, height = gpsmap.get_allocated_width(), gpsmap.get_allocated_height()
        factor = gpsmap.get_scale_factor()
        signature = (
            width,
            height,
            factor,
            gpsmap._zoom,
            tuple(
                (id(c), c.enabled, getattr(c, "force_downscale", False)) for c in charts
            ),
        )
        cached = self._chart_surfaces.get(gpsmap)
        if cached and getattr(gpsmap, "_zoom_animating", False):
            old_signature = cached[0]
            if (
                old_signature[:3] == signature[:3]
                and old_signature[4:] == signature[4:]
            ):
                paint_viewport(gpsmap, cr, cached[1], cached[2])
                return
        offset_x = offset_y = 0.0
        if cached and cached[0] == signature:
            viewport = cached[1]
            offset_x = viewport._lon_to_x(viewport._center_lon)
            offset_x -= viewport._lon_to_x(gpsmap._center_lon)
            offset_y = viewport._lat_to_y(viewport._center_lat)
            offset_y -= viewport._lat_to_y(gpsmap._center_lat)
        if (
            not cached
            or cached[0] != signature
            or max(abs(offset_x), abs(offset_y)) > margin
        ):

            def invalidate():
                self._chart_surfaces.pop(gpsmap, None)
                gpsmap.queue_draw()

            viewport = MapViewport(gpsmap, margin, invalidate)
            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32, viewport.width * factor, viewport.height * factor
            )
            surface.set_device_scale(factor, factor)
            chart_cr = cairo.Context(surface)
            for chart in filter(lambda c: c.enabled, charts):
                chart_cr.save()
                chart.do_draw(viewport, chart_cr)
                chart_cr.restore()
            cached = (signature, viewport, surface)
            self._chart_surfaces[gpsmap] = cached
            offset_x = offset_y = 0.0
        cr.set_source_surface(cached[2], offset_x - margin, offset_y - margin)
        cr.paint()

    def do_render(self, gpsmap):
        for x in filter(lambda x: x.enabled, self.charts):
            x.do_render(gpsmap)

    def do_busy(self):
        for x in filter(lambda x: x.enabled, self.charts):
            x.do_busy()

    def depth_at(self, lat, lon, scale, tolerance=0.001):
        """Depth data near a point from the best enabled ENC chart."""
        settings = getattr(self, "settings_manager", {})
        candidates = sorted(
            (
                c
                for c in self.charts
                if c.enabled
                and isinstance(c, GDALVectorChart)
                and isinstance(c.drawer, S57ChartDrawer)
                and chart_class_enabled(settings, c)
            ),
            key=lambda c: (getattr(c, "_native_scale", 0) or math.inf, c.path),
        )
        for chart in candidates:
            if not chart.covers_point(lat, lon, scale):
                continue
            result = chart.depth_at(lat, lon, tolerance)
            if result:
                return result
        return None

    def query_point(self, lat, lon, tolerance=0.001):
        """Query all vector chart layers for features near a given lat/lon."""
        results = []
        for chart in filter(lambda x: x.enabled, self.charts):
            if isinstance(chart, GDALVectorChart):
                results.extend(chart.query_point(lat, lon, tolerance))
        return results

    def do_button_press(self, gpsmap, gdkeventbutton):
        for x in filter(lambda x: x.enabled, self.charts):
            x.do_button_press(gpsmap, gdkeventbutton)
