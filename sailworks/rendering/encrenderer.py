"""Render ENC detail off the GUI thread; transform the last image during gestures."""

from collections import OrderedDict
from sailworks.core.scheduling import schedule_idle

from osgeo import ogr

from sailworks.core.enccharts import chart_scale_rank, chart_scale_visible
from sailworks.core.mapviewport import MapPoint
from sailworks.rendering.overlayrenderer import OverlayRenderer

from .canvas import FILL_EVEN_ODD, Canvas, CanvasImage
from .chartlayer import ChartLayer
from .gdalvectorchart import GDALVectorChart
from .vectordrawer.s57chartdrawer import S57ChartDrawer


def coverage_path(canvas, viewport, geometry):
    """Append polygon rings to the canvas clip, including islands and holes."""
    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    if kind == ogr.wkbPolygon:
        for index in range(geometry.GetGeometryCount()):
            ring = geometry.GetGeometryRef(index)
            for point_index, (lon, lat, *_) in enumerate(ring.GetPoints()):
                x, y = viewport.convert_geographic_to_screen(MapPoint(lat, lon))
                if point_index == 0:
                    canvas.move_to(x, y)
                else:
                    canvas.line_to(x, y)
            canvas.close_path()
    elif kind in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        for index in range(geometry.GetGeometryCount()):
            coverage_path(canvas, viewport, geometry.GetGeometryRef(index))


class ENCOverlayRenderer(OverlayRenderer):
    def __init__(self, schedule=schedule_idle):
        super().__init__(schedule)
        # These GDAL handles belong exclusively to the rendering worker.
        self._charts = OrderedDict()

    def _render(self, job):
        viewport = job.viewport
        image = CanvasImage.new(viewport.width, viewport.height, job.factor)
        canvas = Canvas.for_image(image)
        p1, p2 = viewport.get_bbox()
        bounding = ogr.CreateGeometryFromWkt(
            ChartLayer.get_bounding_wkt_of_coords(*p1.get_degrees(), *p2.get_degrees())
        )
        min_lon, max_lon, min_lat, max_lat = bounding.GetEnvelope()
        # Fill each location with its best available scale, independent of import
        # order. Other charts contribute only where that coverage ends.
        covered = None
        charts = sorted(
            job.charts,
            key=lambda c: (chart_scale_rank(c[1], viewport.get_scale()), c[0]),
        )
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
            region = (
                region.Intersection(bounding)
                if region is not None
                else bounding.Clone()
            )
            if covered is not None:
                region = region.Difference(covered)
            if region.IsEmpty():
                continue
            canvas.save()
            canvas.new_path()
            coverage_path(canvas, viewport, region)
            canvas.set_fill_rule(FILL_EVEN_ODD)
            canvas.clip()
            chart.do_draw(viewport, canvas)
            canvas.restore()
            if known_coverage:
                covered = region if covered is None else covered.Union(region)
        canvas.finish()
        return None if job.cancel.is_set() else image

    def _worker_chart(self, path, native_scale, palette):
        chart = self._charts.get(path)
        if chart is None:
            # No GUI objects, symbol-provider initialization or settings callbacks here.
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
