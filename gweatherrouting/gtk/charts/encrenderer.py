"""Render ENC detail off the GTK thread; transform the last image during gestures."""

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Event, Thread

import cairo
from gi.repository import GLib
from osgeo import ogr

from gweatherrouting.core.enccharts import chart_scale_rank, chart_scale_visible
from gweatherrouting.gtk.widgets.mapwidget import MapPoint, MapViewport

from .chartlayer import ChartLayer
from .gdalvectorchart import GDALVectorChart
from .vectordrawer.s57chartdrawer import S57ChartDrawer

logger = logging.getLogger("gweatherrouting")
MARGIN = 256


@dataclass
class RenderJob:
    key: tuple
    viewport: MapViewport
    charts: tuple
    factor: int
    palette: str
    cancel: Event = field(default_factory=Event)


@dataclass
class RenderState:
    key: tuple = ()
    frame: object = None
    requested: object = None
    retry_after: float = 0.0


def fits(viewport, canvas):
    if viewport._zoom != canvas._zoom:
        return False
    if viewport.width != canvas.get_allocated_width() + 2 * MARGIN:
        return False
    if viewport.height != canvas.get_allocated_height() + 2 * MARGIN:
        return False
    dx = viewport._lon_to_x(viewport._center_lon) - viewport._lon_to_x(canvas._center_lon)
    dy = viewport._lat_to_y(viewport._center_lat) - viewport._lat_to_y(canvas._center_lat)
    return max(abs(dx), abs(dy)) <= MARGIN / 2


def paint_viewport(canvas, cr, viewport, surface):
    ratio = 2.0 ** (canvas._zoom - viewport._zoom)
    x = canvas._lon_to_x(viewport._center_lon) - canvas._lon_to_x(canvas._center_lon)
    y = canvas._lat_to_y(viewport._center_lat) - canvas._lat_to_y(canvas._center_lat)
    x += canvas.get_allocated_width() / 2 - viewport.width * ratio / 2
    y += canvas.get_allocated_height() / 2 - viewport.height * ratio / 2
    cr.save()
    cr.translate(x, y)
    cr.scale(ratio, ratio)
    cr.set_source_surface(surface, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_BILINEAR)
    cr.paint()
    cr.restore()


def coverage_path(cr, viewport, geometry):
    """Append polygon rings to a Cairo clip, including islands and holes."""
    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    if kind == ogr.wkbPolygon:
        for index in range(geometry.GetGeometryCount()):
            ring = geometry.GetGeometryRef(index)
            for point_index, (lon, lat, *_) in enumerate(ring.GetPoints()):
                x, y = viewport.convert_geographic_to_screen(MapPoint(lat, lon))
                if point_index == 0:
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
            cr.close_path()
    elif kind in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        for index in range(geometry.GetGeometryCount()):
            coverage_path(cr, viewport, geometry.GetGeometryRef(index))



