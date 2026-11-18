"""The viewport's chart layer: an independent generation of chart imagery.

The chart layer is the *application's* chart composition, not a second one.
The base chart is composed by ``BaseChartRenderer`` -- the application's own
worker-side composition with private GDAL handles, the application's chart
drawers and the application's palette -- the installed and enabled ENC cells
are composed by the application's own ``ENCOverlayRenderer`` on the same
cairo surface, and the mean-sea-level pressure contours are the
application's own ``WindPainter.draw_isobars`` on that same surface.  No
chart portrayal is re-implemented here.

The order is the application's own: the base chart first, the ENC second and
the pressure contours last, so the ENC sits under the contours exactly as it
sits under them in the application.

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
* the chart layer's static content is retained while it still covers the
  camera, using the application's own ``fits`` rule, so a small pan does not
  recompose the chart; a larger pan reuses the covered intersection and
  composes only the newly exposed rectangles; a forecast-time change composes
  no chart pixel at all and redraws only the contours;
* the composition is composed at the GUI's own device pixel ratio, and a
  zoom or a ratio change is composed again rather than resampled.

The ENC cells are the application's own cells: the installed and enabled
entries of the user's manifest, read and opened by this layer's own worker
with its own handles.  The ENC source reports its own states -- ``ready``,
``coverage_miss``, ``unavailable`` -- so a missing cell is never presented
as an empty chart that reads as "there is no data here".
"""

from collections import OrderedDict
from dataclasses import dataclass
import math
from pathlib import Path
from threading import Condition, Event, Lock, Thread
import time
from typing import TYPE_CHECKING, cast

from sailworks.core.mapviewport import MercatorProjection
from sailworks.rendering.overlayrenderer import MARGIN, RenderJob, fits

if TYPE_CHECKING:
    from sailworks.rendering.weathercache import DisplayWeatherSource

#: The application's default chart palette.
CHART_PALETTE = "cm93"

#: The settle window the chart layer composes for.  The recorded interaction
#: sweep delivers its inputs at 30 per second (33 ms apart), so a window of
#: Idle interval before requesting a chart for the latest view.
CHART_IDLE_MS = 100
CHART_INTERACTIVE_MS = 150

#: Bounded weather priority before a settled chart is requested.  Weather is
#: given a head start, never an unlimited veto: after this window the chart
#: proceeds even if weather is still preparing.  The readiness window covers a
#: normal cold start; the busy window bounds contention during steady state.
CHART_WEATHER_READY_WAIT_MS = 2000
CHART_WEATHER_BUSY_WAIT_MS = 150

#: The bounded weather priority when a chart frame is already on screen.  The
#: retained frame keeps covering the view while the weather presents, so the
#: chart does not need the cold-start window and must not be starved by it: a
#: camera move every settle interval would otherwise re-arm that window for as
#: long as the user keeps moving.
CHART_RETAINED_HEAD_START_MS = 150

#: How long a complete ENC scene suppresses the raster before the suppression
#: is re-examined.  The scene's coverage is only true for the camera it was
#: assembled for; a bounded re-check keeps a scene that has stopped covering
#: the view from leaving the last camera's raster unpublished until the next
#: five-second source poll.
CHART_SCENE_SKIP_MS = 500

#: The longest a complete ENC scene may withhold the raster for one camera
#: demand.  The scene is re-examined every ``CHART_SCENE_SKIP_MS``, but a
#: scene that keeps *claiming* the view must not keep the raster unpublished
#: forever: a partially presented ENC frame reports coverage of its own
#: bounding geometry while drawing a fraction of the cells.  Past this window
#: the raster composes regardless; a duplicated composition is cheap next to a
#: blank chart.
CHART_SCENE_SUPPRESS_MAX_MS = 1000

#: The weather head start before the *first* raster of a session.  A retained
#: raster keeps covering the view while the weather presents, so it takes the
#: short retained bound; nothing covers the view before the first one, so the
#: long cold-start window would only postpone the only chart content there is.
#: Weather still composes in parallel; this is a small, bounded head start,
#: not a veto.
CHART_STARTUP_HEAD_START_MS = 150

#: The longest a complete ENC scene's expensive adoption is held back while
#: the session's first raster is still being painted.  A partial scene is
#: adopted at once (it is the ENC's own first drawable picture); the complete
#: frame's vertex buffers are the GUI-thread cost that can otherwise delay the
#: raster's first paint by many seconds.  The hold is bounded, released by the
#: first chart publication, and never applies once a raster has been shown.
CHART_STARTUP_SCENE_HOLD_MS = 1500

#: The coastline qualities' initial draw times, as the application's own
#: ``GSHHSVectorChart`` records them.
_INITIAL_TIMES = (("c", 0), ("f", 0), ("h", 0), ("i", 0), ("l", 0))

#: The diagnostic history the layer keeps: the most recent generations only.
#: The history is evidence, never a store: it is bounded so a long session
#: cannot grow it without limit.
HISTORY_LIMIT = 64

#: The largest sub-pixel error a snapped reuse may introduce, in device
#: pixels.  Rounding to the nearest whole device pixel moves retained pixels by
#: at most half a pixel in each axis; that is a translation, not a resample.
STATIC_SNAP_PX = 0.5

#: Padding around an exposed region, in logical pixels.  A region is composed
#: with this padding and cropped to the exposed rectangle, so a label, symbol
#: or stroke that reaches across the boundary is drawn whole: the seam is
#: always interior to a region that rendered the content, never a clipped
#: glyph.  64 px exceeds the largest chart label's box.
STATIC_REGION_PAD = 64

#: Retained isobar-free surfaces.  Two full-quality frames keep a warm
#: travel/reversal pair (A -> B -> A reuses A's frame), and one preview frame
#: keeps a gesture from evicting the full composition.
STATIC_FRAME_LIMIT = 3


@dataclass
class _RegionCamera:
    """The camera of one rectangle of a composition, in world pixels."""

    center_lat: float
    center_lon: float
    zoom: float
    width: int
    height: int

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height


@dataclass
class _StaticFrame:
    """A retained isobar-free chart surface and what it describes.

    The surface holds the base chart and the ENC cells only.  The contours
    belong to the forecast instant and are drawn on a copy, so a time change
    never draws a chart pixel again.  Only the chart worker reads or writes a
    frame.
    """

    surface: object
    viewport: object
    factor: float
    palette: str
    sources: tuple
    enc_metadata: dict
    bytes: int
    # The device-pixel offset these pixels actually carry relative to the
    # position ``viewport`` implies.  A snapped blit leaves up to
    # ``STATIC_SNAP_PX`` there; it is carried so the next pan cancels it
    # instead of rounding from already-snapped metadata a second time.
    snap: tuple = (0.0, 0.0)


def _viewport_bounds(viewport):
    """The viewport's world-pixel rectangle: left, top, right, bottom."""
    center_x = viewport._lon_to_x(viewport._center_lon)
    center_y = viewport._lat_to_y(viewport._center_lat)
    return (
        center_x - viewport.width / 2.0,
        center_y - viewport.height / 2.0,
        center_x + viewport.width / 2.0,
        center_y + viewport.height / 2.0,
    )


def _same_static_geometry(frame_viewport, viewport):
    """Whether a retained frame's pixels are aligned with this view exactly.

    Two viewports are interchangeable only when they have the same extent and
    the same world-pixel rectangle: the frame is then at the target's own
    origin and zoom, so its pixels may be handed back verbatim.  A frame that
    merely covers the view (a pan within the margin, or a differently sized
    view) sits at another offset or scale and must be re-anchored first.
    """
    if int(frame_viewport.width) != int(viewport.width):
        return False
    if int(frame_viewport.height) != int(viewport.height):
        return False
    f_left, f_top, f_right, f_bottom = _viewport_bounds(frame_viewport)
    left, top, right, bottom = _viewport_bounds(viewport)
    tolerance = 1e-6
    return (
        abs(f_left - left) <= tolerance
        and abs(f_top - top) <= tolerance
        and abs(f_right - right) <= tolerance
        and abs(f_bottom - bottom) <= tolerance
    )


