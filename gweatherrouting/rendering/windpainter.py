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

import cairo
import numpy as np
from gweatherrouting.common import wind_color
from gweatherrouting.core.utils import point_in_country

try:
    from contourpy import contour_generator
except ImportError:  # pragma: no cover - matplotlib pulls contourpy in practice
    contour_generator = None  # type: ignore[assignment]


def shoreline_quality(scale):
    """The existing shoreline resolution policy, shared by mask reuse and I/O."""
    if scale > 5000:
        return "c"
    if scale > 500:
        return "l"
    if scale > 200:
        return "i"
    if scale >= 15:
        return "h"
    return "f"


#: Isobar spacing in hPa; levels snap to multiples of this interval.
ISOBAR_INTERVAL_HPA = 4.0

#: Fixed screen-size wind barb geometry (CSS pixels, never zoom-scaled).
BARB_SHAFT_LEN = 20.0
BARB_SPACING = 3.5
BARB_LEN = 8.0
#: Screen-space cell size for thinning live barbs; bounds per-frame work.
BARB_MIN_SPACING_PX = 44.0
BARB_MAX_COUNT = 900
BARB_DENSITIES = (
    ("sparse", "Sparse", 64.0),
    ("standard", "Standard", BARB_MIN_SPACING_PX),
    ("dense", "Dense", 28.0),
    ("very_dense", "Very dense", 16.0),
    ("all", "All forecast points", 0.0),
)
_KT_PER_MS = 1.94384

#: Precomputed bucket colors matching wind_color()'s 2 m/s intervals.
_BUCKET_COLORS = tuple(wind_color(float(b)) for b in range(0, 30, 2))


def barb_bucket_colors():
    """Public bucket colors for Qt foreground painting (r, g, b floats)."""
    return list(_BUCKET_COLORS)


def barb_density_parameters(density):
    """Cell size and proportional symbol budget; 'all' is unthinned/uncapped."""
    step = next(
        (spacing for key, _label, spacing in BARB_DENSITIES if key == density),
        BARB_MIN_SPACING_PX,
    )
    if step == 0:
        return 0.0, None
    return step, round(BARB_MAX_COUNT * (BARB_MIN_SPACING_PX / step) ** 2)


def _speed_bucket(wspeed):
    if wspeed < 0:
        return 0
    bucket = int(wspeed // 2)
    return bucket if bucket < len(_BUCKET_COLORS) else len(_BUCKET_COLORS) - 1


def flatten_wind_columns(columns):
    """Flatten barb columns into numpy vectors for cheap per-frame reprojection.

    Accepts either:
    * Array-columns from grid(): (lon, lats_array, dirs_array, speeds_array)
    * Tuple-columns: list of columns, each column is list of
      (dir, speed, (lat, lon)) tuples
    Returns ``(dirs, speeds, lats, lons)`` or ``None`` when there is nothing
    to draw.
    """
    if not columns:
        return None
    first = columns[0]
    if isinstance(first, tuple) and len(first) >= 3 and hasattr(first[1], "shape"):
        # Array-columns: (lon, lats_array, dirs_array, speeds_array)
        all_lats = np.concatenate([col[1] for col in columns])
        all_lons = np.concatenate(
            [np.full(len(col[1]), col[0], dtype=np.float64) for col in columns]
        )
        all_dirs = np.concatenate([col[2] for col in columns])
        all_speeds = np.concatenate([col[3] for col in columns])
    else:
        # Tuple-columns: list of columns, each column is list of point tuples
        points = [point for column in columns for point in column]
        if not points:
            return None
        dirs, speeds, coordinates = zip(*points)
        lats, lons = zip(*coordinates)
        all_lats = np.array(lats, dtype=np.float64)
        all_lons = np.array(lons, dtype=np.float64)
        all_dirs = np.array(dirs, dtype=np.float64)
        all_speeds = np.array(speeds, dtype=np.float64)
    return (all_dirs, all_speeds, all_lats, all_lons)


def mercator_project_nodes(lats, lons, zoom):
    """View-independent world pixels, with the reference projection arithmetic."""
    tile = (2.0**zoom) * 256.0
    px = (lons + 180.0) / 360.0 * tile
    lat_clipped = np.clip(lats, -85.051129, 85.051129)
    lat_rad = np.radians(lat_clipped)
    py = (1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad)) / math.pi) / 2.0 * tile
    return px, py