class ENCOverlayRenderer:
    def __init__(self):
        self._states = {}
        self._pending = OrderedDict()
        self._running = False
        # Accessed only by the single worker; its GDAL handles are never shared with GTK.
        self._charts = OrderedDict()

    def forget(self, canvas):
        state = self._states.pop(canvas, None)
        self._pending.pop(canvas, None)
        if state and state.requested:
            state.requested.cancel.set()

    def draw(self, canvas, cr, charts, palette):
        if not charts:
            self.forget(canvas)
            return
        factor = canvas.get_scale_factor()
        key = (palette, factor, tuple(c[0] for c in charts))
        state = self._states.setdefault(canvas, RenderState())
        state.key = key
        frame = state.frame
        fresh = frame and frame[0].key == key and fits(frame[0].viewport, canvas)
        requested = state.requested
        pending = requested and requested.key == key and fits(requested.viewport, canvas)
        if fresh and requested and not pending:
            requested.cancel.set()
            self._pending.pop(canvas, None)
            state.requested = None
        if (not fresh and not pending and not getattr(canvas, "_zoom_animating", False)
                and time.monotonic() >= state.retry_after):
            if requested:
                requested.cancel.set()
            viewport = MapViewport(canvas, MARGIN, lambda: None)
            job = RenderJob(key, viewport, charts, factor, palette)
            viewport.render_cancelled = job.cancel.is_set
            state.requested = job
            self._pending[canvas] = job
            self._start_next()
        if frame and self._can_preview(frame[0], key):
            self._paint(canvas, cr, *frame)

    def _can_preview(self, job, key):
        return job.key == key

    @staticmethod
    def _paint(canvas, cr, job, surface):
        paint_viewport(canvas, cr, job.viewport, surface)

    def _start_next(self):
        if self._running or not self._pending:
            return
        canvas, job = self._pending.popitem(last=False)
        self._running = True

        def worker():
            surface, error = None, None
            try:
                surface = self._render(job)
            except Exception as exc:
                error = str(exc)
            GLib.idle_add(self._completed, canvas, job, surface, error)

        Thread(target=worker, name="enc-render", daemon=True).start()

    def _render(self, job):
        viewport = job.viewport
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, viewport.width * job.factor, viewport.height * job.factor
        )
        surface.set_device_scale(job.factor, job.factor)
        cr = cairo.Context(surface)
        p1, p2 = viewport.get_bbox()
        bounding = ogr.CreateGeometryFromWkt(
            ChartLayer.get_bounding_wkt_of_coords(*p1.get_degrees(), *p2.get_degrees())
        )
        min_lon, max_lon, min_lat, max_lat = bounding.GetEnvelope()
        # Fill each location with its best available scale, independent of import
        # order. Other charts contribute only where that coverage ends.
        covered = None
        charts = sorted(job.charts, key=lambda c: (chart_scale_rank(c[1], viewport.get_scale()), c[0]))
        for path, native_scale, envelopes in charts:
            if job.cancel.is_set():
                return None
            if not chart_scale_visible(native_scale, viewport.get_scale()):
                continue
            if envelopes and not any(
                a <= max_lon and b >= min_lon and c <= max_lat and d >= min_lat
                for a, b, c, d in envelopes
            ):
                continue
            chart = self._worker_chart(path, native_scale, job.palette)
            region = None
            for geometry in chart._coverage:
                region = geometry.Clone() if region is None else region.Union(geometry)
            known_coverage = region is not None
            region = region.Intersection(bounding) if known_coverage else bounding.Clone()
            if covered is not None:
                region = region.Difference(covered)
            if region.IsEmpty():
                continue
            cr.save()
            cr.new_path()
            coverage_path(cr, viewport, region)
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.clip()
            chart.do_draw(viewport, cr)
            cr.restore()
            if known_coverage:
                covered = region if covered is None else covered.Union(region)
        surface.flush()
        return None if job.cancel.is_set() else surface

    def _worker_chart(self, path, native_scale, palette):
        chart = self._charts.get(path)
        if chart is None:
            # No GTK objects, symbol-provider initialization or settings callbacks here.
            chart = GDALVectorChart.__new__(GDALVectorChart)
            chart.vector_file = ogr.Open(path)
            if chart.vector_file is None:
                raise ValueError(f"Cannot open chart {path}")
            chart._coverage = GDALVectorChart.read_coverage(chart.vector_file)
            chart._native_scale = native_scale
            chart.drawer = S57ChartDrawer.__new__(S57ChartDrawer)
            self._charts[path] = chart
            if len(self._charts) > 8:
                self._charts.popitem(last=False)
        self._charts.move_to_end(path)
        chart.drawer.palette = palette
        return chart

    def _completed(self, canvas, job, surface, error):
        self._running = False
        state = self._states.get(canvas)
        if state:
            if state.requested is job:
                state.requested = None
            if error:
                state.retry_after = time.monotonic() + 1
                logger.warning("Unable to render chart detail: %s", error)
            if surface is not None and not job.cancel.is_set() and state.key == job.key:
                state.frame = (job, surface)
                canvas.queue_draw()
        self._start_next()
        return False
