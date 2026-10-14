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
from functools import lru_cache
from typing import List

import cairo
import numpy as np
from gweatherrouting.common import wind_color
from gweatherrouting.core.utils import point_in_country

try:
    from contourpy import contour_generator
except ImportError:  # pragma: no cover - matplotlib pulls contourpy in practice
    contour_generator = None  # type: ignore[assignment]

#: Isobar spacing in hPa; levels snap to multiples of this interval.
ISOBAR_INTERVAL_HPA = 4.0


@lru_cache(maxsize=16384)
def wind_on_ground(lat, lon):
    """Reuse the static land mask between neighboring wind viewports."""
    return point_in_country(lat, lon)


class WindPainter:
    """Draw a frozen set of wind samples without accessing GTK or live settings."""

    def __init__(self, settings_manager, on_ground=wind_on_ground):
        self.settings_manager = settings_manager
        self._on_ground = on_ground

    def draw_wind_barb(self, cr, x, y, wdir, wspeed):
        """Draw a wind barb in standard meteorological notation.

        The barb points in the direction the wind comes FROM.
        Short barb = 5 kt, long barb = 10 kt, pennant (flag) = 50 kt.
        """
        wspeed_kt = wspeed * 1.94384  # m/s to knots
        opacity = self.settings_manager.gribArrowOpacity

        r, g, b = wind_color(wspeed)
        cr.set_source_rgba(r, g, b, opacity)

        # Wind barb points into the wind (direction wind comes FROM)
        angle = math.radians(wdir)
        shaft_len = 20

        cr.save()
        cr.translate(x, y)
        cr.rotate(angle)

        # Draw shaft (line pointing up = into the wind)
        cr.move_to(0, 0)
        cr.line_to(0, -shaft_len)
        cr.stroke()

        # Draw dot for calm winds
        if wspeed_kt < 2.5:
            cr.arc(0, 0, 2, 0, 2 * math.pi)
            cr.stroke()
            cr.restore()
            return

        # Build barbs from tip downward
        remaining = wspeed_kt
        barb_y = float(-shaft_len)
        barb_spacing = 3.5
        barb_len = 8

        # Pennants (50 kt each)
        while remaining >= 47.5:
            cr.move_to(0, barb_y)
            cr.line_to(barb_len, barb_y)
            cr.line_to(0, barb_y + barb_spacing)
            cr.close_path()
            cr.fill()
            barb_y += barb_spacing
            remaining -= 50

        # Long barbs (10 kt each)
        while remaining >= 7.5:
            cr.move_to(0, barb_y)
            cr.line_to(barb_len, barb_y)
            cr.stroke()
            barb_y += barb_spacing
            remaining -= 10

        # Short barb (5 kt)
        if remaining >= 2.5:
            cr.move_to(0, barb_y)
            cr.line_to(barb_len * 0.5, barb_y)
            cr.stroke()

        cr.restore()

    def draw(self, gpsmap, cr, data, pressure=None):
        if (not data or len(data) == 0) and not pressure:
            return

        scale = int(gpsmap.get_scale() / 350.0)
        if scale < 1:
            scale = 1

        if data:
            # Draw color fill (smooth gradient between grid points)
            self._draw_color_fill(gpsmap, cr, data, scale)

        # Isobars sit above the fill so thin lines stay readable.
        self.draw_isobars(gpsmap, cr, pressure)

        if not data:
            return

        # Draw wind barbs
        cr.set_line_width(1)
        points = self._barb_points(gpsmap, data, scale)
        if points:
            lats = np.array([p[2][0] for p in points], dtype=np.float64)
            lons = np.array([p[2][1] for p in points], dtype=np.float64)
            xxs, yys = gpsmap.convert_geographic_to_screen_batch(lats, lons)
            for xx, yy, p in zip(xxs, yys, points):
                if gpsmap.render_cancelled():
                    return
                self.draw_wind_barb(cr, xx, yy, p[0], p[1])

    def draw_isobars(self, gpsmap, cr, pressure):
        """Draw mean-sea-level pressure contours every 4 hPa with labels."""
        if not getattr(self.settings_manager, "gribIsobarsVisible", True):
            return
        if contour_generator is None or not pressure or len(pressure) < 2:
            return
        for lats, lons, values in self._pressure_blocks(pressure):
            if gpsmap.render_cancelled():
                return
            self._draw_isobar_block(gpsmap, cr, lats, lons, values)

    @staticmethod
    def _pressure_blocks(pressure):
        """Group longitude columns into rectangular grids.

        Overlapping forecasts on different grids leave ragged columns;
        only columns sharing the same latitude vector contour together.
        """
        blocks = []
        current: list = []
        key = None
        for column in pressure:
            column_lats = tuple(point[0] for point in column)
            if len(column) < 2 or column_lats != key:
                if len(current) >= 2:
                    blocks.append(current)
                current = [column] if len(column) >= 2 else []
                key = column_lats if len(column) >= 2 else None
                continue
            current.append(column)
        if len(current) >= 2:
            blocks.append(current)
        grids = []
        for block in blocks:
            latitudes = [point[0] for point in block[0]]
            grids.append(
                (
                    np.array(latitudes, dtype=np.float64),
                    np.array([column[0][1] for column in block], dtype=np.float64),
                    np.array(
                        [[point[2] for point in column] for column in block],
                        dtype=np.float64,
                    ).T,
                )
            )
        return grids

    @staticmethod
    def _isobar_levels(values):
        """4 hPa levels spanning the block, snapped to multiples of 4."""
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            return None
        low = math.floor(float(finite.min()) / ISOBAR_INTERVAL_HPA)
        high = math.ceil(float(finite.max()) / ISOBAR_INTERVAL_HPA)
        if high <= low:
            return None
        return ISOBAR_INTERVAL_HPA * np.arange(low, high + 1, dtype=np.float64)

    @staticmethod
    def _project_pressure_block(gpsmap, lats, lons):
        """Screen-space grid matching the pressure array, if projectable."""
        grid_lons, grid_lats = np.meshgrid(lons, lats)
        screen = gpsmap.convert_geographic_to_screen_batch(
            grid_lats.ravel(), grid_lons.ravel()
        )
        if screen is None:
            return None
        shape = grid_lats.shape
        xs = np.asarray(screen[0], dtype=np.float64).reshape(shape)
        ys = np.asarray(screen[1], dtype=np.float64).reshape(shape)
        if not (np.isfinite(xs).all() and np.isfinite(ys).all()):
            return None
        return xs, ys

    @staticmethod
    def _contour_lines(gpsmap, xs, ys, values, levels):
        try:
            generator = contour_generator(xs, ys, values)
        except Exception:
            return []
        contours = []
        for level in levels:
            if gpsmap.render_cancelled():
                return []
            try:
                lines = generator.lines(float(level))
            except Exception:
                continue
            for line in lines:
                if len(line) >= 2:
                    contours.append((float(level), np.asarray(line, dtype=np.float64)))
        return contours

    @staticmethod
    def _stroke_contours(cr, gpsmap, contours):
        # A dark halo keeps thin lines readable over the wind gradient fill.
        for width, color in ((3.0, (0, 0, 0, 0.45)), (1.1, (1, 1, 1, 0.85))):
            cr.save()
            cr.set_line_width(width)
            cr.set_source_rgba(*color)
            for _, line in contours:
                if gpsmap.render_cancelled():
                    cr.restore()
                    return
                cr.move_to(line[0][0], line[0][1])
                for x, y in line[1:]:
                    cr.line_to(x, y)
                cr.stroke()
            cr.restore()

    def _draw_isobar_block(self, gpsmap, cr, lats, lons, values):
        levels = self._isobar_levels(values)
        if levels is None:
            return
        projected = self._project_pressure_block(gpsmap, lats, lons)
        if projected is None:
            return
        xs, ys = projected
        contours = self._contour_lines(gpsmap, xs, ys, values, levels)
        if not contours or gpsmap.render_cancelled():
            return
        self._stroke_contours(cr, gpsmap, contours)
        opacity = getattr(self.settings_manager, "gribArrowOpacity", 0.4)
        self._draw_isobar_labels(cr, contours, opacity, gpsmap)

    @staticmethod
    def _draw_isobar_labels(cr, contours, opacity, gpsmap):
        by_level: dict = {}
        for level, line in contours:
            length = float(np.abs(np.diff(line, axis=0)).sum())
            if length <= 60:
                continue
            if level not in by_level or length > by_level[level][0]:
                by_level[level] = (length, line)
        if not by_level:
            return
        cr.save()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(10)
        for level in sorted(by_level)[:15]:
            if gpsmap.render_cancelled():
                break
            _, line = by_level[level]
            mid = line[len(line) // 2]
            text = "%d" % round(level)
            cr.move_to(mid[0] + 3, mid[1] - 3)
            cr.text_path(text)
            cr.set_source_rgba(0, 0, 0, 0.85)
            cr.set_line_width(3)
            cr.stroke_preserve()
            cr.set_source_rgba(1, 1, 1, min(1.0, 0.55 + opacity))
            cr.fill()
        cr.restore()

    def _barb_points(self, gpsmap, data, scale):
        points = []
        for row in data[::scale]:
            if gpsmap.render_cancelled():
                return []
            for point in row[::scale]:
                if not self.settings_manager.gribArrowOnGround:
                    if self._on_ground(point[2][0], point[2][1]):
                        continue
                points.append(point)
        return points

    def _grid_screen_points(self, gpsmap, data, scale):
        # Batch-convert all grid points used as quad corners
        point_map = {}
        all_lats: List = []
        all_lons: List = []
        for i in range(0, len(data), scale):
            if gpsmap.render_cancelled():
                return
            row = data[i]
            for j in range(0, len(row), scale):
                point_map[(i, j)] = len(all_lats)
                d = row[j]
                all_lats.append(d[2][0])
                all_lons.append(d[2][1])

        if not all_lats:
            return

        lats = np.array(all_lats, dtype=np.float64)
        lons = np.array(all_lons, dtype=np.float64)
        screen_xs, screen_ys = gpsmap.convert_geographic_to_screen_batch(lats, lons)
        return point_map, screen_xs, screen_ys

    def _draw_color_fill(self, gpsmap, cr, data, scale):
        """Draw interpolated color fill between grid points."""
        opacity = self.settings_manager.gribArrowOpacity * 0.3
        projected = self._grid_screen_points(gpsmap, data, scale)
        if projected is None:
            return
        point_map, screen_xs, screen_ys = projected

        # A shared mesh avoids independently composited, antialiased cell edges.
        pat = cairo.MeshPattern()
        patches = 0
        for i in range(0, len(data) - scale, scale):
            if gpsmap.render_cancelled():
                return
            for j in range(0, len(data[i]) - scale, scale):
                try:
                    d_tl = data[i][j]
                    d_tr = data[i][j + scale]
                    d_bl = data[i + scale][j]
                    d_br = data[i + scale][j + scale]
                except IndexError:
                    continue

                x_tl = screen_xs[point_map[(i, j)]]
                y_tl = screen_ys[point_map[(i, j)]]
                x_tr = screen_xs[point_map[(i, j + scale)]]
                y_tr = screen_ys[point_map[(i, j + scale)]]
                x_bl = screen_xs[point_map[(i + scale, j)]]
                y_bl = screen_ys[point_map[(i + scale, j)]]
                x_br = screen_xs[point_map[(i + scale, j + scale)]]
                y_br = screen_ys[point_map[(i + scale, j + scale)]]

                # Rows are longitude columns. Never join different latitude
                # intervals when partial forecast coverage leaves a ragged grid.
                if d_tl[2][0] != d_bl[2][0] or d_tr[2][0] != d_br[2][0]:
                    continue
                pat.begin_patch()
                # Path: TL → TR → BR → BL (clockwise)
                pat.move_to(x_tl, y_tl)
                pat.line_to(x_tr, y_tr)
                pat.line_to(x_br, y_br)
                pat.line_to(x_bl, y_bl)

                # Corners match path order: 0=TL, 1=TR, 2=BR, 3=BL
                r, g, b = wind_color(d_tl[1])
                pat.set_corner_color_rgba(0, r, g, b, opacity)
                r, g, b = wind_color(d_tr[1])
                pat.set_corner_color_rgba(1, r, g, b, opacity)
                r, g, b = wind_color(d_br[1])
                pat.set_corner_color_rgba(2, r, g, b, opacity)
                r, g, b = wind_color(d_bl[1])
                pat.set_corner_color_rgba(3, r, g, b, opacity)
                pat.end_patch()

                patches += 1

        if patches:
            cr.save()
            cr.set_source(pat)
            cr.paint()
            cr.restore()
