"""The viewport's chart layer: an independent generation of chart imagery.

The chart layer is the *application's* chart composition, not a second one.
The base chart is composed by ``BaseChartRenderer`` -- the application's own
worker-side composition with private GDAL handles, the application's chart
drawers and the application's palette -- and the mean-sea-level pressure
contours are the application's own ``WindPainter.draw_isobars`` on the same
cairo surface.  No chart portrayal is re-implemented here.

What the layer adds is the viewport's *generation*.  The chart composition is
its own pipeline with its own worker thread, its own state and its own cancel
event:

* the weather is presented before the chart layer is ready, and the chart
  layer never waits for the weather to be presented to begin;
* a superseded chart generation is cancelled and can never publish, exactly
  as the field pipeline guarantees for fields;
* the chart layer never claims the field worker: it waits for the weather
  pipeline to be idle before it composes, so the chart can never delay the
  weather;
* the chart layer's composition is retained while it still covers the
  camera, using the application's own ``fits`` rule, so a small pan does not
  recompose the chart.
"""

from pathlib import Path
from threading import Condition, Event, Lock, Thread
import time

from gweatherrouting.core.mapviewport import MercatorProjection
from gweatherrouting.rendering.overlayrenderer import MARGIN, RenderJob, fits

#: The application's default chart palette.
CHART_PALETTE = "cm93"

#: The settle window the chart layer composes for.  The recorded interaction
#: sweep delivers its inputs at 30 per second (33 ms apart), so a window of
#: 500 ms spans more than fifteen inputs: an interaction never triggers a
#: composition, and the chart layer never competes with the weather
#: pipeline for the worker.  This is a policy, not a measured threshold.
CHART_SETTLE_MS = 500

#: The coastline qualities' initial draw times, as the application's own
#: ``GSHHSVectorChart`` records them.
_INITIAL_TIMES = (("c", 0), ("f", 0), ("h", 0), ("i", 0), ("l", 0))


class ChartSettings:
    """The read-only settings the application's chart code reads.

    The viewport does not own the application's settings manager and never
    writes user data, so the chart code is given the values it reads.  The
    palette is the application's own default.
    """

    chartPalette = CHART_PALETTE
    gribIsobarsVisible = True
    gribArrowOpacity = 1.0

    def register_on_change(self, key, callback):
        """The viewport's chart settings do not change while it runs."""
        return None


class ChartCamera(MercatorProjection):
    """The viewport's camera as the canvas the application's chart code wants.

    The application's chart composition draws into a canvas; the viewport has
    a camera.  This adapter presents the camera with the canvas API the
    application's own chart code already uses, so the composition stays the
    application's composition.
    """

    def __init__(self, camera):
        self._center_lat = camera.center_lat
        self._center_lon = camera.center_lon
        self._zoom = camera.zoom
        self._width = camera.width
        self._height = camera.height

    def get_allocated_width(self):
        return self._width

    def get_allocated_height(self):
        return self._height

    def get_scale_factor(self):
        return 1

    def queue_draw(self):
        """The chart layer publishes its own image; no canvas repaint is due."""


def _countries():
    """The coastline's country labels, read from the application's resource.

    The labels are the application's own data (``countries_en.txt``), parsed
    the way ``GSHHSVectorChart`` parses them.
    """
    from gweatherrouting.common import resource_path

    with open(resource_path("gweatherrouting", "data/countries_en.txt")) as handle:
        rows = handle.read().split("\n")
    return tuple(
        tuple(country)
        for country in filter(
            lambda row: len(row) == 4, map(lambda row: row.split(";"), rows)
        )
    )


def base_sources(data_dir):
    """The application's base chart sources, described for the worker.

    The description is the application's own ``BaseSource``: the worker opens
    the datasets privately and draws them with the application's drawers.
    """
    from gweatherrouting.qt.basecharts import BaseSource

    path = Path(data_dir) / "gshhs"
    if not path.is_dir():
        return ()
    return (
        BaseSource(
            "gshhs",
            str(path),
            False,
            _countries(),
            _INITIAL_TIMES,
            id(path),
        ),
    )


def chart_placement(viewport, camera):
    """Where the composed chart image sits for the current camera.

    The composition covers the camera's window with the application's own
    margin, so a retained composition is placed by the same offset the
    application paints it with.
    """
    view = camera.view()
    x = view._lon_to_x(viewport._center_lon) - view._lon_to_x(camera.center_lon)
    y = view._lat_to_y(viewport._center_lat) - view._lat_to_y(camera.center_lat)
    x += camera.width / 2.0 - viewport.width / 2.0
    y += camera.height / 2.0 - viewport.height / 2.0
    return (x, y, viewport.width, viewport.height)


