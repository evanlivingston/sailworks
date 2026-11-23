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
import threading
from collections import OrderedDict
from functools import lru_cache

import cairo
import numpy as np
from sailworks.common import wave_color, wind_color
from sailworks.core.utils import point_in_country
from sailworks.rendering.canvas import (
    CAP_BUTT,
    SLANT_NORMAL,
    WEIGHT_BOLD,
    CairoCanvas,
    CanvasImage,
    QPainterCanvas,
)

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


#: Wave arrows: a shorter shaft than a wind barb, scaled by the wave period.
WAVE_ARROW_SHAFT_MIN = 8.0
WAVE_ARROW_SHAFT_MAX = 20.0
WAVE_ARROW_HEAD_LEN = 5.0
WAVE_ARROW_HEAD_SPREAD = 3.0
#: The period range the shaft length spans (seconds).
WAVE_PERIOD_MIN_S = 4.0
WAVE_PERIOD_MAX_S = 20.0

#: The legend's wave-height edges (metres), the same buckets wave_color() uses.
_WAVE_BUCKET_EDGES = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
#: Precomputed bucket colors, derived from wave_color() so the arrows can
#: never drift from the legend and the wave fill.
_WAVE_BUCKET_COLORS = tuple(
    wave_color(index * 0.5) for index in range(len(_WAVE_BUCKET_EDGES) + 1)
)


def wave_bucket_colors():
    """Public wave bucket colors (r, g, b floats), derived from wave_color()."""
    return list(_WAVE_BUCKET_COLORS)


def _wave_bucket(height):
    """The legend bucket of a significant wave height in metres."""
    if height is None or not math.isfinite(height) or height < 0:
        return 0
    bucket = 0
    for edge in _WAVE_BUCKET_EDGES:
        if height < edge:
            break
        bucket += 1
    return bucket


def wave_arrow_length(period):
    """Shaft length in screen pixels for a wave period in seconds.

    Periods map linearly from 4 s to 20 s onto
    ``WAVE_ARROW_SHAFT_MIN``..``WAVE_ARROW_SHAFT_MAX`` and are clamped at
    both ends. A missing, zero or non-finite period keeps the minimum length,
    so the direction is still drawn instead of the symbol vanishing.
    """
    if period is None or not math.isfinite(period) or period <= 0:
        return WAVE_ARROW_SHAFT_MIN
    fraction = (period - WAVE_PERIOD_MIN_S) / (WAVE_PERIOD_MAX_S - WAVE_PERIOD_MIN_S)
    fraction = min(1.0, max(0.0, fraction))
    return WAVE_ARROW_SHAFT_MIN + fraction * (
        WAVE_ARROW_SHAFT_MAX - WAVE_ARROW_SHAFT_MIN
    )


