"""Background portrayal of Qt base vectors using exclusively private GDAL handles.

ENC and CHS already have workers. This adapter covers GSHHS and simple vectors;
unaudited chart types retain ChartCollection's existing path.
"""

from collections import OrderedDict
from dataclasses import dataclass

import cairo
from osgeo import ogr

from gweatherrouting.core.geo.shoreline import ShorelineGeometryCache
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from gweatherrouting.rendering.gshhs import GSHHSVectorChart
from gweatherrouting.rendering.overlayrenderer import MARGIN, OverlayRenderer
from gweatherrouting.rendering.vectordrawer.simplechartdrawer import SimpleChartDrawer


@dataclass(frozen=True)
class BaseSource:
    kind: str
    path: str
    downscale: bool = False
    countries: tuple = ()
    initial_times: tuple = ()
    generation: int = 0


def describe_sources(charts):
    """Snapshot source identity on the GUI thread; never pass chart objects on."""
    result = []
    for chart in charts:
        if not chart.enabled:
            continue
        if type(chart) is GSHHSVectorChart:
            result.append(
                BaseSource(
                    "gshhs",
                    chart.path,
                    chart.force_downscale,
                    tuple(tuple(country) for country in chart.countries),
                    tuple(sorted(chart.last_time.items())),
                    id(chart),
                )
            )
        elif type(chart) is GDALVectorChart and type(chart.drawer) is SimpleChartDrawer:
            result.append(BaseSource("simple", chart.path, generation=id(chart)))
        else:
            return None
    return tuple(result)


class BaseChartRenderer(OverlayRenderer):
    # Includes visible area plus BOTH existing Qt padding and worker margin.
    MAX_SURFACE_BYTES = 128 * 1024 * 1024

    def __init__(self, schedule):
        super().__init__(schedule)
        self._charts = OrderedDict()
        self._failed_key = None
        self.diagnostics = {}

    def forget(self, canvas):
        super().forget(canvas)
        self._release_if_idle()

    def _release_if_idle(self):
        # Cancellation is asynchronous. Never drop a dataset while the worker
        # may still be using it; completion hands ownership back to this thread.
        if not self._states and not self._running:
            self._release_resources()

    def _release_resources(self):
        self._charts.clear()

    def draw_base(self, canvas, cr, charts, palette, fallback=None):
        sources = describe_sources(charts)
        # A list, not a tuple: the fallback path expects the chart sequence and
        # the diagnostics sum over it, so the element type has to be known.
        synchronous: list = []
        if sources is None and fallback is not None:
            # Keep layer order: only an audited PREFIX may move to the worker.
            # OSM/raster/CM93 and everything above them keep the existing path.
            for index, chart in enumerate(charts):
                if describe_sources([chart]) is None:
                    sources = describe_sources(charts[:index])
                    synchronous = list(charts[index:])
                    break
        factor = canvas.get_scale_factor()
        pixels = (canvas.get_allocated_width() + 2 * MARGIN) * (
            canvas.get_allocated_height() + 2 * MARGIN
        )
        key = (palette, factor, sources)
        self.diagnostics = {
            "source_types": [type(chart).__name__ for chart in charts if chart.enabled],
            "source_count": len(sources) if sources is not None else None,
            "synchronous_source_count": sum(chart.enabled for chart in synchronous),
            "surface_bytes": pixels * factor * factor * 4,
            "mode": "worker",
        }
        if (
            sources is None
            or key == self._failed_key
            or pixels * factor * factor * 4 > self.MAX_SURFACE_BYTES
        ):
            self.diagnostics["mode"] = "synchronous"
            self.forget(canvas)
            return False
        # OverlayRenderer's source signature uses the first member of each tuple.
        self.draw(canvas, cr, tuple((source,) for source in sources), palette)
        if synchronous:
            fallback(canvas, cr, synchronous)
        return True

    def _completed(self, canvas, job, surface, error):
        demanded = canvas in self._states
        if error and demanded:
            self._failed_key = job.key
        result = super()._completed(canvas, job, surface, error)
        self._release_if_idle()
        if error and demanded:
            # Do not leave a missing coastline after an unsupported driver/open
            # failure. Retry via the existing synchronous path for this content.
            canvas.queue_draw()
        return result

    def _worker_chart(self, source, palette):
        chart = self._charts.get(source)
        if chart is None:
            # Deliberately bypass GUI-facing constructors/settings callbacks and
            # symbol-provider initialization. Only audited portrayal fields exist.
            if source.kind == "gshhs":
                chart = GSHHSVectorChart.__new__(GSHHSVectorChart)
                chart.vector_files = {}
                for quality in ("f", "h", "i", "l", "c"):
                    path = f"{source.path}/GSHHS_shp/{quality}/GSHHS_{quality}_L1.shp"
                    dataset = ogr.Open(path)
                    if dataset is None:
                        raise ValueError(f"Cannot open coastline chart {path}")
                    chart.vector_files[quality + "1"] = dataset
                chart.countries = source.countries
                chart.force_downscale = source.downscale
                chart.last_time = dict(source.initial_times)
                chart.drawer = SimpleChartDrawer.__new__(SimpleChartDrawer)
            else:
                chart = GDALVectorChart.__new__(GDALVectorChart)
                chart.vector_file = ogr.Open(source.path)
                if chart.vector_file is None:
                    raise ValueError(f"Cannot open base chart {source.path}")
                chart.drawer = SimpleChartDrawer.__new__(SimpleChartDrawer)
            if isinstance(chart.drawer, SimpleChartDrawer):
                chart.drawer._shorelines = ShorelineGeometryCache()
            self._charts[source] = chart
            if len(self._charts) > 8:
                self._charts.popitem(last=False)
        self._charts.move_to_end(source)
        chart.drawer.palette = palette
        return chart

    def _render(self, job):
        viewport = job.viewport
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            viewport.width * job.factor,
            viewport.height * job.factor,
        )
        surface.set_device_scale(job.factor, job.factor)
        cr = cairo.Context(surface)
        for (source,) in job.charts:
            if job.cancel.is_set():
                return None
            chart = self._worker_chart(source, job.palette)
            cr.save()
            chart.do_draw(viewport, cr)
            cr.restore()
        surface.flush()
        return None if job.cancel.is_set() else surface