def _static_frame_serves(frame, key, viewport):
    """Whether a retained frame describes this request's content and zoom.

    A frame serves a request only when it was composed for the same palette,
    device factor and base sources, at the same zoom.  The camera may differ:
    a pan within the frame's extent is served from its pixels, and only the
    newly exposed strip is composed.
    """
    if (frame.palette, frame.factor, frame.sources) != key:
        return False
    return frame.viewport._zoom == viewport._zoom


def _intersection_rect(viewport, frame_viewport, shift=None, factor=1.0):
    """The covered part of a composition, in integer surface coordinates.

    Both viewports are at one zoom, so the intersection is an axis-aligned
    rectangle.  The result is the frame's coverage clipped to the viewport,
    rounded inward to whole surface pixels so a region never claims a pixel
    the frame does not contain.  When ``shift`` (the snapped device offset the
    frame is painted at) is given, the coverage is measured from the pixels
    that actually land, not from the fractional rect they were rounded from:
    the composed strips then abut the painted edge exactly, with no unpainted
    column at the seam.
    """
    left, top, right, bottom = _viewport_bounds(viewport)
    f_left, f_top, f_right, f_bottom = _viewport_bounds(frame_viewport)
    if shift is not None:
        width = f_right - f_left
        height = f_bottom - f_top
        f_left = left + shift[0] / factor
        f_top = top + shift[1] / factor
        f_right = f_left + width
        f_bottom = f_top + height
    x0 = max(0, min(int(viewport.width), int(math.ceil(max(left, f_left) - left))))
    y0 = max(0, min(int(viewport.height), int(math.ceil(max(top, f_top) - top))))
    x1 = max(0, min(int(viewport.width), int(math.floor(min(right, f_right) - left))))
    y1 = max(0, min(int(viewport.height), int(math.floor(min(bottom, f_bottom) - top))))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _exposed_regions(width, height, overlap):
    """The rectangles of a composition the retained frame does not cover.

    The rectangles are disjoint, integer-aligned and, together with
    ``overlap``, partition the whole target surface.
    """
    if overlap is None:
        return [(0, 0, width, height)]
    x0, y0, x1, y1 = overlap
    regions = []
    if x0 > 0:
        regions.append((0, 0, x0, height))
    if x1 < width:
        regions.append((x1, 0, width, height))
    if y0 > 0:
        regions.append((x0, 0, x1, y0))
    if y1 < height:
        regions.append((x0, y1, x1, height))
    return regions


def _empty_surface(viewport, factor):
    """A transparent surface of the composition's own device size."""
    import cairo

    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32,
        max(1, int(round(viewport.width * factor))),
        max(1, int(round(viewport.height * factor))),
    )
    surface.set_device_scale(factor, factor)
    return surface


def _static_shift(frame, viewport, factor):
    """The frame's translation onto this view in whole device pixels.

    A retained frame is already-rendered pixels, and a blit at a fractional
    device offset would *resample* them.  The offset is therefore snapped to
    the nearest whole device pixel, which is a pure translation of the
    retained pixels: the largest error it can introduce is
    ``STATIC_SNAP_PX`` (half a device pixel, the worst case of rounding) in
    each axis, and it is the same value this module reports as
    ``snapped_px``.  An integer offset is unchanged, so those blits stay
    byte-for-byte identical to a full composition.

    The frame's own ``snap`` is subtracted first.  A previous reuse may have
    left the pixels a fraction of a device pixel off the position its
    viewport describes; without this term each pan would snap from that
    already-snapped position again and the error would grow by up to half a
    pixel per published frame.  Cancelling it keeps the residual inside half
    a device pixel for any number of pans.

    Returns ``None`` only for an offset that rounding cannot express within
    the bound, which cannot happen for finite coordinates; the check is the
    explicit statement of the bound rather than a reachable branch.
    """
    left, top, _right, _bottom = _viewport_bounds(viewport)
    f_left, f_top, _f_right, _f_bottom = _viewport_bounds(frame.viewport)
    dx = (f_left - left) * factor - frame.snap[0]
    dy = (f_top - top) * factor - frame.snap[1]
    rx = int(round(dx))
    ry = int(round(dy))
    if abs(dx - rx) > STATIC_SNAP_PX + 1e-9 or abs(dy - ry) > STATIC_SNAP_PX + 1e-9:
        return None
    return rx, ry


def _static_shift_error(frame, viewport, factor):
    """The snap's own error, in device pixels, for the report and the trace."""
    left, top, _right, _bottom = _viewport_bounds(viewport)
    f_left, f_top, _f_right, _f_bottom = _viewport_bounds(frame.viewport)
    dx = (f_left - left) * factor - frame.snap[0]
    dy = (f_top - top) * factor - frame.snap[1]
    return max(abs(dx - round(dx)), abs(dy - round(dy)))


def _static_residual(frame, shift, viewport, factor):
    """The offset the retained pixels carry after landing at ``shift``.

    A feature the frame holds at pixel ``p`` sits at ``p + snap`` in its own
    view; painting it at ``shift`` puts it at ``p + snap + shift``, while a
    full composition of this view would put it at ``p + delta``.  The
    difference is what the next reuse must cancel, and it stays within
    ``STATIC_SNAP_PX`` because ``shift`` is the rounded ``delta - snap``.
    """
    left, top, _right, _bottom = _viewport_bounds(viewport)
    f_left, f_top, _f_right, _f_bottom = _viewport_bounds(frame.viewport)
    return (
        frame.snap[0] + shift[0] - (f_left - left) * factor,
        frame.snap[1] + shift[1] - (f_top - top) * factor,
    )


def _paint_static(surface, frame, viewport, factor):
    """Paint a retained frame into a target surface at its snapped offset.

    The caller has already established, through :func:`_static_shift`, that
    the frame lands within ``STATIC_SNAP_PX`` of a whole device pixel, so the
    blit translates exactly ``shift / factor`` user-space units: the source is
    copied pixel for pixel and is never resampled.
    """
    import cairo

    shift = _static_shift(frame, viewport, factor)
    if shift is None:  # pragma: no cover - unreachable within the bound
        raise ValueError("a retained frame may not be resampled")
    cr = cairo.Context(surface)
    cr.set_source_surface(frame.surface, shift[0] / factor, shift[1] / factor)
    cr.paint()
    surface.flush()


def _copy_surface(surface, factor):
    """A writable copy of a retained surface, for this instant's contours.

    The retained frame stays isobar-free, so a later instant never inherits
    the previous contours.  The copy is one fast paint of the surface.
    """
    import cairo

    copy = cairo.ImageSurface(
        cairo.FORMAT_ARGB32, surface.get_width(), surface.get_height()
    )
    copy.set_device_scale(factor, factor)
    cr = cairo.Context(copy)
    cr.set_source_surface(surface, 0, 0)
    cr.paint()
    copy.flush()
    return copy


def _surface_bytes(surface):
    """The retained surface's device pixel payload, in bytes."""
    return int(surface.get_width()) * int(surface.get_height()) * 4


def disabled_enc_metadata():
    """The ENC report of a composition that composes no ENC at all."""
    from sailworks.qt.viewportenc import disabled_metadata

    return disabled_metadata()


