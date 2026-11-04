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

import math

import cairo
import numpy as np

from sailworks.core.geo.shoreline import ShorelineGeometryCache
from sailworks.rendering.style import Style

from .vectorchartdrawer import VectorChartDrawer


class SimpleChartDrawer(VectorChartDrawer):
    def __init__(self, settings_manager):
        super().__init__(settings_manager)
        self._shorelines = ShorelineGeometryCache()

    def draw(self, gpsmap, cr, vector_file, bounding):
        stroke_style = Style.chart_palettes[self.palette].land_stroke
        fill_style = Style.chart_palettes[self.palette].land_fill
        sea_style = Style.chart_palettes[self.palette].sea
        contourn_style = Style.chart_palettes[self.palette].shallow_sea

        self.background_render(gpsmap, cr, sea_style)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        for i in range(vector_file.GetLayerCount()):
            layer = vector_file.GetLayerByIndex(i)
            layer.SetSpatialFilter(bounding)
            layer.ResetReading()

            # Iterate over features
            feat = layer.GetNextFeature()
            while feat is not None:
                if not feat:
                    continue

                geom = self._shorelines.clip(
                    (vector_file.GetDescription(), i, feat.GetFID()),
                    feat.GetGeometryRef(),
                    bounding,
                )
                self.feature_render(
                    gpsmap,
                    cr,
                    geom,
                    feat,
                    layer,
                    stroke_style,
                    fill_style,
                    contourn_style,
                )
                feat = layer.GetNextFeature()

    def background_render(self, gpsmap, cr, sea_style):
        width = float(gpsmap.get_allocated_width())
        height = float(gpsmap.get_allocated_height())
        sea_style.apply(cr)
        cr.rectangle(0, 0, width, height)
        cr.stroke_preserve()
        cr.fill()

    @staticmethod
    def _geom_to_screen_batch(gpsmap, g):
        """Extract OGR geometry points and batch-convert to screen coords."""
        npts = g.GetPointCount()
        if npts == 0:
            return None, None
        lats = np.empty(npts, dtype=np.float64)
        lons = np.empty(npts, dtype=np.float64)
        for ii in range(npts):
            pt = g.GetPoint(ii)
            lats[ii] = pt[1]
            lons[ii] = pt[0]
        xs, ys = gpsmap.convert_geographic_to_screen_batch(lats, lons)
        return xs.tolist(), ys.tolist()

    @staticmethod
    def _simplify_polygon(gpsmap, cr, geom):
        if not all(hasattr(gpsmap, name) for name in ("_zoom", "TILE_SIZE", "MAX_LAT")):
            return geom
        if not geom.GetGeometryCount() or any(
            geom.GetGeometryRef(i).GetPointCount() <= 8
            for i in range(geom.GetGeometryCount())
        ):
            return geom
        _, _, south, north = geom.GetEnvelope()
        latitude = min(gpsmap.MAX_LAT, max(abs(south), abs(north)))
        factor = max(cr.get_target().get_device_scale())
        try:
            # 0.3 device pixels at the most distorted latitude keeps the
            # topology-preserving approximation below a visible pixel.
            tolerance = (
                0.3
                * 360.0
                * math.cos(math.radians(latitude))
                / (2.0**gpsmap._zoom * gpsmap.TILE_SIZE * factor)
            )
        except (OverflowError, ZeroDivisionError):
            return geom
        if not math.isfinite(tolerance) or tolerance <= 0:
            return geom
        simplified = geom.SimplifyPreserveTopology(tolerance)
        if simplified is None or simplified.IsEmpty() or not simplified.IsValid():
            return geom
        return simplified

    def feature_render(
        self, gpsmap, cr, geom, feat, layer, stroke_style, fill_style, contourn_style
    ):
        if geom is None or geom.IsEmpty():
            return
        if geom.GetGeometryName() in ("MULTIPOLYGON", "GEOMETRYCOLLECTION"):
            for i in range(geom.GetGeometryCount()):
                self.feature_render(
                    gpsmap,
                    cr,
                    geom.GetGeometryRef(i),
                    feat,
                    layer,
                    stroke_style,
                    fill_style,
                    contourn_style,
                )
            return
        if geom.GetGeometryName() == "POLYGON":
            geom = self._simplify_polygon(gpsmap, cr, geom)
        cr.new_path()
        for i in range(geom.GetGeometryCount()):
            ring = geom.GetGeometryRef(i)
            xs, ys = self._geom_to_screen_batch(gpsmap, ring)
            if xs is None:
                continue
            cr.move_to(xs[0], ys[0])
            for x, y in zip(xs[1:], ys[1:]):
                cr.line_to(x, y)
            cr.close_path()
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        fill_style.apply(cr)
        cr.fill_preserve()
        stroke_style.apply(cr)
        cr.stroke()