class _ChartComposer:
    """The application's base chart composition, with the application's isobars.

    ``BaseChartRenderer._render`` is the application's own worker-side
    composition: the private GDAL handles, the application's chart drawers
    and the application's palette.  The isobars are the application's own
    ``WindPainter.draw_isobars`` on the same cairo surface, so the chart
    layer's pixels are the application's chart pixels.
    """

    def __init__(self, palette, sources):
        from gweatherrouting.core.scheduling import schedule_idle
        from gweatherrouting.qt.basecharts import BaseChartRenderer

        self._renderer = BaseChartRenderer(schedule_idle)
        self._palette = palette
        self._sources = tuple(sources)
        self._painter = None

    def painter(self):
        if self._painter is None:
            from gweatherrouting.rendering.windpainter import WindPainter

            self._painter = WindPainter(ChartSettings())
        return self._painter

    def render(self, job, isobars=None):
        import cairo

        if self._sources:
            surface = self._renderer._render(job)
        else:
            # No installed coastline: the layer composes the isobars alone,
            # on an empty surface of the same size and scale.
            viewport = job.viewport
            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32,
                viewport.width * job.factor,
                viewport.height * job.factor,
            )
            surface.set_device_scale(job.factor, job.factor)
            surface.flush()
        if surface is None or not isobars:
            return surface
        cr = cairo.Context(surface)
        self.painter().draw_isobars(job.viewport, cr, isobars)
        surface.flush()
        return surface