def _trace_camera(camera):
    """The camera identity for a trace record, guarded for stand-ins."""
    if camera is None:
        return {}
    fields = {}
    for name in ("center_lat", "center_lon", "zoom"):
        value = getattr(camera, name, None)
        if value is not None:
            fields["lat" if name == "center_lat" else name] = round(float(value), 5)
    for name in ("width", "height"):
        value = getattr(camera, name, None)
        if value is not None:
            fields[name] = int(value)
    return fields


def _trace_rejection(generation, cause, camera, cancelled, closed, outcome, composed):
    """Record why a composition may not publish (opt-in, zero cost when off).

    ``cause`` is the asker the generation was requested for, so a rejection
    can be attributed to a camera move, a timer, a deferral retry or a region
    composition instead of being an anonymous counter.
    """
    from sailworks.qt import enc_trace

    if not enc_trace.enabled():
        return None
    if closed:
        reason = "closed"
    elif cancelled:
        reason = "cancelled"
    elif outcome == "stale":
        reason = "stale"
    else:
        reason = "superseded"
    return enc_trace.event(
        "chart_reject",
        generation=generation,
        cause=cause,
        reason=reason,
        composed=bool(composed),
        **_trace_camera(camera),
    )


def _trace_composition(generation, cause, metadata):
    """Record what a published composition reused (opt-in, no cost when off)."""
    from sailworks.qt import enc_trace

    if not enc_trace.enabled():
        return None
    static = (metadata or {}).get("static") or {}
    return enc_trace.event(
        "chart_compose",
        generation=generation,
        cause=cause,
        static_mode=static.get("mode"),
        static_reason=static.get("reason"),
        regions=static.get("regions"),
        region_rects=static.get("region_rects"),
        shift=static.get("shift"),
        snapped_px=static.get("snapped_px"),
        delta_px=static.get("delta_px"),
        factor=static.get("factor"),
        composed_pixels=static.get("composed_pixels"),
        reused_pixels=static.get("reused_pixels"),
        timings=(metadata or {}).get("timings"),
        **_trace_camera((metadata or {}).get("camera")),
    )


class ChartSettings:
    """The read-only settings the application's chart code reads.

    The viewport does not own the application's settings manager and never
    writes user data, so the chart code is given the values it reads.  The
    palette is the application's own default.
    """

    chartPalette = CHART_PALETTE  # noqa: N815 - Qt app config property name
    gribIsobarsVisible = True  # noqa: N815 - Qt app config property name
    gribArrowOpacity = 1.0  # noqa: N815 - Qt app config property name

    def register_on_change(self, key, callback):
        """The viewport's chart settings do not change while it runs."""
        return None


class ChartCamera(MercatorProjection):
    """The viewport's camera as the canvas the application's chart code wants.

    The application's chart composition draws into a canvas; the viewport has
    a camera.  This adapter presents the camera with the canvas API the
    application's own chart code already uses, so the composition stays the
    application's composition.

    The device pixel ratio is the GUI's own: the composition is rendered at
    the ratio the display has, so the published image is one device pixel per
    display pixel and never has to be resampled.
    """

    def __init__(self, camera, dpr=1):
        self._center_lat = camera.center_lat
        self._center_lon = camera.center_lon
        self._zoom = camera.zoom
        self._width = camera.width
        self._height = camera.height
        self._dpr = dpr

    def get_allocated_width(self):
        return self._width

    def get_allocated_height(self):
        return self._height

    def get_scale_factor(self):
        return self._dpr

    def queue_draw(self):
        """The chart layer publishes its own image; no canvas repaint is due."""