def mercator_project_flat(
    lats, lons, center_lat, center_lon, zoom, width, height, projected=None
):
    """Project vectors to screen pixels for an explicit viewport size.

    Used by foreground overlays whose canvas reports a padded allocation
    (Qt base images) while painting into an unpadded widget.
    """
    tile = (2.0**zoom) * 256.0
    cx = (center_lon + 180.0) / 360.0 * tile
    clat = max(-85.051129, min(85.051129, center_lat))
    clat_rad = math.radians(clat)
    cy = (
        (1.0 - math.log(math.tan(clat_rad) + 1.0 / math.cos(clat_rad)) / math.pi)
        / 2.0
        * tile
    )
    px, py = (
        mercator_project_nodes(lats, lons, zoom) if projected is None else projected
    )
    return px - cx + width / 2.0, py - cy + height / 2.0


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

    def draw(self, gpsmap, cr, data, pressure=None, fill_data=None, draw_barbs=True):
        """Draw the cached background (fill + isobars) and, optionally, barbs.

        Background rendering passes ``draw_barbs=False`` so barbs stay out of
        the zoom-scaled raster; the foreground repaints them live via
        :meth:`draw_barbs_live` at a fixed screen size.
        """
        if (not data or len(data) == 0) and not pressure:
            return

        scale = int(gpsmap.get_scale() / 350.0)
        if scale < 1:
            scale = 1

        if data:
            # Draw color fill (smooth gradient between grid points).
            # A ragged multi-model union cannot form quads, so the caller
            # may supply a resampled regular grid for the fill while barbs
            # keep the dense native points.
            self._draw_color_fill(gpsmap, cr, fill_data if fill_data else data, scale)

        # Isobars sit above the fill so thin lines stay readable.
        self.draw_isobars(gpsmap, cr, pressure)

        if not data or not draw_barbs:
            return

        # Legacy path: bake barbs into the surface (kept for compatibility).
        # The live overlay below is the preferred foreground.
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

    def draw_barbs_live(
        self,
        gpsmap,
        cr,
        data_or_flat,
        opacity=None,
        hide_on_ground=None,
        on_ground=None,
    ):
        """Draw crisp fixed-size barbs for the current viewport.

        Reprojects cached wind vectors every frame instead of scaling a
        raster, so zooming never pixelates. Thins to a screen-space grid
        and batches one stroke/fill per color bucket.
        Returns the number of barbs drawn.
        """
        if data_or_flat is None:
            return 0
        if (
            isinstance(data_or_flat, tuple)
            and len(data_or_flat) == 4
            and isinstance(data_or_flat[0], np.ndarray)
        ):
            flat = data_or_flat
        else:
            flat = flatten_wind_columns(data_or_flat)
        if flat is None:
            return 0
        if opacity is None:
            opacity = getattr(self.settings_manager, "gribArrowOpacity", 0.8)
        if hide_on_ground is None:
            hide_on_ground = not getattr(
                self.settings_manager, "gribArrowOnGround", True
            )
        if on_ground is None:
            on_ground = self._on_ground
        step, max_count = barb_density_parameters(
            getattr(self.settings_manager, "gribBarbDensity", "standard")
        )
        geometry, count = self.live_geometry_for_view(
            flat,
            gpsmap._center_lat,
            gpsmap._center_lon,
            gpsmap._zoom,
            gpsmap.get_allocated_width(),
            gpsmap.get_allocated_height(),
            hide_on_ground=hide_on_ground,
            on_ground=on_ground,
            step=step,
            max_count=max_count,
        )
        self.paint_barb_geometry(cr, geometry, opacity)
        return count

    @staticmethod
    def build_barb_geometry(xxs, yys, dirs, speeds):
        """Compute screen-space barb segments grouped by color bucket."""
        buckets: dict[int, tuple[list, list, list]] = {}
        for x, y, wdir, wspeed in zip(xxs, yys, dirs, speeds):
            if not all(math.isfinite(value) for value in (x, y, wdir, wspeed)):
                continue
            angle = math.radians(float(wdir))
            speed = float(wspeed)
            bucket = _speed_bucket(speed)
            entry = buckets.get(bucket)
            if entry is None:
                entry = buckets[bucket] = ([], [], [])
            lines, tris, dots = entry
            kt = speed * _KT_PER_MS
            sin_a = math.sin(angle)
            cos_a = math.cos(angle)
            shaft_x, shaft_y = sin_a, -cos_a
            perp_x, perp_y = cos_a, sin_a
            tip_x = x + shaft_x * BARB_SHAFT_LEN
            tip_y = y + shaft_y * BARB_SHAFT_LEN
            lines.append((float(x), float(y), float(tip_x), float(tip_y)))
            if kt < 2.5:
                dots.append((float(x), float(y)))
                continue
            dist = BARB_SHAFT_LEN
            remaining = kt
            while remaining >= 47.5:
                ax = x + shaft_x * dist
                ay = y + shaft_y * dist
                bx = ax + perp_x * BARB_LEN
                by = ay + perp_y * BARB_LEN
                cx = x + shaft_x * (dist - BARB_SPACING)
                cy = y + shaft_y * (dist - BARB_SPACING)
                tris.append((ax, ay, bx, by, cx, cy))
                dist -= BARB_SPACING
                remaining -= 50.0
            while remaining >= 7.5:
                ax = x + shaft_x * dist
                ay = y + shaft_y * dist
                lines.append((ax, ay, ax + perp_x * BARB_LEN, ay + perp_y * BARB_LEN))
                dist -= BARB_SPACING
                remaining -= 10.0
            if remaining >= 2.5:
                ax = x + shaft_x * dist
                ay = y + shaft_y * dist
                lines.append(
                    (ax, ay, ax + perp_x * BARB_LEN * 0.5, ay + perp_y * BARB_LEN * 0.5)
                )
        return buckets

    @staticmethod
    def paint_barb_geometry(cr, geometry, opacity):
        """Paint grouped geometry with one stroke/fill per color bucket."""
        if not geometry:
            return
        cr.save()
        cr.set_line_width(1.0)
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        for bucket in sorted(geometry):
            lines, tris, dots = geometry[bucket]
            color = _BUCKET_COLORS[bucket]
            cr.set_source_rgba(color[0], color[1], color[2], opacity)
            if lines or dots:
                for x1, y1, x2, y2 in lines:
                    cr.move_to(x1, y1)
                    cr.line_to(x2, y2)
                for x, y in dots:
                    cr.move_to(x + 2.0, y)
                    cr.arc(x, y, 2.0, 0, 2 * math.pi)
                cr.stroke()
            if tris:
                for ax, ay, bx, by, cx, cy in tris:
                    cr.move_to(ax, ay)
                    cr.line_to(bx, by)
                    cr.line_to(cx, cy)
                    cr.close_path()
                cr.fill()
        cr.restore()

    @staticmethod
    def live_geometry_for_view(
        flat,
        center_lat,
        center_lon,
        zoom,
        width,
        height,
        hide_on_ground=False,
        on_ground=None,
        step=BARB_MIN_SPACING_PX,
        max_count=BARB_MAX_COUNT,
        land_mask=None,
        projected=None,
    ):
        """Toolkit-independent live barb geometry for an explicit viewport.

        Returns ``(geometry, count)`` where geometry groups screen-space
        lines/triangles/dots by color bucket. Used by the Qt foreground
        painter; the cairo path above shares the same math.
        """
        if flat is None:
            return {}, 0
        dirs, speeds, lats, lons = flat
        if len(dirs) == 0:
            return {}, 0
        xxs, yys = mercator_project_flat(
            lats, lons, center_lat, center_lon, zoom, width, height, projected=projected
        )
        xxs = np.asarray(xxs, dtype=np.float64)
        yys = np.asarray(yys, dtype=np.float64)
        margin = BARB_SHAFT_LEN + BARB_LEN + 4.0
        visible = (
            (xxs >= -margin)
            & (xxs <= width + margin)
            & (yys >= -margin)
            & (yys <= height + margin)
            & np.isfinite(xxs)
            & np.isfinite(yys)
            & np.isfinite(dirs)
            & np.isfinite(speeds)
        )
        idx = np.flatnonzero(visible)
        if len(idx) == 0:
            return {}, 0
        # Select one native sample per cell in NumPy, avoiding a Python
        # loop over every visible model node during each gesture tick.
        kept_arr = idx
        if step > 0:
            cols = int(math.ceil((width + 2 * margin) / step)) + 1
            cells = np.floor((yys[idx] + margin) / step).astype(
                np.int64
            ) * cols + np.floor((xxs[idx] + margin) / step).astype(np.int64)
            _, first = np.unique(cells, return_index=True)
            kept_arr = idx[np.sort(first)]
        if max_count is not None and len(kept_arr) > max_count:
            # Keep coverage across the whole view, including large displays.
            kept_arr = kept_arr[
                np.linspace(0, len(kept_arr) - 1, max_count, dtype=np.int64)
            ]
        # Mask after both density thinning and the count cap: hiding land must
        # never replace a selected symbol with a different forecast sample.
        if land_mask is not None:
            kept_arr = kept_arr[~land_mask[kept_arr]]
        elif hide_on_ground and on_ground is not None:
            kept_arr = np.array(
                [i for i in kept_arr if not on_ground(float(lats[i]), float(lons[i]))],
                dtype=np.int64,
            )
        kxx, kyy = xxs[kept_arr], yys[kept_arr]
        kdirs, kspeeds = dirs[kept_arr], speeds[kept_arr]
        geometry = WindPainter.build_barb_geometry(kxx, kyy, kdirs, kspeeds)
        return geometry, int(len(kxx))

    def draw_isobars(self, gpsmap, cr, pressure):
        """Draw mean-sea-level pressure contours with configurable spacing and labels."""
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
            column_lats = tuple(column[1].tolist())
            if len(column[1]) < 2 or column_lats != key:
                if len(current) >= 2:
                    blocks.append(current)
                current = [column] if len(column[1]) >= 2 else []
                key = column_lats if len(column[1]) >= 2 else None
                continue
            current.append(column)
        if len(current) >= 2:
            blocks.append(current)
        grids = []
        for block in blocks:
            grids.append(
                (
                    np.array(block[0][1], dtype=np.float64),
                    np.array([column[0] for column in block], dtype=np.float64),
                    np.array([column[2] for column in block], dtype=np.float64).T,
                )
            )
        return grids

    @staticmethod
    def _isobar_levels(values, interval=ISOBAR_INTERVAL_HPA):
        """Levels spanning the block, snapped to multiples of the interval."""
        if not math.isfinite(interval) or interval <= 0:
            interval = ISOBAR_INTERVAL_HPA
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            return None
        low = math.floor(float(finite.min()) / interval)
        high = math.ceil(float(finite.max()) / interval)
        if high <= low:
            return None
        return interval * np.arange(low, high + 1, dtype=np.float64)

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
    def _stroke_contours(cr, gpsmap, contours, opacity=0.85):
        # A dark halo keeps thin lines readable over the wind gradient fill.
        for width, color in (
            (3.0, (0, 0, 0, opacity * 0.45 / 0.85)),
            (1.1, (1, 1, 1, opacity)),
        ):
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
        levels = self._isobar_levels(
            values, getattr(self.settings_manager, "gribIsobarInterval", 4.0)
        )
        if levels is None:
            return
        projected = self._project_pressure_block(gpsmap, lats, lons)
        if projected is None:
            return
        xs, ys = projected
        contours = self._contour_lines(gpsmap, xs, ys, values, levels)
        if not contours or gpsmap.render_cancelled():
            return
        opacity = getattr(self.settings_manager, "gribIsobarOpacity", 0.85)
        self._stroke_contours(cr, gpsmap, contours, opacity)
        if getattr(self.settings_manager, "gribIsobarLabels", True):
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
            cr.set_source_rgba(0, 0, 0, opacity)
            cr.set_line_width(3)
            cr.stroke_preserve()
            cr.set_source_rgba(1, 1, 1, opacity)
            cr.fill()
        cr.restore()

    def _barb_points(self, gpsmap, data, scale):
        points = []
        for column in data[::scale]:
            if gpsmap.render_cancelled():
                return []
            lon, lats, dirs, speeds = column[0], column[1], column[2], column[3]
            for j in range(0, len(lats), scale):
                lat = float(lats[j])
                if not self.settings_manager.gribArrowOnGround:
                    if self._on_ground(lat, float(lon)):
                        continue
                points.append((dirs[j], speeds[j], (lat, float(lon))))
        return points

    def _grid_screen_points(self, gpsmap, data, scale):
        # Batch-convert all grid points used as quad corners. The columns are
        # numpy arrays, so the whole grid is projected in one call and the
        # result is kept per column; no per-point Python objects are made.
        lat_chunks = []
        lon_chunks = []
        for i in range(0, len(data), scale):
            if gpsmap.render_cancelled():
                return
            lon, lats = data[i][0], data[i][1]
            sampled = lats[::scale]
            lat_chunks.append(sampled)
            lon_chunks.append(np.full(sampled.shape, lon, dtype=np.float64))

        if not lat_chunks:
            return

        lats = np.concatenate(lat_chunks)
        lons = np.concatenate(lon_chunks)
        screen_xs, screen_ys = gpsmap.convert_geographic_to_screen_batch(lats, lons)
        xs_columns = []
        ys_columns = []
        offset = 0
        for chunk in lat_chunks:
            count = len(chunk)
            xs_columns.append(screen_xs[offset : offset + count])
            ys_columns.append(screen_ys[offset : offset + count])
            offset += count
        return xs_columns, ys_columns

    def _draw_color_fill(self, gpsmap, cr, data, scale):
        """Draw interpolated color fill between grid points."""
        opacity = self.settings_manager.gribArrowOpacity * 0.3
        projected = self._grid_screen_points(gpsmap, data, scale)
        if projected is None:
            return
        screen_xs, screen_ys = projected

        # A shared mesh avoids independently composited, antialiased cell edges.
        pat = cairo.MeshPattern()
        patches = 0
        # Plain float lists index faster than numpy scalars and keep the work
        # inside the quad loop free of new objects.
        lats_by_column = [column[1].tolist() for column in data]
        speeds_by_column = [column[3].tolist() for column in data]
        for i in range(0, len(data) - scale, scale):
            if gpsmap.render_cancelled():
                return
            try:
                lats_next = lats_by_column[i + scale]
                speeds_next = speeds_by_column[i + scale]
            except IndexError:
                continue
            patches += self._fill_column(
                pat,
                opacity,
                screen_xs,
                screen_ys,
                i // scale,
                lats_by_column[i],
                lats_next,
                speeds_by_column[i],
                speeds_next,
                scale,
            )

        if patches:
            cr.save()
            cr.set_source(pat)
            cr.paint()
            cr.restore()

    def _fill_column(
        self,
        pat,
        opacity,
        screen_xs,
        screen_ys,
        column,
        lats,
        lats_next,
        speeds,
        speeds_next,
        scale,
    ):
        """Add the fill quads between two neighbouring columns."""
        patches = 0
        for j in range(0, len(lats) - scale, scale):
            try:
                lat_tl = lats[j]
                lat_tr = lats[j + scale]
                lat_bl = lats_next[j]
                lat_br = lats_next[j + scale]
                speed_tl = speeds[j]
                speed_tr = speeds[j + scale]
                speed_bl = speeds_next[j]
                speed_br = speeds_next[j + scale]
            except IndexError:
                continue

            row = j // scale
            x_tl = screen_xs[column][row]
            y_tl = screen_ys[column][row]
            x_tr = screen_xs[column][row + 1]
            y_tr = screen_ys[column][row + 1]
            x_bl = screen_xs[column + 1][row]
            y_bl = screen_ys[column + 1][row]
            x_br = screen_xs[column + 1][row + 1]
            y_br = screen_ys[column + 1][row + 1]

            # Rows are longitude columns. Never join different latitude
            # intervals when partial forecast coverage leaves a ragged grid.
            if lat_tl != lat_bl or lat_tr != lat_br:
                continue
            pat.begin_patch()
            # Path: TL → TR → BR → BL (clockwise)
            pat.move_to(x_tl, y_tl)
            pat.line_to(x_tr, y_tr)
            pat.line_to(x_br, y_br)
            pat.line_to(x_bl, y_bl)

            # Corners match path order: 0=TL, 1=TR, 2=BR, 3=BL
            r, g, b = wind_color(speed_tl)
            pat.set_corner_color_rgba(0, r, g, b, opacity)
            r, g, b = wind_color(speed_tr)
            pat.set_corner_color_rgba(1, r, g, b, opacity)
            r, g, b = wind_color(speed_br)
            pat.set_corner_color_rgba(2, r, g, b, opacity)
            r, g, b = wind_color(speed_bl)
            pat.set_corner_color_rgba(3, r, g, b, opacity)
            pat.end_patch()

            patches += 1
        return patches