class ViewportChartLayer:
    """The viewport's chart layer: its own state, generation and worker.

    The layer is requested from the GUI thread and publishes on the GUI
    thread; the composition itself runs on the layer's own worker, so it
    never blocks the field worker or the GUI thread.
    """

    def __init__(
        self,
        paths,
        data_dir,
        palette=CHART_PALETTE,
        settle_ms=CHART_SETTLE_MS,
        worker_idle=None,
        current_camera=None,
        on_publish=None,
        on_state=None,
    ):
        self._paths = tuple(str(path) for path in paths)
        self._data_dir = Path(data_dir)
        self._palette = palette
        self._settle_ms = settle_ms
        # The chart yields to the weather pipeline: it never claims the
        # field worker, so the chart can never delay the weather.
        self._worker_idle = worker_idle or (lambda: True)
        # The view the composition was asked for: a composition that no
        # longer matches the view is abandoned before it starts.
        self._current_camera = current_camera
        self._on_publish = on_publish
        self._on_state = on_state
        self._condition = Condition()
        self._pending = None
        self._active = None
        self._generation = 0
        self._published = None
        self._state = "idle"
        self._error = ""
        self._thread = None
        self._closed = False
        self._sources = ()
        self._sampler = None
        self._sampler_sources = ()
        self._composer_instance = None
        self._outcome = None
        self._spans = []
        self._published_generations = set()
        self._rejected_generations = set()
        self._outcomes = {}
        self.counters = dict(
            requests=0,
            reuses=0,
            compositions=0,
            publications=0,
            superseded_rejected=0,
            cancelled=0,
            failures=0,
            isobars=0,
            yielded=0,
            stale=0,
        )

    # ------------------------------------------------------------- state
    @property
    def state(self):
        """``idle``, ``loading``, ``ready`` or ``unavailable``."""
        with self._condition:
            return self._state

    @property
    def error(self):
        with self._condition:
            return self._error

    def observations(self):
        """The layer's generation evidence, for the gate."""
        with self._condition:
            return {
                "state": self._state,
                "generation": self._generation,
                "published_generations": tuple(sorted(self._published_generations)),
                "rejected_generations": tuple(sorted(self._rejected_generations)),
                "outcomes": dict(self._outcomes),
                "counters": dict(self.counters),
                "spans": tuple(self._spans),
                "error": self._error,
            }

    # ----------------------------------------------------------- requests
    def request(self, camera, replace=False):
        """Ask for the chart layer at this camera (GUI thread).

        A retained composition that still covers the camera is reused: no new
        generation is created and nothing is recomposed.  Otherwise a new
        generation replaces the previous one, which is cancelled and can
        never publish.  ``replace`` asks for a new generation even when the
        same view is already composing.
        """
        with self._condition:
            if self._closed:
                return None
            if not replace and self._active is not None and self._active[1] == camera:
                # The same view is already composing: asking again would
                # supersede a composition that is still running.
                return self._active[0]
            if self._published is not None and self._reusable(camera):
                self.counters["reuses"] += 1
                return self._published[0]
            self._generation += 1
            generation = self._generation
            for request in (self._pending, self._active):
                if request is not None:
                    request[2].set()
                    self.counters["cancelled"] += 1
            self._pending = None
            self._pending = (generation, camera, Event())
            self._state = "loading"
            self._error = ""
            self.counters["requests"] += 1
            if self._thread is None:
                self._thread = Thread(
                    target=self._run, name="grib-viewport-chart", daemon=True
                )
                self._thread.start()
        self._notify_state("loading")
        with self._condition:
            self._condition.notify_all()
        return generation

    def _reusable(self, camera):
        """Whether the retained composition still serves this camera."""
        _generation, _image, metadata = self._published
        if metadata["time"] != camera.stamp():
            # The isobars belong to the forecast instant.
            return False
        return fits(metadata["viewport"], ChartCamera(camera))

    def close(self):
        """Stop the chart worker; a pending composition is released."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for request in (self._pending, self._active):
                if request is not None:
                    request[2].set()
                    self.counters["cancelled"] += 1
            self._pending = None
            self._condition.notify_all()

    # ------------------------------------------------------------ worker
    def _notify_state(self, state):
        if self._on_state is not None:
            self._on_state(state)

    def _notify_publish(self, payload):
        if self._on_publish is not None:
            self._on_publish(payload)

    def _run(self):
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    request, self._pending = self._pending, None
                    self._active = request
                generation, camera, cancel = request
                began = time.perf_counter()
                error = ""
                image = None
                metadata = None
                self._outcome = None
                try:
                    if not cancel.is_set():
                        image, metadata = self._compose(camera, cancel)
                except Exception as exc:  # a chart failure is never fatal
                    error = str(exc)
                ended = time.perf_counter()
                publish = None
                with self._condition:
                    self._active = None
                    self._spans.append((began, ended))
                    if len(self._spans) > 256:
                        del self._spans[: len(self._spans) - 256]
                    if (
                        self._outcome == "yielded"
                        and not cancel.is_set()
                        and (generation == self._generation)
                    ):
                        # The weather pipeline claimed the worker: the chart
                        # yields, and the layer asks again when the weather
                        # pipeline is idle.
                        self._state = "idle"
                        self._pending = None
                    elif (
                        cancel.is_set()
                        or generation != self._generation
                        or self._outcome == "stale"
                    ):
                        # Superseded: this generation may never publish.
                        self.counters["superseded_rejected"] += 1
                        self._rejected_generations.add(generation)
                        self._outcomes[generation] = "superseded"
                    elif image is not None:
                        self.counters["compositions"] += 1
                        self.counters["publications"] += 1
                        self._published_generations.add(generation)
                        self._published = (generation, image, metadata)
                        self._state = "ready"
                        self._outcomes[generation] = "published"
                        publish = self._published
                    else:
                        self.counters["failures"] += 1
                        self._state = "unavailable"
                        self._error = error
                        self._outcomes[generation] = "failed"
                if publish is not None:
                    self._notify_publish(publish)
                    self._notify_state("ready")
                elif error:
                    self._notify_state("unavailable")
        finally:
            self._release()

    def _await_stable(self, camera, cancel):
        """Whether the view still matches the composed camera.

        The layer does not wait here: the settle policy belongs to the
        caller that asks for a composition, so the worker never polls and
        never competes with the weather pipeline.
        """
        return not self._view_moved(camera)

    def _await_idle(self, cancel):
        """Whether the weather pipeline is idle.

        The check is a single reading of the weather pipeline's state: the
        layer never polls, so it never competes with the weather pipeline
        for the interpreter's lock.
        """
        return self._worker_idle()

    def _release(self):
        for source in self._sampler_sources:
            try:
                source.close()
            except Exception:  # pragma: no cover - a closed handle is fine
                pass
        self._sampler_sources = ()
        self._sampler = None

    # --------------------------------------------------------- composition
    def _compose(self, camera, cancel):
        """Compose the chart layer: the application's base chart and isobars.

        The base chart is the application's own composition; the isobars are
        the application's own contour drawing on the same surface.  The
        composition runs on the chart worker, never on the GUI thread and
        never on the field worker.
        """
        from gweatherrouting.core.mapviewport import MapViewport

        canvas = ChartCamera(camera)
        viewport = MapViewport(canvas, MARGIN, lambda: None)
        viewport.render_cancelled = cancel.is_set
        sources = self._base_sources()
        job = RenderJob(
            key=(self._palette, 1, sources),
            viewport=viewport,
            charts=tuple((source,) for source in sources),
            factor=1,
            palette=self._palette,
            cancel=cancel,
        )
        if self._view_moved(camera):
            # The view moved on: this generation is stale before it starts.
            with self._condition:
                self.counters["stale"] += 1
            self._outcome = "stale"
            return None, None
        columns = self._isobar_columns(job, camera, cancel)
        if columns:
            with self._condition:
                self.counters["isobars"] += 1
        if self._view_moved(camera):
            with self._condition:
                self.counters["stale"] += 1
            self._outcome = "stale"
            return None, None
        surface = self._composer().render(job, columns)
        if surface is None:
            return None, None
        image = _surface_image(surface)
        metadata = {"viewport": viewport, "time": camera.stamp()}
        return image, metadata

    def _composer(self):
        if self._composer_instance is None:
            self._composer_instance = _ChartComposer(
                self._palette, self._base_sources()
            )
        return self._composer_instance

    def _base_sources(self):
        if not self._sources:
            self._sources = base_sources(self._data_dir)
        return self._sources

    def _view_moved(self, camera):
        """Whether the view has moved on from the composed camera."""
        if self._current_camera is None:
            return False
        current = self._current_camera()
        return current is not None and current != camera

    def _isobar_columns(self, job, camera, cancel):
        """The mean-sea-level pressure columns for the isobars.

        The pressure field is the application's own sampler over the same
        selected forecasts, resampled on the application's own regular grid
        so that the contours have one shared latitude vector.  This is the
        application's own data path; the chart layer does not change it.
        """
        import numpy as np

        from gweatherrouting.rendering.windrenderer import WindOverlayRenderer

        sampler = self._ensure_sampler()
        if sampler is None:
            return None
        bounds = WindOverlayRenderer._viewport_bounds(job)
        axes = WindOverlayRenderer._resample_axes(bounds)
        if axes is None:
            return None
        lats, lons = axes
        grid_lons, grid_lats = np.meshgrid(lons, lats)
        values = np.asarray(
            sampler.sample(
                "pressure",
                camera.time,
                grid_lats.ravel(),
                grid_lons.ravel(),
                cancel.is_set,
            )
        ).reshape(lats.size, lons.size, -1)
        if cancel.is_set():
            return None
        return WindOverlayRenderer._sampled_columns(values, lats, lons, "pressure")

    def _ensure_sampler(self):
        """The chart layer's own sampler over the selected forecasts.

        The chart layer owns its own handles: it never borrows the field
        worker's, so the two pipelines are independent and either can be
        replaced or cancelled without the other.
        """
        if self._sampler is not None:
            return self._sampler
        from gweatherrouting.core.grib import Grib
        from gweatherrouting.core.gribsampling import WeatherSampler
        from gweatherrouting.rendering.weathercache import (
            DecodedBandCache,
            DisplayWeatherSource,
        )

        bands = DecodedBandCache(32 * 1024 * 1024)
        sources = []
        for path in self._paths:
            metadata = Grib.parse_metadata(path)
            sources.append(
                DisplayWeatherSource(
                    metadata.path,
                    metadata.name,
                    metadata.centre,
                    metadata.bounds,
                    metadata.start_time,
                    metadata.last_forecast,
                    metadata.model_name,
                    band_cache=bands,
                )
            )
        if not sources:
            return None
        self._sampler_sources = tuple(sources)
        self._sampler = WeatherSampler(sources)
        return self._sampler


def _surface_image(surface):
    """The composed cairo surface as a Qt image.

    The surface is ARGB32 and premultiplied, the same layout as the
    application's own framebuffer readback, so the bytes are copied without
    a conversion.
    """
    from PySide6.QtGui import QImage

    surface.flush()
    width = surface.get_width()
    height = surface.get_height()
    stride = surface.get_stride()
    data = bytes(surface.get_data())
    return QImage(
        data,
        width,
        height,
        stride,
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()


def chart_image_provider():
    """The provider that serves the composed chart image.

    It is created here so the module stays importable without Qt: the
    viewport's render context is the only user.
    """
    from PySide6.QtGui import QImage
    from PySide6.QtQuick import QQuickImageProvider

    class _Provider(QQuickImageProvider):
        """The chart image, published under its generation."""

        def __init__(self):
            super().__init__(QQuickImageProvider.ImageType.Image)
            self._lock = Lock()
            self._images = {}
            self.publishes = 0
            self.requests = 0

        def publish(self, generation, image):
            with self._lock:
                self.publishes += 1
                self._images = {str(generation): image}
            return f"image://chart/{generation}"

        def requestImage(self, identifier, size, requested_size):  # noqa: N802
            with self._lock:
                self.requests += 1
                image = QImage(self._images.get(identifier, QImage()))
            if size is not None:
                size.setWidth(image.width())
                size.setHeight(image.height())
            return image

        def clear(self):
            with self._lock:
                self._images = {}

    return _Provider()