def _countries():
    """The coastline's country labels, read from the application's resource.

    The labels are the application's own data (``countries_en.txt``), parsed
    the way ``GSHHSVectorChart`` parses them.
    """
    from sailworks.common import resource_path

    with open(resource_path("sailworks", "data/countries_en.txt")) as handle:
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
    Includes a bundled low-detail fallback when GSHHS is absent.
    """
    from sailworks.qt.basecharts import BaseSource
    from sailworks.common import resource_path

    path = Path(data_dir) / "gshhs"
    if path.is_dir():
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
    # Fallback to bundled low-detail map when GSHHS is absent
    fallback_path = Path(resource_path("sailworks", "data/countries.geojson"))
    if not fallback_path.is_file():
        return ()
    return (
        BaseSource(
            "simple",
            str(fallback_path),
            False,
            (),  # No countries for simple vector chart
            _INITIAL_TIMES,
            id(fallback_path),
        ),
    )


def chart_placement(viewport, camera):
    """Where the composed chart image sits for the current camera.

    The composition covers the camera's window with the application's own
    margin. While a zoomed view is settling, scale the retained image around
    its geographic centre as an explicitly marked preview; the settled view
    is recomposed at native scale and replaces it.
    """
    view = camera.view()
    scale = 2.0 ** (camera.zoom - viewport._zoom)
    center_x = (
        view._lon_to_x(viewport._center_lon)
        - view._lon_to_x(camera.center_lon)
        + camera.width / 2.0
    )
    center_y = (
        view._lat_to_y(viewport._center_lat)
        - view._lat_to_y(camera.center_lat)
        + camera.height / 2.0
    )
    width = viewport.width * scale
    height = viewport.height * scale
    return (center_x - width / 2.0, center_y - height / 2.0, width, height)


class _ChartComposer:
    """The application's chart composition, with the application's isobars.

    ``BaseChartRenderer._render`` is the application's own worker-side
    composition: the private GDAL handles, the application's chart drawers
    and the application's palette.  The ENC cells are composed by the
    application's own ``ENCOverlayRenderer`` onto the same surface, and the
    isobars are the application's own ``WindPainter.draw_isobars`` on that
    same surface, so the chart layer's pixels are the application's chart
    pixels.

    The order is the application's own: base chart, then ENC, then the
    pressure contours.
    """

    def __init__(self, palette, sources):
        from sailworks.core.scheduling import schedule_idle
        from sailworks.qt.basecharts import BaseChartRenderer

        self._renderer = BaseChartRenderer(schedule_idle)
        self._palette = palette
        self._sources = tuple(sources)
        self._painter = None

    def painter(self):
        if self._painter is None:
            from sailworks.rendering.windpainter import WindPainter

            self._painter = WindPainter(ChartSettings())
        return self._painter

    def render(self, job, isobars=None, enc=None):
        import cairo

        if self._sources:
            surface = self._renderer._render(job)
        else:
            # No installed coastline: the layer composes the isobars alone,
            # on an empty surface of the same size and scale.
            viewport = job.viewport
            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32,
                max(1, int(round(viewport.width * job.factor))),
                max(1, int(round(viewport.height * job.factor))),
            )
            surface.set_device_scale(job.factor, job.factor)
            surface.flush()
        if surface is None:
            return None, None
        enc_metadata = None
        if enc is not None:
            surface, enc_metadata = enc.compose(job, surface)
            if surface is None:
                return None, None
        if isobars:
            cr = cairo.Context(surface)
            self.painter().draw_isobars(job.viewport, cr, isobars)
        surface.flush()
        return surface, enc_metadata


class ViewportChartLayer:
    """The viewport's chart layer: its own state, generation and worker.

    The layer is requested from the GUI thread and publishes on the GUI
    thread; the composition itself runs on the layer's own worker, so it
    never blocks the field worker or the GUI thread.

    The ENC cells are an optional part of the composition.  They are off by
    default: the default path composes the base chart and the isobars, as
    it always did.  When they are on, they are the installed and enabled
    cells of the user's own manifest, composed by the application's own ENC
    renderer on the layer's own worker, and the layer reports their state
    apart from the chart's own.
    """

    def __init__(
        self,
        paths,
        data_dir,
        palette=CHART_PALETTE,
        settle_ms=CHART_IDLE_MS,
        worker_idle=None,
        current_camera=None,
        on_publish=None,
        on_state=None,
        enc_enabled=False,
        satellite_background=False,
        dpr=1,
        enc_only=False,
        session=None,
    ):
        self._paths = tuple(str(path) for path in paths)
        # The chart shares the session's immutable metadata and decoded bands
        # but keeps its own GDAL handles and composition pipeline.
        self._session = session
        self._owns_session = session is None
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
        self._reset_sources = False
        self._sampler = None
        self._sampler_sources: "tuple[DisplayWeatherSource, ...]" = ()
        self._composer_instance = None
        # Retained isobar-free static content, owned by the worker thread.
        # ``_static_bytes`` mirrors its payload for the GUI-visible report.
        self._static_frames = OrderedDict()
        self._static_serial = 0
        self._static_bytes = 0
        self._static_count = 0
        # The request instant of each in-flight generation, for the
        # queue/preparation split in the published timings.  Bounded.
        self._requested_at = {}
        self._outcome = None
        self._spans = []
        self._published_generations = set()
        self._rejected_generations = set()
        self._outcomes = {}
        # The ENC source is off by default and composed by this layer's own
        # worker, with the worker's own handles.
        self._enc_enabled = bool(enc_enabled)
        # The chart composition is transparent over imagery: the raster
        # background is the picture, and an opaque base chart painted above
        # it would hide the satellite layer entirely.  With the basemap
        # selected the composition carries the vector chart as before.
        self._satellite_background = bool(satellite_background)
        self._enc_only = bool(enc_only)
        self._enc_source = None
        self._enc_state = "disabled" if not self._enc_enabled else "idle"
        self._enc_metadata = {}
        self._enc_stale = False
        # The user's manifest identity: a cheap signal that the installed or
        # the enabled cells may have changed since the last composition.
        self._manifest_stamp = None
        # The composition's device pixel ratio: the GUI's own.
        self._dpr = max(1, int(round(float(dpr))))
        self._payload_bytes = 0
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
            enc_compositions=0,
            enc_cells=0,
            enc_failures=0,
            static_full_compositions=0,
            static_region_compositions=0,
            static_reuses=0,
            static_fractional_rejections=0,
            static_snapped_reuses=0,
            static_regions=0,
            static_pixels_composed=0,
            static_pixels_reused=0,
            isobar_only=0,
            cancelled_active=0,
        )
        # The cause each generation was requested for, so a rejection can name
        # who asked.  Bounded so a long session cannot grow it.
        self._request_causes = {}
        # Basemap tracking for independent usability
        self._has_gshhs = False
        self._has_fallback = False
        self._basemap_available = False
        self._basemap_error = ""
        self._check_basemap_sources(data_dir)

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

    @property
    def enc_state(self):
        """The ENC source's own state, apart from the chart's own."""
        with self._condition:
            return self._enc_state

    @property
    def enc_metadata(self):
        """The ENC metadata of the published composition."""
        with self._condition:
            return dict(self._enc_metadata)

    def get_basemap_status(self):
        """Get the basemap status text for the scene.

        Returns a descriptive status message about the basemap availability
        and state, for explicit visibility when no usable basemap source loads.
        """
        with self._condition:
            if not self._basemap_available:
                return "World basemap unavailable"
            if not self._has_gshhs and self._has_fallback:
                return "World basemap: bundled low-detail map"
            return ""  # Normal case: GSHHS is available

    def _check_basemap_sources(self, data_dir):
        """Check which basemap sources are available at the given data directory."""
        from sailworks.common import resource_path

        gshhs_available = (Path(data_dir) / "gshhs").is_dir()
        fallback_available = Path(
            resource_path("sailworks", "data/countries.geojson")
        ).is_file()
        with self._condition:
            self._has_gshhs = gshhs_available
            self._has_fallback = bool(fallback_available)
            self._basemap_available = gshhs_available or bool(fallback_available)
            if not self._basemap_available:
                self._basemap_error = (
                    "World coastlines not installed and fallback unavailable"
                )
            elif not gshhs_available and fallback_available:
                self._basemap_error = (
                    "Using bundled low-detail map (GSHHS not installed)"
                )

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
                "enc": {
                    "enabled": self._enc_enabled,
                    "state": self._enc_state,
                    "stale": self._enc_stale,
                    "metadata": dict(self._enc_metadata),
                },
                "payload_bytes": self._payload_bytes,
                "factor": self._dpr,
                "static": {
                    "frames": self._static_count,
                    "bytes": self._static_bytes,
                    "limit": STATIC_FRAME_LIMIT,
                    "region_pad": STATIC_REGION_PAD,
                },
                "forecast_session": (
                    None if self._session is None else self._session.statistics()
                ),
                "basemap": {
                    "has_gshhs": self._has_gshhs,
                    "has_fallback": self._has_fallback,
                    "available": self._basemap_available,
                    "error": self._basemap_error,
                },
            }

    # ----------------------------------------------------------- requests
    def request(self, camera, replace=False, preview=False, cause=None):
        """Ask for the chart layer at this camera (GUI thread).

        A retained composition that still covers the camera is reused: no new
        generation is created and nothing is recomposed.  Otherwise a new
        generation supersedes the previous one, which can never publish.
        Active work is allowed to finish; only the pending slot is replaced.
        The exception is work whose pixels cannot serve the new demand at all
        -- another zoom or the other preview mode -- which is cancelled so the
        latest demand starts now instead of queueing behind it.
        ``replace`` asks for a new generation even when the same view is
        already composing or already published.  ``cause`` names the asker for
        the trace only.
        """
        with self._condition:
            if self._closed:
                return None
            if preview and (self._active is not None or self._pending is not None):
                return None
            sources_changed = self._sources_changed()
            basemap_changed = self._basemap_sources_changed()
            if sources_changed or basemap_changed:
                self._invalidate_sources_locked()
            if sources_changed:
                # The user's own manifest changed: the retained composition
                # may no longer describe the user's cells, so it is withdrawn
                # and the cells are composed again.
                self._published = None
                self._payload_bytes = 0
                self._enc_stale = False
            if basemap_changed:
                # The basemap sources changed: the retained composition
                # may no longer be valid, so it is withdrawn.
                self._published = None
                self._payload_bytes = 0
            reused = self._reuse(camera, replace, preview)
            if reused is not None:
                return reused
            self._cancel_unusable_active(camera, preview)
            self._generation += 1
            generation = self._generation
            if self._pending is not None:
                self._pending[2].set()
                self.counters["cancelled"] += 1
            self._pending = (generation, camera, Event(), preview)
            self._requested_at[generation] = time.perf_counter()
            while len(self._requested_at) > 64:
                self._requested_at.pop(next(iter(self._requested_at)))
            if cause is not None:
                self._request_causes[generation] = cause
            while len(self._request_causes) > 64:
                self._request_causes.pop(next(iter(self._request_causes)))
            self._state = "loading"
            self._error = ""
            self.counters["requests"] += 1
            if self._enc_enabled:
                # The published ENC, if any, is superseded by this
                # generation: it is not current any more.
                self._enc_state = "loading"
                self._enc_stale = self._published is not None
            if self._thread is None:
                self._thread = Thread(
                    target=self._run, name="grib-viewport-chart", daemon=True
                )
                self._thread.start()
        self._notify_state("loading")
        with self._condition:
            self._condition.notify_all()
        return generation

    def _cancel_unusable_active(self, camera, preview):
        """Cancel a running composition whose pixels cannot serve the demand.

        A composition is superseded the moment a newer demand exists, but its
        pixels are still useful to a *pan*: they become the retained frame the
        next view reuses.  A composition at another zoom or in the other
        preview mode is different: a stale-scale image is never reused, so
        letting it run only makes the latest demand wait behind it.  It is
        cancelled instead, and the worker starts the pending demand as soon as
        the composer notices -- that wait is the seconds-long visible stall.
        """
        active = self._active
        if active is None or active[2].is_set():
            return
        if active[1].zoom == camera.zoom and active[3] == preview:
            return
        active[2].set()
        self.counters["cancelled_active"] += 1

    def _reuse(self, camera, replace, preview=False):
        """The generation that already serves this camera, if there is one.

        A composition that is still running serves the same view, and a
        retained composition that still covers the camera is reused.  A
        replaced request asks for a new generation either way.
        """
        if replace:
            return None
        if self._pending is not None:
            if (
                self._pending[0] == self._generation
                and self._pending[1] == camera
                and self._pending[3] == preview
            ):
                return self._pending[0]
            return None
        if (
            self._active is not None
            and self._active[0] == self._generation
            and self._active[1] == camera
            and self._active[3] == preview
        ):
            # The same view is already composing: asking again would
            # supersede a composition that is still running.
            return self._active[0]
        if self._published is not None and self._reusable(camera, preview):
            self.counters["reuses"] += 1
            return self._published[0]
        return None

    def _reusable(self, camera, preview=False):
        """Whether the retained composition still serves this camera."""
        _generation, _image, metadata = cast(tuple, self._published)
        if metadata.get("preview", False) != preview:
            return False
        if metadata["time"] != camera.stamp():
            # The isobars belong to the forecast instant.
            return False
        expected_factor = self._dpr * 0.5 if preview else self._dpr
        if metadata.get("factor", 1) != expected_factor:
            # The composition was made at another device pixel ratio: it is
            # not current, and it may not be resampled into this one.
            return False
        canvas = ChartCamera(camera, self._dpr)
        recorded_extent = metadata.get("image_extent")
        if recorded_extent is not None:
            width = canvas.get_allocated_width() + 2 * MARGIN
            height = canvas.get_allocated_height() + 2 * MARGIN
            expected_extent = (
                max(1, int(round(width * expected_factor))),
                max(1, int(round(height * expected_factor))),
            )
            if tuple(recorded_extent) != expected_extent:
                # The retained pixels are not the extent this view needs: the
                # composition describes another surface.  Serving it would
                # present mismatched pixels under this demand's own metadata.
                return False
        return fits(metadata["viewport"], canvas)

    def set_device_pixel_ratio(self, dpr):
        """Adopt the GUI's device pixel ratio (GUI thread).

        A change withdraws the published composition: it was composed at
        another ratio and may not be resampled, so it is composed again.
        """
        factor = max(1, int(round(float(dpr))))
        with self._condition:
            if factor == self._dpr:
                return False
            self._dpr = factor
            self._published = None
            self._payload_bytes = 0
            self._enc_stale = False
        return True

    def _sources_changed(self):
        """Whether the user's own manifest changed since the last request.

        The manifest is the user's file.  Its identity is a cheap signal that
        an installed, a replaced or a switched off cell may have changed; the
        worker reads the manifest itself, and the cells are composed again.
        The signal says nothing about the cells: only the manifest decides.
        """
        if not self._enc_enabled:
            return False
        from sailworks.qt.viewportenc import manifest_stamp

        stamp = manifest_stamp(self._data_dir)
        if stamp == self._manifest_stamp:
            return False
        self._manifest_stamp = stamp
        return True

    def _basemap_sources_changed(self):
        """Whether the basemap sources have changed since the last request."""
        from sailworks.common import resource_path

        current_gshhs = (self._data_dir / "gshhs").is_dir()
        current_fallback = Path(
            resource_path("sailworks", "data/countries.geojson")
        ).is_file()

        with self._condition:
            if (
                current_gshhs == self._has_gshhs
                and current_fallback == self._has_fallback
            ):
                return False
            self._check_basemap_sources(self._data_dir)
            return True

    def invalidate_sources(self):
        """Drop the worker's source metadata (GUI thread).

        The manifest is read again by the next composition, so an installed,
        replaced or switched off chart is picked up without a restart.  The
        published composition is withdrawn: it describes sources that may no
        longer be the user's.
        """
        with self._condition:
            self._invalidate_sources_locked()
            self._published = None
            self._payload_bytes = 0
            self._enc_stale = False
            if self._enc_enabled:
                # The ENC that was published is not the user's ENC any more.
                self._enc_state = "loading"
                self._enc_metadata = {}
            # Also recheck basemap sources
            self._check_basemap_sources(self._data_dir)

    def _invalidate_sources_locked(self):
        """Reject old work and refresh worker-owned source handles."""
        self._generation += 1
        self._reset_sources = True
        for request in (self._pending, self._active):
            if request is not None:
                request[2].set()

    def set_enc_enabled(self, enabled):
        """Add or remove the ENC cells from the composition (GUI thread).

        The retention belongs to the layer identity: a surface composed with
        the cells out may not serve a request with them in, and the reverse.
        The published composition is withdrawn and the worker drops its
        retained pixels before it composes again, so the other identity is
        never reused or presented as the current one.
        """
        enabled = bool(enabled)
        with self._condition:
            if enabled == self._enc_enabled:
                return False
            self._enc_enabled = enabled
            self._enc_state = "loading" if enabled else "disabled"
            self._enc_metadata = {}
            self._enc_stale = False
            self._published = None
            self._payload_bytes = 0
            self._invalidate_sources_locked()
        self._notify_state("loading" if enabled else "disabled")
        return True

    def set_satellite_background(self, enabled):
        """Whether imagery owns the background (GUI thread).

        The composition's identity includes the background it was painted
        for: over imagery the base chart must stay transparent so the raster
        background shows through, while over the basemap the composition
        carries the vector chart.  A change withdraws the published pixels
        and drops the worker's retained frames before it composes again, so
        neither identity is reused or presented as the current one.
        """
        enabled = bool(enabled)
        with self._condition:
            if enabled == self._satellite_background:
                return False
            self._satellite_background = enabled
            self._published = None
            self._payload_bytes = 0
            self._enc_stale = False
            self._invalidate_sources_locked()
        return True

    def enc_status(self):
        """The ENC source's own status text, for the scene."""
        with self._condition:
            if not self._enc_enabled or self._enc_state == "disabled":
                return ""
            published = self._enc_metadata or {}
            if self._enc_state == "loading":
                return (
                    "ENC updating"
                    if published.get("state") == "ready"
                    else "ENC loading"
                )
            if self._enc_state == "coverage_miss":
                return published.get("reason") or "No ENC coverage here"
            if self._enc_state == "unavailable":
                return "ENC unavailable"
            return ""

    def close(self):
        """Stop the chart worker; a pending composition is released."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._published = None
            self._payload_bytes = 0
            for request in (self._pending, self._active):
                if request is not None:
                    request[2].set()
                    self.counters["cancelled"] += 1
            self._pending = None
            self._condition.notify_all()
        if self._owns_session and self._session is not None:
            self._session.close()

    # ------------------------------------------------------------ worker
    def _notify_state(self, state):
        if self._on_state is not None:
            self._on_state(state)

    def _notify_publish(self, payload):
        if self._on_publish is not None:
            self._on_publish(payload)

    def _run(self):  # noqa: C901 - keep worker lifecycle and cancellation together
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    request, self._pending = cast(tuple, self._pending), None
                    self._active = request
                    reset_sources = self._reset_sources
                    self._reset_sources = False
                if reset_sources:
                    self._release()
                    self._sources = ()
                generation, camera, cancel, preview = request
                began = time.perf_counter()
                error = ""
                image = None
                metadata = None
                self._outcome = None
                try:
                    if not cancel.is_set():
                        if preview and not self._await_idle(cancel):
                            self._outcome = "yielded"
                        elif preview:
                            image, metadata = self._compose(
                                camera, cancel, preview=True
                            )
                        else:
                            image, metadata = self._compose(camera, cancel)
                except Exception as exc:  # a chart failure is never fatal
                    error = str(exc)
                ended = time.perf_counter()
                requested_at = self._requested_at.pop(generation, None)
                if metadata is not None and requested_at is not None:
                    metadata.setdefault("timings", {})["queue_ms"] = round(
                        (began - requested_at) * 1000.0, 3
                    )
                publish = None
                notify_state = None
                with self._condition:
                    self._active = None
                    self._spans.append((began, ended))
                    if len(self._spans) > 256:
                        del self._spans[: len(self._spans) - 256]
                    superseded = (
                        cancel.is_set()
                        or generation != self._generation
                        or self._outcome == "stale"
                        or self._closed
                    )
                    if (
                        self._outcome == "yielded"
                        and not cancel.is_set()
                        and not superseded
                        and (generation == self._generation)
                    ):
                        # The weather pipeline claimed the worker: the chart
                        # yields, and the layer asks again when the weather
                        # pipeline is idle.
                        self._state = "idle"
                        notify_state = "idle"
                    elif superseded:
                        # Superseded: this generation may never publish.
                        self.counters["superseded_rejected"] += 1
                        self._rejected_generations.add(generation)
                        self._outcomes[generation] = "superseded"
                        _trace_rejection(
                            generation,
                            self._request_causes.get(generation),
                            camera,
                            cancel.is_set(),
                            self._closed,
                            self._outcome,
                            image is not None,
                        )
                    elif image is not None:
                        self.counters["compositions"] += 1
                        self.counters["publications"] += 1
                        self._published_generations.add(generation)
                        self._published = (generation, image, metadata)
                        self._state = "ready"
                        self._outcomes[generation] = "published"
                        publish = self._published
                        self._payload_bytes = _image_bytes(image)
                        self._adopt_enc_metadata(metadata)
                        _trace_composition(
                            generation,
                            self._request_causes.get(generation),
                            metadata,
                        )
                    else:
                        self.counters["failures"] += 1
                        self._state = "unavailable"
                        notify_state = "unavailable"
                        self._error = error
                        self._outcomes[generation] = "failed"
                        self._published = None
                        self._payload_bytes = 0
                        if self._enc_enabled:
                            # A failed composition never leaves the previous
                            # ENC presented: it is not current, and it is not
                            # presented as if it were.  The composition is
                            # withdrawn, so nothing stale can be reused.
                            self._enc_state = "unavailable"
                            self._enc_stale = False
                            self._enc_metadata = {}
                    self._trim_history()
                if publish is not None:
                    with self._condition:
                        # A close between the composition and the delivery
                        # withdraws the publication: a closed layer never
                        # publishes.
                        if self._closed:
                            publish = None
                    if publish is not None:
                        self._notify_publish(publish)
                        self._notify_state("ready")
                elif notify_state is not None:
                    self._notify_state(notify_state)
        finally:
            self._release()

    def _adopt_enc_metadata(self, metadata):
        """Adopt the ENC source's own report for the published composition."""
        if not self._enc_enabled:
            return
        enc = (metadata or {}).get("enc") or {}
        self._enc_metadata = dict(enc)
        self._enc_state = enc.get("state", "unavailable")
        self._enc_stale = False
        if self._enc_state == "ready":
            self.counters["enc_compositions"] += 1
            self.counters["enc_cells"] += len(enc.get("selected") or ())
        elif self._enc_state == "unavailable":
            self.counters["enc_failures"] += 1

    def _trim_history(self):
        """Keep the diagnostic history bounded.

        The history is evidence about the recent generations, never a store:
        only the most recent generations are kept, so a long session cannot
        grow it without limit.
        """
        if len(self._outcomes) <= HISTORY_LIMIT:
            return
        keep = set(sorted(self._outcomes)[-HISTORY_LIMIT:])
        self._outcomes = {
            generation: outcome
            for generation, outcome in self._outcomes.items()
            if generation in keep
        }
        self._published_generations &= keep
        self._rejected_generations &= keep

    def _await_idle(self, cancel):
        """Whether the weather pipeline is idle.

        The check is a single reading of the weather pipeline's state: the
        layer never polls, so it never competes with the weather pipeline
        for the interpreter's lock.
        """
        return self._worker_idle()

    def _release(self):
        self._static_frames.clear()
        with self._condition:
            self._static_bytes = 0
            self._static_count = 0
        for source in self._sampler_sources:
            try:
                source.close()
            except Exception:  # pragma: no cover - a closed handle is fine
                pass
        self._sampler_sources = ()
        self._sampler = None
        if self._composer_instance is not None:
            self._composer_instance._renderer._release_resources()
            self._composer_instance = None
        if self._enc_source is not None:
            # The worker's own ENC handles are released here, on the worker
            # thread, and never on the GUI thread.  The source itself is
            # kept while the worker lives: the prepared geometry it holds
            # survives the redraw, and the cells themselves decide when it
            # must be dropped.  A closed worker drops it with the worker.
            self._enc_source.release()
            if self._closed:
                self._enc_source = None

    # --------------------------------------------------------- composition
    def _compose(self, camera, cancel, preview=False):
        """Compose the chart layer: the application's base chart and isobars.

        The base chart is the application's own composition; the ENC cells
        are the application's own ENC renderer on the same surface; the
        isobars are the application's own contour drawing.  The chart
        content is retained without the isobars and reused for a camera that
        it still covers; a pan composes only the newly exposed rectangles,
        and a forecast-time change composes no chart pixel at all.  The
        composition runs on the chart worker, never on the GUI thread and
        never on the field worker.
        """
        from sailworks.core.mapviewport import MapViewport

        import cairo

        factor = self._dpr * 0.5 if preview else self._dpr
        canvas = ChartCamera(camera, factor)
        viewport = MapViewport(canvas, MARGIN, lambda: None)
        viewport.render_cancelled = cancel.is_set
        sources = self._base_sources()
        job = RenderJob(
            key=(self._palette, factor, sources),
            viewport=viewport,
            charts=tuple((source,) for source in sources),
            factor=factor,
            palette=self._palette,
            cancel=cancel,
        )
        if self._view_moved(camera):
            # The view moved on: this generation is stale before it starts.
            with self._condition:
                self.counters["stale"] += 1
            self._outcome = "stale"
            return None, None
        composed_at = time.perf_counter()
        static = self._static_content(job, viewport, factor, cancel)
        static_ms = (time.perf_counter() - composed_at) * 1000.0
        if static is None:
            return None, None
        surface, enc, reuse = static
        if self._view_moved(camera):
            with self._condition:
                self.counters["stale"] += 1
            self._outcome = "stale"
            return None, None
        columns_started = time.perf_counter()
        columns = None if self._enc_only else self._isobar_columns(job, camera, cancel)
        columns_ms = (time.perf_counter() - columns_started) * 1000.0
        if cancel.is_set():
            return None, None
        isobar_ms = 0.0
        copy_ms = 0.0
        isobar_only = bool(columns) and reuse["mode"] == "reused"
        if isobar_only:
            with self._condition:
                self.counters["isobar_only"] += 1
        if columns:
            with self._condition:
                self.counters["isobars"] += 1
            started = time.perf_counter()
            surface = _copy_surface(surface, factor)
            copy_ms = (time.perf_counter() - started) * 1000.0
            started = time.perf_counter()
            cr = cairo.Context(surface)
            self._composer().painter().draw_isobars(viewport, cr, columns)
            surface.flush()
            isobar_ms = (time.perf_counter() - started) * 1000.0
        if self._view_moved(camera):
            with self._condition:
                self.counters["stale"] += 1
            self._outcome = "stale"
            return None, None
        image = _surface_image(surface, factor)
        reuse["isobar_only"] = isobar_only
        timings = {
            "static_ms": round(static_ms, 3),
            "columns_ms": round(columns_ms, 3),
            "copy_ms": round(copy_ms, 3),
            "isobars_ms": round(isobar_ms, 3),
            "composition_ms": round(static_ms + columns_ms + copy_ms + isobar_ms, 3),
        }
        metadata = {
            "viewport": viewport,
            "camera": camera,
            "time": camera.stamp(),
            "factor": factor,
            "preview": preview,
            # The published image's own device-pixel extent.  It is read from
            # the image, not from the requested viewport, so a surface that
            # does not match this demand can never be re-served under
            # metadata that claims it does.
            "image_extent": (int(image.width()), int(image.height())),
            # The background identity these pixels were painted for.  The
            # viewport presents a deferred switch only on a composition that
            # matches it.
            "satellite_background": bool(self._satellite_background),
            "enc": enc if enc is not None else disabled_enc_metadata(),
            "static": reuse,
            "timings": timings,
        }
        return image, metadata

    def _static_content(self, job, viewport, factor, cancel):
        """The isobar-free chart content, reusing what still covers the view.

        Returns ``(surface, enc_metadata, report)``.  The surface is the base
        chart and the ENC cells only, at this composition's factor.  A
        retained frame that encloses the view is painted as it is; one that
        overlaps composes only the exposed rectangles; no frame at this
        quality composes the whole view.  ``None`` means cancelled.
        """
        key = (self._palette, factor, self._base_sources())
        import cairo

        report = {
            "mode": "full",
            "reason": "none",
            "shift": None,
            "snapped_px": 0.0,
            "delta_px": None,
            "factor": factor,
            "regions": 0,
            "region_rects": [],
            "composed_pixels": int(viewport.width) * int(viewport.height),
            "reused_pixels": 0,
            "retained_bytes": 0,
        }
        frame, overlap, shift = self._best_static_frame(key, viewport)
        if frame is not None and overlap is not None:
            # A frame at a fractional device offset is translated to the
            # nearest whole device pixel rather than rejected: a mouse pan
            # produces exactly those offsets, and rounding is a translation of
            # retained pixels, never a resample.  The bound is reported, and
            # the residual it leaves is carried on the retained frame so it is
            # cancelled by the next pan instead of accumulating.
            report["shift"] = shift
            left, top, _right, _bottom = _viewport_bounds(viewport)
            f_left, f_top, _f_right, _f_bottom = _viewport_bounds(frame.viewport)
            report["delta_px"] = (
                round((f_left - left) * factor, 4),
                round((f_top - top) * factor, 4),
            )
            snapped = _static_shift_error(frame, viewport, factor)
            report["snapped_px"] = round(snapped, 4)
            if snapped > 1e-6:
                with self._condition:
                    self.counters["static_snapped_reuses"] += 1
        if frame is not None and overlap is not None:
            if overlap == (0, 0, int(viewport.width), int(viewport.height)):
                # Every pixel is covered by the retained frame, but its
                # surface may be sized and anchored for another view.  It is
                # handed back only when its geometry is this view's; otherwise
                # the covered pixels are re-anchored into a surface of the
                # requested extent, and that corrected frame is retained.  The
                # caller's metadata must describe the pixels it actually got:
                # a mismatched extent would place other pixels' content under
                # this view's camera and show only an overlapping corner.
                report["mode"] = "reused"
                report["reason"] = "covered"
                report["composed_pixels"] = 0
                report["reused_pixels"] = int(viewport.width) * int(viewport.height)
                if (
                    _same_static_geometry(frame.viewport, viewport)
                    and shift == (0, 0)
                    and frame.snap == (0.0, 0.0)
                ):
                    self._count_static(report)
                    self._touch_static(frame)
                    report["retained_bytes"] = self._retained_static_bytes()
                    return frame.surface, frame.enc_metadata, report
                surface = _empty_surface(viewport, factor)
                _paint_static(surface, frame, viewport, factor)
                enc_metadata = self._enc_report(job, self._enc_composer())
                if enc_metadata is None:
                    return None
                self._retain_static(
                    key,
                    surface,
                    viewport,
                    factor,
                    enc_metadata,
                    _static_residual(frame, shift, viewport, factor),
                )
                self._count_static(report)
                report["retained_bytes"] = self._retained_static_bytes()
                return surface, enc_metadata, report
            surface = _empty_surface(viewport, factor)
            _paint_static(surface, frame, viewport, factor)
            regions = _exposed_regions(
                int(viewport.width), int(viewport.height), overlap
            )
            report["composed_pixels"] = 0
            report["region_rects"] = [
                (int(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in regions
            ]
            enc = self._enc_composer()
            for rect in regions:
                if cancel.is_set():
                    return None
                region = self._compose_region(job, viewport, rect, factor, cancel)
                if region is None:
                    return None
                region_surface, x, y = region
                cr = cairo.Context(surface)
                cr.set_source_surface(region_surface, x, y)
                cr.paint()
                report["regions"] += 1
                report["composed_pixels"] += (rect[2] - rect[0]) * (rect[3] - rect[1])
            report["reused_pixels"] = (overlap[2] - overlap[0]) * (
                overlap[3] - overlap[1]
            )
            enc_metadata = self._enc_report(job, enc)
            if enc_metadata is None:
                return None
            report["mode"] = "regions"
            report["reason"] = "exposed"
            self._retain_static(
                key,
                surface,
                viewport,
                factor,
                enc_metadata,
                _static_residual(frame, shift, viewport, factor),
            )
            self._count_static(report)
            report["retained_bytes"] = self._retained_static_bytes()
            return surface, enc_metadata, report
        surface, enc_metadata = self._composer().render(job, None, self._enc_composer())
        if surface is None:
            return None
        if enc_metadata is None:
            enc_metadata = disabled_enc_metadata()
        if report["reason"] == "none":
            report["reason"] = "full"
        self._retain_static(key, surface, viewport, factor, enc_metadata)
        self._count_static(report)
        report["retained_bytes"] = self._retained_static_bytes()
        return surface, enc_metadata, report

    def _best_static_frame(self, key, viewport):
        """The retained frame that covers this view, if one does.

        A short history is kept so a warm reversal finds the frame it came
        from.  The frame that covers the target is preferred; otherwise the
        frame with the largest overlap is used and only the rest is composed.
        The returned overlap is measured against the frame's *snapped* device
        offset (see :func:`_static_shift`), so it describes the pixels the blit
        actually lands.
        """
        full = (0, 0, int(viewport.width), int(viewport.height))
        factor = key[1]
        best = (None, None, None)
        best_area = 0
        for frame in self._static_frames.values():
            if not _static_frame_serves(frame, key, viewport):
                continue
            shift = _static_shift(frame, viewport, factor)
            if shift is None:  # pragma: no cover - unreachable within the bound
                continue
            overlap = _intersection_rect(viewport, frame.viewport, shift, factor)
            if overlap is None:
                continue
            if overlap == full:
                return frame, overlap, shift
            area = (overlap[2] - overlap[0]) * (overlap[3] - overlap[1])
            if area > best_area:
                best = (frame, overlap, shift)
                best_area = area
        return best

    def _touch_static(self, frame):
        """Make a reused frame the most recently used one."""
        for key, candidate in self._static_frames.items():
            if candidate is frame:
                self._static_frames.move_to_end(key)
                return

    def _compose_region(self, job, viewport, rect, factor, cancel):
        """Compose one exposed rectangle with its own padding.

        The region is rendered on its own surface, centred on the rectangle,
        with ``STATIC_REGION_PAD`` on every side.  The padding is composed so
        a label or symbol that reaches across the boundary is drawn whole;
        the caller paints the surface at the padded origin and the exposed
        rectangle lands exactly on the target.
        """
        from sailworks.core.mapviewport import MapViewport

        left, top, right, bottom = _viewport_bounds(viewport)
        x0, y0, x1, y1 = rect
        pad = STATIC_REGION_PAD
        world_left = left + x0 - pad
        world_top = top + y0 - pad
        world_right = left + x1 + pad
        world_bottom = top + y1 + pad
        # The padded rectangle is an integer number of surface pixels, and its
        # centre reproduces those exact bounds: no subpixel shift is introduced
        # by the region itself.
        width = max(1, x1 - x0 + 2 * pad)
        height = max(1, y1 - y0 + 2 * pad)
        center_x = (world_left + world_right) / 2.0
        center_y = (world_top + world_bottom) / 2.0
        camera = _RegionCamera(
            viewport._y_to_lat(center_y),
            viewport._x_to_lon(center_x),
            viewport._zoom,
            width,
            height,
        )
        region_viewport = MapViewport(ChartCamera(camera, factor), 0, lambda: None)
        region_viewport.render_cancelled = cancel.is_set
        region_job = RenderJob(
            key=job.key,
            viewport=region_viewport,
            charts=job.charts,
            factor=factor,
            palette=job.palette,
            cancel=cancel,
        )
        region_surface, _enc = self._composer().render(
            region_job, None, self._enc_composer()
        )
        if region_surface is None:
            return None
        return region_surface, x0 - pad, y0 - pad

    def _enc_report(self, job, enc):
        """The ENC identity of the whole viewport, without drawing it again.

        The pixels may have been reused from another camera, but the report
        describes this viewport: the same cells, in the same order, with the
        same coverage a full composition would have drawn.
        """
        if enc is None:
            return disabled_enc_metadata()
        report = enc.describe(job)
        if report is None:
            return None
        return report

    def _retained_static_bytes(self):
        """The retained static payload, as the last retention computed it."""
        with self._condition:
            return self._static_bytes

    def _retain_static(
        self, key, surface, viewport, factor, enc_metadata, snap=(0.0, 0.0)
    ):
        """Keep the isobar-free surface, bounded by count and by quality.

        ``snap`` is the residual device offset the pixels carry after the blit
        that produced them; it is stored so the next reuse cancels it.
        """
        surface.flush()
        frame = _StaticFrame(
            surface=surface,
            viewport=viewport,
            factor=factor,
            palette=self._palette,
            sources=self._base_sources(),
            enc_metadata=enc_metadata,
            bytes=_surface_bytes(surface),
            snap=snap,
        )
        self._static_serial += 1
        self._static_frames[(key, self._static_serial)] = frame
        while len(self._static_frames) > STATIC_FRAME_LIMIT:
            self._evict_static()
        with self._condition:
            self._static_bytes = sum(
                frame.bytes for frame in self._static_frames.values()
            )
            self._static_count = len(self._static_frames)

    def _evict_static(self):
        """Drop one retained frame, a preview before a full-quality frame.

        A preview frame is half resolution and every settled composition
        supersedes it, while the full-quality frame it shares the pool with
        is what the next pan reuses.  Evicting by age alone lets two or three
        previews each pan push out the frame that would have served the next
        settle, so every pan composes the whole chart again.  A preview is
        dropped first; only when every slot holds a full-quality frame is the
        oldest of them dropped.
        """
        preview = next(
            (
                key
                for key, frame in self._static_frames.items()
                if frame.factor != self._dpr
            ),
            None,
        )
        if preview is None:
            preview = next(iter(self._static_frames))
        del self._static_frames[preview]

    def _count_static(self, report):
        with self._condition:
            if report["mode"] == "full":
                self.counters["static_full_compositions"] += 1
            elif report["mode"] == "regions":
                self.counters["static_region_compositions"] += 1
                self.counters["static_regions"] += report["regions"]
            else:
                self.counters["static_reuses"] += 1
            self.counters["static_pixels_composed"] += report["composed_pixels"]
            self.counters["static_pixels_reused"] += report["reused_pixels"]

    def _composer(self):
        if self._composer_instance is None:
            self._composer_instance = _ChartComposer(
                self._palette, self._base_sources()
            )
        return self._composer_instance

    def _enc_composer(self):
        """The worker's own ENC source; it is never shared with the app.

        The source is created on the chart worker, reads the user's manifest
        and opens the charts with the worker's own handles.  It is released
        with the worker.
        """
        if not self._enc_enabled:
            return None
        if self._enc_source is None:
            from sailworks.qt.viewportenc import ViewportEncSource

            self._enc_source = ViewportEncSource(self._data_dir, self._palette)
        return self._enc_source

    def _base_sources(self):
        if self._enc_only:
            return ()
        if self._enc_enabled or self._satellite_background:
            # The ENC cells are an overlay, not a background: the composition
            # is transparent outside their coverage, so the active background
            # (satellite imagery or the vector basemap) shows through.  The
            # same is true of the whole composition while imagery owns the
            # background.  The world coastline is already the viewport's own
            # basemap layer and must not be painted opaque over that
            # background here.
            return ()
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

        from sailworks.rendering.windrenderer import WindOverlayRenderer

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

        The chart layer owns its own GDAL handles: it never borrows the field
        worker's, so the two pipelines are independent and either can be
        replaced or cancelled without the other.  Their immutable metadata and
        decoded bands come from the shared session.
        """
        if self._sampler is not None:
            return self._sampler
        from sailworks.core.gribsampling import WeatherSampler
        from sailworks.rendering.weathercache import ForecastSession

        if self._session is None:
            self._session = ForecastSession()
            self._owns_session = True
        sources = [self._session.source(path) for path in self._paths]
        if not sources:
            return None
        self._sampler_sources = tuple(sources)
        self._sampler = WeatherSampler(sources)
        return self._sampler


def _image_bytes(image):
    """The published payload's own size, in bytes.

    The payload is accounted for, never assumed: a payload that is not an
    image has no size and is reported as zero rather than failing the
    publication.
    """
    width = getattr(image, "width", None)
    height = getattr(image, "height", None)
    if width is None or height is None:
        return 0
    return int(width()) * int(height()) * 4


def _surface_image(surface, dpr=1):
    """The composed cairo surface as a Qt image.

    The surface is ARGB32 and premultiplied, the same layout as the
    application's own framebuffer readback, so the bytes are copied without
    a conversion.  The copy is the surface's bytes and nothing else: the
    image shares them, so the composed surface is published with one copy
    instead of two.

    The image carries the composition's device pixel ratio, so the scene
    shows one device pixel per display pixel and never rescales the
    composition.
    """
    from PySide6.QtGui import QImage

    surface.flush()
    width = surface.get_width()
    height = surface.get_height()
    stride = surface.get_stride()
    data = bytes(surface.get_data())
    image = QImage(
        data,
        width,
        height,
        stride,
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.setDevicePixelRatio(dpr)
    return image


def chart_image_provider(name="chart"):
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
            return f"image://{name}/{generation}"

        def publish_retained(self, generation, image, visible_generation=None):
            """Keep only the visible image and its replacement."""
            with self._lock:
                self.publishes += 1
                visible = self._images.get(str(visible_generation))
                self._images = (
                    {} if visible is None else {str(visible_generation): visible}
                )
                self._images[str(generation)] = image
            return f"image://{name}/{generation}"

        def retain(self, generation=None):
            with self._lock:
                image = self._images.get(str(generation))
                self._images = {} if image is None else {str(generation): image}

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