def build_wave_geometry(xxs, yys, dirs, heights, periods):
    """Compute screen-space wave arrows grouped by wave-height colour bucket.

    ``dirs`` is the meteorological *coming-from* bearing in degrees true, the
    same convention as :meth:`WindPainter.build_barb_geometry`: the shaft
    points from the anchor towards the direction the waves come from (0 deg
    points north/up, 90 deg points east/right). Colour is the wave height and
    the shaft length is the wave period, so the height fill, the legend and
    the arrows share one ramp.

    The glyph is a shaft plus one chevron head. It deliberately carries no
    feathers: feathers encode Beaufort force on a wind barb and would read as
    a wind speed on a wave arrow.
    """
    buckets: dict[int, tuple[list, list, list]] = {}
    for x, y, wdir, height, period in zip(xxs, yys, dirs, heights, periods):
        if not all(math.isfinite(value) for value in (x, y, wdir)):
            continue
        if height is None or not math.isfinite(height):
            # A missing height has no colour; never invent a bucket for it.
            continue
        angle = math.radians(float(wdir))
        shaft_x, shaft_y = math.sin(angle), -math.cos(angle)
        perp_x, perp_y = math.cos(angle), math.sin(angle)
        length = wave_arrow_length(period)
        entry = buckets.setdefault(_wave_bucket(float(height)), ([], [], []))
        lines, _tris, _dots = entry
        tip_x = float(x) + shaft_x * length
        tip_y = float(y) + shaft_y * length
        lines.append((float(x), float(y), tip_x, tip_y))
        base_x = tip_x - shaft_x * WAVE_ARROW_HEAD_LEN
        base_y = tip_y - shaft_y * WAVE_ARROW_HEAD_LEN
        lines.append(
            (
                tip_x,
                tip_y,
                base_x + perp_x * WAVE_ARROW_HEAD_SPREAD,
                base_y + perp_y * WAVE_ARROW_HEAD_SPREAD,
            )
        )
        lines.append(
            (
                tip_x,
                tip_y,
                base_x - perp_x * WAVE_ARROW_HEAD_SPREAD,
                base_y - perp_y * WAVE_ARROW_HEAD_SPREAD,
            )
        )
    return buckets


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
    """Draw a frozen set of wind samples without accessing the GUI or live settings."""

    # Reprojected live barb arrays, bounded by entry count and memory. One
    # entry covers a (field, camera, allocation, selection) combination, so a
    # stationary repaint reuses the world pixels. The key includes the palette,
    # so a palette change conservatively invalidates the entry even though the
    # geometry is identical for the same field and camera. A hit also checks
    # object identity, so a recycled id() can never serve another field's
    # pixels, land mask, projection or on-ground callable.
    _LIVE_GEOMETRY_CACHE_ENTRIES = 4
    _LIVE_GEOMETRY_CACHE_BYTES = 64 * 1024 * 1024
    _live_geometry_cache = OrderedDict()
    _live_geometry_cache_bytes = 0
    _live_geometry_lock = threading.Lock()

    def __init__(self, settings_manager, on_ground=wind_on_ground):
        self.settings_manager = settings_manager
        self._on_ground = on_ground

    def draw_wind_barb(self, canvas, x, y, wdir, wspeed):
        """Draw a wind barb in standard meteorological notation.

        The barb points in the direction the wind comes FROM.
        Short barb = 5 kt, long barb = 10 kt, pennant (flag) = 50 kt.
        """
        wspeed_kt = wspeed * 1.94384  # m/s to knots
        opacity = self.settings_manager.gribArrowOpacity

        r, g, b = wind_color(wspeed)
        canvas.set_source_rgba(r, g, b, opacity)

        # Wind barb points into the wind (direction wind comes FROM)
        angle = math.radians(wdir)
        shaft_len = 20

        canvas.save()
        canvas.translate(x, y)
        canvas.rotate(angle)

        # Draw shaft (line pointing up = into the wind)
        canvas.move_to(0, 0)
        canvas.line_to(0, -shaft_len)
        canvas.stroke()

        # Draw dot for calm winds
        if wspeed_kt < 2.5:
            canvas.arc(0, 0, 2, 0, 2 * math.pi)
            canvas.stroke()
            canvas.restore()
            return

        # Build barbs from tip downward
        remaining = wspeed_kt
        barb_y = float(-shaft_len)
        barb_spacing = 3.5
        barb_len = 8

        # Pennants (50 kt each)
        while remaining >= 47.5:
            canvas.move_to(0, barb_y)
            canvas.line_to(barb_len, barb_y)
            canvas.line_to(0, barb_y + barb_spacing)
            canvas.close_path()
            canvas.fill()
            barb_y += barb_spacing
            remaining -= 50

        # Long barbs (10 kt each)
        while remaining >= 7.5:
            canvas.move_to(0, barb_y)
            canvas.line_to(barb_len, barb_y)
            canvas.stroke()
            barb_y += barb_spacing
            remaining -= 10

        # Short barb (5 kt)
        if remaining >= 2.5:
            canvas.move_to(0, barb_y)
            canvas.line_to(barb_len * 0.5, barb_y)
            canvas.stroke()

        canvas.restore()

    def draw(
        self, gpsmap, canvas, data, pressure=None, fill_data=None, draw_barbs=True
    ):
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

        if data and getattr(self.settings_manager, "gribWindGradient", True):
            # Draw color fill (smooth gradient between grid points).
            # A ragged multi-model union cannot form quads, so the caller
            # may supply a resampled regular grid for the fill while barbs
            # keep the dense native points.
            self._draw_color_fill(
                gpsmap, canvas, fill_data if fill_data else data, scale
            )

        # Isobars sit above the fill so thin lines stay readable.
        self.draw_isobars(gpsmap, canvas, pressure)

        if not data or not draw_barbs:
            return

        # Legacy path: bake barbs into the surface (kept for compatibility).
        # The live overlay below is the preferred foreground.
        canvas.set_line_width(1)
        points = self._barb_points(gpsmap, data, scale)
        if points:
            lats = np.array([p[2][0] for p in points], dtype=np.float64)
            lons = np.array([p[2][1] for p in points], dtype=np.float64)
            xxs, yys = gpsmap.convert_geographic_to_screen_batch(lats, lons)
            for xx, yy, p in zip(xxs, yys, points):
                if gpsmap.render_cancelled():
                    return
                self.draw_wind_barb(canvas, xx, yy, p[0], p[1])

    def draw_barbs_live(
        self,
        gpsmap,
        canvas,
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
            hide_on_ground = False
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
        self.paint_barb_geometry(canvas, geometry, opacity)
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
    def paint_barb_geometry(canvas, geometry, opacity, colors=None):
        """Paint grouped geometry with one stroke/fill per color bucket.

        ``colors`` defaults to the wind-speed buckets; the wave arrows pass
        their own height ramp so one painter serves both glyph layers.
        """
        if not geometry:
            return
        palette = _BUCKET_COLORS if colors is None else colors
        canvas.save()
        canvas.set_line_width(1.0)
        canvas.set_line_cap(CAP_BUTT)
        for bucket in sorted(geometry):
            lines, tris, dots = geometry[bucket]
            color = palette[bucket]
            canvas.set_source_rgba(color[0], color[1], color[2], opacity)
            if lines or dots:
                for x1, y1, x2, y2 in lines:
                    canvas.move_to(x1, y1)
                    canvas.line_to(x2, y2)
                for x, y in dots:
                    canvas.move_to(x + 2.0, y)
                    canvas.arc(x, y, 2.0, 0, 2 * math.pi)
                canvas.stroke()
            if tris:
                for ax, ay, bx, by, cx, cy in tris:
                    canvas.move_to(ax, ay)
                    canvas.line_to(bx, by)
                    canvas.line_to(cx, cy)
                    canvas.close_path()
                canvas.fill()
        canvas.restore()

    @classmethod
    def clear_live_geometry_cache(cls):
        """Drop every cached reprojection; used by tests and view resets."""
        with cls._live_geometry_lock:
            cls._live_geometry_cache.clear()
            cls._live_geometry_cache_bytes = 0

    @classmethod
    def _store_live_geometry(
        cls, key, flat, land_mask, projected, on_ground, xxs, yys, idx
    ):
        """Insert one reprojection entry, evicting LRU entries past the limits."""
        xxs.setflags(write=False)
        yys.setflags(write=False)
        idx.setflags(write=False)
        # The entry pins every object whose id() is in the key, so account for
        # all of them; only the callable is free.
        nbytes = int(xxs.nbytes + yys.nbytes + idx.nbytes)
        nbytes += sum(int(getattr(array, "nbytes", 0)) for array in flat)
        if land_mask is not None:
            nbytes += int(getattr(land_mask, "nbytes", 0))
        if projected is not None:
            nbytes += sum(int(getattr(array, "nbytes", 0)) for array in projected)
        if nbytes > cls._LIVE_GEOMETRY_CACHE_BYTES:
            return
        with cls._live_geometry_lock:
            previous = cls._live_geometry_cache.pop(key, None)
            if previous is not None:
                cls._live_geometry_cache_bytes -= previous[7]
            cls._live_geometry_cache[key] = (
                flat,
                land_mask,
                projected,
                on_ground,
                xxs,
                yys,
                idx,
                nbytes,
            )
            cls._live_geometry_cache_bytes += nbytes
            while (
                len(cls._live_geometry_cache) > cls._LIVE_GEOMETRY_CACHE_ENTRIES
                or cls._live_geometry_cache_bytes > cls._LIVE_GEOMETRY_CACHE_BYTES
            ):
                _, evicted = cls._live_geometry_cache.popitem(last=False)
                cls._live_geometry_cache_bytes -= evicted[7]

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
        palette_key=None,
    ):
        """Toolkit-independent live barb geometry for an explicit viewport.

        Returns ``(geometry, count)`` where geometry groups screen-space
        lines/triangles/dots by color bucket. Used by the Qt foreground
        painter; the cairo path above shares the same math.

        Reprojected world pixels are cached per field, camera, allocation and
        selection. ``palette_key`` participates in the key so a palette change
        never reuses geometry drawn for another palette; callers that omit it
        keep the previous behaviour.
        """
        if flat is None:
            return {}, 0
        dirs, speeds, lats, lons = flat
        if len(dirs) == 0:
            return {}, 0
        key = (
            id(flat),
            (center_lat, center_lon, zoom, width, height),
            step,
            max_count,
            bool(hide_on_ground),
            id(land_mask) if land_mask is not None else None,
            id(on_ground) if on_ground is not None else None,
            id(projected) if projected is not None else None,
            palette_key,
        )
        xxs = None
        yys = None
        idx = None
        with WindPainter._live_geometry_lock:
            entry = WindPainter._live_geometry_cache.get(key)
            if (
                entry is not None
                and entry[0] is flat
                and entry[1] is land_mask
                and entry[2] is projected
                and entry[3] is on_ground
            ):
                WindPainter._live_geometry_cache.move_to_end(key)
                _, _, _, _, xxs, yys, idx, _nbytes = entry
        margin = BARB_SHAFT_LEN + BARB_LEN + 4.0
        if xxs is None or yys is None or idx is None:
            xxs, yys = mercator_project_flat(
                lats,
                lons,
                center_lat,
                center_lon,
                zoom,
                width,
                height,
                projected=projected,
            )
            xxs = np.asarray(xxs, dtype=np.float64)
            yys = np.asarray(yys, dtype=np.float64)
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
            WindPainter._store_live_geometry(
                key, flat, land_mask, projected, on_ground, xxs, yys, idx
            )
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

    def draw_isobars(self, gpsmap, canvas, pressure):
        """Draw mean-sea-level pressure contours with configurable spacing and labels."""
        if not getattr(self.settings_manager, "gribIsobarsVisible", True):
            return
        if contour_generator is None or not pressure or len(pressure) < 2:
            return
        for lats, lons, values in self._pressure_blocks(pressure):
            if gpsmap.render_cancelled():
                return
            self._draw_isobar_block(gpsmap, canvas, lats, lons, values)

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
    def _stroke_contours(canvas, gpsmap, contours, opacity=0.85):
        # A dark halo keeps thin lines readable over the wind gradient fill.
        for width, color in (
            (3.0, (0, 0, 0, opacity * 0.45 / 0.85)),
            (1.1, (1, 1, 1, opacity)),
        ):
            canvas.save()
            canvas.set_line_width(width)
            canvas.set_source_rgba(*color)
            for _, line in contours:
                if gpsmap.render_cancelled():
                    canvas.restore()
                    return
                canvas.move_to(line[0][0], line[0][1])
                for x, y in line[1:]:
                    canvas.line_to(x, y)
                canvas.stroke()
            canvas.restore()

    def _draw_isobar_block(self, gpsmap, canvas, lats, lons, values):
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
        self._stroke_contours(canvas, gpsmap, contours, opacity)
        if getattr(self.settings_manager, "gribIsobarLabels", True):
            self._draw_isobar_labels(canvas, contours, opacity, gpsmap)

    @staticmethod
    def _draw_isobar_labels(canvas, contours, opacity, gpsmap):
        by_level: dict = {}
        for level, line in contours:
            length = float(np.abs(np.diff(line, axis=0)).sum())
            if length <= 60:
                continue
            if level not in by_level or length > by_level[level][0]:
                by_level[level] = (length, line)
        if not by_level:
            return
        canvas.save()
        canvas.set_font_face("Sans", SLANT_NORMAL, WEIGHT_BOLD)
        canvas.set_font_size(10)
        for level in sorted(by_level)[:15]:
            if gpsmap.render_cancelled():
                break
            _, line = by_level[level]
            mid = line[len(line) // 2]
            text = "%d" % round(level)
            canvas.move_to(mid[0] + 3, mid[1] - 3)
            canvas.text_path(text)
            canvas.set_source_rgba(0, 0, 0, opacity)
            canvas.set_line_width(3)
            canvas.stroke_preserve()
            canvas.set_source_rgba(1, 1, 1, opacity)
            canvas.fill()
        canvas.restore()

    def _barb_points(self, gpsmap, data, scale):
        points = []
        for column in data[::scale]:
            if gpsmap.render_cancelled():
                return []
            lon, lats, dirs, speeds = column[0], column[1], column[2], column[3]
            for j in range(0, len(lats), scale):
                lat = float(lats[j])
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

    def _draw_color_fill(self, gpsmap, canvas, data, scale):
        """Draw interpolated color fill between grid points.

        The Cairo backend keeps the shared ``cairo.MeshPattern``: it is the
        byte-exact reference and the production backend until Phase 5. Every
        other backend takes the single-image approximation in
        :meth:`_draw_color_fill_image`.
        """
        opacity = self.settings_manager.gribArrowOpacity * 0.3
        projected = self._grid_screen_points(gpsmap, data, scale)
        if projected is None:
            return
        screen_xs, screen_ys = projected
        if isinstance(canvas, CairoCanvas):
            self._draw_color_fill_mesh(
                gpsmap, canvas, data, scale, opacity, screen_xs, screen_ys
            )
        else:
            self._draw_color_fill_image(
                gpsmap, canvas, data, scale, opacity, screen_xs, screen_ys
            )

    def _draw_color_fill_mesh(
        self, gpsmap, canvas, data, scale, opacity, screen_xs, screen_ys
    ):
        """The cairo mesh: one straight-edged quad per sampled grid cell."""
        cr = canvas.context
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

    def _draw_color_fill_image(
        self, gpsmap, canvas, data, scale, opacity, screen_xs, screen_ys
    ):
        """Approximate the mesh with one RGBA image of the sampled grid.

        The image is built in grid space, one pixel per sampled grid node, from
        the same ``wind_color`` values the mesh sets as corner colours. It is
        blitted once into the run's screen-space bounding box so the backend's
        bilinear filter does the interpolation the mesh used to do: the fill is
        smooth and has no per-row seams, because there is no per-row blit to
        fade at its top and bottom edge.

        Columns are grouped into runs that share their sampled latitudes, and
        every cell inside a run passes the mesh's own ragged-grid guard
        (:meth:`_strip_cell_is_regular`). A partial forecast coverage therefore
        never joins different latitude intervals, exactly as it broke a strip
        run before.

        Opacity is folded into the image alpha, matching the alpha the mesh
        gives each corner colour, so the blit itself is opaque.

        Node colours are placed on their screen positions by drawing the image
        half a pixel-step outside its top-left corner and half a step past its
        bottom-right one, with edge padding. That keeps node *k* at node *k*'s
        real x/y instead of the half-cell shift a plain bounding-box blit
        gives. The overdraw is clipped back to the grid's exact screen
        bounding box, so the fill still stops where the mesh stops.

        Known geometric error: the blit scales uniformly in y, so interior rows
        sit evenly between the first and last latitude instead of at their true
        mercator offsets. ``test_canvas_equivalence`` records that error for a
        mercator-projected grid.
        """
        backend = "qt" if isinstance(canvas, QPainterCanvas) else "cairo"
        lats_by_column = [column[1].tolist() for column in data]
        speeds_by_column = [column[3].tolist() for column in data]
        column_count = len(screen_xs)
        if column_count < 2 or len(speeds_by_column) < 2:
            return
        alpha = int(round(min(1.0, max(0.0, opacity)) * 255))

        for first, last in self._regular_column_runs(lats_by_column, screen_xs, scale):
            if gpsmap.render_cancelled():
                return
            self._blit_grid_image(
                canvas,
                backend,
                alpha,
                speeds_by_column,
                screen_xs,
                screen_ys,
                first,
                last,
                scale,
            )

    @staticmethod
    def _regular_column_runs(lats_by_column, screen_xs, scale):
        """Maximal column ranges whose adjacent cells all pass the guard."""
        column_count = len(screen_xs)
        runs = []
        first = 0
        for column in range(1, column_count):
            rows = min(len(screen_xs[column - 1]), len(screen_xs[column]))
            joins = rows > 1 and all(
                WindPainter._strip_cell_is_regular(
                    lats_by_column, screen_xs, column - 1, row, scale
                )
                for row in range(rows - 1)
            )
            if joins:
                continue
            runs.append((first, column - 1))
            first = column
        runs.append((first, column_count - 1))
        return runs

    @staticmethod
    def _strip_row_count(screen_xs):
        return min(len(column) for column in screen_xs)

    @staticmethod
    def _strip_cell_is_regular(lats_by_column, screen_xs, column, row, scale):
        """The mesh's ragged-grid guard, per cell.

        Never join different latitude intervals when partial forecast coverage
        leaves a ragged grid.
        """
        right = (column + 1) * scale
        if right >= len(lats_by_column) or row + 1 >= len(screen_xs[column]):
            return False
        if row + 1 >= len(screen_xs[column + 1]):
            return False
        if row * scale >= len(lats_by_column[column * scale]):
            return False
        if row * scale >= len(lats_by_column[right]):
            return False
        try:
            return (
                lats_by_column[column * scale][row * scale]
                == lats_by_column[right][row * scale]
                and lats_by_column[column * scale][(row + 1) * scale]
                == lats_by_column[right][(row + 1) * scale]
            )
        except IndexError:
            return False

    @staticmethod
    def _blit_grid_image(
        canvas,
        backend,
        alpha,
        speeds_by_column,
        screen_xs,
        screen_ys,
        first_column,
        last_column,
        scale,
    ):
        """Blit one run of regular columns as a single grid-space RGBA image."""
        width = last_column - first_column + 1
        height = min(
            len(screen_xs[column]) for column in range(first_column, last_column + 1)
        )
        if width < 2 or height < 2:
            return

        # RGBA rows are premultiplied, as ``CanvasImage.from_rgba`` expects.
        coverage = alpha / 255.0
        pixels = np.empty((height, width, 4), dtype=np.uint8)
        try:
            for row in range(height):
                for index, column in enumerate(range(first_column, last_column + 1)):
                    r, g, b = wind_color(speeds_by_column[column * scale][row * scale])
                    pixels[row, index, 0] = int(round(r * coverage * 255.0))
                    pixels[row, index, 1] = int(round(g * coverage * 255.0))
                    pixels[row, index, 2] = int(round(b * coverage * 255.0))
                    pixels[row, index, 3] = alpha
        except IndexError:
            return

        xs = np.concatenate(
            [
                screen_xs[column][:height]
                for column in range(first_column, last_column + 1)
            ]
        )
        ys = np.concatenate(
            [
                screen_ys[column][:height]
                for column in range(first_column, last_column + 1)
            ]
        )
        x0, x1 = float(xs.min()), float(xs.max())
        y0, y1 = float(ys.min()), float(ys.max())
        if x1 <= x0 or y1 <= y0:
            return
        # Row 0 is the first latitude. When the projection puts it below the
        # last one, flip the image so image space and screen space agree.
        if ys[0] > ys[-1]:
            pixels = np.ascontiguousarray(pixels[::-1])

        step_x = (x1 - x0) / (width - 1)
        step_y = (y1 - y0) / (height - 1)
        image = CanvasImage.from_rgba(width, height, pixels, backend=backend)
        canvas.save()
        canvas.rectangle(x0, y0, x1 - x0, y1 - y0)
        canvas.clip()
        canvas.draw_image(
            image,
            x0 - step_x / 2.0,
            y0 - step_y / 2.0,
            width=(x1 - x0) + step_x,
            height=(y1 - y0) + step_y,
            alpha=1.0,
            smooth=True,
            pad=True,
        )
        canvas.restore()

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
