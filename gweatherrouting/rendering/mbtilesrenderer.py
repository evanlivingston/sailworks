"""Offline raster tile portrayal; SQLite access and PNG decode stay in workers."""

import io
import logging
import math
import sqlite3
from collections import OrderedDict

import cairo

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.core.mbtiles import MBTilesInfo, open_archive, png_size, read_info
from gweatherrouting.core.scheduling import schedule_idle

from .chartlayer import ChartLayer
from .overlayrenderer import OverlayRenderer

logger = logging.getLogger("gweatherrouting")
TILE_SIZE = 256


class MBTilesChart(ChartLayer):
    def __init__(self, path, settings_manager, metadata=None, enabled=True):
        info = (
            MBTilesInfo.from_metadata(path, metadata) if metadata else read_info(path)
        )
        super().__init__(
            info.path, "mbtiles", settings_manager, info.metadata(), enabled
        )
        self.info = info


def tile_window(viewport, info, zoom):
    """Bound tile enumeration to archive coverage, including wrapped longitudes."""
    view = MapViewport(viewport)
    view._zoom = zoom
    ratio = 2.0 ** (viewport._zoom - zoom)
    left = view._lon_to_x(view._center_lon) - view.width / (2 * ratio)
    top = view._lat_to_y(view._center_lat) - view.height / (2 * ratio)
    x0, x1 = math.floor(left / TILE_SIZE), math.ceil(
        (left + view.width / ratio) / TILE_SIZE
    )
    y0, y1 = math.floor(top / TILE_SIZE), math.ceil(
        (top + view.height / ratio) / TILE_SIZE
    )
    west, south, east, north = info.bounds
    limit = view.MAX_LAT
    north_px = view._lat_to_y(min(limit, north))
    south_px = view._lat_to_y(max(-limit, south))
    if north_px >= top + view.height / ratio or south_px <= top:
        return ratio, left, top, []
    y0 = max(0, y0, math.floor(north_px / TILE_SIZE))
    y1 = min(2**zoom, y1, math.ceil(south_px / TILE_SIZE))
    spans = [(west, east)] if west <= east else [(west, 180), (-180, east)]
    ranges = []
    n = 2**zoom
    for west, east in spans:
        west_px, east_px = view._lon_to_x(west), view._lon_to_x(east)
        a, b = math.floor(west_px / TILE_SIZE), math.ceil(east_px / TILE_SIZE)
        for cycle in range(x0 // n, (x1 - 1) // n + 1):
            shifted_west, shifted_east = (
                west_px + cycle * n * TILE_SIZE,
                east_px + cycle * n * TILE_SIZE,
            )
            if shifted_west >= left + view.width / ratio or shifted_east <= left:
                continue
            first, last = max(x0, a + cycle * n), min(x1, b + cycle * n)
            if first < last and y0 < y1:
                clip = (
                    (shifted_west - left) * ratio,
                    (north_px - top) * ratio,
                    (shifted_east - shifted_west) * ratio,
                    (south_px - north_px) * ratio,
                )
                ranges.append((first, last, y0, y1, clip))
    return ratio, left, top, ranges


class MBTilesRenderer(OverlayRenderer):
    CACHE_ENTRIES = 256
    CACHE_BYTES = 64 * 1024 * 1024
    MAX_VIEW_TILES = 512

    def __init__(self, schedule=schedule_idle):
        super().__init__(schedule)
        self._tiles = OrderedDict()
        self._tile_bytes = 0

    def draw(self, canvas, cr, charts, palette):
        super().draw(canvas, cr, charts, palette)
        state = self._states.get(canvas)
        if not state or not state.frame or state.frame[0].key != state.key:
            return
        # Labels stay at the visible frontend edge, not the worker's padded edge.
        p1, p2 = canvas.get_bbox()
        lat1, lon1 = p1.get_degrees()
        lat2, lon2 = p2.get_degrees()
        sources = []
        for (info,) in charts:
            west, south, east, north = info.bounds
            if (
                south <= max(lat1, lat2)
                and north >= min(lat1, lat2)
                and (
                    west > east or (west <= max(lon1, lon2) and east >= min(lon1, lon2))
                )
            ):
                sources.append(info.attribution or info.name)
        if not sources:
            return
        bounds = getattr(canvas, "get_attribution_bounds", None)
        x, y, width, height = (
            bounds()
            if bounds
            else (0, 0, canvas.get_allocated_width(), canvas.get_allocated_height())
        )
        cr.save()
        cr.rectangle(x, y, width, height)
        cr.clip()
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        text = "Offline imagery · " + " / ".join(dict.fromkeys(sources))
        text_width = cr.text_extents(text)[2]
        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.rectangle(x + 4, y + height - 40, min(width - 8, text_width + 8), 18)
        cr.fill()
        cr.set_source_rgb(0.15, 0.15, 0.15)
        cr.move_to(x + 8, y + height - 27)
        cr.show_text(text)
        cr.restore()

    def _tile(self, db, info, zoom, x, y):
        key = (info.path, info.mtime_ns, info.size, zoom, x, y)
        if key in self._tiles:
            self._tiles.move_to_end(key)
            return self._tiles[key]
        row = 2**zoom - 1 - y if info.scheme == "tms" else y
        result = db.execute(
            "SELECT tile_data FROM tiles"
            " WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1",
            (zoom, x, row),
        ).fetchone()
        surface = None
        if result:
            try:
                png_size(result[0])
                surface = cairo.ImageSurface.create_from_png(io.BytesIO(result[0]))
                surface.set_device_scale(
                    surface.get_width() / TILE_SIZE, surface.get_height() / TILE_SIZE
                )
            except (ValueError, cairo.Error) as error:
                logger.warning(
                    "Unable to decode MBTiles tile %s/%s/%s in %s: %s",
                    zoom,
                    x,
                    y,
                    info.path,
                    error,
                )
        self._tiles[key] = surface
        self._tile_bytes += (
            0 if surface is None else surface.get_stride() * surface.get_height()
        )
        while (
            len(self._tiles) > self.CACHE_ENTRIES or self._tile_bytes > self.CACHE_BYTES
        ):
            _, old = self._tiles.popitem(last=False)
            self._tile_bytes -= (
                0 if old is None else old.get_stride() * old.get_height()
            )
        return surface

    def _render(self, job):
        viewport = job.viewport
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            viewport.width * job.factor,
            viewport.height * job.factor,
        )
        surface.set_device_scale(job.factor, job.factor)
        cr = cairo.Context(surface)
        for (info,) in job.charts:
            if job.cancel.is_set():
                return None
            try:
                window = self._tile_window(viewport, info)
                if window is None:
                    continue
                target, ratio, left, top, ranges = window
                # Each job owns and closes its connection on the same thread.
                with open_archive(info.path) as db:
                    if not self._paint_ranges(
                        cr, db, info, target, ratio, left, top, ranges, job
                    ):
                        return None
            except (sqlite3.Error, OSError, ValueError, cairo.Error) as error:
                logger.warning("Unable to render MBTiles %s: %s", info.path, error)
        surface.flush()
        return None if job.cancel.is_set() else surface

    def _tile_window(self, viewport, info):
        """Zoom level and tile window to draw, or None when the chart is skipped."""
        target = min(info.zooms, key=lambda z: (abs(z - viewport._zoom), -z))
        choices = [z for z in reversed(info.zooms) if z <= target]
        for target in choices:
            ratio, left, top, ranges = tile_window(viewport, info, target)
            if (
                sum((b - a) * (d - c) for a, b, c, d, _ in ranges)
                <= self.MAX_VIEW_TILES
            ):
                break
        else:
            return None
        if not ranges:
            return None
        return target, ratio, left, top, ranges

    def _paint_ranges(self, cr, db, info, target, ratio, left, top, ranges, job):
        """Draw the tile windows; returns False when the job was cancelled."""
        for a, b, c, d, clip in ranges:
            cr.save()
            try:
                cr.rectangle(*clip)
                cr.clip()
                for y in range(c, d):
                    for x in range(a, b):
                        if job.cancel.is_set():
                            return False
                        self._paint_tile(cr, db, info, target, x, y, ratio, left, top)
            finally:
                cr.restore()
        return True

    def _paint_tile(self, cr, db, info, target, x, y, ratio, left, top):
        for zoom in reversed(info.zooms):
            if zoom > target:
                continue
            distance = 2 ** (target - zoom)
            px, py = x // distance, y // distance
            tile = self._tile(db, info, zoom, px % 2**zoom, py)
            if tile is None:
                continue
            cr.save()
            cr.rectangle(
                (x * TILE_SIZE - left) * ratio,
                (y * TILE_SIZE - top) * ratio,
                TILE_SIZE * ratio,
                TILE_SIZE * ratio,
            )
            cr.clip()
            cr.translate(
                (px * TILE_SIZE * distance - left) * ratio,
                (py * TILE_SIZE * distance - top) * ratio,
            )
            cr.scale(ratio * distance, ratio * distance)
            cr.set_source_surface(tile)
            cr.get_source().set_filter(cairo.FILTER_BILINEAR)
            cr.get_source().set_extend(cairo.EXTEND_PAD)
            cr.paint()
            cr.restore()
            break
