"""An independent GRIB viewport: its own camera, scheduler and GPU context.

The viewport is a standalone subsystem.  It does not instantiate the
application's Core, SessionController, legacy map widget, MapFrame, MapScene or
WeatherStager, and it imports nothing from ``tools/``.  It reuses the weather
decoding and sampler semantics, the native-grid geometry, the packed-float
transport and the reviewed field/selection shader.

The split is the one the field store established: the *field* is the
geographic numerical payload of a spatial window, retained and published
once; the *selection* is the exact current-view portrayal, recomputed for
every view.  A covered camera move therefore reuses the resident field and
publishes selection traffic only, and it never waits for the worker: a
deliberately stalled preparation leaves the camera free.
"""

from dataclasses import dataclass, replace
from bisect import bisect_left
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import sys
from threading import Condition, Event, Thread
import time
from typing import Any, cast

from sailworks.core.mapmotion import DragVelocity, PanInertia
from sailworks.qt.gpuweatherfields import (
    FIELD_COLUMNS,
    FieldIdentity,
    FieldStore,
    GeographicField,
    validate_binding,
)
from sailworks.rendering.weatherbarbs import (
    BarbView,
    FIELD_LATTICE_PX,
    WAVE_BANDS,
    anchor_view,
    field_window,
    node_anchor_atlas,
    _view_bounds,
    select_resident,
    stage_field,
    stage_wave_selection,
)
from sailworks.rendering.weatherstaging import NodeSampleMemo, time_window
from sailworks.rendering.windpainter import shoreline_quality
from sailworks.qt.viewportpublication import (
    LayerRole,
    LayerIdentity,
    LayerPlacement,
    ResourceKind,
    PublicationManager,
)
from sailworks.qt.viewporttransform import (
    PanReference,
    CameraChange,
    PaddedPanWindow,
    continuous_longitude,
    zoom_preview_transform,
)

logger = logging.getLogger("sailworks")


def world_placement(camera):
    """Place the canonical world; adjacent copies cover the longitude seam."""
    view = camera.view()
    size = view.TILE_SIZE * 2.0**camera.zoom
    longitude = continuous_longitude(camera.center_lon, 0.0)
    left = camera.width / 2.0 - (longitude + 180.0) / 360.0 * size
    top = (
        camera.height / 2.0
        + view._lat_to_y(view.MAX_LAT)
        - view._lat_to_y(camera.center_lat)
    )
    return left, top, size, size


def _render_world(data_dir, palette, factor):
    """Render the fixed-zoom world using private worker-side chart handles."""
    from sailworks.core.mapviewport import MapViewport, MercatorProjection
    from sailworks.core.scheduling import schedule_idle
    from sailworks.qt.basecharts import BaseChartRenderer
    from sailworks.qt.gribviewportchart import ChartCamera, _surface_image, base_sources
    from sailworks.rendering.overlayrenderer import RenderJob

    if data_dir is None:
        from sailworks.core.storage import DATA_DIR

        data_dir = DATA_DIR
    sources = base_sources(data_dir)
    if not sources:
        raise RuntimeError("No coastline data available for world underlay")
    size = MercatorProjection.TILE_SIZE * 2**2
    camera = ViewportCamera(zoom=2, width=size, height=size)
    canvas = ChartCamera(camera, factor)
    viewport = MapViewport(canvas)
    job = RenderJob(
        (palette, factor, sources),
        viewport,
        tuple((s,) for s in sources),
        factor,
        palette,
    )
    renderer = BaseChartRenderer(schedule_idle)
    try:
        surface = renderer._render(job)
        if surface is None:
            raise RuntimeError("World underlay rendering returned no image")
        return _surface_image(surface, factor)
    finally:
        renderer._release_resources()


# The residency budget of the viewport's field store.  This is a declared
# ceiling, not a measured optimum, and it was re-derived on 2026-10-19
# (issue #73) against the recorded runs:
#
#   * the #64 follow-up held 33 fields in 58,785,331 B (56.05 MiB) with 0
#     evictions -- the observed worst case
#     (docs/profiles/rendering/2026-10-19/viewport-continuous-summary.json);
#   * #67's resident time window holds 13 fields in 24,472,139 B (23.34 MiB)
#     with 0 evictions (docs/profiles/rendering/2026-10-19/
#     viewport-time-window-summary.json);
#   * the sampling memo declares 16 MiB and the image provider 32 MiB.  Both
#     are separate budgets; neither is drawn from this store.
#
# The ceiling was raised to 128 MiB during the precursor experiment, so it
# was never derived from a residency requirement.  It is kept because it is
# a declared cap, the observed worst case is 43.8% of it (a 2.28x margin),
# the resident time window adds members over the same spatial window rather
# than replacing fields, and no eviction has ever been observed.  Lowering it
# would trade against an unmeasured workload -- a longer time horizon, a
# larger view -- that no recorded run covers.  It is a CPU payload budget,
# not VRAM, and larger views retain fewer fields.
FIELD_BYTE_LIMIT = 128 * 1024 * 1024

# The viewport's spatial window and refill-guard policy.  These two factors
# are the window/guard policy named by #66 (overlapped, double-buffered
# preparation) and #67 (resident time window); they replace the precursor's
# unexplained tuning factors.  The values are unchanged.
#
# RESIDENT_WINDOW_FACTOR (1.5): a resident field's spatial window covers
# 1.5x the camera viewport, so a covered camera move reuses the resident
# field instead of preparing a new one.  On the recorded fixture camera
# (a 900x700 view with the 32 px symbol margin) the 1.5x window is
# 1350x1050 px against the camera's own 964x764 px demand, so the surplus
# is 193 px per side in x (675 - 450 - 32) and 143 px per side in y
# (525 - 350 - 32): about 0.53 s of pan at the recorded 12 px per input and
# 30 inputs/s (360 px/s).  The 64 px field lattice quantises the window
# outward, so the real runway is never smaller than this.
#
# REFILL_GUARD_FACTOR (1.25): the refill guard is the viewport expanded by
# 1.25x.  The re-centring trigger fires when the resident field stops
# covering the guard, which is (1.25 - 1) / 2 * 900 = 112.5 px -- about
# 0.31 s of pan -- before the camera's own demand leaves the resident
# window: the re-centre starts while the camera still has runway, so #66's
# overlapped pipeline can publish the re-centred field before coverage is
# lost.
#
# Sufficiency is recorded, not assumed.  During the recorded sustained pan
# (120 inputs at 30 Hz, 4.0 s) the #66 prototype completed 11 field
# preparations, one per 0.36 s, and the #67 run completed the same pan with
# 0/120 loading inputs (docs/profiles/rendering/2026-10-19/
# viewport-time-window-summary.json).  The measured cost of the re-centred
# window itself -- one field-lattice step (64 px) away -- is 159 ms, against
# 792 ms for a cold window (docs/profiles/rendering/2026-10-19/
# prepare-breakdown.json).
RESIDENT_WINDOW_FACTOR = 1.5
REFILL_GUARD_FACTOR = 1.25

# The declared horizon of the resident time window.  The recorded interaction
# sweep moves the forecast instant over nine hours (camera.time plus
# ``i % 9`` hours), so the window holds the demand windows of the forecast
# instants inside that nine-hour span, over one spatial window.  The horizon
# is a workload property, not a measured threshold: it is derived from the
# recorded sweep and is bounded by the field store's byte ceiling.
RESIDENT_TIME_WINDOW_HOURS = 9.0

# Lookahead: the window re-anchors once the forecast instant is within this many
# hours of its horizon, so the next intervals are prepared before they are needed.
RESIDENT_TIME_LOOKAHEAD_HOURS = 2.0


@dataclass(frozen=True)
class ViewportCamera:
    """The viewport's own camera and time state."""

    center_lat: float = 0.0
    center_lon: float = 0.0
    zoom: float = 3.0
    width: int = 640
    height: int = 480
    time: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    density: str = "standard"
    over_land: bool = True
    geographic: bool = True
    source: str | None = None

    def view(self):
        """The staging view of this camera."""
        return BarbView(
            self.width,
            self.height,
            self.center_lat,
            self.center_lon,
            self.zoom,
            self.density,
            self.over_land,
            geographic=self.geographic,
        )

    def stamp(self):
        return self.time.timestamp()


def _expanded(camera, factor):
    return replace(
        camera, width=round(camera.width * factor), height=round(camera.height * factor)
    )


#: Pixels of pan a cached selection may cover before it is recomputed.
#: This matches the selection's existing 32 px symbol margin, so reuse never
#: changes which symbols a viewport shows.
SELECTION_REUSE_MARGIN = 32.0

#: Satellite frames are padded like the chart composition, so a pan exposes
#: already-rendered imagery instead of the bare chart until the next frame.
SATELLITE_MARGIN = 256


def _selection_translation(first, second):
    """Logical-pixel translation from ``first`` to ``second`` at one zoom."""
    dx = first._lon_to_x(first._center_lon) - first._lon_to_x(
        continuous_longitude(second._center_lon, first._center_lon)
    )
    dy = first._lat_to_y(first._center_lat) - first._lat_to_y(second._center_lat)
    return dx, dy


def _selection_reusable(computed_view, current_view):
    """Whether a padded selection still covers the current camera."""
    if (
        computed_view._zoom != current_view._zoom
        or computed_view.width != current_view.width
        or computed_view.height != current_view.height
    ):
        return False
    dx, dy = _selection_translation(computed_view, current_view)
    return abs(dx) <= SELECTION_REUSE_MARGIN and abs(dy) <= SELECTION_REUSE_MARGIN


def _spatially_covers(field, camera):
    """Geographic coverage and the same shoreline tier, independent of zoom."""
    if not camera.over_land and (
        field.land_flags is None
        or field.land_quality != shoreline_quality(camera.view().get_scale())
    ):
        return False
    south, west = field.identity.window[0]
    north, east = field.identity.window[1]
    required = _view_bounds(camera.view())
    return (
        south <= required[0][0] <= required[1][0] <= north
        and west <= required[0][1] <= required[1][1] <= east
    )


def _covers(field, camera):
    """Whether immutable values and mask policy cover the entire current demand."""
    return field.identity.contains(camera.stamp()) and _spatially_covers(field, camera)


def _contains(outer, inner):
    """Whether the geographic window ``outer`` contains the window ``inner``."""
    return (
        outer[0][0] <= inner[0][0]
        and outer[1][0] >= inner[1][0]
        and outer[0][1] <= inner[0][1]
        and outer[1][1] >= inner[1][1]
    )


def _instant(camera, stamp):
    """The datetime of a timestamp, in the camera's own time convention."""
    moment = (
        datetime.fromtimestamp(stamp)
        if camera.time.tzinfo is None
        else datetime.fromtimestamp(stamp, timezone.utc)
    )
    return moment


def _generation_key(field):
    """The content generation of a prepared resource: source/time/coverage/scope.

    ``field.identity.key()`` already hashes source, parameter, time window,
    spatial window and resolution.  The remaining components are kept explicit
    so a resource assembled for another demand cannot be mistaken for this one
    even if some identity fields happen to collide.
    """
    identity = field.identity
    return (
        field.generation,
        identity.key(),
        identity.source,
        identity.time_window,
        identity.window,
        identity.resolution,
        field.land_quality,
    )


@dataclass(frozen=True)
class PreparedResource:
    """One prepared field in a front/back slot.

    ``generation`` is computed when the worker finishes the preparation and is
    re-derived before publication.  A mismatch is rejected rather than shown.
    ``span`` is the ``perf_counter`` preparation interval used to observe
    overlap with presentation.
    """

    field: GeographicField
    generation: tuple
    demand: tuple
    slot: int
    span: tuple


@dataclass(frozen=True)
class ResidentTimeWindow:
    """The declared resident time window of one spatial window.

    ``spatial`` is the geographic window of the presented field, ``anchor``
    the forecast instant the window starts at and ``members`` the demand
    windows the window holds, nearest first.  A member is one forecast
    interval: the singleton at a selection event and the bracket between two
    events are separate members, because their masks and their interpolation
    differ.
    """

    spatial: tuple
    anchor: float
    members: tuple

    @property
    def identity(self):
        return (self.spatial, self.anchor, self.members)


def _spatial_state(camera):
    """The spatial parameters of a camera: its window without its time."""
    return (
        camera.center_lon,
        camera.center_lat,
        camera.zoom,
        camera.width,
        camera.height,
    )


def _band_endpoints(field):
    """The two time endpoints of one scalar band's stored images.

    A scalar ``GeographicField`` stages each endpoint as a value plane plus
    the zero U/V companion ``_field_images`` appends for a one-component
    field, so its images are ``(value A, zero A, value B, zero B)``.  The
    shader samples only the value planes: indices 0 and 2.  A two-image field
    is the historical pair form, ``(A, B)``.
    """
    images = field.images
    if len(images) == 4:
        return images[0], images[2]
    return images[0], images[1]


def wave_barbs_images(fields):
    """The six wave endpoint images in the glyph shader's binding order.

    ``WAVE_BANDS`` is (height, period, direction) and each scalar field
    publishes two endpoint images (time A, time B).  The shader expects the
    three A planes first, then the three B planes:

    u0 height A, u1 period A, u2 direction A,
    u3 height B, u4 period B, u5 direction B.
    """
    endpoints = {band: _band_endpoints(fields[band]) for band in WAVE_BANDS}
    return (
        endpoints["wave_height"][0],
        endpoints["wave_period"][0],
        endpoints["wave_direction"][0],
        endpoints["wave_height"][1],
        endpoints["wave_period"][1],
        endpoints["wave_direction"][1],
    )


def wave_bands_ready(fields):
    """True only when all three bands are present, so no partial arrows draw."""
    return bool(fields) and all(fields.get(band) is not None for band in WAVE_BANDS)


def wave_arrow_view(view, density):
    """The same camera view thinned by a wave density.

    The returned view is a copy: the wind barbs keep their own density in the
    original view, so changing the wave density never re-thins the wind layer.
    """
    return BarbView(
        view.width,
        view.height,
        view._center_lat,
        view._center_lon,
        view._zoom,
        str(density),
        view.over_land,
        view.geographic,
    )


class GribViewportModel:
    """The viewport's field scheduler and current-selection owner.

    The worker thread owns the GDAL handles and prepares geographic fields.
    The render context owns the GPU resources.  A covered camera move is
    served from the resident field without touching the worker, so a stalled
    preparation never stops camera updates.
    """

    def __init__(
        self,
        paths,
        field_bytes=FIELD_BYTE_LIMIT,
        land_mask=None,
        resident_time_window=False,
        grid=False,
        display_identity=None,
        parameter="wind",
        components=2,
        selection=True,
    ):
        self.paths = tuple(str(path) for path in paths)
        if display_identity is not None and len(self.paths) != 1:
            raise ValueError("A display identity must belong to exactly one file")
        self.display_identity = display_identity
        self._parameter = parameter
        self._components = int(components)
        # A scalar layer (wave height) has no barb selection: staging one
        # would sample a second, 2-component field for nothing.
        self._selection_enabled = bool(selection)
        self._land_mask = land_mask
        # The gridded form of the field is staged only when the numerical
        # fill needs it.  The default path stages the node list alone, so
        # the fill's payload costs the default path nothing.
        self._grid = bool(grid)
        self._condition = Condition()
        self._pending = None
        self._active = None
        self._desired_key = None
        self._failed_key = None
        self._field = None
        self._sources: tuple[Any, ...] = ()
        self._sampler: Any = None
        self._memo = None
        self._land = None
        self._thread = None
        self._closed = False
        self._initialization_failed = False
        self._camera = None
        self._state = "loading"
        self._error = ""
        self._store = FieldStore(field_bytes)
        self._events: tuple[float, ...] = ()
        # The resident time window is opt-in: the default viewport behaviour
        # is the one-interval lookahead of the previous issue.
        self._resident_window = bool(resident_time_window)
        self._window = None
        self._window_started = False
        self._prepared_by = {}
        self._selection_cache = {}
        self._selection_cache_order = []
        # Node-indexed anchor atlases outlive a mesh slot permutation: one
        # atlas per field generation and anchor camera.  A pan inside one pan
        # reference reuses it instead of re-projecting every field node.
        self._anchor_atlas_cache = {}
        self._anchor_atlas_cache_order = []
        self._prepare_target = None
        self._bands = None
        # Two prepared-resource slots: the front slot is what the viewport may
        # present, the back slot is what the worker fills.  At most two requests
        # are in flight (one active, one pending); a replaced request is
        # cancelled and can never publish.
        self._slots: list[PreparedResource | None] = [None, None]
        self._front_index = None
        self._preparation_spans = []
        self._published_generations = set()
        self._rejected_generations = set()
        self._published_demands = set()
        self._rejected_demands = set()
        self.on_change = None
        self.counters = dict(
            field_preparations=0,
            covered_reuses=0,
            misses=0,
            cancelled=0,
            stale_rejected=0,
            generation_rejected=0,
            slot_handoffs=0,
            proactive_recentres=0,
            time_lookaheads=0,
            superseded_retained=0,
            time_window_hits=0,
            time_window_fills=0,
            time_window_fields=0,
            time_window_cancelled=0,
            time_window_blocked=0,
        )

    # ------------------------------------------------------------- state
    @property
    def state(self):
        """``loading``, ``ready`` or ``unavailable``."""
        with self._condition:
            return self._state

    @property
    def error(self):
        with self._condition:
            return self._error

    def field_statistics(self):
        with self._condition:
            return self._store.statistics()

    def band_statistics(self):
        """The decoded-band cache's counters: a decode is a cache miss."""
        with self._condition:
            return {} if self._bands is None else self._bands.statistics()

    def busy(self):
        """Whether a field preparation is in flight.

        The chart layer waits for this: it never claims the field worker, so
        the chart can never delay the weather.
        """
        with self._condition:
            return self._active is not None or self._pending is not None

    def window_fields(self):
        """The resident time window's fields, for the render context.

        The window's images are published once, so a forecast-time change
        inside the window republishes nothing and re-encodes nothing.
        """
        with self._condition:
            state = self._window
            if state is None:
                return ()
            members = set(state.members)
            return tuple(
                field
                for field in self._store.fields()
                if field.identity.window == state.spatial
                and (field.identity.before, field.identity.after) in members
            )

    # ----------------------------------------------------------- camera
    def update(self, camera, reference=None):  # noqa: C901 - coverage transition
        """Adopt a camera and return the portrayal that may be shown.

        ``reference`` is the map pan reference the selection anchors are
        encoded against; a covered pan then resolves the same anchor bytes.

        Returns ``(state, field, selection)``.  ``state`` is ``ready`` only
        when the selection belongs to the field that is actually published.
        The front slot is presented; the back slot is a prepared N+1 that may
        be handed forward without waiting for the worker.
        """
        with self._condition:
            if self._closed:
                return ("unavailable", None, None)
            if self._initialization_failed:
                self._camera = camera
                return ("unavailable", None, None)
            previous = self._camera
            self._camera = camera
            for index, resource in enumerate(self._slots):
                if resource is None:
                    continue
                if self._generation_rejection(resource) is not None:
                    self.counters["generation_rejected"] += 1
                    self._rejected_generations.add(resource.generation)
                    self._slots[index] = None
            candidates = []
            seen = set()

            def add_candidate(field):
                if field is not None and id(field) not in seen:
                    seen.add(id(field))
                    candidates.append(field)

            # The front slot is preferred, then the prepared back slot, then
            # the byte-budgeted store for fields no longer in a slot.
            if self._front_index is not None:
                front = self._slots[self._front_index]
                if front is not None and _covers(front.field, camera):
                    add_candidate(front.field)
            for resource in self._slots:
                if resource is not None and _covers(resource.field, camera):
                    add_candidate(resource.field)
            if self._field is not None and _covers(self._field, camera):
                add_candidate(self._field)
            for field in self._store.fields():
                if _covers(field, camera):
                    add_candidate(field)
            guard = _expanded(camera, REFILL_GUARD_FACTOR)
            field = next(
                (item for item in candidates if _covers(item, guard)),
                candidates[0] if candidates else None,
            )
            if field is not None:
                self._field = field
                self._store.protect(field.identity)
                for index, resource in enumerate(self._slots):
                    if resource is not None and resource.field is field:
                        self._front_index = index
                        break
        if field is not None and not self._selection_enabled:
            # Scalar layers stop at the field: there is no selection to build
            # and no second sampling pass to pay for.
            with self._condition:
                self._field = field
                self._store.acquire(field.identity)
                self._store.protect(field.identity)
                self._state = "ready"
                self._failed_key = None
                self._error = ""
                self._published_generations.add(field.generation)
                self._published_demands.add(self._request_key(camera))
                self._recentre_if_needed(camera, previous, field)
            return ("ready", field, None)
        if field is not None and _covers(field, camera):
            view = camera.view()
            anchor = anchor_view(view, reference)
            anchor_key = (
                anchor._center_lat,
                anchor._center_lon,
                anchor._zoom,
                anchor.width,
                anchor.height,
            )
            cache_key = (
                field.generation,
                field.identity.key(),
                camera.stamp(),
                view.width,
                view.height,
                view._zoom,
                view.density,
                view.over_land,
                view.geographic,
                anchor_key,
            )
            # A reference at another zoom or viewport size cannot place the
            # shared layer, so the atlas is built from the current view.  Key
            # it by the frame that is actually projected, not by the reference
            # that was offered: an incompatible reference must not reuse the
            # atlas of a previous compatible one.
            atlas_key = (
                field.generation,
                anchor._center_lat,
                anchor._center_lon,
                anchor._zoom,
                anchor.width,
                anchor.height,
            )
            atlas = self._anchor_atlas_cache.get(atlas_key)
            if atlas is None:
                try:
                    atlas = node_anchor_atlas(field, anchor)
                except Exception as exc:  # projection metadata escape
                    logger.debug("Resident anchor atlas unavailable: %s", exc)
                    atlas = None
                if atlas is not None:
                    if len(self._anchor_atlas_cache) >= 8:
                        oldest = self._anchor_atlas_cache_order.pop(0)
                        self._anchor_atlas_cache.pop(oldest, None)
                    self._anchor_atlas_cache[atlas_key] = atlas
                    self._anchor_atlas_cache_order.append(atlas_key)
            cached = self._selection_cache.get(cache_key)
            selection = None
            if cached is not None:
                cached_selection, computed_view = cached
                # A covered pan reuses the padded selection and lets the
                # shared layer translate; recompute only when the padding is
                # exhausted.
                if _selection_reusable(computed_view, view):
                    selection = cached_selection
            if selection is None:
                try:
                    selection = select_resident(
                        field,
                        camera.stamp(),
                        view,
                        anchor,
                        anchor_atlas=atlas,
                        margin=SELECTION_REUSE_MARGIN,
                    ).selection
                except Exception as exc:  # coverage or time escape
                    logger.debug("Resident selection unavailable: %s", exc)
                    selection = None
                if selection is not None:
                    if cache_key in self._selection_cache:
                        self._selection_cache[cache_key] = (selection, view)
                    else:
                        if len(self._selection_cache) >= 16:
                            oldest = self._selection_cache_order.pop(0)
                            self._selection_cache.pop(oldest, None)
                        self._selection_cache[cache_key] = (selection, view)
                        self._selection_cache_order.append(cache_key)
            if selection is not None:
                with self._condition:
                    self.counters["covered_reuses"] += 1
                    self._field = field
                    self._store.acquire(field.identity)
                    self._store.protect(field.identity)
                    self._state = "ready"
                    self._failed_key = None
                    self._error = ""
                    self._published_generations.add(field.generation)
                    self._published_demands.add(self._request_key(camera))
                    if previous is None or self._time_key(
                        previous.stamp()
                    ) != self._time_key(camera.stamp()):
                        # A forecast-time change served from a resident field:
                        # no decode, no re-sample and no re-encode.
                        self.counters["time_window_hits"] += 1
                    self._recentre_if_needed(camera, previous, field)
                    self._queue_window_fill(camera, field)
                return ("ready", field, selection)
        with self._condition:
            if self._failed_key == self._request_key(camera):
                self._state = "unavailable"
                return ("unavailable", None, None)
            self.counters["misses"] += 1
            self._state = "loading"
            self._request(camera)
        return ("loading", None, None)

    # ------------------------------------------------------- scheduling
    def _time_key(self, stamp):
        pos = bisect_left(self._events, stamp)
        if pos == 0 or pos == len(self._events) or self._events[pos] == stamp:
            return stamp, stamp
        return self._events[pos - 1], self._events[pos]

    def _request_key(self, camera):
        return (
            field_window(_expanded(camera, RESIDENT_WINDOW_FACTOR).view()),
            self._time_key(camera.stamp()),
            None if camera.over_land else shoreline_quality(camera.view().get_scale()),
        )

    def _generation_rejection(self, resource):
        """Reject a resource whose recorded generation no longer derives."""
        if resource.generation != _generation_key(resource.field):
            return "generation"
        return None

    def _publication_rejection(self, resource, camera):
        """Why this resource may not be presented, or ``None`` if it may."""
        rejection = self._generation_rejection(resource)
        if rejection is not None:
            return rejection
        if camera is None or not _covers(resource.field, camera):
            return "coverage"
        return None

    def _back_index(self):
        """The slot the worker may fill without overwriting the front."""
        if self._front_index is not None:
            return 1 - self._front_index
        for index, resource in enumerate(self._slots):
            if resource is None:
                return index
        return 1

    def _adopt(self, resource):
        """Promote a prepared back-slot resource to the presented front.

        The presented field has a single owner: the render context's thread.
        The worker hands the slot forward and the next camera update adopts
        it, so the published content and the model's content cannot disagree
        while a publication is in flight.
        """
        self._front_index = resource.slot
        self._store.acquire(resource.field.identity)
        self._store.protect(resource.field.identity)
        self._failed_key = None
        self._error = ""
        self._state = "ready"
        self.counters["slot_handoffs"] += 1

    def _request(self, camera):
        """Queue the latest demand; only a replaced pending request is cancelled.

        Only one request is active and one is pending, so at most two are in
        flight.  A pending request is always replaceable.  The active request
        is never cancelled here: a 30 Hz pan or time sweep must not starve the
        single worker, because the completed field is useful to a later demand
        and is retained instead.  Explicit cancellation remains available to
        ``close()`` and to the caller that owns a request's event.

        The resident time window's fill is the one exception: an
        interactive demand takes the worker at once, so the spatial pipeline
        keeps its priority over the fill.
        """
        key = self._request_key(camera)
        self._desired_key = key
        if self._active is not None and self._active[2] == key:
            if self._pending is not None and self._pending[2] != key:
                self._pending[1].set()
                self.counters["cancelled"] += 1
                self._pending = None
            return
        if self._active is not None and len(self._active) > 3:
            if self._active[3] is not None:
                self._active[1].set()
                self.counters["time_window_cancelled"] += 1
        if self._pending is not None:
            if self._pending[2] == key:
                return
            self._pending[1].set()
            self.counters["cancelled"] += 1
            self._pending = None
        cancel = Event()
        self._pending = (camera, cancel, key, None)
        if self._thread is None:
            self._thread = Thread(target=self._run, name="grib-viewport", daemon=True)
            self._thread.start()
        self._condition.notify_all()

    def _recentre_camera(self, camera, previous):
        """The next lattice window ahead of the direction of motion.

        Deterministic: the camera is advanced by one field-lattice step along
        the sign of its movement since the previous update, so the prepared
        window is centred ahead of the camera rather than on the current
        position.  A stationary camera is not re-centred.
        """
        if previous is None:
            return None
        view = camera.view()
        dx = view._lon_to_x(camera.center_lon) - view._lon_to_x(previous.center_lon)
        dy = view._lat_to_y(camera.center_lat) - view._lat_to_y(previous.center_lat)
        step_x = math.copysign(FIELD_LATTICE_PX, dx) if dx else 0.0
        step_y = math.copysign(FIELD_LATTICE_PX, dy) if dy else 0.0
        if not step_x and not step_y:
            return None
        center_lon = (
            view._x_to_lon(view._lon_to_x(camera.center_lon) + step_x)
            if step_x
            else camera.center_lon
        )
        center_lat = (
            view._y_to_lat(view._lat_to_y(camera.center_lat) + step_y)
            if step_y
            else camera.center_lat
        )
        return replace(camera, center_lon=center_lon, center_lat=center_lat)

    def _recentre_if_needed(self, camera, previous, field):
        """Prepare the next window while the current field is still presented.

        Called with the lock held, only for a ready presentation.  The trigger
        is the same guard the scheduler prefers (``REFILL_GUARD_FACTOR``): while
        the presented field still covers the guard, the camera has runway; once
        it does not, one deterministic re-centred window is queued.  The back
        slot is filled by the existing two-slot pipeline, so no third slot and
        no speculative random prefetch is introduced.

        When the camera is spatially stationary, the same idle worker prepares
        exactly one temporal lookahead window instead: the forecast interval
        that follows the presented field.  It uses the same two-slot pipeline
        and is skipped as soon as that window is resident.
        """
        if self._active is not None or self._pending is not None:
            return
        if not _covers(field, _expanded(camera, REFILL_GUARD_FACTOR)):
            target = self._recentre_camera(camera, previous)
            if target is None:
                # Stationary: no spatial window to move to.
                if not self._resident_window:
                    self._look_ahead_time(camera, field)
                return
            if (
                field_window(_expanded(target, RESIDENT_WINDOW_FACTOR).view())
                == field.identity.window
            ):
                return
            key = self._request_key(target)
            if key == self._request_key(camera):
                # The camera has not yet crossed into a new window; nothing to
                # prepare that its own demand would not already ask for.
                return
            if self._active is not None and self._active[2] == key:
                return
            self.counters["proactive_recentres"] += 1
            self._request(target)
            return
        if self._spatially_stationary(camera, previous):
            if not self._resident_window:
                # The resident time window supersedes the single-step
                # lookahead: it holds every forecast interval of the horizon
                # over the same spatial window.
                self._look_ahead_time(camera, field)

    @staticmethod
    def _spatially_stationary(camera, previous):
        if previous is None:
            return False
        return (
            camera.center_lon,
            camera.center_lat,
            camera.zoom,
            camera.width,
            camera.height,
        ) == (
            previous.center_lon,
            previous.center_lat,
            previous.zoom,
            previous.width,
            previous.height,
        )

    def _next_time_camera(self, camera, field):
        """The camera for the forecast interval after ``field``, or None."""
        after = field.identity.after
        events = self._events
        index = bisect_left(events, after)
        if index < len(events) and events[index] == after:
            index += 1
        if index >= len(events):
            return None
        following = events[index]
        gap = following - after
        if gap <= 0:
            return None
        stamp = after + min(1.0, gap / 2.0)
        moment = (
            datetime.fromtimestamp(stamp)
            if camera.time.tzinfo is None
            else datetime.fromtimestamp(stamp, timezone.utc)
        )
        return replace(camera, time=moment)

    def _look_ahead_time(self, camera, field):
        """Prepare the single next forecast window for a stationary camera.

        Deterministic N+1 only: the interval that starts at the presented
        field's ``after`` boundary.  The existing two-slot pipeline carries it,
        and a prepared window stays resident for the demand that follows; this
        is not #67-style time residency.
        """
        target = self._next_time_camera(camera, field)
        if target is None:
            return
        key = self._request_key(target)
        if key == self._request_key(camera):
            return
        for resource in self._slots:
            if resource is not None and _covers(resource.field, target):
                return
        for resident in self._store.fields():
            if _covers(resident, target):
                return
        self.counters["time_lookaheads"] += 1
        self._request(target)

    # --------------------------------------------------- resident window
    def _time_members(self, anchor):
        """The demand windows of the declared horizon, nearest first.

        The horizon is the recorded nine-hour sweep: the forecast instants
        from ``anchor`` to ``anchor + RESIDENT_TIME_WINDOW_HOURS``.  An instant
        at a selection event is served by a singleton field and an instant
        between two events by the bracket field, so the window holds one
        member per event instant and one per interval.
        """
        events = self._events
        index = bisect_left(events, anchor)
        if index >= len(events) or events[index] != anchor:
            return ((anchor, anchor),)
        horizon = anchor + RESIDENT_TIME_WINDOW_HOURS * 3600.0
        members = [(anchor, anchor)]
        position = index
        while position + 1 < len(events):
            following = events[position + 1]
            if following > horizon:
                break
            members.append((events[position], following))
            position += 1
            if events[position] < horizon:
                members.append((events[position], events[position]))
        return tuple(members)

    def _window_state(self, camera, field):
        """The resident time window that serves the presented field.

        The window is anchored on the demand's interval start and keeps that
        anchor while the demand stays inside the declared horizon, so a
        forecast-time change inside the window does not re-anchor it.  A
        different spatial window starts a new window.
        """
        if not self._resident_window or field is None:
            return None
        before, _after = self._time_key(camera.stamp())
        state = self._window
        horizon = RESIDENT_TIME_WINDOW_HOURS - RESIDENT_TIME_LOOKAHEAD_HOURS
        if (
            state is None
            or state.spatial != field.identity.window
            or not state.anchor <= camera.stamp() <= state.anchor + horizon * 3600.0
        ):
            state = ResidentTimeWindow(
                spatial=field.identity.window,
                anchor=before,
                members=self._time_members(before),
            )
            self._window = state
            self._window_started = False
        return state

    def _resident_identity(self, identity):
        """Whether a field with this identity is already resident."""
        for resource in self._slots:
            if resource is not None and resource.field.identity == identity:
                return True
        return any(item.identity == identity for item in self._store.fields())

    @staticmethod
    def _member_moment(before, after):
        """The instant inside a demand window that selects it."""
        if after <= before:
            return before
        return before + min(1.0, (after - before) / 2.0)

    def _queue_window_fill(self, camera, field):
        """Queue the next missing member of the resident time window.

        The fill is the lowest-priority work the worker does.  It runs only
        while the interactive pipeline is idle, and only while the camera is
        inside the presented field's prepared window, so no spatial
        preparation is imminent and the pan pipeline keeps its priority.  The
        fill starts where the presented field was prepared, so a moving
        camera never claims the worker, and an interactive demand cancels it.
        """
        if not self._resident_window:
            return False
        if camera is None or field is None:
            return False
        if self._active is not None or self._pending is not None:
            # The interactive pipeline has the worker; the fill waits.
            return False
        state = self._window_state(camera, field)
        if state is None or state.spatial != field.identity.window:
            return False
        requirement = field_window(_expanded(camera, RESIDENT_WINDOW_FACTOR).view())
        if not _contains(field.identity.window, requirement):
            self.counters["time_window_blocked"] += 1
            return False
        if not self._window_started:
            # The fill starts where the presented field was prepared: a camera
            # that moved away from it needs a spatial field first.
            if self._prepared_by.get(field.identity.key()) != _spatial_state(camera):
                self.counters["time_window_blocked"] += 1
                return False
            self._window_started = True
        for before, after in state.members:
            identity = replace(field.identity, before=before, after=after)
            if self._resident_identity(identity):
                continue
            target = {
                "window": field.identity.window,
                "geometry": field,
                "resolution": field.identity.resolution,
                "member": (before, after),
            }
            fill_camera = replace(
                camera,
                time=_instant(camera, self._member_moment(before, after)),
                zoom=(camera.zoom if field.land_zoom is None else field.land_zoom),
            )
            key = (
                target["window"],
                (before, after),
                field.identity.resolution[1],
            )
            self.counters["time_window_fills"] += 1
            self._pending = (fill_camera, Event(), key, target)
            self._condition.notify_all()
            return True
        return False

    def observations(self):
        """Preparation spans and publication-rejection evidence for the gate."""
        with self._condition:
            memo = {} if self._memo is None else self._memo.statistics()
            return {
                "preparation_spans": tuple(self._preparation_spans),
                "published_generations": tuple(sorted(self._published_generations)),
                "rejected_generations": tuple(sorted(self._rejected_generations)),
                "published_demands": tuple(sorted(self._published_demands, key=repr)),
                "rejected_demands": tuple(sorted(self._rejected_demands, key=repr)),
                "generation_rejected": self.counters["generation_rejected"],
                "superseded_rejected": self.counters["stale_rejected"],
                "superseded_retained": self.counters["superseded_retained"],
                "time_lookaheads": self.counters["time_lookaheads"],
                "time_window_hits": self.counters["time_window_hits"],
                "time_window_fills": self.counters["time_window_fills"],
                "time_window_fields": self.counters["time_window_fields"],
                "time_window_cancelled": self.counters["time_window_cancelled"],
                "time_window_blocked": self.counters["time_window_blocked"],
                "resident_window": (
                    None
                    if self._window is None
                    else {
                        "spatial": self._window.spatial,
                        "anchor": self._window.anchor,
                        "members": self._window.members,
                    }
                ),
                "slot_handoffs": self.counters["slot_handoffs"],
                "slots_filled": sum(resource is not None for resource in self._slots),
                "front_slot": self._front_index,
                "sampling_memo": memo,
            }

    def _run(self):  # noqa: C901 - keep worker cleanup and request state together
        sources: list[Any] = []
        try:
            from sailworks.core.grib import Grib
            from sailworks.core.gribsampling import WeatherSampler
            from sailworks.rendering.windrenderer import WindLandMask
            from sailworks.rendering.weathercache import (
                DecodedBandCache,
                DisplayWeatherSource,
            )

            bands = DecodedBandCache(128 * 1024 * 1024)
            with self._condition:
                self._bands = bands
            for path in self.paths:
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
                        display_identity=self.display_identity,
                    )
                )
            # Opening/indexing must never happen while holding the caller's lock.
            sampler = WeatherSampler(sources)
            if len(sampler.indices) != len(sources) or not sources:
                raise ValueError("A selected forecast is unavailable")
            self._sources = tuple(sources)
            self._sampler = sampler
            self._memo = NodeSampleMemo()
            self._land = self._land_mask or WindLandMask()
            with self._condition:
                self._events = tuple(
                    sorted(
                        {
                            event
                            for identity in sampler.order
                            for event in (
                                identity[0],
                                sampler.coverage[identity],
                                *sampler._times[identity, self._parameter],
                            )
                        }
                    )
                )
            while True:
                with self._condition:
                    if self._closed:
                        return
                    while self._pending is None and not self._closed:
                        # The worker is idle: the resident time window may
                        # claim it, and it only does so while the camera is
                        # inside the presented field's window.
                        if self._queue_window_fill(self._camera, self._field):
                            break
                        self._condition.wait()
                    if self._closed:
                        return
                    request = cast(tuple, self._pending)
                    self._pending = None
                    self._active = request
                    self._prepare_target = request[3] if len(request) > 3 else None
                camera, cancel, key = request[0], request[1], request[2]
                target = request[3] if len(request) > 3 else None
                began = time.perf_counter()
                error = ""
                try:
                    field = self._prepare(camera, cancel)
                except Exception as exc:
                    field, error = None, str(exc)
                ended = time.perf_counter()
                with self._condition:
                    self._active = None
                    if self._closed:
                        return
                    self._preparation_spans.append((began, ended))
                    if len(self._preparation_spans) > 1024:
                        del self._preparation_spans[
                            : len(self._preparation_spans) - 1024
                        ]
                    notify = False
                    if field is None:
                        if cancel.is_set() or key != self._desired_key:
                            self.counters["stale_rejected"] += 1
                            self._rejected_demands.add(key)
                        elif error:
                            self._failed_key, self._error = key, error
                            self._state = "unavailable"
                            notify = True
                    else:
                        resource = PreparedResource(
                            field=field,
                            generation=_generation_key(field),
                            demand=key,
                            slot=-1,
                            span=(began, ended),
                        )
                        if cancel.is_set():
                            # Explicitly cancelled: it may never publish or be
                            # retained as if it were merely superseded.
                            self.counters["stale_rejected"] += 1
                            self._rejected_generations.add(resource.generation)
                            self._rejected_demands.add(key)
                        elif self._generation_rejection(resource) is not None:
                            self.counters["generation_rejected"] += 1
                            self._rejected_generations.add(resource.generation)
                            self._rejected_demands.add(key)
                        else:
                            slot = self._back_index()
                            stored = replace(resource, slot=slot)
                            self._slots[slot] = stored
                            if target is not None:
                                # A resident time window member: it is retained
                                # for the forecast-time change that will ask
                                # for it, and it never publishes by itself.
                                self.counters["time_window_fields"] += 1
                            if key != self._desired_key:
                                # A newer demand replaced this one while it was
                                # preparing.  The field is valid and useful to a
                                # later update: retain it in the back slot and
                                # the store, but never adopt or publish it now.
                                self.counters["superseded_retained"] += 1
                            elif (
                                self._camera is not None
                                and self._publication_rejection(stored, self._camera)
                                is None
                            ):
                                self._adopt(stored)
                                notify = True
                    callback = self.on_change if notify else None
                if callback is not None:
                    callback()
        except Exception as exc:
            with self._condition:
                self._error = str(exc)
                self._failed_key = self._desired_key
                self._state = "unavailable"
                # Opening an empty/missing source can fail before the request
                # loop consumes its first item. It must not remain "busy"
                # forever and prevent an independent chart from preparing.
                self._active = None
                self._pending = None
                self._initialization_failed = True
                callback = None if self._closed else self.on_change
            if callback is not None:
                callback()
        finally:
            for source in sources:
                source.close()
            with self._condition:
                self._sources, self._sampler, self._land = (), None, None
                self._memo = None
                self._bands = None
                self._store.clear()
                self._condition.notify_all()

    def _prepare(self, camera, cancel):  # noqa: C901 - bounded field/window staging
        """Prepare the geographic field of the camera's window.

        A resident time window member is prepared over the presented field's
        own spatial window: the target carries that window, that field's
        geometry and that field's resolution, so the member shares the
        spatial preparation instead of invalidating it.
        """
        if cancel.is_set():
            return None
        try:
            view = camera.view()
            with self._condition:
                target = self._prepare_target
                if target is not None:
                    geometry = target["geometry"]
                    window = target["window"]
                else:
                    geometry = next(
                        (
                            field
                            for field in reversed(self._store.fields())
                            if _spatially_covers(
                                field, _expanded(camera, REFILL_GUARD_FACTOR)
                            )
                        ),
                        None,
                    )
                    window = (
                        geometry.identity.window
                        if geometry is not None
                        else field_window(
                            _expanded(camera, RESIDENT_WINDOW_FACTOR).view()
                        )
                    )
            source = tuple(_source_key(item) for item in self._sources)
            before, after = time_window(self._sampler, camera.stamp(), self._parameter)
            if target is not None:
                resolution = target["resolution"]
            else:
                resolution = (
                    tuple(
                        sorted(
                            {
                                index.nominal_step_degrees
                                for index in self._sampler.indices
                            }
                        )
                    ),
                    None if camera.over_land else shoreline_quality(view.get_scale()),
                    self._grid,
                )
            identity = FieldIdentity(
                source=source,
                parameter=self._parameter,
                before=before,
                after=after,
                window=window,
                resolution=resolution,
            )
            with self._condition:
                resident = self._store.acquire(identity)
                if resident is None:
                    # The camera that prepared this field: the resident time
                    # window starts its fill where the field was prepared.
                    self._prepared_by[identity.key()] = _spatial_state(camera)
            if resident is not None:
                # The field is already resident: no re-decode, no re-sample.
                return resident
            field = stage_field(
                self._sampler,
                camera.stamp(),
                window,
                identity,
                cancel.is_set,
                view=view,
                land_mask=self._land,
                geometry=geometry,
                memo=self._memo,
                grid=self._grid,
                parameter=self._parameter,
                components=self._components,
            )
        except Exception as exc:
            raise ValueError(f"GRIB field preparation failed: {exc}") from exc
        if field is None:
            return None
        with self._condition:
            self.counters["field_preparations"] += 1
            if self._field is not None:
                self._store.protect(self._field.identity)
            if not self._store.store(field, protect=self._field is None):
                # The field is valid and may be shown; it is simply too large
                # to retain, so the next demand prepares it again.
                logger.debug("Prepared field exceeds the retention budget")
        return field

    # ------------------------------------------------------------ close
    def close(self):
        """Stop the worker; a pending preparation is released, not waited for."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._state = "unavailable"
            self.on_change = None
            if self._pending is not None:
                self._pending[1].set()
                self.counters["cancelled"] += 1
                self._pending = None
            if self._active is not None:
                self._active[1].set()
                self.counters["cancelled"] += 1
            self._field = None
            self._slots = [None, None]
            self._front_index = None
            self._condition.notify_all()
        # Never join GDAL or close its handles from the GUI thread. The worker
        # releases them in finally, even when it completes after this method.


def _source_key(source):
    """The identity of one forecast file, for the field's source signature."""
    try:
        stat = os.stat(source.path)
        version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except OSError:
        version = None
    return (
        source.path,
        source.name,
        source.centre,
        source.start_time,
        source.last_forecast,
        getattr(source, "display_identity", None),
        version,
    )


def _connect_signal(item, name, slot):
    """Connect ``slot`` to ``name`` when the item declares that signal.

    A QML ``ShaderEffect`` declares ``shaderErrorChanged`` and
    ``sourceErrorChanged``; the object a scene yields can still be a plain
    ``QQuickItem``, where a generic connect raises.  Ask the meta-object
    first, so a scene without the signal is skipped rather than taking the
    viewport down.  Returns whether the connection was made.
    """
    signal = getattr(item, name, None)
    if signal is None:
        return False
    signal.connect(slot)
    return True


def _grid_rect(view, lattice):
    """The gridded field's rectangle in the scene's pixels.

    The fill's shader interpolates its texture linearly across the item it
    draws on, so the item *is* the lattice's rectangle: the texture
    coordinate of a fragment is then the fragment's position in the
    viewport's projection, and the shader's bilinear is a geographic
    interpolation of the lattice.

    The lattice's longitudes are taken into the branch of the global grid
    that is nearest the camera, so a window across the antimeridian is drawn
    once and not split.
    """
    from PySide6.QtCore import QRectF

    (south, west), (north, east) = lattice.bounds()
    shift = 360.0 * round((view._center_lon - west) / 360.0)
    west += shift
    east += shift
    width = view.get_allocated_width()
    height = view.get_allocated_height()
    x = view._lon_to_x(west) - view._lon_to_x(view._center_lon) + width / 2.0
    y = view._lat_to_y(north) - view._lat_to_y(view._center_lat) + height / 2.0
    span = view._lon_to_x(west + 1.0) - view._lon_to_x(west)
    span_y = view._lat_to_y(south) - view._lat_to_y(north)
    return QRectF(x, y, (east - west) * span, span_y)


def chart_withdrawn_on_camera_change(previous, camera, published):
    """Whether a camera change must hide the retained chart image.

    Only a zoom or forecast-time change makes the image wrong content; a pan
    keeps the same scale and instant, so the image is shown at its shifted
    position until its replacement is composed.
    """
    if not published or previous is None:
        return False
    return previous.zoom != camera.zoom or previous.stamp() != camera.stamp()


# Wave height and wave arrows are optional layers.  Scenes, reduced viewports
# and test harnesses that predate them carry none of the wave members, so each
# wave entry point resolves the absent ones to the inert value the layer starts
# from and degrades to a no-op, exactly as the wind path tolerates a partial
# viewport.  A real ``GribViewport`` initialises every one of them, so the
# resolution is a no-op there.
_WAVE_MEMBER_DEFAULTS = {
    "_wave_visible": False,
    "_wave_opacity": 0.85,
    "_wave_gradient_smooth": True,
    "_wave_gradient_levels": 9,
    "_wave_model": None,
    "_wave_fill": None,
    "_wave_fill_back": None,
    "_wave_state": "unavailable",
    "_wave_error": "",
    "_wave_token": None,
    "_wave_status": None,
    "_wave_arrows_visible": False,
    "_wave_arrow_density": "standard",
    "_wave_arrow_models": {},
    "_wave_barbs": None,
    "_wave_barbs_back": None,
    "_wave_barbs_token": None,
    "_wave_arrows_state": "hidden",
    "_wave_arrows_error": "",
}


def _prepare_wave_layer(viewport):
    """Install the inert wave members an incomplete viewport lacks.

    ``__init__`` and the QML binding pass set all of them on a real viewport,
    so this returns immediately there.  A reduced scene gets the defaults
    instead of an ``AttributeError``; it then stages nothing because it has no
    fill or arrow slots, which is the documented optional-layer behaviour.
    """
    if all(hasattr(viewport, name) for name in _WAVE_MEMBER_DEFAULTS):
        return
    for name, default in _WAVE_MEMBER_DEFAULTS.items():
        if hasattr(viewport, name):
            continue
        if isinstance(default, dict):
            default = dict(default)
        setattr(viewport, name, default)


def _viewport_widget_class():  # noqa: C901 - lazy factory includes Qt event methods
    """Build the render-context widget class on demand.

    Qt is imported lazily so the model and the tests stay usable headless.
    """
    from PySide6.QtCore import QEvent, QObject, QRectF, QTimer, QUrl, Qt, Signal
    from PySide6.QtGui import QSurfaceFormat, QVector2D
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QLabel
    import shiboken6

    from sailworks.qt.gpuweather import WeatherImageProvider
    from sailworks.qt.gribviewportchart import (
        CHART_PALETTE,
        CHART_IDLE_MS,
        CHART_INTERACTIVE_MS,
        ViewportChartLayer,
        chart_image_provider,
        chart_placement,
    )

    class GribViewport(QQuickWidget):
        """The viewport's render context: the GPU side of the split.

        The field's endpoint images are published once and stay resident; the
        current-view selection is published for every view.  Only a selection
        that belongs to the published field is shown as current.

        The numerical fill is an optional second layer behind the barbs.  It
        is off by default: the default viewport path loads the barbs' own
        component and nothing else.  When it is on, the two layers have
        separate inputs, separate publication paths and separate counters, so
        a change to one layer never restages the other.

        The chart layer is an optional third layer behind both.  It is off by
        default and, when it is on, it is an *independent generation*: it has
        its own worker, its own state and its own cancel event, it is
        published after the weather is already presented, and adding it
        resets neither the weather nor the camera nor the forecast time.
        """

        prepared = Signal(int)
        cameraChanged = Signal(object)  # noqa: N815 - Qt signal name
        stateChanged = Signal(str)  # noqa: N815 - Qt signal name
        chartPublished = Signal(object)  # noqa: N815 - Qt signal name
        chartStateChanged = Signal(str)  # noqa: N815 - Qt signal name
        nativeError = Signal(str)  # noqa: N815 - Qt signal name
        streamlinesPrepared = Signal()  # noqa: N815
        wavePrepared = Signal()  # noqa: N815
        marineStateChanged = Signal(str)  # noqa: N815
        worldRendered = Signal(object, str)  # noqa: N815
        layerVisibilityChanged = Signal(str, bool)  # noqa: N815

        def __init__(
            self,
            model,
            parent=None,
            gpu_weather_fill=False,
            chart_layer=False,
            chart_data_dir=None,
            native_weather_fields=None,
            native_weather_budget=None,
            enc_chart_layer=False,
            chart_without_weather=False,
            chart_palette=CHART_PALETTE,
            enc_only=False,
            gpu_streamlines=True,
            satellite_visible=False,
            satellite_pack="",
            basemap_visible=True,
            enc_visible=True,
        ):
            super().__init__(parent)
            self._model = model
            self._gpu_streamlines_enabled = True
            self._gpu_streamlines_error = ""
            self._gpu_streamlines_item: Any = None
            self._source_generation = 0
            self._disposed = False
            self._wind_opacity = 1.0
            self._wind_gradient_enabled = True
            self._wind_gradient_opacity = 0.12
            self._wind_style = "barbs"
            # Wave height is a separate scalar layer: its own model (a
            # one-component ``wave_height`` field), its own QML fill and its
            # own gradient settings. Hidden until the panel asks for it.
            self._wave_visible = False
            self._wave_opacity = 0.85
            self._wave_gradient_smooth = True
            self._wave_gradient_levels = 9
            self._wave_model = None
            self._wave_fill = None
            self._wave_state = "unavailable"
            self._wave_error = ""
            # Wave direction arrows are a separate glyph layer, staged from
            # the wave bands and never from the wind field: they follow the
            # wave layer's visibility, own their density and publication
            # (LayerRole.WAVE_BARBS) and leave the wind barbs untouched.
            self._wave_arrows_visible = False
            self._wave_arrow_density = "standard"
            # Three scalar workers, one per wave band.  Height, period and
            # direction are staged and published independently of the wind
            # field and of the wave fill's own height worker.
            self._wave_arrow_models = {}
            self._wave_barbs = None
            self._wave_barbs_back = None
            self._wave_barbs_token = None
            self._wave_arrows_state = "hidden"
            self._wave_arrows_error = ""
            self._camera = None
            self._weather_display_camera = None
            self._weather_display_dpr = None
            self._drag = None
            self._drag_velocity = DragVelocity()
            self._pan_inertia = None
            self._pan_inertia_last_tick = None
            self._pan_inertia_enabled = True
            self._pan_inertia_tick = QTimer(self)
            self._pan_inertia_tick.setInterval(16)
            self._pan_inertia_tick.timeout.connect(self._advance_pan_inertia)
            self._wheel_zoom_target = None
            self._wheel_zoom_anchor = None
            self._wheel_zoom_last_tick = None
            self._wheel_trackpad_panning = False
            self._smooth_zoom = True
            self._wheel_zoom_tick = QTimer(self)
            self._wheel_zoom_tick.setInterval(16)
            self._wheel_zoom_tick.timeout.connect(self._advance_wheel_zoom)
            self._wheel_zoom_settle = QTimer(self)
            self._wheel_zoom_settle.setSingleShot(True)
            self._wheel_zoom_settle.timeout.connect(self._finish_wheel_zoom)
            self._wheel_scroll_settle = QTimer(self)
            self._wheel_scroll_settle.setSingleShot(True)
            self._wheel_scroll_settle.timeout.connect(
                lambda: setattr(self, "_wheel_trackpad_panning", False)
            )
            self._state = "loading"
            self._presentation_spans = []
            self._gpu_weather_fill = bool(gpu_weather_fill)
            self._chart_layer = bool(chart_layer)
            self._chart_without_weather = bool(chart_without_weather)
            from .gpuweathertextures import native_weather_fields_enabled

            self._native_weather_fields = native_weather_fields_enabled(
                native_weather_fields
            )
            self._native_budget = native_weather_budget
            self._native_contexts = {}
            self._native_bindings = {}
            self._native_requests = {}
            self._native_images = {}
            self._native_publication_tokens = {}
            self._native_error = ""
            # The ENC cells are an experimental addition to the chart layer.
            # They are off by default, and they never turn on by themselves.
            self._enc_chart_layer = bool(enc_chart_layer) and self._chart_layer
            self._chart_palette = chart_palette
            self._environment_provider = None
            self._environment = None
            self._environment_key = None
            self._base_provider = None
            self._base = None
            self._base_key = None
            self._world_provider = None
            self._world = None
            self._world_placement = None
            self._chart_data_dir = chart_data_dir
            # Satellite imagery is an optional raster layer below the chart.
            # The worker exists only while the layer is on.
            self._satellite_visible = bool(satellite_visible)
            self._satellite_pack = satellite_pack or ""
            self._satellite = None
            self._satellite_provider = None
            self._satellite_worker = None
            self._satellite_timer = None
            self._satellite_generation = 0
            self._satellite_camera = None
            self._satellite_pending_camera = None
            self._satellite_source_camera = None
            self._satellite_placement = None
            self._satellite_source = None
            self._satellite_base_source = None
            self._satellite_attribution = ""
            self._satellite_error = ""
            self._satellite_fatal = False
            self._satellite_retry_at = 0.0
            self._satellite_network = None
            self._satellite_front_slot = 0
            self._satellite_pending_token = None
            self._satellite_token = 0
            self._satellite_status_text = ""
            self._satellite_visible_generation = None
            format_ = QSurfaceFormat()
            format_.setSamples(4)
            format_.setAlphaBufferSize(8)
            self.setFormat(format_)
            self.setClearColor(Qt.GlobalColor.transparent)
            self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
            self._provider = WeatherImageProvider()
            self.engine().addImageProvider("weather", self._provider)
            from .viewportstreamlines import StreamlineImageProvider

            self._streamline_provider = StreamlineImageProvider()
            self.engine().addImageProvider("streamlines", self._streamline_provider)
            self._chart_provider = None
            self._navigation_provider = None
            self._dashboard_provider = None
            self._chart_model = None
            self._chart = None
            self._route_overlay = None
            self._route_content_key = None
            self._route_pan_window = None
            self._map_content = None
            self._map_pan_reference = None
            self._camera_dpr = None
            self._map_anchor_camera = None
            self._map_pan_window = None
            self._base_placement = None
            self._environment_placement = None
            self._base_source_camera = None
            self._environment_source_camera = None
            self._base_preview_hidden = False
            self._environment_preview_hidden = False
            self._fill_placements = {}
            self._navigation_overlay = None
            self._navigation_key = None
            self._navigation_target = None
            self._dashboard_overlay = None
            self._dashboard_key = None
            self._chart_state = "disabled"
            self._chart_viewport = None
            self._chart_camera = None
            self._chart_composition_camera = None
            self._chart_composition_dpr = None
            self._chart_camera_changed = time.perf_counter()
            self._chart_interactive_at = None
            self._chart_ready_at = None
            self._chart_request_state = None
            self._chart_publish_state = None
            self._chart_pending = None
            self._chart_attempt = 0
            self._chart_redelivery_generation = None
            self._chart_budget_refusal = None
            self._chart_visible_generation = None
            self._chart_front_slot = 0
            self._chart_loading_shown = False
            self._chart_status_item = None
            self._weather_ready_at = None
            self._chart_requested_after_weather = False
            if self._chart_layer:
                self._chart_provider = chart_image_provider()
                self.engine().addImageProvider("chart", self._chart_provider)
                self._navigation_provider = chart_image_provider("navigation")
                self.engine().addImageProvider("navigation", self._navigation_provider)
                self._dashboard_provider = chart_image_provider("dashboard")
                self.engine().addImageProvider("dashboard", self._dashboard_provider)
            shaders = Path(__file__).parent / "shaders"
            if self._chart_layer:
                source = shaders / "ViewportChartScene.qml"
            elif self._gpu_weather_fill:
                source = shaders / "WeatherScene.qml"
            else:
                source = shaders / (
                    "WeatherBufferedBarbs.qml"
                    if self._native_weather_fields
                    else "WeatherBarbs.qml"
                )
            self.setSource(QUrl.fromLocalFile(str(source)))
            self._weather: Any = self.rootObject()
            if self._weather is None:
                raise RuntimeError("The GRIB viewport could not load its shader")
            if self._chart_layer:
                self._map_content = self._weather.findChild(QObject, "mapContent")
            if self._gpu_streamlines_enabled:
                try:
                    from .gpustreamlines import GpuStreamlineItem

                    self._gpu_streamlines_item = GpuStreamlineItem(self._weather)
                    self._gpu_streamlines_item.setParentItem(self._weather)
                    self._gpu_streamlines_item.attach_window(self.quickWindow())
                    self._gpu_streamlines_item.setZ(1000)
                    self._gpu_streamlines_item.gpuError.connect(
                        self._gpu_streamlines_failed,
                        Qt.ConnectionType.QueuedConnection,
                    )
                except Exception as exc:
                    logger.warning("GPU streamline initialization failed: %s", exc)
                    self._gpu_streamlines_error = f"GPU streamlines unavailable: {exc}"
            from .marinequick import MarineQuickLayer
            from .icoadsquick import IcoadsQuickLayer

            marine_parent = self._weather
            if self._chart_layer:
                chart_weather_band = self._weather.findChild(QObject, "weatherBand")
                if chart_weather_band is not None:
                    marine_parent = chart_weather_band.findChild(QObject, "marineBand")
                    if marine_parent is None:
                        marine_parent = chart_weather_band

            self._marine_layer = MarineQuickLayer(marine_parent, self)
            self._marine_layer.stateChanged.connect(self.marineStateChanged.emit)
            self._icoads_layer = IcoadsQuickLayer(marine_parent, self)

            self._barbs = self._weather
            self._fill = cast(
                Any,
                self._weather.findChild(QObject, "weatherFill"),
            )
            if (
                self._gpu_weather_fill
                or self._chart_layer
                or self._native_weather_fields
            ):
                # The layered scenes carry the barbs as their own item.
                self._barbs = cast(
                    Any,
                    self._weather.findChild(QObject, "weatherBarbs"),
                )
                if self._barbs is None:
                    raise RuntimeError(
                        "The GRIB viewport could not load its weather scene"
                    )
            if self._gpu_weather_fill:
                if self._fill is None:
                    raise RuntimeError(
                        "The GRIB viewport could not load its weather scene"
                    )
            if self._fill is not None:
                self._weather.setProperty("fillEnabled", False)
            self._barbs_back = (
                self._weather.findChild(QObject, "weatherBarbsBack")
                if self._native_weather_fields
                else None
            )
            self._fill_back = (
                self._weather.findChild(QObject, "weatherFillBack")
                if self._fill is not None
                else None
            )
            self._wave_fill = cast(Any, self._weather.findChild(QObject, "waveFill"))
            self._wave_fill_back = (
                cast(Any, self._weather.findChild(QObject, "waveFillBack"))
                if self._wave_fill is not None
                else None
            )
            # The wave arrow layer is its own pair of QML items.  It reuses
            # the scene's shared anchor atlas exactly as the wind barbs do.
            self._wave_barbs = cast(Any, self._weather.findChild(QObject, "waveBarbs"))
            self._wave_barbs_back = (
                cast(Any, self._weather.findChild(QObject, "waveBarbsBack"))
                if self._wave_barbs is not None
                else None
            )
            # Scenes without the image fill (the barbs' own scenes) carry an
            # explicit wave status item instead of a fill, so "waves cannot be
            # drawn here" is stated rather than leaving a silent blank layer.
            self._wave_status = cast(
                Any, self._weather.findChild(QObject, "waveStatus")
            )
            self._wave_token = None
            # The scene may own the node-anchor textures and hand the same
            # Image instances to the front and back barbs.  Two slots let a
            # pending frame load beside the one on screen, and a slot's pair
            # is never rewritten while the front layer displays it.
            self._shared_anchor_items = self._find_shared_anchor_items()
            self._shared_anchor_urls = [None, None]
            self._shared_anchor_front = None
            self._shared_anchor_pending = None
            self._fill_staging = False
            self._image_fill_token = None
            self._image_fill_pending_epoch = None
            self._image_fill_failed = None
            self._image_fill_requested_key = None
            self._image_weather_visible = False
            self._native_front_index = 0
            self._native_active_camera = None
            self._native_pending_camera = None
            self._native_active_identity = self._native_pending_identity = None
            self._native_active_dpr = None
            self._native_pending_dpr = None
            self._barb_zoom_preview = None
            self._native_publishing = False
            self._native_pending_selection_urls = ()
            self._native_active_selection = None
            self._native_pending_selection = None
            if self._chart_layer:
                self._chart = self._weather.findChild(QObject, "chartLayer")
                self._chart_status_item = self._weather.findChild(
                    QObject, "chartStatus"
                )
                if self._chart is None or self._chart_status_item is None:
                    raise RuntimeError(
                        "The GRIB viewport could not load its chart scene"
                    )
                getattr(self._chart, "slotResult").connect(
                    self._chart_slot_result, Qt.ConnectionType.QueuedConnection
                )
                self._chart_model = ViewportChartLayer(
                    paths=model.paths,
                    data_dir=chart_data_dir,
                    palette=self._chart_palette,
                    worker_idle=lambda: not model.busy(),
                    current_camera=lambda: self._camera,
                    on_publish=self.chartPublished.emit,
                    on_state=self.chartStateChanged.emit,
                    enc_enabled=False,
                    dpr=self.devicePixelRatio(),
                    enc_only=enc_only,
                )
                self._route_overlay = self._weather.findChild(QObject, "routeOverlay")
                self._navigation_overlay = self._weather.findChild(
                    QObject, "navigationOverlay"
                )
                self._dashboard_overlay = self._weather.findChild(
                    QObject, "dashboardOverlay"
                )
                if enc_only:
                    self._base_provider = chart_image_provider("base")
                    self.engine().addImageProvider("base", self._base_provider)
                    self._base = self._weather.findChild(QObject, "baseLayer")
                # The underlay belongs to every mode: the composed chart layer
                # covers the whole camera, so the world has to sit beneath it.
                self._world_provider = chart_image_provider("world")
                self.engine().addImageProvider("world", self._world_provider)
                self._world = self._weather.findChild(QObject, "worldLayer")
                self.worldRendered.connect(
                    self._world_rendered, Qt.ConnectionType.QueuedConnection
                )
                self._satellite = self._weather.findChild(QObject, "satelliteLayer")
                self._satellite_provider = chart_image_provider("satellite")
                self.engine().addImageProvider("satellite", self._satellite_provider)
                self.cameraChanged.connect(self._satellite_camera_changed)
                getattr(self._satellite, "slotResult").connect(
                    self._satellite_slot_result
                )
                self._satellite_timer = QTimer(self)
                self._satellite_timer.setSingleShot(True)
                self._satellite_timer.setInterval(220)
                self._satellite_timer.timeout.connect(self._request_satellite)
                self._satellite_retry_timer = QTimer(self)
                self._satellite_retry_timer.setSingleShot(True)
                self._satellite_retry_timer.timeout.connect(
                    lambda: self._satellite_camera_changed(self._camera)
                )
                try:
                    from PySide6.QtNetwork import QNetworkInformation

                    if QNetworkInformation.loadDefaultBackend():
                        self._satellite_network = QNetworkInformation.instance()
                        self._satellite_network.reachabilityChanged.connect(
                            self._satellite_reachability_changed
                        )
                except Exception:  # noqa: BLE001 - reachability is advisory
                    self._satellite_network = None
                self._weather.setProperty("satelliteVisible", self._satellite_visible)
                # The vector world underlay is the basemap; the composed chart
                # layer carries the ENC portrayal. They are distinct items.
                self._base_layer_item = self._weather.findChild(QObject, "worldLayer")
                self._layer_basemap_visible = bool(basemap_visible)
                self._layer_enc_visible = bool(enc_visible)
                self._weather.setProperty("basemapVisible", self._layer_basemap_visible)
                self._weather.setProperty("encVisible", self._layer_enc_visible)
                if self._base_layer_item is not None:
                    self._base_layer_item.setProperty(
                        "visible", self._layer_basemap_visible
                    )
                if self._chart is not None:
                    self._chart.setProperty("visible", self._layer_enc_visible)
                getattr(self._weather, "layerToggled").connect(self._layer_toggled)
                if self._satellite_visible:
                    self._satellite_camera = self._camera
                    self._start_satellite()
                world_factor = max(1, round(self.devicePixelRatio()))

                def render_world():
                    started = time.perf_counter()
                    try:
                        image = _render_world(
                            chart_data_dir, self._chart_palette, world_factor
                        )
                        logger.info(
                            "World underlay rendered in %.3f s",
                            time.perf_counter() - started,
                        )
                        self.worldRendered.emit(image, "")
                    except Exception as exc:
                        logger.warning("World underlay unavailable: %s", exc)
                        try:
                            self.worldRendered.emit(None, str(exc))
                        except RuntimeError:
                            pass

                Thread(target=render_world, name="world-underlay", daemon=True).start()
                if self._chart_layer:
                    self._environment_provider = chart_image_provider("environment")
                    self.engine().addImageProvider(
                        "environment", self._environment_provider
                    )
                    self._environment = self._weather.findChild(
                        QObject, "environmentLayer"
                    )
            self._published = None
            self._published_fill = None
            self._published_generations = set()
            # Publication manager for coherent layer state and atomic rebasing
            self._publication_manager = PublicationManager()
            self.counters = dict(
                fill_publications=0,
                fill_uploads=0,
                fill_selection_publications=0,
                barbs_field_publications=0,
                barbs_field_uploads=0,
                barbs_selection_publications=0,
                barbs_selection_uploads=0,
                chart_publications=0,
                chart_requests=0,
                native_field_requests=0,
            )
            if not self._native_weather_fields:
                for item in (self._fill, self._fill_back):
                    if item is not None:
                        for signal in (
                            "preparedChanged",
                            "shaderErrorChanged",
                            "sourceErrorChanged",
                        ):
                            _connect_signal(
                                item,
                                signal,
                                lambda *args, slot=item: self._image_fill_changed(slot),
                            )
            for item in (self._wave_fill, self._wave_fill_back):
                if item is None:
                    continue
                for signal in (
                    "preparedChanged",
                    "shaderErrorChanged",
                    "sourceErrorChanged",
                ):
                    _connect_signal(
                        item,
                        signal,
                        lambda *args, slot=item: self._wave_fill_changed(slot),
                    )
            for item in (self._wave_barbs, self._wave_barbs_back):
                if item is None:
                    continue
                for signal in (
                    "preparedChanged",
                    "shaderErrorChanged",
                    "sourceErrorChanged",
                ):
                    _connect_signal(
                        item,
                        signal,
                        lambda *args, slot=item: self._wave_barbs_changed(slot),
                    )
            if self._native_weather_fields:
                from .gpuweathertextures import (
                    NativeWeatherBudget,
                    native_texture_context,
                )

                if self._native_budget is None:
                    self._native_budget = NativeWeatherBudget()
                for role, items in (
                    ("barbs", (self._barbs, self._barbs_back)),
                    ("fill", (self._fill, self._fill_back)),
                ):
                    for index, item in enumerate(items):
                        if item is None:
                            continue
                        context = native_texture_context(
                            self.quickWindow(), self._weather, self._native_budget
                        )
                        self._native_contexts[role, index] = context
                        item.setProperty("nativeFields", True)
                        for name, source_item in zip(
                            ("nativeU0", "nativeV0", "nativeU1", "nativeV1"),
                            context.sources,
                        ):
                            item.setProperty(name, source_item)
                        context.stateChanged.connect(
                            lambda status, layer=role, slot=index: self._native_status(
                                layer, slot, status
                            )
                        )
                        getattr(item, "readyChanged").connect(self._readiness_changed)
                        getattr(item, "preparedChanged").connect(
                            self._readiness_changed
                        )
                        getattr(item, "shaderErrorChanged").connect(
                            self._readiness_changed
                        )

                def release_native_owners(
                    _obj=None, owners=tuple(self._native_contexts.values())
                ):
                    # QQuickWidget's scene graph has invalidated before QObject
                    # destruction. Keep GUI receivers alive until that boundary.
                    for owner in owners:
                        if shiboken6.isValid(owner):
                            owner.close()
                            owner.deleteLater()

                self.destroyed.connect(release_native_owners)
            self._status = QLabel("Wind loading…", self)
            self._status.setStyleSheet(
                "background: rgba(0,0,0,190); color: white; padding: 6px"
            )
            self._status.move(8, 8)
            self._status.adjustSize()
            # Request once after the most recent view or input change.
            self._chart_timer = QTimer(self)
            self._chart_timer.setSingleShot(True)
            self._chart_timer.setInterval(CHART_IDLE_MS)
            self._chart_timer.timeout.connect(self._request_chart)
            # Check external chart sources every five seconds while idle.
            self._chart_source_timer = QTimer(self)
            self._chart_source_timer.setSingleShot(True)
            self._chart_source_timer.setInterval(5000)
            self._chart_source_timer.timeout.connect(self._check_chart_sources)
            self._chart_source_timer.start()
            self._watch_chart_budget()
            self.prepared.connect(self._refresh, Qt.ConnectionType.QueuedConnection)
            self.wavePrepared.connect(
                self._refresh_wave, Qt.ConnectionType.QueuedConnection
            )
            self.chartPublished.connect(
                self._publish_chart, Qt.ConnectionType.QueuedConnection
            )
            self.chartStateChanged.connect(
                self._chart_state_changed, Qt.ConnectionType.QueuedConnection
            )
            self._attach_model()
            from .viewportstreamlines import ViewportStreamlineModel

            self._streamline_model = ViewportStreamlineModel(
                model.paths,
                model.display_identity,
                gpu_trace=self._gpu_streamlines_enabled,
            )
            self._streamline_model.on_change = self.streamlinesPrepared.emit
            self._streamline_publication = None
            self._streamline_display_camera: ViewportCamera | None = None
            self._streamline_last_camera = None
            self._streamline_urls = ()
            self._pending_streamline_groups: list[dict[str, Any]] = []
            self._pending_streamline_urls = ()
            self._pending_streamline_camera: ViewportCamera | None = None
            self._pending_streamline_publication = None
            self._pending_streamline_buffer = -1
            self._streamline_settle = QTimer(self)
            self._streamline_settle.setSingleShot(True)
            self._streamline_settle.setInterval(120)
            self._streamline_settle.timeout.connect(self._retrace_streamlines)
            self.streamlinesPrepared.connect(
                self._refresh_streamlines, Qt.ConnectionType.QueuedConnection
            )
            self._streamlines: Any = self._weather.findChild(
                QObject, "weatherStreamlines"
            )
            if self._streamlines is None:
                raise RuntimeError("The weather scene has no GPU streamline layer")
            self._streamlines.setProperty("buffer0Groups", [])
            self._streamlines.setProperty("buffer1Groups", [])
            self._streamlines.setProperty("frontBuffer", -1)
            self._streamlines.setProperty("pendingBuffer", -1)
            self._pending_streamline_groups = []
            self._pending_streamline_urls = ()
            self._pending_streamline_camera = None
            self._pending_streamline_publication = None
            self._streamlines.setVisible(False)
            if self._gpu_streamlines_item is not None:
                self._gpu_streamlines_item.setVisible(False)
            getattr(self._streamlines, "readyChanged").connect(
                self._streamline_readiness_changed
            )
            getattr(self._streamlines, "pendingReadyChanged").connect(
                self._streamline_readiness_changed,
                Qt.ConnectionType.QueuedConnection,
            )
            getattr(self._streamlines, "pendingErrorChanged").connect(
                self._streamline_readiness_changed,
                Qt.ConnectionType.QueuedConnection,
            )
            getattr(self._streamlines, "shaderErrorChanged").connect(
                self._streamline_readiness_changed
            )
            self.destroyed.connect(self._streamline_model.close)
            # Parent teardown need not deliver closeEvent to child widgets.
            self.destroyed.connect(lambda: model.close())
            # This signal is declared by QML, not by the QQuickItem Python stub.
            getattr(self._barbs, "readyChanged").connect(self._readiness_changed)

        def _show_state(self, state, message=None):
            self._state = state
            self._status.setText(
                message
                or (
                    "Wind loading…"
                    if state == "loading"
                    else (self._model.error or self._native_error or "Wind unavailable")
                )
            )
            self._status.adjustSize()
            self._status.setVisible(state != "ready")
            self._status.raise_()
            if state == "ready" and self._weather_ready_at is None:
                # The weather is presented: the chart layer may now be asked
                # for its own generation.
                self._weather_ready_at = time.perf_counter()
                if getattr(self, "_chart_model", None) is not None:
                    self._restart_chart_settle()
            self.stateChanged.emit(state)

        def _chart_state_changed(self, state):
            """The chart layer's own state; it never changes the weather's."""
            self._chart_state = state
            if self._chart is not None:
                self._chart.setProperty(
                    "chartLoading",
                    state == "loading" or self._chart_pending is not None,
                )
                self._chart.setProperty("chartUnavailable", state == "unavailable")
                if state == "loading":
                    # The chart layer's own loading state was shown.
                    self._chart_loading_shown = True
                if state == "unavailable" and self._enc_chart_layer:
                    # A failed composition never leaves the previous ENC on
                    # screen: it is not current, and it is not presented as
                    # if it were.
                    self._clear_chart()
            self._update_enc_status()
            if state in ("idle", "unavailable") and not self._disposed:
                self._restart_chart_settle()

        def _publish_chart(self, payload):
            """Stage a composed image without disturbing the visible chart."""
            generation, image, metadata = payload
            if self._disposed:
                return
            if not self._chart_payload_valid(generation, metadata):
                self._chart_redelivery_generation = generation
                return
            if self._chart_pending is not None and self._chart_pending[0] == generation:
                return
            if self._chart_visible_generation == generation:
                return
            budget = self._publication_manager.budget
            signature = (
                budget.current_usage[LayerRole.CHART],
                budget.total_used(),
                budget.layer_caps[LayerRole.CHART],
                budget.global_cap,
            )
            if getattr(self, "_chart_budget_refusal", None) == (generation, signature):
                return
            self._chart_redelivery_generation = None
            if image.isNull():
                self._chart_state_changed("unavailable")
                return
            self._reject_chart_pending()
            identity = LayerIdentity(
                role=LayerRole.CHART,
                generation=generation,
                source_key=(generation, metadata.get("camera_hash", "")),
                content_hash=str(image),
            )
            self._chart_attempt += 1
            token = str(self._chart_attempt)
            manager = self._publication_manager
            size = image.sizeInBytes()
            if not manager.stage_resources(
                LayerRole.CHART,
                identity,
                LayerPlacement(),
                {ResourceKind.IMAGE: image},
                size,
                completion_token=token,
            ):
                layer = manager.layers[LayerRole.CHART]
                budget = manager.budget
                visible = layer.visible_budget_bytes
                chart_used = budget.current_usage[LayerRole.CHART]
                chart_cap = budget.layer_caps[LayerRole.CHART]
                # Trade the front only when withdrawing it makes both caps fit.
                fallback = (
                    layer.visible is not None
                    and visible > 0
                    and (
                        chart_used + size > chart_cap
                        or budget.total_used() + size > budget.global_cap
                    )
                    and chart_used - visible + size <= chart_cap
                    and budget.total_used() - visible + size <= budget.global_cap
                )
                if fallback:
                    self._chart_state_changed("loading")
                    self._clear_chart()
                    if not self._chart_payload_valid(generation, metadata):
                        self._chart_redelivery_generation = generation
                        return
                    self._chart_attempt += 1
                    token = str(self._chart_attempt)
                    staged = manager.stage_resources(
                        LayerRole.CHART,
                        identity,
                        LayerPlacement(),
                        {ResourceKind.IMAGE: image},
                        size,
                        completion_token=token,
                    )
                else:
                    staged = False
                if not staged:
                    self._clear_chart()
                    self._chart_state_changed("unavailable")
                    self._chart_budget_refusal = (
                        generation,
                        (
                            budget.current_usage[LayerRole.CHART],
                            budget.total_used(),
                            budget.layer_caps[LayerRole.CHART],
                            budget.global_cap,
                        ),
                    )
                    return
            self._chart_budget_refusal = None
            slot = 1 - self._chart_front_slot
            self._chart_pending = (generation, metadata, slot, token)
            url = self._chart_provider.publish_retained(
                generation, image, self._chart_visible_generation
            )
            self._chart.setProperty("chartLoading", True)
            self._chart.setProperty("chartUnavailable", False)
            self._chart.setProperty(f"slot{slot}Token", token)
            self._chart.setProperty(f"slot{slot}Source", QUrl(url))

        def _chart_payload_valid(self, generation, metadata):
            model = self._chart_model
            if model is None or self._camera is None or not isinstance(metadata, dict):
                return False
            with model._condition:
                return (
                    not model._closed
                    and model._generation == generation
                    and model._published is not None
                    and model._published[0] == generation
                    and metadata.get("viewport") is not None
                    and model._reusable(self._camera, metadata.get("preview", False))
                )

        def _reject_chart_pending(self):
            pending = self._chart_pending
            if pending is None:
                return
            _, _, slot, token = pending
            self._chart_pending = None
            self._publication_manager.reject_pending(LayerRole.CHART, token)
            self._chart.setProperty(f"slot{slot}Token", "")
            self._chart.setProperty(f"slot{slot}Source", QUrl(""))
            self._chart_provider.retain(self._chart_visible_generation)

        def _clear_chart(self):
            self._reject_chart_pending()
            self._chart.setProperty(f"slot{self._chart_front_slot}Token", "")
            self._chart.setProperty(f"slot{self._chart_front_slot}Source", QUrl(""))
            self._publication_manager.withdraw_visible(LayerRole.CHART)
            self._chart_visible_generation = None
            self._chart_viewport = None
            self._chart_composition_camera = None
            self._chart_composition_dpr = None
            self._chart_provider.retain()

        def _chart_slot_result(self, slot, token, success):
            pending = self._chart_pending
            if self._disposed or pending is None or (slot, token) != pending[2:]:
                return
            generation, metadata, _, _ = pending
            if not success or not self._chart_payload_valid(generation, metadata):
                if success:
                    self._chart_redelivery_generation = generation
                self._reject_chart_pending()
                if not success:
                    self._chart_state_changed("unavailable")
                return
            adopted, _ = self._publication_manager.verify_and_adopt(
                LayerRole.CHART,
                lambda resources: ResourceKind.IMAGE in resources,
                token,
            )
            if not adopted:
                self._reject_chart_pending()
                return
            old_slot = self._chart_front_slot
            self._chart_pending = None
            self._chart_viewport = metadata["viewport"]
            self._chart_composition_camera = metadata.get("camera")
            self._chart_composition_dpr = metadata.get("factor")
            if metadata.get("preview") and self._chart_composition_dpr is not None:
                self._chart_composition_dpr *= 2
            self._place_chart()
            self._chart_front_slot = slot
            self._chart.setProperty("frontSlot", slot)
            self._chart.setProperty(f"slot{old_slot}Token", "")
            self._chart.setProperty(f"slot{old_slot}Source", QUrl(""))
            self._chart_visible_generation = generation
            self._chart_provider.retain(generation)
            self._chart.setProperty("chartLoading", False)
            self._chart.setProperty("chartUnavailable", False)
            self.counters["chart_publications"] += 1
            self._chart_publish_state = self._chart_state_now()
            self._update_enc_status()
            if self._chart_ready_at is None:
                self._chart_ready_at = time.perf_counter()

        def _update_enc_status(self):
            """The ENC source's own state, in the scene's own status.

            The ENC state never changes the weather's state and never
            changes the chart's own: it is reported beside them.
            """
            model = getattr(self, "_chart_model", None)
            scene = getattr(self, "_weather", None)
            if model is None or scene is None:
                return
            if not getattr(self, "_enc_chart_layer", False):
                return
            status = model.enc_status()
            scene.setProperty("encStatus", status)

        def _adopt_device_pixel_ratio(self):
            """The GUI's device pixel ratio, given to the chart worker.

            A composition made at another ratio is not current and may not
            be resampled, so it is withdrawn and composed again.
            """
            model = getattr(self, "_chart_model", None)
            if model is None:
                return
            if not model.set_device_pixel_ratio(self.devicePixelRatio()):
                return
            chart = getattr(self, "_chart", None)
            if chart is not None:
                self._clear_chart()
            self._update_enc_status()
            self._chart_force_replace = True
            self._restart_chart_settle()

        def _place_chart(self):
            """Place the retained chart image from its own composition camera."""
            if self._chart is None or self._chart_viewport is None:
                return
            if self._camera is None:
                return
            reference = self._chart_composition_camera
            dpr = self.devicePixelRatio()
            if (
                getattr(self, "_map_content", None) is not None
                and self._map_pan_reference is not None
            ):
                anchor = self._map_pan_reference
                anchor_camera = self._map_anchor_camera or self._camera
                if (
                    reference is None
                    or PanReference.from_camera(
                        reference, self._chart_composition_dpr or dpr
                    ).classify(anchor, anchor.dpr)
                    != CameraChange.PAN
                ):
                    x, y, width, height = chart_placement(
                        self._chart_viewport, anchor_camera
                    )
                else:
                    x, y, width, height = chart_placement(
                        self._chart_viewport, reference
                    )
                    dx, dy = PanReference.from_camera(
                        reference, self._chart_composition_dpr or dpr
                    ).translation_to(anchor, anchor.dpr)
                    x += dx
                    y += dy
            elif reference is None:
                x, y, width, height = chart_placement(
                    self._chart_viewport, self._camera
                )
            else:
                ref_pan = PanReference.from_camera(
                    reference, self._chart_composition_dpr or dpr
                )
                if ref_pan.classify(self._camera, dpr) == CameraChange.PAN:
                    x, y, width, height = chart_placement(
                        self._chart_viewport, reference
                    )
                    dx, dy = ref_pan.translation_to(self._camera, dpr)
                    x += dx
                    y += dy
                else:
                    # Keep the existing scaled-preview behavior for an old
                    # composition while a replacement at this camera loads.
                    x, y, width, height = chart_placement(
                        self._chart_viewport, self._camera
                    )
            self._chart.setProperty("imageX", x)
            self._chart.setProperty("imageY", y)
            self._chart.setProperty("imageWidth", width)
            self._chart.setProperty("imageHeight", height)

        def set_base(self, image, target, source_camera=None):
            """Compose the app's basemap inside Quick, below ENC and weather.

            The producer's target is current-camera-relative. Convert it once
            to the map parent's reference; covered pans move the parent only.
            """
            if self._base is None or self._base_provider is None:
                return
            key = image.cacheKey()
            if key != self._base_key:
                url = self._base_provider.publish(key, image)
                self._base.setProperty("source", QUrl(url))
                self._base_key = key
                self._base_source_camera = None
            if source_camera is not None:
                size = image.deviceIndependentSize()
                self._base_source_camera = (*source_camera, size.width(), size.height())
            self._place_retained_image(self._base, "_base_placement", target)
            if self._camera is not None:
                self._place_world(self._camera)
            if getattr(self, "_base_preview_hidden", False):
                self._base.setProperty("chartLoading", False)
                self._base.setVisible(True)
                self._base_preview_hidden = False

        def set_environment(self, image, target, source_camera=None):
            """Present the app's retained above-ENC layers without changing order.

            The producer returns a current-camera target, as for the basemap.
            """
            if self._environment is None or self._environment_provider is None:
                return
            key = image.cacheKey()
            if key != self._environment_key:
                url = self._environment_provider.publish(key, image)
                self._environment.setProperty("source", QUrl(url))
                self._environment_key = key
                self._environment_source_camera = None
            if source_camera is not None:
                size = image.deviceIndependentSize()
                self._environment_source_camera = (
                    *source_camera,
                    size.width(),
                    size.height(),
                )
            self._place_retained_image(
                self._environment, "_environment_placement", target
            )
            if getattr(self, "_environment_preview_hidden", False):
                self._environment.setVisible(True)
                self._environment_preview_hidden = False

        def _world_rendered(self, image, error):
            if self._disposed:
                return
            if error:
                logger.warning("World underlay unavailable: %s", error)
                return
            if self._world is not None and self._world_provider is not None:
                self._world.setProperty(
                    "source",
                    QUrl(self._world_provider.publish(image.cacheKey(), image)),
                )
                if self._camera is not None:
                    self._place_world(self._camera)

        def _place_world(self, camera):
            if self._world is not None:
                self._place_retained_image(
                    self._world, "_world_placement", QRectF(*world_placement(camera))
                )

        def _start_satellite(self):
            """Create the satellite worker on first use; report why it cannot."""
            if self._satellite_worker is not None:
                return True
            if self._satellite_fatal:
                return False
            if self._satellite is None:
                self._satellite_error = "satellite layer is not in the scene"
                self._satellite_fatal = True
                return False
            from .satellite import SatelliteWorker, available, configured_source

            if not available():
                self._satellite_error = "MapLibre satellite support is not installed"
                self._satellite_fatal = True
                logger.info("Satellite basemap unavailable: %s", self._satellite_error)
                return False
            try:
                source = configured_source(
                    self._satellite_pack, self._chart_data_dir or ""
                )
                cache_dir = ""
                if self._chart_data_dir:
                    cache_dir = str(Path(self._chart_data_dir) / "satellite" / "cache")
                worker = SatelliteWorker(source, cache_dir)
            except Exception as exc:  # noqa: BLE001 - display state, not a crash
                self._satellite_error = str(exc)
                self._satellite_fatal = True
                logger.warning("Satellite basemap unavailable: %s", exc)
                return False
            worker.rendered.connect(
                self._satellite_rendered, Qt.ConnectionType.QueuedConnection
            )
            worker.failed.connect(
                self._satellite_failed, Qt.ConnectionType.QueuedConnection
            )
            self._satellite_worker = worker
            self._satellite_base_source = source
            self._satellite_source = source
            self._set_satellite_attribution(source.attribution)
            logger.info("Satellite basemap source: %s", source.tiles[0][:80])
            return True

        def _set_satellite_attribution(self, text):
            self._satellite_attribution = text
            if self._weather is not None:
                self._weather.setProperty("satelliteAttribution", text)

        def _set_satellite_status(self, text):
            if text == self._satellite_status_text:
                return
            self._satellite_status_text = text
            if self._weather is not None:
                self._weather.setProperty("satelliteStatus", text)

        def _satellite_disconnected(self):
            if self._satellite_network is None:
                return False
            try:
                from PySide6.QtNetwork import QNetworkInformation

                return (
                    self._satellite_network.reachability()
                    == QNetworkInformation.Reachability.Disconnected
                )
            except Exception:  # noqa: BLE001 - reachability is advisory
                return False

        def _satellite_sync_source(self):
            """Drop the online underlay while disconnected so a pack can render."""
            base = self._satellite_base_source
            if base is None:
                return
            offline = self._satellite_disconnected()
            if offline and base.underlay is not None:
                desired = replace(
                    base,
                    underlay=None,
                    attribution="Offline imagery pack",
                )
            else:
                desired = base
            if desired == self._satellite_source:
                return
            self._satellite_source = desired
            if self._satellite_worker is not None:
                self._satellite_worker.set_source(desired)
            self._set_satellite_attribution(desired.attribution)

        def _satellite_reachability_changed(self, _reachability):
            if self._satellite_visible:
                self._satellite_retry_at = 0.0
                self._satellite_sync_source()
                self._satellite_camera_changed(self._camera)

        def set_satellite_visible(self, visible):
            """Show or hide the satellite layer, starting its worker on demand."""
            visible = bool(visible)
            if visible == self._satellite_visible:
                return
            self._satellite_visible = visible
            if self._weather is not None:
                self._weather.setProperty("satelliteVisible", visible)
            if visible:
                self._satellite_camera = self._camera
                if self._start_satellite():
                    self._satellite_camera_changed(self._camera)
            else:
                if self._satellite_timer is not None:
                    self._satellite_timer.stop()
                if self._satellite_retry_timer is not None:
                    self._satellite_retry_timer.stop()

        def set_layer_visible(self, layer, visible):
            """Apply a layer switch from the scene or the settings panels."""
            visible = bool(visible)
            if layer == "satellite":
                self.set_satellite_visible(visible)
            elif layer == "basemap":
                self._layer_basemap_visible = visible
                if self._weather is not None:
                    self._weather.setProperty("basemapVisible", visible)
                if self._base_layer_item is not None:
                    self._base_layer_item.setProperty("visible", visible)
            elif layer == "enc":
                self._layer_enc_visible = visible
                if self._weather is not None:
                    self._weather.setProperty("encVisible", visible)
                if self._chart is not None:
                    self._chart.setProperty("visible", visible)

        def _layer_toggled(self, layer, visible):
            self.set_layer_visible(layer, visible)
            self.layerVisibilityChanged.emit(layer, bool(visible))

        def _satellite_camera_changed(self, camera):
            if not self._satellite_visible or camera is None:
                return
            if not self._start_satellite():
                return
            self._satellite_sync_source()
            self._satellite_camera = camera
            now = time.monotonic()
            if now < self._satellite_retry_at:
                if self._satellite_retry_timer is not None:
                    self._satellite_retry_timer.start(
                        max(50, int((self._satellite_retry_at - now) * 1000))
                    )
                return
            source = self._satellite_source
            if (
                source is not None
                and source.requires_network
                and not source.offline_capable
                and self._satellite_disconnected()
            ):
                self._set_satellite_status("Satellite offline — no connection")
                return
            if self._satellite_covers(camera):
                self._set_satellite_status("")
                self._place_satellite(camera)
                return
            self._set_satellite_status("")
            if self._satellite_timer is not None:
                self._satellite_timer.start()

        def _satellite_continuous_source(self, source, camera):
            lon = continuous_longitude(source.center_lon, camera.center_lon)
            if lon == source.center_lon:
                return source
            return replace(source, center_lon=lon)

        def _satellite_covers(self, camera):
            """Whether the retained frame still covers the whole viewport.

            Coverage only excuses a pan. A zoom needs a new frame at the new
            scale, otherwise the retained image is stretched and permanently
            soft, even though it technically covers the view.
            """
            source = getattr(self, "_satellite_source_camera", None)
            if source is None:
                return False
            if abs(source.zoom - camera.zoom) > 1e-6:
                return False
            target = chart_placement(
                self._satellite_continuous_source(source, camera).view(), camera
            )
            x, y, width, height = target
            return (
                x <= 0
                and y <= 0
                and x + width >= camera.width
                and y + height >= camera.height
            )

        def _request_satellite(self):
            if not self._satellite_visible or self._satellite_worker is None:
                return
            camera = self._satellite_camera or self._camera
            if camera is None:
                return
            padded = replace(
                camera,
                width=camera.width + 2 * SATELLITE_MARGIN,
                height=camera.height + 2 * SATELLITE_MARGIN,
            )
            self._satellite_generation += 1
            self._satellite_pending_camera = padded
            # Raster context is worth a full-density frame only up to 2x, the
            # point where most panels stop gaining visible detail.
            scale = min(self.devicePixelRatioF(), 2.0)
            self._satellite_worker.submit(self._satellite_generation, padded, scale)

        def _satellite_rendered(self, generation, image):
            if self._disposed or not self._satellite_visible:
                return
            if generation != self._satellite_generation:
                return
            if self._satellite is None or self._satellite_provider is None:
                return
            slot = 1 - self._satellite_front_slot
            self._satellite_token += 1
            token = str(self._satellite_token)
            url = self._satellite_provider.publish_retained(
                generation, image, self._satellite_visible_generation
            )
            self._satellite_pending_token = (slot, token, generation)
            self._satellite.setProperty(f"slot{slot}Token", token)
            self._satellite.setProperty(f"slot{slot}Source", QUrl(url))

        def _satellite_slot_result(self, slot, token, success):
            pending = self._satellite_pending_token
            if pending is None or (slot, token) != pending[:2]:
                return
            generation = pending[2]
            self._satellite_pending_token = None
            if not success:
                self._satellite.setProperty(f"slot{slot}Token", "")
                self._satellite.setProperty(f"slot{slot}Source", QUrl(""))
                self._satellite_failed(self._satellite_generation, "image load failed")
                return
            previous = self._satellite_front_slot
            self._satellite_front_slot = slot
            self._satellite.setProperty("frontSlot", slot)
            self._satellite.setProperty(f"slot{previous}Token", "")
            self._satellite.setProperty(f"slot{previous}Source", QUrl(""))
            self._satellite_source_camera = self._satellite_pending_camera
            self._satellite_visible_generation = generation
            self._satellite_provider.retain(generation)
            if self._camera is not None:
                self._place_satellite(self._camera)
                if not self._satellite_covers(self._camera):
                    self._satellite_timer.start()
            self._set_satellite_status("")

        def _satellite_failed(self, generation, reason):
            if generation != self._satellite_generation:
                return
            if not self._satellite_error:
                logger.warning("Satellite basemap frame unavailable: %s", reason)
            self._satellite_error = reason
            self._satellite_retry_at = time.monotonic() + 15.0
            self._set_satellite_status("Satellite unavailable")
            retry = getattr(self, "_satellite_retry_timer", None)
            if retry is not None:
                retry.start(15000)

        def _place_satellite(self, camera):
            source = getattr(self, "_satellite_source_camera", None)
            item = getattr(self, "_satellite", None)
            if item is None or source is None:
                return
            source = self._satellite_continuous_source(source, camera)
            # The target is current-camera-relative; the shared map parent owns
            # the pan, so convert through the same retained-image placement the
            # chart, world and environment layers use.
            self._place_retained_image(
                self._satellite,
                "_satellite_placement",
                QRectF(*chart_placement(source.view(), camera)),
            )

        def _preview_retained_imagery(self, camera):
            """Reproject the existing padded images during a zoom camera tick.

            The host renders/replaces them independently; its callback may not
            run until after this camera is displayed. Never scale a previous
            placement incrementally: project the original image's geographic
            centre at the current zoom, as the chart preview does.
            """
            view = camera.view()
            self._place_world(camera)
            place_satellite = getattr(self, "_place_satellite", None)
            if place_satellite is not None:
                place_satellite(camera)
            for item, source, attribute, hidden_attribute in (
                (
                    getattr(self, "_base", None),
                    getattr(self, "_base_source_camera", None),
                    "_base_placement",
                    "_base_preview_hidden",
                ),
                (
                    getattr(self, "_environment", None),
                    getattr(self, "_environment_source_camera", None),
                    "_environment_placement",
                    "_environment_preview_hidden",
                ),
            ):
                if item is None or source is None:
                    continue
                lat, lon, zoom, width, height = source
                scale = 2.0 ** (camera.zoom - zoom)
                x = (
                    camera.width / 2.0
                    + view._lon_to_x(continuous_longitude(lon, camera.center_lon))
                    - view._lon_to_x(camera.center_lon)
                    - width * scale / 2.0
                )
                y = (
                    camera.height / 2.0
                    + view._lat_to_y(lat)
                    - view._lat_to_y(camera.center_lat)
                    - height * scale / 2.0
                )
                # Keep the cached pixels on screen at their true position even
                # when they cannot cover the whole camera: hiding the layer
                # makes a pan read as "the map vanished". It is slid, never
                # stretched, so the incomplete edge stays honest until the
                # host replaces it.
                self._place_retained_image(
                    item, attribute, QRectF(x, y, width * scale, height * scale)
                )
                if getattr(self, hidden_attribute, False):
                    if item is getattr(self, "_base", None):
                        item.setProperty("chartLoading", False)
                    item.setVisible(True)
                    setattr(self, hidden_attribute, False)

        def _place_retained_image(self, item, attribute, target):
            reference = getattr(self, "_map_pan_reference", None)
            camera = getattr(self, "_camera", None)
            if reference is not None and camera is not None:
                dx, dy = reference.translation_to(camera, reference.dpr)
            else:
                dx = dy = 0.0
            placement = (
                target.x() - dx,
                target.y() - dy,
                target.width(),
                target.height(),
            )
            previous = getattr(self, attribute, None)
            if previous is None or any(
                abs(a - b) > 1e-6 for a, b in zip(previous, placement)
            ):
                for name, value in zip(
                    ("imageX", "imageY", "imageWidth", "imageHeight"), placement
                ):
                    item.setProperty(name, value)
                setattr(self, attribute, placement)

        def _rebase_retained_imagery(self, dx, dy):
            """Preserve the on-screen anchors when the bounded parent resets."""
            navigation = getattr(self, "_navigation_overlay", None)
            target = getattr(self, "_navigation_target", None)
            if navigation is not None and target is not None:
                target = (target[0] + dx, target[1] + dy, *target[2:])
                navigation.setProperty("imageX", target[0])
                navigation.setProperty("imageY", target[1])
                self._navigation_target = target
            for item, attribute in (
                (getattr(self, "_world", None), "_world_placement"),
                (getattr(self, "_base", None), "_base_placement"),
                (getattr(self, "_environment", None), "_environment_placement"),
                (getattr(self, "_satellite", None), "_satellite_placement"),
            ):
                placement = getattr(self, attribute, None)
                if item is not None and placement is not None:
                    placement = (
                        placement[0] + dx,
                        placement[1] + dy,
                        *placement[2:],
                    )
                    item.setProperty("imageX", placement[0])
                    item.setProperty("imageY", placement[1])
                    setattr(self, attribute, placement)
            for item in (
                getattr(self, "_fill", None),
                getattr(self, "_fill_back", None),
            ):
                if item is not None:
                    item.setProperty("gridX", float(item.property("gridX")) + dx)
                    item.setProperty("gridY", float(item.property("gridY")) + dy)
                    placement = getattr(self, "_fill_placements", {}).get(id(item))
                    if placement is not None:
                        self._fill_placements[id(item)] = (
                            placement[0] + dx,
                            placement[1] + dy,
                            *placement[2:],
                        )
            # The barb anchors are encoded for the camera that published them.
            # Their displayed position is the anchor plus this offset plus the
            # parent translation, so a rebase shifts both front and pending by
            # the same translation instead of re-uploading anchor textures.
            for item in (
                getattr(self, "_barbs", None),
                getattr(self, "_barbs_back", None),
            ):
                if item is not None:
                    for name, delta in (
                        ("anchorOffsetX", dx),
                        ("anchorOffsetY", dy),
                    ):
                        item.setProperty(
                            name, float(item.property(name) or 0.0) + delta
                        )

        def _place_fill_rect(self, item, rect):
            placement = (rect.x(), rect.y(), rect.width(), rect.height())
            placements = getattr(self, "_fill_placements", None)
            if placements is not None:
                previous = placements.get(id(item))
                if previous is not None and all(
                    abs(a - b) <= 1e-6 for a, b in zip(previous, placement)
                ):
                    return
            for name, value in zip(
                ("gridX", "gridY", "gridWidth", "gridHeight"), placement
            ):
                item.setProperty(name, value)
            if placements is not None:
                placements[id(item)] = placement

        def set_route_overlay(self, overlay):
            """Present retained route geometry over a padded pan window."""
            item = self._route_overlay
            camera = self._camera
            if item is None or camera is None:
                return
            content = (
                overlay["route_plan"],
                overlay["route_preview"],
                overlay["selected_point"],
                overlay["track_points"],
                overlay["track_position"],
                overlay.get("track_heading"),
                overlay.get("track_awa"),
                overlay.get("pois", []),
                overlay.get("tracks", []),
                overlay.get("race_tracks", []),
                overlay.get("race_courses", []),
                overlay.get("race_positions", []),
                overlay.get("routes", []),
                overlay.get("ais_targets", []),
                overlay.get("isochrones", {}),
                overlay.get("dashboard", []),
                overlay["caption"],
                overlay["dashboard_visible"],
            )
            # Only trusted builder payloads carry an identity token; arbitrary
            # callers retain full value-based mutation detection.
            race_key = overlay.get("_race_tracks_key")
            if race_key is None:
                race_key = repr(content[9])
            tracks_key = overlay.get("_tracks_key")
            if tracks_key is None:
                tracks_key = repr(content[8])
            pois_key = overlay.get("_pois_key")
            if pois_key is None:
                pois_key = repr(content[7])
            routes_key = overlay.get("_routes_key")
            if routes_key is None:
                routes_key = repr(content[12])
            # raceTracks (9) and routes (12) are keyed above; pois (7) and
            # tracks (8) are keyed too. Courses (10) and boat markers (11)
            # change cheaply by value.
            content_key = (
                repr(content[:7]),
                pois_key,
                tracks_key,
                race_key,
                routes_key,
                repr(content[10:12]),
                repr(content[13:]),
            )
            content_changed = content_key != self._route_content_key
            if self._route_pan_window is None or content_changed:
                race_changed = self._route_pan_window is None or race_key != getattr(
                    self, "_route_race_key", None
                )
                tracks_changed = (
                    self._route_pan_window is None
                    or tracks_key != getattr(self, "_route_tracks_key", None)
                )
                reference = PanReference.from_camera(camera, self.devicePixelRatio())
                padding = 0.5 * max(float(camera.width), float(camera.height))
                self._route_pan_window = PaddedPanWindow(reference, padding)
                self._route_content_key = content_key
                self._route_race_key = race_key
                self._route_tracks_key = tracks_key
                for name, value in zip(
                    (
                        "routePlan",
                        "routePreview",
                        "selectedPoint",
                        "trackPoints",
                        "trackPosition",
                        "trackHeading",
                        "trackAwa",
                        "pois",
                        "tracks",
                        "raceTracks",
                        "raceCourses",
                        "racePositions",
                        "routes",
                        "aisTargets",
                        "isochrones",
                        "dashboardGauges",
                        "captionText",
                        "dashboardVisible",
                    ),
                    content,
                ):
                    if name == "raceTracks" and not race_changed:
                        continue
                    if name == "tracks" and not tracks_changed:
                        continue
                    item.setProperty(name, value)
                item.setProperty("centerLatitude", camera.center_lat)
                item.setProperty("centerLongitude", camera.center_lon)
                item.setProperty("zoom", camera.zoom)
                item.setProperty("panX", 0.0)
                item.setProperty("panY", 0.0)

            self._place_route_overlay(camera, content_changed=content_changed)

        def _place_route_overlay(
            self, camera, anchor_changed=False, content_changed=False
        ):
            """Move the retained route Canvas without republishing its content."""
            item = self._route_overlay
            if item is None or camera is None or self._route_pan_window is None:
                return

            window = self._route_pan_window
            dx, dy, rebase = window.placement(camera, self.devicePixelRatio())
            shared_parent = getattr(self, "_map_content", None) is not None
            if not shared_parent:
                if rebase:
                    reference = PanReference.from_camera(
                        camera, self.devicePixelRatio()
                    )
                    self._route_pan_window = PaddedPanWindow(
                        reference,
                        0.5 * max(float(camera.width), float(camera.height)),
                    )
                    item.setProperty("centerLatitude", camera.center_lat)
                    item.setProperty("centerLongitude", camera.center_lon)
                    item.setProperty("zoom", camera.zoom)
                    dx = dy = 0.0
                item.setProperty("panX", dx)
                item.setProperty("panY", dy)
                return

            if not rebase and not anchor_changed and not content_changed:
                return
            if rebase:
                reference = PanReference.from_camera(camera, self.devicePixelRatio())
                self._route_pan_window = PaddedPanWindow(
                    reference,
                    0.5 * max(float(camera.width), float(camera.height)),
                )
                item.setProperty("centerLatitude", camera.center_lat)
                item.setProperty("centerLongitude", camera.center_lon)
                item.setProperty("zoom", camera.zoom)
                window = self._route_pan_window
            dx, dy = window.reference.translation_to(
                self._map_pan_reference, self._map_pan_reference.dpr
            )
            item.setProperty("panX", dx)
            item.setProperty("panY", dy)

        def set_marine(self, sources, instant, visible):
            if self._camera is not None:
                self._marine_layer.request(sources, instant, visible, self._camera)

        def set_ship_observations(self, reports, visible):
            if self._camera is not None:
                self._icoads_layer.present(reports, visible, self._camera)

        def set_navigation_overlay(self, image, target):
            """Present padded navigation imagery in shared map-content space."""
            item = self._navigation_overlay
            provider = self._navigation_provider
            if item is None or provider is None:
                return
            key = image.cacheKey()
            if key != self._navigation_key:
                url = provider.publish(key, image)
                item.setProperty("source", QUrl(url))
                self._navigation_key = key
            local_target = target
            if (
                getattr(self, "_map_content", None) is not None
                and self._map_pan_reference is not None
            ):
                dx, dy = self._map_pan_reference.translation_to(
                    self._camera, self._map_pan_reference.dpr
                )
                local_target = QRectF(
                    target.x() - dx,
                    target.y() - dy,
                    target.width(),
                    target.height(),
                )
            values = (
                local_target.x(),
                local_target.y(),
                local_target.width(),
                local_target.height(),
            )
            if values == getattr(self, "_navigation_target", None):
                return
            for name, value in zip(
                ("imageX", "imageY", "imageWidth", "imageHeight"),
                values,
            ):
                item.setProperty(name, value)
            self._navigation_target = values

        def set_dashboard_overlay(self, image):
            """Present the app's retained instrument dashboard in Quick."""
            item = self._dashboard_overlay
            provider = self._dashboard_provider
            if item is None or provider is None:
                return
            if image is None:
                item.setProperty("source", QUrl(""))
                self._dashboard_key = None
                return
            key = image.cacheKey()
            if key != self._dashboard_key:
                url = provider.publish(key, image)
                item.setProperty("source", QUrl(url))
                self._dashboard_key = key
            for name, value in (
                ("imageX", 0),
                ("imageY", 0),
                ("imageWidth", self.width()),
                ("imageHeight", self.height()),
            ):
                item.setProperty(name, value)

        def _restart_chart_settle(self):
            """Request a chart after the view and inputs have been idle."""
            if not getattr(self, "_disposed", False):
                self._chart_timer.start(CHART_IDLE_MS)

        def _request_interactive_chart(self):
            """Offer one low-resolution chart on camera events, never by timer."""
            if self._chart_model is None or self._camera is None:
                return
            if (
                self._weather_ready_at is None
                and not getattr(self, "_chart_without_weather", False)
                and not (self._enc_chart_layer and self._model.state == "unavailable")
            ):
                return
            if self._model.busy():
                return
            now = time.perf_counter()
            last = self._chart_interactive_at
            if last is not None and (now - last) * 1000 < CHART_INTERACTIVE_MS:
                return
            generation = self._chart_model.request(self._camera, preview=True)
            if generation is not None:
                self._chart_interactive_at = now
                self.counters["chart_requests"] += 1
                self._chart_requested_after_weather = True
                self._chart_request_state = self._chart_state_now()

        def _watch_chart_budget(self):
            """Wake a refused chart when another layer releases capacity."""
            budget = self._publication_manager.budget
            release = budget.release

            def release_and_rearm(role, size):
                before = budget.total_used()
                release(role, size)
                if (
                    budget.total_used() < before
                    and getattr(self, "_chart_budget_refusal", None) is not None
                ):
                    self._restart_chart_settle()

            budget.release = release_and_rearm

        def _check_chart_sources(self):
            """Recheck sources without defeating retained-composition reuse."""
            if self._disposed:
                return
            self._request_chart()
            self._chart_source_timer.start()

        def _request_chart(self):
            """Ask the chart layer for its own generation.

            The chart is composed for a settled view: the view must not have
            moved for the settle window, the weather must already be
            presented and the weather pipeline must be idle.  While any of
            those is untrue the chart asks again, so the chart never delays
            the weather.
            """
            if self._chart_model is None or self._camera is None:
                self._restart_chart_settle()
                return
            if (
                self._weather_ready_at is None
                and not getattr(self, "_chart_without_weather", False)
                and not (self._enc_chart_layer and self._model.state == "unavailable")
            ):
                # The chart is added after the weather is presented.
                self._restart_chart_settle()
                return
            elapsed = (time.perf_counter() - self._chart_camera_changed) * 1000.0
            if elapsed < CHART_IDLE_MS:
                # The view is still moving: the chart waits for it to settle.
                self._chart_timer.start(max(1, math.ceil(CHART_IDLE_MS - elapsed)))
                return
            if self._model.busy():
                # The weather pipeline is working: the chart waits.
                self._restart_chart_settle()
                return
            self.counters["chart_requests"] += 1
            self._chart_requested_after_weather = True
            self._chart_request_state = self._chart_state_now()
            if getattr(self, "_chart_force_replace", False):
                self._chart_force_replace = False
                generation = self._chart_model.request(self._camera, replace=True)
            else:
                generation = self._chart_model.request(self._camera)
            refusal = getattr(self, "_chart_budget_refusal", None)
            retry_refusal = False
            if refusal is not None and refusal[0] == generation:
                budget = self._publication_manager.budget
                signature = (
                    budget.current_usage[LayerRole.CHART],
                    budget.total_used(),
                    budget.layer_caps[LayerRole.CHART],
                    budget.global_cap,
                )
                retry_refusal = refusal[1] != signature
            if generation is not None and (
                generation == self._chart_redelivery_generation or retry_refusal
            ):
                with self._chart_model._condition:
                    payload = self._chart_model._published
                    if payload is None or payload[0] != generation:
                        payload = None
                self._chart_redelivery_generation = None
                if payload is not None:
                    self._publish_chart(payload)

        def _chart_state_now(self):
            """The camera, the forecast time and the published weather."""
            camera = self._camera
            return {
                "center_lon": None if camera is None else camera.center_lon,
                "center_lat": None if camera is None else camera.center_lat,
                "zoom": None if camera is None else camera.zoom,
                "time": None if camera is None else camera.time.isoformat(),
                "weather": self._published,
            }

        def _readiness_changed(self):
            if self._wind_style == "streamlines":
                return
            if self._native_weather_fields:
                self._update_native_state()
                return
            if self._barbs.property("ready") and self._model.state == "ready":
                self._show_state("ready")

        def _native_item(self, role, slot):
            front = self._barbs if role == "barbs" else self._fill
            back = self._barbs_back if role == "barbs" else self._fill_back
            return front if slot == self._native_front_index else back

        def _native_status(self, role, slot, status):
            if self._disposed:
                return
            item = self._native_item(role, slot)
            if item is None or not shiboken6.isValid(item):
                return
            if status.get("invalidated"):
                request = getattr(self, "_native_requests", {}).get((role, slot))
                if request is not None and status.get("sequence") != request[0]:
                    return
                self._native_bindings.pop((role, slot), None)
                getattr(self, "_native_requests", {}).pop((role, slot), None)
                getattr(self, "_native_images", {}).pop((role, slot), None)
                token = self._native_publication_tokens.pop((role, slot), None)
                item.setProperty("nativeReady", False)
                item.setProperty("nativeFrameObserved", False)
                if slot == self._native_front_index:
                    self._native_active_camera = None
                    self._native_active_identity = None
                    self._native_active_selection = None
                    self._native_active_dpr = None
                    self._weather_display_camera = None
                    self._clear_barb_zoom_preview()
                    self._set_weather_visible(False)
                if self._camera is not None:
                    QTimer.singleShot(
                        0,
                        lambda: (
                            self.set_camera(self._camera)
                            if not self._disposed and self.isVisible()
                            else None
                        ),
                    )
                if token is not None:
                    self._publication_manager.reject_pending(role, expected_token=token)
                return
            if status["key"].identity != self._native_bindings.get((role, slot)):
                return
            request = getattr(self, "_native_requests", {}).get((role, slot))
            if (
                request is not None
                and (status.get("sequence"), status["key"]) != request
            ):
                return
            if status.get("error"):
                self._native_bindings.pop((role, slot), None)
                getattr(self, "_native_requests", {}).pop((role, slot), None)
                getattr(self, "_native_images", {}).pop((role, slot), None)
                token = self._native_publication_tokens.pop((role, slot), None)
                if token is not None:
                    self._publication_manager.reject_pending(role, expected_token=token)
                item.setProperty("nativeReady", False)
                item.setProperty("nativeFrameObserved", False)
                self._native_error = status["error"]
                if slot == self._native_front_index:
                    self._set_weather_visible(False)
                self._show_state("unavailable")
                self.nativeError.emit(self._native_error)
                return
            item.setProperty("nativeReady", status["ready"])
            item.setProperty("nativeFrameObserved", bool(status.get("frame_observed")))
            # Verify and adopt through PublicationManager
            token = self._native_publication_tokens.get((role, slot))
            if (
                status["ready"]
                and token is not None
                and slot == self._native_front_index
            ):
                ok, msg = self._publication_manager.verify_and_adopt(
                    role, lambda r: status["ready"], expected_token=token
                )
                if ok:
                    # Update native tracking
                    if slot == self._native_front_index:
                        self._native_active_camera = self._camera
                        self._native_active_identity = self._native_pending_identity
                        self._native_active_selection = self._native_pending_selection
                        if getattr(self, "_native_active_dpr", None) is None:
                            self._native_active_dpr = self._native_publication_dpr()
            self._update_native_state()

        def _update_native_state(self):
            if self._wind_style == "streamlines":
                return
            if self._disposed or self._camera is None:
                return
            pending = self._native_pending_camera is not None
            if pending and self._native_publishing:
                return
            fill_needed = self._fill is not None and self._wind_gradient_enabled
            items = (
                [self._barbs_back]
                + (
                    [self._fill_back]
                    if fill_needed and self._fill_back is not None
                    else []
                )
                if pending
                else [self._barbs] + ([self._fill] if fill_needed else [])
            )
            error = next(
                (
                    str(item.property("shaderError"))
                    for item in items
                    if item.property("shaderError")
                ),
                "",
            )
            if error:
                self._native_error = error
                self._set_weather_visible(False)
                self._show_state("unavailable")
                self.nativeError.emit(error)
            elif self._model.state == "ready" and all(
                item.property("prepared" if pending else "ready") for item in items
            ):
                if pending:
                    if self._native_pending_camera != self._camera:
                        return
                    self._commit_native_weather()
                    self._show_state("loading")
                else:
                    if self._native_active_camera is None:
                        self._native_active_camera = self._camera
                        self._native_active_identity = self._native_pending_identity
                        self._native_active_selection = self._native_pending_selection
                        if getattr(self, "_native_active_dpr", None) is None:
                            self._native_active_dpr = (
                                getattr(self, "_native_pending_dpr", None)
                                or self._native_publication_dpr()
                            )
                        self._weather_display_camera = self._camera
                        self._adopt_shared_anchor_front()
                        self._provider.pin_selection(
                            self._native_pending_selection_urls
                        )
                    if self._state != "ready":
                        self._clear_barb_zoom_preview()
                        self._show_state("ready")
            elif self._model.state == "ready" and self._state != "loading":
                self._show_state("loading")

        def _commit_native_weather(self):
            old_barbs, old_fill = self._barbs, self._fill
            old_index = self._native_front_index
            old_barbs.setVisible(False)
            if old_fill is not None:
                old_fill.setVisible(False)
            self._barbs, self._barbs_back = self._barbs_back, old_barbs
            self._fill, self._fill_back = self._fill_back, old_fill
            self._barbs.setOpacity(self._wind_opacity)
            self._native_front_index = 1 - old_index
            self._native_active_camera = self._native_pending_camera
            self._native_active_identity = self._native_pending_identity
            self._native_active_selection = self._native_pending_selection
            self._native_active_dpr = getattr(self, "_native_pending_dpr", None)
            self._weather_display_camera = self._native_pending_camera
            old_front_slot = getattr(self, "_shared_anchor_front", None)
            retained_anchors = ()
            if old_front_slot is not None:
                retained_anchors = (
                    getattr(self, "_shared_anchor_urls", [None, None])[old_front_slot]
                    or ()
                )
            # The item that was staged as pending now owns the front layer, so
            # its atlas slot is the one on screen.  The superseded slot keeps
            # its loaded pair and is only rewritten by a later frame.
            self._adopt_shared_anchor_front()
            self._native_pending_camera = None
            self._native_pending_identity = None
            self._native_pending_selection = None
            # The staged frame is now the front: identity preview uniforms on
            # both sides, so the next camera's placement uses anchor offsets.
            self._clear_barb_zoom_preview()
            self._barbs.setVisible(True)
            if self._fill is not None:
                self._fill.setOpacity(
                    self._wind_gradient_opacity if self._wind_gradient_enabled else 0.0
                )
                self._fill.setVisible(self._wind_gradient_enabled)
            # Commit through PublicationManager
            for role in ("barbs", "fill"):
                token = getattr(self, "_native_publication_tokens", {}).get(
                    (role, self._native_front_index)
                )
                if token is not None:
                    self._publication_manager.commit_pending(role, expected_token=token)
            for role in ("barbs", "fill"):
                context = self._native_contexts.get((role, old_index))
                if context is not None:
                    context.clear()
            self._provider.pin_selection(
                self._native_pending_selection_urls, retained=retained_anchors
            )
            self._native_pending_selection_urls = ()

        def _bind_native_layer(
            self, role, identity, images, pending=False, completion_token=None
        ):
            slot = 1 - self._native_front_index if pending else self._native_front_index
            item = self._native_item(role, slot)
            if item is None:
                raise RuntimeError(f"Viewport has no native {role} layer")
            context = self._native_contexts[role, slot]
            images = tuple(images)
            image_refs = getattr(self, "_native_images", None)
            if image_refs is None:
                image_refs = self._native_images = {}
            previous = image_refs.get((role, slot))
            image_keys = tuple(
                image.cacheKey() if callable(getattr(image, "cacheKey", None)) else None
                for image in images
            )
            same_images = (
                previous is not None
                and len(previous[0]) == len(images)
                and all(old is new for old, new in zip(previous[0], images))
                and previous[1] == image_keys
            )
            tokens = getattr(self, "_native_publication_tokens", None)
            if tokens is not None:
                if completion_token is None:
                    tokens.pop((role, slot), None)
                else:
                    tokens[role, slot] = completion_token
            if (
                self._native_bindings.get((role, slot)) == identity
                and context.ready
                and same_images
            ):
                if completion_token is not None and not pending:
                    self._publication_manager.commit_pending(
                        role, expected_token=completion_token
                    )
                return
            self._native_bindings[role, slot] = identity
            self._native_error = ""
            item.setProperty("nativeFrameObserved", False)
            warm = context.request(
                identity, images, "nodes" if role == "barbs" else "grid"
            )
            requests = getattr(self, "_native_requests", None)
            if requests is None:
                requests = self._native_requests = {}
            lock = getattr(context, "_lock", None)
            if lock is not None:
                with lock:
                    requests[role, slot] = context._latest
            else:
                requests.pop((role, slot), None)
            image_refs[role, slot] = (images, image_keys)
            item.setProperty("nativeReady", warm)
            if warm and completion_token is not None and not pending:
                self._publication_manager.commit_pending(
                    role, expected_token=completion_token
                )
            self.counters["native_field_requests"] += 1

        def native_observations(self):
            return {
                "enabled": self._native_weather_fields,
                "layers": {
                    role: context.snapshot()
                    for (role, index), context in self._native_contexts.items()
                    if index == self._native_front_index
                },
                "pending_layers": {
                    role: context.snapshot()
                    for (role, index), context in self._native_contexts.items()
                    if index != self._native_front_index
                },
                "budget": (
                    {}
                    if self._native_budget is None
                    else self._native_budget.statistics()
                ),
                "error": self._native_error,
            }

        def _attach_model(self):
            generation = self._source_generation
            self._model.on_change = lambda: self.prepared.emit(generation)

        def replace_model(self, model):
            """Retire a source immediately; queued old callbacks cannot republish."""
            if self._chart_model is not None and not self._chart_model._enc_only:
                raise ValueError("Replace chart-enabled viewports as a whole")
            self._weather.setVisible(False)
            if not self._native_weather_fields:
                self._cancel_image_fill(withdraw=True)
                self._publication_manager.rebase_all(
                    self._camera, self._publication_manager.current_generation + 1
                )
            self._weather_display_camera = None
            self._native_active_camera = self._native_pending_camera = None
            self._native_active_identity = self._native_pending_identity = None
            self._native_active_selection = self._native_pending_selection = None
            self._native_active_dpr = self._native_pending_dpr = None
            self._native_pending_selection_urls = ()
            self._clear_weather_sources()
            for context in self._native_contexts.values():
                context.clear()
            self._native_bindings.clear()
            self._native_requests.clear()
            self._native_images.clear()
            self._native_publication_tokens.clear()
            self._native_error = ""
            old = self._model
            old.on_change = None
            self._source_generation += 1
            old.close()  # nonjoining; the owning worker releases private handles
            if self._wave_model is not None:
                self._wave_model.on_change = None
                self._wave_model.close()
                self._wave_model = None
            self._publication_manager.reject_pending(LayerRole.WAVE_FILL)
            self._publication_manager.withdraw_visible(LayerRole.WAVE_FILL)
            self._provider.release_fields((self, "wave"))
            self._wave_token = None
            self._wave_state = "hidden"
            self._wave_error = ""
            self._set_wave_status("")
            for item in (self._wave_fill, self._wave_fill_back):
                if item is None:
                    continue
                item.setVisible(False)
                for name in ("u0Source", "v0Source", "u1Source", "v1Source"):
                    item.setProperty(name, QUrl())
            for worker in self._wave_arrow_models.values():
                worker.on_change = None
                worker.close()
            self._wave_arrow_models = {}
            self._publication_manager.reject_pending(LayerRole.WAVE_BARBS)
            self._publication_manager.withdraw_visible(LayerRole.WAVE_BARBS)
            self._provider.release_fields((self, "wave_barbs"))
            self._wave_barbs_token = None
            self._wave_arrows_state = "hidden"
            self._wave_arrows_error = ""
            for item in (self._wave_barbs, self._wave_barbs_back):
                if item is not None:
                    item.setVisible(False)
            from .viewportstreamlines import ViewportStreamlineModel

            self._streamline_model.close()
            self._streamline_model = ViewportStreamlineModel(
                model.paths,
                model.display_identity,
                gpu_trace=self._gpu_streamlines_enabled,
            )
            self._streamline_model.on_change = self.streamlinesPrepared.emit
            self._streamline_publication = None
            self._streamline_display_camera = None
            self._streamline_last_camera = None
            self._streamline_urls = ()
            self._pending_streamline_groups = []
            self._pending_streamline_urls = ()
            self._pending_streamline_camera = None
            self._pending_streamline_publication = None
            self._pending_streamline_buffer = -1
            self._streamlines.setProperty("buffer0Groups", [])
            self._streamlines.setProperty("buffer1Groups", [])
            self._streamlines.setProperty("frontBuffer", -1)
            self._streamlines.setProperty("pendingBuffer", -1)
            self._streamlines.setVisible(False)
            if self._gpu_streamlines_item is not None:
                self._gpu_streamlines_item.set_trace_input(None)
                self._gpu_streamlines_item.setVisible(False)
            self._streamline_provider.clear()
            self._provider.clear()
            self._published = self._published_fill = None
            self._published_generations.clear()
            self._weather_ready_at = None
            self._model = model
            self._attach_model()
            self._show_state("loading")
            if self._camera is not None and not self._disposed:
                self.set_camera(self._camera)

        def set_wind_opacity(self, opacity):
            previous = self._wind_opacity
            self._wind_opacity = max(0.0, min(1.0, float(opacity)))
            self._barbs.setOpacity(self._wind_opacity)
            self._streamlines.setOpacity(self._wind_opacity)
            if previous != self._wind_opacity and self._camera is not None:
                self.set_camera(self._camera)

        def set_wind_style(self, style):
            if style not in ("barbs", "streamlines"):
                raise ValueError("Unsupported wind style: " + style)
            if style != self._wind_style:
                self._wind_style = style
                self._streamlines.setVisible(False)
                if self._camera is not None:
                    self.set_camera(self._camera)

        def set_wind_gradient(self, enabled):
            self._wind_gradient_enabled = bool(enabled)
            if self._native_weather_fields:
                self._weather.setProperty("fillEnabled", self._wind_gradient_enabled)
            if self._fill is not None:
                self._fill.setOpacity(
                    self._wind_gradient_opacity if self._wind_gradient_enabled else 0.0
                )
                self._fill.setVisible(
                    self._wind_gradient_enabled
                    and (
                        self._native_weather_fields
                        or (
                            getattr(self, "_image_weather_visible", False)
                            and self._image_fill_matches_requested()
                        )
                    )
                )
            if self._fill_back is not None:
                self._fill_back.setOpacity(
                    0.01
                    if self._wind_gradient_enabled and self._wind_gradient_opacity > 0
                    else 0.0
                )

        def set_wind_gradient_opacity(self, opacity):
            self._wind_gradient_opacity = max(0.0, min(1.0, float(opacity)))
            if self._fill is not None:
                self._fill.setOpacity(
                    self._wind_gradient_opacity if self._wind_gradient_enabled else 0.0
                )
            if self._fill_back is not None and not self._wind_gradient_enabled:
                self._fill_back.setOpacity(0.0)

        def set_wind_gradient_style(self, smooth, levels):
            for fill in (self._fill, self._fill_back):
                if fill is not None:
                    fill.setProperty("gradientSmooth", bool(smooth))
                    fill.setProperty("gradientLevels", max(2, min(15, int(levels))))

        # ------------------------------------------------------- wave layer
        # The wave layer is optional: every entry point below resolves its
        # members through ``_prepare_wave_layer`` so a scene that lacks them
        # degrades to a no-op instead of raising (see that helper).
        def set_wave_visible(self, visible):
            """Show or hide the wave height fill.

            The wave layer is independent of the wind layer: hiding it never
            hides, restages or recolours the wind fill, and showing it stages
            only the scalar ``wave_height`` field.
            """
            _prepare_wave_layer(self)
            visible = bool(visible)
            self._wave_visible = visible
            if not visible:
                self._wave_state = "hidden"
                self._wave_error = ""
                self._set_wave_status("")
                self._show_wave_fill(False)
                if self._wave_fill_back is not None:
                    self._wave_fill_back.setVisible(False)
                return
            ensure_wave_model = getattr(self, "_ensure_wave_model", None)
            if ensure_wave_model is None or not ensure_wave_model():
                # The scene has no image fill (the barbs' own scenes) or the
                # source has no field worker: state that, do not draw a blank.
                self._show_wave_state("unavailable", "Wave height unavailable here")
                return
            # The caption states what is *not* on screen yet.  Re-applying the
            # toggle -- the presenter does that whenever the arrows are synced
            # -- must not paint "Loading" over a frame that was already
            # adopted, so a ready layer keeps its caption cleared.  A state
            # that is loading or unavailable is re-announced, which is what
            # retries a previously failed stage.
            if self._wave_state != "ready":
                self._show_wave_state("loading", "Loading wave height")
            self._update_wave(self._camera)

        def set_wave_arrows_visible(self, visible):
            """Show or hide the wave arrows without touching the height fill.

            The arrows are their own publication layer: this never restages
            the wind barbs, the wind fill or the wave height fill.
            """
            _prepare_wave_layer(self)
            visible = bool(visible)
            self._wave_arrows_visible = visible
            if not visible:
                self._wave_arrows_state = "hidden"
                self._wave_arrows_error = ""
                for item in (self._wave_barbs, self._wave_barbs_back):
                    if item is not None:
                        item.setVisible(False)
                return
            if self._ensure_wave_arrow_models() is None:
                self._show_wave_arrow_state(
                    "unavailable", "Wave arrows unavailable here"
                )
                return
            self._show_wave_arrow_state("loading", "Loading wave arrows")
            self._update_wave_arrows(self._camera)

        def wave_arrows_visible(self):
            return getattr(self, "_wave_arrows_visible", False)

        def wave_arrows_state(self):
            """``hidden``, ``loading``, ``ready`` or ``unavailable``."""
            return getattr(self, "_wave_arrows_state", "hidden")

        def set_wave_arrow_density(self, density):
            """Re-thin the wave arrows, independent of the wind barbs.

            The density selects the wave layer's own ``gribWaveDensity``
            layout; the wind barbs keep their camera density untouched.
            """
            _prepare_wave_layer(self)
            self._wave_arrow_density = str(density)
            if self._wave_arrows_visible:
                self._update_wave_arrows(self._camera)

        def wave_arrow_density(self):
            return getattr(self, "_wave_arrow_density", "standard")

        def set_wave_opacity(self, opacity):
            _prepare_wave_layer(self)
            self._wave_opacity = max(0.0, min(1.0, float(opacity)))
            for item in (self._wave_fill, self._wave_fill_back):
                if item is not None:
                    item.setOpacity(self._wave_opacity)

        def set_wave_gradient_style(self, smooth, levels):
            _prepare_wave_layer(self)
            self._wave_gradient_smooth = bool(smooth)
            self._wave_gradient_levels = max(2, min(9, int(levels)))
            for item in (self._wave_fill, self._wave_fill_back):
                if item is None:
                    continue
                item.setProperty("gradientSmooth", self._wave_gradient_smooth)
                item.setProperty("gradientLevels", self._wave_gradient_levels)

        def wave_state(self):
            """``hidden``, ``loading``, ``ready`` or ``unavailable``."""
            return getattr(self, "_wave_state", "unavailable")

        def _new_wave_model(self, parameter, grid=False):
            """Create one scalar wave worker for a single band, or ``None``.

            The fill and the arrow layer share this factory: a band is a
            one-component field with no selection, so the only thing that
            differs between the workers is the parameter name.
            """
            model = self._model
            if model is None or not getattr(model, "paths", ()):
                return None
            worker = GribViewportModel(
                model.paths,
                grid=grid,
                display_identity=model.display_identity,
                parameter=parameter,
                components=1,
                selection=False,
            )
            worker.on_change = self.wavePrepared.emit
            return worker

        def _ensure_wave_model(self):
            """Create the scalar wave worker, if the scene can display it."""
            if self._wave_model is not None:
                return True
            if self._wave_fill is None:
                return False
            worker = self._new_wave_model("wave_height", grid=True)
            if worker is None:
                return False
            self._wave_model = worker
            return True

        def _ensure_wave_arrow_models(self):
            """The three band workers, or ``None`` when the scene cannot show them.

            Height, period and direction are separate one-component fields so
            each band is staged and published on its own.  A source missing a
            band leaves that worker unavailable rather than substituting zero.
            """
            if self._wave_barbs is None or self._wave_barbs_back is None:
                return None
            if self._model is None:
                return None
            for band in WAVE_BANDS:
                if band in self._wave_arrow_models:
                    continue
                worker = self._new_wave_model(band)
                if worker is None:
                    return None
                self._wave_arrow_models[band] = worker
            return self._wave_arrow_models

        def _refresh_wave(self):
            _prepare_wave_layer(self)
            if not self._disposed and self._wave_visible:
                self._update_wave(self._camera)
            if not self._disposed and self._wave_arrows_visible:
                self._update_wave_arrows(self._camera)

        def _set_wave_status(self, message, loading=False):
            status = getattr(self, "_wave_status", None)
            if status is not None:
                status.setProperty("text", message)
            for item in (
                getattr(self, "_wave_fill", None),
                getattr(self, "_wave_fill_back", None),
            ):
                if item is None:
                    continue
                item.setProperty("statusText", message)
                item.setProperty("waveLoading", loading)

        def _show_wave_state(self, state, message=""):
            _prepare_wave_layer(self)
            self._wave_state = state
            self._wave_error = message
            self._set_wave_status(message, loading=state == "loading")
            if state != "ready":
                self._show_wave_fill(False)
                back = getattr(self, "_wave_fill_back", None)
                if back is not None:
                    back.setVisible(False)

        def _show_wave_fill(self, visible):
            """Show or hide the wave front slot through the scene property.

            The scene binds ``waveFill.visible`` to ``root.waveFillEnabled``,
            exactly as it binds the wind fill to ``root.fillEnabled``.  The
            property write is what survives a scene rebuild: it lands on the
            scene instance the viewport currently holds, while the item slot
            is only a handle onto one instance's child.  The direct
            ``setVisible`` keeps the slot in step when a scene does not carry
            the property at all (the barbs-only scenes).
            """
            visible = bool(visible)
            root = getattr(self, "_weather", None)
            if root is not None and root.property("waveFillEnabled") is not None:
                root.setProperty("waveFillEnabled", visible)
            item = getattr(self, "_wave_fill", None)
            if item is not None:
                item.setVisible(visible)

        def _show_wave_stage(self, item):
            """Draw the wave staging slot so its shader can compile.

            Qt Quick compiles a ``ShaderEffect`` only while it is rendered, so
            a slot that is not effectively visible never satisfies
            ``prepared``.  ``visible`` alone is not enough: a hidden container
            keeps the slot unrendered.  A container carries no content of its
            own, so it is unhidden the way ``_publish`` unhides the wind
            staging band; the slot's own opacity stays at the low staging
            value set by the caller.
            """
            if item is None:
                return
            item.setVisible(True)
            root = getattr(self, "_weather", None)
            parent_item = getattr(item, "parentItem", None)
            parent = parent_item() if parent_item is not None else None
            while parent is not None and parent is not root:
                if not parent.isVisible():
                    parent.setVisible(True)
                parent = parent.parentItem()

        def _wave_grid_rect(self, camera, lattice):
            # The wave fill shares the wind fill's reference placement, so a
            # pan that keeps one reference moves the whole layer together.
            map_camera = getattr(self, "_map_anchor_camera", None)
            reference = self._publication_manager.reference_camera
            if map_camera is not None:
                return _grid_rect(map_camera.view(), lattice)
            if reference is not None:
                ref_pan = PanReference.from_camera(
                    reference, getattr(reference, "dpr", 1.0)
                )
                dx, dy = ref_pan.translation_to(camera)
                rect = _grid_rect(reference.view(), lattice)
                return type(rect)(
                    rect.x() + dx, rect.y() + dy, rect.width(), rect.height()
                )
            return _grid_rect(camera.view(), lattice)

        def _update_wave(self, camera):
            """Publish the wave field's gridded form through the manager.

            The wave layer stages under ``LayerRole.WAVE_FILL`` exactly as the
            wind fill stages under ``LayerRole.FILL``: its bytes are charged
            to the wave layer, a replaced publication is rejected by token,
            and a rebase withdraws the frame, so an old forecast's heights are
            never drawn beside a new forecast.  A missing sample stays a NaN
            and the shader discards it, so no zero-height field is invented.
            """
            if not getattr(self, "_wave_visible", False):
                return
            if (
                getattr(self, "_wave_fill", None) is None
                or getattr(self, "_wave_fill_back", None) is None
            ):
                self._show_wave_state("unavailable", "Wave height unavailable here")
                return
            if camera is None:
                return
            model = self._wave_model
            if model is None:
                ensure_wave_model = getattr(self, "_ensure_wave_model", None)
                if ensure_wave_model is None or not ensure_wave_model():
                    self._show_wave_state("unavailable", "No wave forecast")
                    return
                model = self._wave_model
            if model.busy():
                # A preparation is already in flight; a camera change must
                # not queue a second demand for the same field.
                if self._wave_state != "ready":
                    self._show_wave_state("loading", "Loading wave height")
                return
            state, field, _selection = model.update(
                camera, self._barb_reference_view(camera)
            )
            if field is None or field.grid is None:
                self._show_wave_state(
                    "loading" if state == "loading" else "unavailable",
                    (
                        "Loading wave height"
                        if state == "loading"
                        else (model.error or "Wave height unavailable")
                    ),
                )
                return
            grid = field.grid
            key = f"{field.generation}/wave"
            manager = self._publication_manager
            layer = manager.layers[LayerRole.WAVE_FILL]
            epoch = manager.current_generation
            if (
                layer.visible is not None
                and layer.visible.generation == epoch
                and layer.visible_resources.get("key") == key
                and layer.pending is None
            ):
                # A covered pan reuses the retained frame: only its placement
                # moves, nothing is restaged or re-encoded.
                item = self._wave_fill
                item.setProperty("blend", field.staged.weight(camera.stamp()))
                self._place_fill_rect(item, self._wave_grid_rect(camera, grid.lattice))
                self._show_wave_state("ready", "")
                self._show_wave_fill(True)
                return
            budget = sum(image.sizeInBytes() for image in grid.images)
            delta = max(0, budget - layer.pending_budget_bytes)
            if not manager.budget.can_allocate(LayerRole.WAVE_FILL, delta):
                self._show_wave_state("unavailable", "Wave texture budget exceeded")
                return
            try:
                urls = self._provider.reserve_field((self, "wave"), grid.images, key)
            except ValueError:
                self._show_wave_state("unavailable", "Wave texture unavailable")
                return
            token = object()
            identity = LayerIdentity(
                role=LayerRole.WAVE_FILL,
                generation=epoch,
                source_key=(field.identity, key, camera.stamp()),
                content_hash=getattr(grid, "content_hash", str(grid.images)),
            )
            if not manager.stage_resources(
                LayerRole.WAVE_FILL,
                identity,
                LayerPlacement(),
                {ResourceKind.IMAGE: grid.images, "key": key, "urls": urls},
                budget,
                completion_token=token,
            ):
                self._provider.cancel_field((self, "wave"))
                self._show_wave_state("unavailable", "Wave texture budget exceeded")
                return
            self._wave_token = token
            item = self._wave_fill_back
            item.setProperty("blend", field.staged.weight(camera.stamp()))
            item.setProperty("gradientSmooth", self._wave_gradient_smooth)
            item.setProperty("gradientLevels", self._wave_gradient_levels)
            item.setOpacity(self._wave_opacity)
            self._place_fill_rect(item, self._wave_grid_rect(camera, grid.lattice))
            for name, url in zip(
                ("u0Source", "v0Source", "u1Source", "v1Source"), urls
            ):
                item.setProperty(name, QUrl(url))
            # Qt Quick compiles a ShaderEffect only when it is rendered, so a
            # fully hidden staging slot would never satisfy ``prepared`` and
            # the fill would always time out.  Draw it at 1% opacity -- the
            # same trick the native weather staging slots use -- so the
            # program compiles; the adoption gate still refuses to publish a
            # frame whose shader errored, and the opacity is restored to the
            # requested value when the slot is adopted.
            item.setOpacity(0.01)
            self._show_wave_stage(item)
            self._try_commit_wave(token)
            if isinstance(self, QObject):
                QTimer.singleShot(
                    10000, lambda staged=token: self._wave_timeout(staged)
                )

        def _wave_fill_changed(self, item):
            if not self._disposed and item is self._wave_fill_back:
                self._try_commit_wave(getattr(self, "_wave_token", None))

        def _try_commit_wave(self, token):
            if (
                self._disposed
                or token is None
                or token is not getattr(self, "_wave_token", None)
            ):
                return
            item = self._wave_fill_back
            if item is None:
                return
            layer = self._publication_manager.layers[LayerRole.WAVE_FILL]
            if layer.pending is None or layer.completion_token is not token:
                return
            if item.property("shaderFailed") or item.property("sourceError"):
                self._fail_wave(token, "Wave shader unavailable")
                return
            # A ShaderEffect is compiled only once it is rendered, so the
            # staging slot is drawn at 1% opacity while it waits (see
            # ``_update_wave``).  ``prepared`` is therefore "all four images
            # ready and the shader compiled", and waiting on it is what
            # guarantees the adopted frame can actually be drawn.  A failed
            # shader is reported by ``shaderFailed``; its ``shaderError`` log
            # can be empty, so that string alone is not a reliable signal.
            if not item.property("prepared"):
                return
            adopted, _ = self._publication_manager.verify_and_adopt(
                LayerRole.WAVE_FILL,
                lambda resources: len(resources.get(ResourceKind.IMAGE, ())) == 4
                and len(resources.get("urls", ())) == 4,
                expected_token=token,
            )
            if not adopted:
                self._fail_wave(token, "Wave frame rejected")
                return
            self._provider.promote_field((self, "wave"))
            self._wave_token = None
            old = self._wave_fill
            if old is not None:
                old.setVisible(False)
            self._wave_fill, self._wave_fill_back = item, old
            item.setOpacity(self._wave_opacity)
            self._show_wave_state("ready", "")
            self._show_wave_fill(getattr(self, "_wave_visible", False))

        def _fail_wave(self, token, message):
            if token is not getattr(self, "_wave_token", None):
                return
            self._publication_manager.reject_pending(LayerRole.WAVE_FILL, token)
            self._provider.cancel_field((self, "wave"))
            self._wave_token = None
            back = self._wave_fill_back
            if back is not None:
                back.setVisible(False)
                for name in ("u0Source", "v0Source", "u1Source", "v1Source"):
                    back.setProperty(name, QUrl())
            self._show_wave_state("unavailable", message)

        def _wave_timeout(self, token):
            self._fail_wave(token, "Wave height unavailable")

        # ------------------------------------------------- wave arrow layer
        def _wave_arrow_view(self, camera):
            """The current view thinned by the wave density, not the barbs'."""
            return wave_arrow_view(
                self._barb_reference_view(camera), self._wave_arrow_density
            )

        def _show_wave_arrow_state(self, state, message=""):
            """Record the arrow layer's state without touching the caption.

            The height fill owns the shared caption: an arrow update runs on
            its own cadence (every arrow adoption and camera change), so it
            must never put a loading string back over a drawn, adopted height
            field.  It only withdraws stale arrows.
            """
            _prepare_wave_layer(self)
            self._wave_arrows_state = state
            self._wave_arrows_error = message
            if state != "ready":
                for item in (
                    getattr(self, "_wave_barbs", None),
                    getattr(self, "_wave_barbs_back", None),
                ):
                    if item is not None:
                        item.setVisible(False)

        def _update_wave_arrows(self, camera):
            """Stage and publish WAVE_BARBS from the three wave bands.

            Each band is its own resident scalar field, and the layout is
            ``stage_wave_selection``'s: the wind selection's thinning, land
            policy and cap applied to the wave view's density.  If any band is
            missing no arrows are published and the explicit unavailable state
            is set, so a previous forecast's arrows are never left on screen.
            """
            if not getattr(self, "_wave_arrows_visible", False) or self._disposed:
                return
            models = self._ensure_wave_arrow_models()
            if models is None:
                self._show_wave_arrow_state(
                    "unavailable", "Wave arrows unavailable here"
                )
                return
            if camera is None:
                return
            view = self._wave_arrow_view(camera)
            fields = {}
            result = "ready"
            for band in WAVE_BANDS:
                worker = models[band]
                if worker.busy():
                    result = "loading"
                    continue
                state, field, _selection = worker.update(
                    camera, self._barb_reference_view(camera)
                )
                fields[band] = field
                if field is None:
                    if state == "loading" or result == "loading":
                        result = "loading"
                    else:
                        result = "unavailable"
            if not wave_bands_ready(fields):
                self._publication_manager.reject_pending(
                    LayerRole.WAVE_BARBS, self._wave_barbs_token
                )
                self._provider.cancel_field((self, "wave_barbs"))
                self._wave_barbs_token = None
                self._show_wave_arrow_state(
                    result,
                    (
                        "Loading wave arrows"
                        if result == "loading"
                        else (
                            next(
                                (m.error for m in models.values() if m.error),
                                "",
                            )
                            or "Wave direction unavailable"
                        )
                    ),
                )
                return
            sampler = getattr(models["wave_height"], "_sampler", None)
            land = getattr(models["wave_height"], "_land", None)
            try:
                selection = stage_wave_selection(
                    sampler,
                    fields,
                    camera.stamp(),
                    view,
                    land,
                    reference=view,
                )
            except Exception as exc:  # coverage, land tier or time escape
                # A band that does not cover this view, or a camera stamp that
                # crossed a source-selection event, leaves the layer explicitly
                # unavailable; stale arrows are withdrawn, never kept on screen.
                logger.debug("Wave arrow selection unavailable: %s", exc)
                selection = None
            if selection is None:
                self._provider.cancel_field((self, "wave_barbs"))
                self._wave_barbs_token = None
                self._show_wave_arrow_state("unavailable", "Wave arrows unavailable")
                return
            self._publish_wave_barbs(fields, selection, camera)

        def _publish_wave_barbs(self, fields, selection, camera):
            """Publish six scalar endpoint images and the selection anchors.

            The layer stages under ``LayerRole.WAVE_BARBS`` with its own
            budget and token, exactly as ``WAVE_FILL`` stages its gridded
            form.  It binds u0..u5 in ``wave_barbs_images`` order and the
            node-id/anchor textures the glyph QML expects.
            """
            if self._wave_barbs is None or self._wave_barbs_back is None:
                self._show_wave_arrow_state(
                    "unavailable", "Wave arrows unavailable here"
                )
                return
            images = wave_barbs_images(fields)
            stamp = camera.stamp()
            key = "/".join(f"{band}:{fields[band].generation}" for band in WAVE_BANDS)
            key = f"{key}/{self._wave_arrow_density}/{stamp}"
            manager = self._publication_manager
            layer = manager.layers[LayerRole.WAVE_BARBS]
            if (
                layer.visible is not None
                and layer.visible.generation == manager.current_generation
                and layer.visible_resources.get("key") == key
                and layer.pending is None
            ):
                self._wave_barbs.setVisible(True)
                self._show_wave_arrow_state("ready", "")
                return
            budget = sum(image.sizeInBytes() for image in images)
            delta = max(0, budget - layer.pending_budget_bytes)
            if not manager.budget.can_allocate(LayerRole.WAVE_BARBS, delta):
                self._show_wave_arrow_state(
                    "unavailable", "Wave arrow texture budget exceeded"
                )
                return
            try:
                urls = self._provider.reserve_field((self, "wave_barbs"), images, key)
                selection_urls = self._provider.publish_selection(
                    selection.images, f"wave:{selection.generation}"
                )
            except ValueError:
                self._provider.cancel_field((self, "wave_barbs"))
                self._show_wave_arrow_state(
                    "unavailable", "Wave arrow texture unavailable"
                )
                return
            token = object()
            identity = LayerIdentity(
                role=LayerRole.WAVE_BARBS,
                generation=manager.current_generation,
                source_key=(
                    tuple(fields[band].identity for band in WAVE_BANDS),
                    key,
                    stamp,
                ),
                content_hash=str((key, selection.generation)),
            )
            resources = {
                ResourceKind.IMAGE: images,
                "key": key,
                "urls": urls,
                "selection_urls": selection_urls,
            }
            if not manager.stage_resources(
                LayerRole.WAVE_BARBS,
                identity,
                LayerPlacement(),
                resources,
                budget,
                completion_token=token,
            ):
                self._provider.cancel_field((self, "wave_barbs"))
                self._show_wave_arrow_state(
                    "unavailable", "Wave arrow texture budget exceeded"
                )
                return
            self._wave_barbs_token = token
            item = self._wave_barbs_back
            for name, url in zip(
                (
                    "u0Source",
                    "u1Source",
                    "u2Source",
                    "u3Source",
                    "u4Source",
                    "u5Source",
                ),
                urls,
            ):
                item.setProperty(name, QUrl(url))
            for name, url in zip(
                ("nodeIdSource", "anchorXSource", "anchorYSource"),
                selection_urls,
            ):
                item.setProperty(name, QUrl(url))
            # Anchors are encoded for the camera that produced them; a pan
            # inside one reference reuses them and a rebase invalidates them.
            offset = self._barb_anchor_offset(camera, selection)
            item.setProperty("anchorFrameValid", offset is not None)
            if offset is not None:
                item.setProperty("anchorOffsetX", offset[0])
                item.setProperty("anchorOffsetY", offset[1])
            rows, cols = selection.shape
            item.setProperty("columns", cols)
            item.setProperty("rows", rows)
            item.setProperty("nodeIdIndirection", True)
            item.setProperty("fieldColumns", FIELD_COLUMNS)
            item.setProperty("blend", fields["wave_height"].staged.weight(stamp))
            item.setOpacity(self._wind_opacity)
            item.setVisible(False)
            self._try_commit_wave_barbs(token)
            if isinstance(self, QObject):
                QTimer.singleShot(
                    10000, lambda staged=token: self._wave_barbs_timeout(staged)
                )

        def _wave_barbs_changed(self, item):
            if not self._disposed and item is self._wave_barbs_back:
                self._try_commit_wave_barbs(getattr(self, "_wave_barbs_token", None))

        def _try_commit_wave_barbs(self, token):
            if (
                self._disposed
                or token is None
                or token is not getattr(self, "_wave_barbs_token", None)
            ):
                return
            item = self._wave_barbs_back
            if item is None:
                return
            layer = self._publication_manager.layers[LayerRole.WAVE_BARBS]
            if layer.pending is None or layer.completion_token is not token:
                return
            if item.property("shaderError") or item.property("sourceError"):
                self._fail_wave_barbs(token, "Wave arrow shader unavailable")
                return
            if not item.property("prepared"):
                return
            adopted, _ = self._publication_manager.verify_and_adopt(
                LayerRole.WAVE_BARBS,
                lambda resources: (
                    len(resources.get(ResourceKind.IMAGE, ())) == 6
                    and len(resources.get("urls", ())) == 6
                    and len(resources.get("selection_urls", ())) == 3
                ),
                expected_token=token,
            )
            if not adopted:
                self._fail_wave_barbs(token, "Wave arrow frame rejected")
                return
            self._provider.promote_field((self, "wave_barbs"))
            self._wave_barbs_token = None
            old = self._wave_barbs
            if old is not None:
                old.setVisible(False)
            self._wave_barbs, self._wave_barbs_back = item, old
            item.setOpacity(self._wind_opacity)
            item.setVisible(getattr(self, "_wave_arrows_visible", False))
            self._show_wave_arrow_state("ready", "")

        def _fail_wave_barbs(self, token, message):
            if token is not getattr(self, "_wave_barbs_token", None):
                return
            self._publication_manager.reject_pending(
                LayerRole.WAVE_BARBS, expected_token=token
            )
            self._provider.cancel_field((self, "wave_barbs"))
            self._wave_barbs_token = None
            back = self._wave_barbs_back
            if back is not None:
                back.setVisible(False)
                for name in (
                    "u0Source",
                    "u1Source",
                    "u2Source",
                    "u3Source",
                    "u4Source",
                    "u5Source",
                    "nodeIdSource",
                    "anchorXSource",
                    "anchorYSource",
                ):
                    back.setProperty(name, QUrl())
            self._show_wave_arrow_state("unavailable", message)

        def _wave_barbs_timeout(self, token):
            self._fail_wave_barbs(token, "Wave arrows unavailable")

        def _refresh_streamlines(self):
            if (
                not self._disposed
                and self._camera is not None
                and not self._streamline_settle.isActive()
            ):
                self._publish_streamlines(self._camera, force=True)

        def _update_streamline_camera(self, camera):
            # Use shared pan reference so streamlines move with the map.
            reference = getattr(self, "_map_pan_reference", None)
            if reference is not None:
                try:
                    dx, dy = reference.translation_to(camera)
                except ValueError:
                    # Zoom/size/DPR changed - reference cannot be translated;
                    # fall back to the current camera until it is rebased.
                    view = camera.view()
                    size = 256.0 * 2.0**camera.zoom
                    center = QVector2D(
                        view._lon_to_x(camera.center_lon) / size,
                        view._lat_to_y(camera.center_lat) / size,
                    )
                    self._streamlines.setProperty("cameraCenter", center)
                    self._streamlines.setProperty("cameraZoom", camera.zoom)
                    return
                size = 256.0 * 2.0**reference.zoom
                center = QVector2D(0.5 - dx / size, 0.5 - dy / size)
                self._streamlines.setProperty("cameraCenter", center)
                self._streamlines.setProperty("cameraZoom", reference.zoom)
            else:
                # No shared pan reference yet, use current camera
                view = camera.view()
                size = 256.0 * 2.0**camera.zoom
                center = QVector2D(
                    view._lon_to_x(camera.center_lon) / size,
                    view._lat_to_y(camera.center_lat) / size,
                )
                self._streamlines.setProperty("cameraCenter", center)
                self._streamlines.setProperty("cameraZoom", camera.zoom)

        def _retrace_streamlines(self):
            if (
                self._disposed
                or self._camera is None
                or self._wind_style != "streamlines"
            ):
                return
            self._publish_streamlines(self._camera, force=True)

        @staticmethod
        def _streamline_view_transform(source, target):
            source_view = source.view()
            target_view = target.view()
            scale = 2.0 ** (target.zoom - source.zoom)
            world_width = target_view.TILE_SIZE * 2.0**target.zoom
            source_x = source_view._lon_to_x(source.center_lon)
            target_x = target_view._lon_to_x(target.center_lon)
            source_y = source_view._lat_to_y(source.center_lat)
            target_y = target_view._lat_to_y(target.center_lat)
            dx = (source_x * scale - target_x) % world_width
            if dx > world_width / 2.0:
                dx -= world_width
            return (
                scale,
                target.width / 2.0 - source.width / 2.0 * scale + dx,
                target.height / 2.0
                - source.height / 2.0 * scale
                + source_y * scale
                - target_y,
                world_width,
            )

        def _streamline_readiness_changed(self):
            debug_streamlines = os.environ.get("GWR_STREAMLINE_DEBUG")
            if (
                self._disposed
                or self._wind_style != "streamlines"
                or self._streamline_model._state != "ready"
                or (
                    self._streamline_publication is None
                    and self._pending_streamline_publication is None
                )
            ):
                return
            if debug_streamlines:
                logger.warning(
                    "streamlines readiness: state=%s front=%s pending=%s "
                    "front_ready=%s pending_ready=%s visible=%s opacity=%.2f",
                    self._streamline_model._state,
                    self._streamlines.property("frontBuffer"),
                    self._streamlines.property("pendingBuffer"),
                    self._streamlines.property("ready"),
                    self._streamlines.property("pendingReady"),
                    self._streamlines.isVisible(),
                    self._streamlines.opacity(),
                )
            error = self._streamlines.property("shaderError")
            if error:
                self._show_state("unavailable")
                self._status.setText(str(error))
                self._status.adjustSize()
            else:
                if self._streamlines.property("pendingError"):
                    pending_error = str(self._streamlines.property("pendingError"))
                    pending_buffer = self._pending_streamline_buffer
                    if pending_buffer == 0:
                        self._streamlines.setProperty("buffer0Groups", [])
                    elif pending_buffer == 1:
                        self._streamlines.setProperty("buffer1Groups", [])
                    self._streamlines.setProperty("pendingBuffer", -1)
                    self._streamline_provider.retain(self._streamline_urls)
                    self._pending_streamline_groups = []
                    self._pending_streamline_urls = ()
                    self._pending_streamline_camera = None
                    self._pending_streamline_publication = None
                    self._pending_streamline_buffer = -1
                    self._show_state("unavailable")
                    self._status.setText(pending_error)
                    self._status.adjustSize()
                    return
                if self._streamlines.property("pendingReady"):
                    old_front = self._streamlines.property("frontBuffer")
                    new_front = self._pending_streamline_buffer
                    if debug_streamlines:
                        logger.warning(
                            "streamlines swap: %s -> %s, groups=%d, camera=%r",
                            old_front,
                            new_front,
                            len(self._pending_streamline_groups),
                            self._pending_streamline_camera,
                        )
                    self._streamlines.setProperty("frontBuffer", new_front)
                    self._streamlines.setProperty("pendingBuffer", -1)
                    if old_front == 0:
                        self._streamlines.setProperty("buffer0Groups", [])
                    elif old_front == 1:
                        self._streamlines.setProperty("buffer1Groups", [])
                    self._streamline_urls = self._pending_streamline_urls
                    self._streamlines.setOpacity(self._wind_opacity)
                    self._streamlines.setVisible(True)
                    self._streamline_provider.retain(self._streamline_urls)
                    self._pending_streamline_groups = []
                    self._pending_streamline_urls = ()
                    self._streamline_display_camera = self._pending_streamline_camera
                    self._streamline_publication = self._pending_streamline_publication
                    self._pending_streamline_camera = None
                    self._pending_streamline_publication = None
                    self._pending_streamline_buffer = -1
                elif self._pending_streamline_publication is not None:
                    self._show_state("loading")
                    previous = self._streamline_display_camera
                    pending = self._pending_streamline_camera
                    self._status.setText(
                        "Updating streamlines — previous forecast time"
                        if previous is not None
                        and pending is not None
                        and previous.time != pending.time
                        else "Updating streamlines — preparing display"
                    )
                    self._status.adjustSize()
                    return
                self._show_state(
                    "ready" if self._streamlines.property("ready") else "loading"
                )

        def _publish_streamlines(self, camera, force=False):
            debug_streamlines = os.environ.get("GWR_STREAMLINE_DEBUG")
            enabled = self._wind_style == "streamlines" and self._wind_opacity > 0
            gpu_enabled = getattr(self, "_gpu_streamlines_enabled", False)
            gpu_item = cast(Any, getattr(self, "_gpu_streamlines_item", None))
            self._update_streamline_camera(camera)
            if self._gpu_streamlines_error:
                if gpu_item is not None:
                    gpu_item.set_trace_input(None)
                    gpu_item.setVisible(False)
                self._show_state("unavailable")
                return "unavailable"
            camera_changed = camera != self._streamline_last_camera
            self._streamline_last_camera = camera
            prior: ViewportCamera | None = cast(
                ViewportCamera | None, self._streamline_display_camera
            )
            prior_time = prior.time if prior is not None else camera.time
            if gpu_item is not None:
                if not enabled:
                    gpu_item.set_trace_input(None)
                    gpu_item.setVisible(False)
                elif prior is not None and (
                    prior.center_lat,
                    prior.center_lon,
                    prior.zoom,
                    prior.width,
                    prior.height,
                ) != (
                    camera.center_lat,
                    camera.center_lon,
                    camera.zoom,
                    camera.width,
                    camera.height,
                ):
                    scale, offset_x, offset_y, world_width = (
                        self._streamline_view_transform(prior, camera)
                    )
                    gpu_item.set_view_transform(
                        camera.width,
                        camera.height,
                        scale,
                        offset_x,
                        offset_y,
                        world_width,
                    )
                    gpu_item.set_opacity(self._wind_opacity * 0.6)
                    gpu_item.setVisible(True)
                    self._show_state("loading")
                    self._status.setText(
                        "Updating streamlines — previous time and coverage"
                        if prior.time != camera.time
                        else "Updating streamlines — previous coverage"
                    )
                    self._status.adjustSize()
                    if not force:
                        self._streamline_settle.start()
                        return "loading"
                elif prior is not None and prior.time != camera.time:
                    gpu_item.set_opacity(self._wind_opacity * 0.6)
            same_trace = prior is not None and (
                prior.time,
                prior.density,
                prior.over_land,
            ) == (camera.time, camera.density, camera.over_land)
            if (
                same_trace
                and not force
                and self._streamline_publication is not None
                and (self._streamline_urls or gpu_enabled)
            ):
                if gpu_enabled:
                    if not camera_changed:
                        gpu_item.set_opacity(self._wind_opacity)
                        gpu_item.setVisible(enabled)
                        return "ready"
                else:
                    self._streamlines.setVisible(enabled)
                    self._streamlines.setOpacity(self._wind_opacity)
                    if not camera_changed:
                        self._streamline_readiness_changed()
                        return "ready"
                    self._streamline_settle.start()
                    if prior_time == camera.time:
                        self._streamline_readiness_changed()
                        return "ready"
            else:
                self._streamline_settle.stop()
            state, paths = self._streamline_model.update(camera, enabled)
            preview = False
            if gpu_enabled and enabled and state == "loading":
                from .gpustreamlines import GpuTraceInput

                snapshot = getattr(self._streamline_model, "snapshot", None)
                candidate = snapshot(camera) if callable(snapshot) else None
                if (
                    isinstance(candidate, tuple)
                    and len(candidate) == 2
                    and isinstance(candidate[0], ViewportCamera)
                    and isinstance(candidate[1], GpuTraceInput)
                ):
                    represented, trace = candidate
                    if (
                        represented.center_lat,
                        represented.center_lon,
                        represented.zoom,
                        represented.width,
                        represented.height,
                        represented.density,
                        represented.over_land,
                        represented.geographic,
                    ) == (
                        camera.center_lat,
                        camera.center_lon,
                        camera.zoom,
                        camera.width,
                        camera.height,
                        camera.density,
                        camera.over_land,
                        camera.geographic,
                    ):
                        preview = True
                        state, paths, camera = "preview", trace, represented
            if (
                gpu_enabled
                and enabled
                and state == "unavailable"
                and self._streamline_model.error
                == "No traceable wind cells in selected forecast"
            ):
                self._gpu_streamlines_failed(self._streamline_model.error)
                return self._streamline_model._state
            if debug_streamlines and state != "ready":
                logger.warning(
                    "streamlines awaiting trace: state=%s time=%s front=%s "
                    "pending=%s visible=%s",
                    state,
                    camera.time,
                    self._streamlines.property("frontBuffer"),
                    self._streamlines.property("pendingBuffer"),
                    self._streamlines.isVisible(),
                )
            from sailworks.rendering.streamlinepaths import (
                paths_to_render_buffers,
            )
            from sailworks.rendering.windpainter import barb_bucket_colors
            from PySide6.QtGui import QColor
            from .viewportstreamlines import MAX_BATCH_SIZE

            if state not in ("ready", "preview") or paths is None:
                if gpu_item is not None and state in (
                    "hidden",
                    "unavailable",
                ):
                    gpu_item.set_trace_input(None)
                    gpu_item.setVisible(False)
                source = self._streamline_display_camera
                retain_previous = (
                    state == "loading"
                    and enabled
                    and source is not None
                    and source.over_land == camera.over_land
                )
                if retain_previous and source is not None:
                    self._streamlines.setOpacity(self._wind_opacity)
                    self._streamlines.setVisible(True)
                else:
                    self._streamline_publication = None
                    self._streamline_display_camera = None
                    self._streamline_urls = ()
                    self._streamlines.setProperty("buffer0Groups", [])
                    self._streamlines.setProperty("buffer1Groups", [])
                    self._streamlines.setProperty("frontBuffer", -1)
                    self._streamlines.setProperty("pendingBuffer", -1)
                    self._streamlines.setVisible(False)
                if self._wind_style == "streamlines":
                    self._show_state(state)
                    if retain_previous and source is not None:
                        self._status.setText(
                            "Updating streamlines — previous forecast time"
                            if source.time != camera.time
                            else "Updating streamlines — updating coverage"
                        )
                        self._status.adjustSize()
                    elif state == "hidden":
                        self._status.hide()
                    elif state == "unavailable":
                        self._status.setText(self._streamline_model.error)
                        self._status.adjustSize()
                return state
            publication = (id(paths), camera)
            if publication == self._streamline_publication and not force:
                if gpu_item is not None and gpu_enabled:
                    gpu_item.set_opacity(self._wind_opacity * (0.6 if preview else 1.0))
                    gpu_item.setVisible(enabled)
                    if preview and self._wind_style == "streamlines":
                        self._show_state("loading")
                        self._status.setText(
                            f"Updating streamlines — showing {camera.time:%Y-%m-%d %H:%M UTC}"
                        )
                        self._status.adjustSize()
                        return "loading"
                else:
                    self._streamlines.setOpacity(self._wind_opacity)
                    self._streamlines.setVisible(enabled)
                self._streamline_readiness_changed()
                return state
            if gpu_enabled:
                from .gpustreamlines import GpuTraceInput

                if isinstance(paths, GpuTraceInput):
                    if gpu_item is None:
                        raise RuntimeError("GPU streamline layer is not initialized")
                    self._streamlines.setVisible(False)
                    same_layout = prior is not None and (
                        prior.center_lat,
                        prior.center_lon,
                        prior.zoom,
                        prior.width,
                        prior.height,
                        prior.density,
                        prior.over_land,
                    ) == (
                        camera.center_lat,
                        camera.center_lon,
                        camera.zoom,
                        camera.width,
                        camera.height,
                        camera.density,
                        camera.over_land,
                    )
                    gpu_item.set_trace_input(
                        replace(paths, opacity=self._wind_opacity),
                        transition=same_layout and prior_time != camera.time,
                    )
                    gpu_item.setVisible(enabled)
                    self._streamline_publication = publication
                    self._streamline_display_camera = camera
                    self._streamline_urls = ()
                    if preview:
                        gpu_item.set_opacity(self._wind_opacity * 0.6)
                        self._show_state("loading")
                        self._status.setText(
                            f"Updating streamlines — showing {camera.time:%Y-%m-%d %H:%M UTC}"
                        )
                        self._status.adjustSize()
                        return "loading"
                    self._show_state("ready")
                    return "ready"
            groups = []
            buffers = paths_to_render_buffers(paths, camera.over_land)
            for bucket, (lines, arrows) in buffers.items():
                for arrow, segments in ((False, lines), (True, arrows)):
                    if not len(segments):
                        continue
                    for offset in range(0, len(segments), MAX_BATCH_SIZE):
                        batch = segments[offset : offset + MAX_BATCH_SIZE]
                        try:
                            url, count = self._streamline_provider.publish(
                                bucket, batch
                            )
                        except (ValueError, MemoryError) as exc:
                            self._streamline_model.error = (
                                f"Streamline encoding failed: {exc}"
                            )
                            # Failed staging must not tear down the front buffer.
                            self._show_state("unavailable")
                            self._status.setText(self._streamline_model.error)
                            self._status.adjustSize()
                            return
                        groups.append(
                            {
                                "source": url,
                                "count": count,
                                "color": QColor.fromRgbF(*barb_bucket_colors()[bucket]),
                                "arrow": arrow,
                            }
                        )
            self._pending_streamline_groups = groups
            self._pending_streamline_urls = tuple(group["source"] for group in groups)
            self._pending_streamline_camera = camera
            self._pending_streamline_publication = publication
            front_buffer = self._streamlines.property("frontBuffer")
            pending_buffer = 0 if front_buffer != 0 else 1
            self._pending_streamline_buffer = pending_buffer
            self._streamlines.setProperty("pendingBuffer", pending_buffer)
            self._streamlines.setProperty(f"buffer{pending_buffer}Groups", groups)
            self._streamlines.setOpacity(self._wind_opacity)
            self._streamlines.setVisible(enabled)
            if not groups:
                self._streamlines.setProperty("buffer0Groups", [])
                self._streamlines.setProperty("buffer1Groups", [])
                self._streamlines.setProperty("frontBuffer", -1)
                self._streamlines.setProperty("pendingBuffer", -1)
                self._streamline_display_camera = None
                self._streamline_publication = None
                self._streamline_urls = ()
                self._streamline_provider.retain(())
                self._show_state("unavailable")
                self._status.setText("No visible streamlines in the selected forecast")
                self._status.adjustSize()
                return "unavailable"
            self._streamline_provider.retain(
                self._streamline_urls + self._pending_streamline_urls
            )
            if debug_streamlines:
                logger.warning(
                    "streamlines staged: segments=%d groups=%d front=%s "
                    "pending=%s pending_ready=%s visible=%s diagnostics=%s",
                    sum(group["count"] for group in groups),
                    len(groups),
                    self._streamlines.property("frontBuffer"),
                    self._streamlines.property("pendingBuffer"),
                    self._streamlines.property("pendingReady"),
                    self._streamlines.isVisible(),
                    self._streamlines.property("batchDiagnostics"),
                )
            self._streamline_readiness_changed()
            return state

        def _gpu_streamlines_failed(self, reason):
            if self._gpu_streamlines_error:
                return
            self._gpu_streamlines_error = f"GPU streamlines unavailable: {reason}"
            logger.warning(self._gpu_streamlines_error)
            if self._gpu_streamlines_item is not None:
                self._gpu_streamlines_item.set_trace_input(None)
                self._gpu_streamlines_item.setVisible(False)
            self._streamline_publication = None
            self._show_state("unavailable")
            self._streamline_display_camera = None
            self._streamline_urls = ()
            self._streamline_publication = None

        def dispose(self):
            if self._disposed:
                return
            self._disposed = True
            if not self._native_weather_fields:
                self._cancel_image_fill(withdraw=True)
            self._marine_layer.close()
            self._icoads_layer.close()
            self._streamline_model.close()
            self._streamline_settle.stop()
            self._streamlines.setVisible(False)
            if self._gpu_streamlines_item is not None:
                self._gpu_streamlines_item.set_trace_input(None)
                self._gpu_streamlines_item.setVisible(False)
            self._source_generation += 1
            self._wheel_zoom_tick.stop()
            self._wheel_zoom_settle.stop()
            self._wheel_scroll_settle.stop()
            self._pan_inertia_tick.stop()
            for context in self._native_contexts.values():
                context.close()
            self._clear_weather_sources()
            self._model.on_change = None
            self._model.close()
            if self._wave_model is not None:
                self._wave_model.on_change = None
                self._wave_model.close()
                self._wave_model = None
            for worker in self._wave_arrow_models.values():
                worker.on_change = None
                worker.close()
            self._wave_arrow_models = {}
            self._provider.release_fields((self, "wave"))
            self._provider.release_fields((self, "wave_barbs"))
            if shiboken6.isValid(self._chart_timer):
                self._chart_timer.stop()
            if shiboken6.isValid(self._chart_source_timer):
                self._chart_source_timer.stop()
            if self._chart_model is not None:
                self._chart_model.close()
            self._provider.clear()
            self._streamline_provider.clear()
            if self._chart_provider is not None:
                self._chart_provider.clear()
            if self._navigation_provider is not None:
                self._navigation_provider.clear()
            if self._dashboard_provider is not None:
                self._dashboard_provider.clear()
            if self._environment_provider is not None:
                self._environment_provider.clear()
            if self._base_provider is not None:
                self._base_provider.clear()
            if self._world_provider is not None:
                self._world_provider.clear()
            timer = getattr(self, "_satellite_timer", None)
            if timer is not None:
                timer.stop()
            retry = getattr(self, "_satellite_retry_timer", None)
            if retry is not None:
                retry.stop()
            worker = getattr(self, "_satellite_worker", None)
            if worker is not None:
                worker.stop()
                worker.join(2.0)
                self._satellite_worker = None
            provider = getattr(self, "_satellite_provider", None)
            if provider is not None:
                provider.clear()
            if shiboken6.isValid(self._weather):
                self._weather.setVisible(False)

        def _clear_weather_sources(self):
            _prepare_wave_layer(self)
            # Clearing the CPU provider alone leaves QML Images holding the
            # previous source's native textures. Retire both ownership layers.
            pool = self._shared_anchor_pool()
            for item, names in (
                (
                    self._barbs,
                    (
                        "u0Source",
                        "v0Source",
                        "u1Source",
                        "v1Source",
                        "nodeIdSource",
                        "anchorXSource",
                        "anchorYSource",
                    ),
                ),
                (
                    self._barbs_back,
                    (
                        "u0Source",
                        "v0Source",
                        "u1Source",
                        "v1Source",
                        "nodeIdSource",
                        "anchorXSource",
                        "anchorYSource",
                    ),
                ),
                (self._fill, ("u0Source", "v0Source", "u1Source", "v1Source")),
                (self._fill_back, ("u0Source", "v0Source", "u1Source", "v1Source")),
                (
                    self._wave_barbs,
                    (
                        "u0Source",
                        "u1Source",
                        "u2Source",
                        "u3Source",
                        "u4Source",
                        "u5Source",
                        "nodeIdSource",
                        "anchorXSource",
                        "anchorYSource",
                    ),
                ),
                (
                    self._wave_barbs_back,
                    (
                        "u0Source",
                        "u1Source",
                        "u2Source",
                        "u3Source",
                        "u4Source",
                        "u5Source",
                        "nodeIdSource",
                        "anchorXSource",
                        "anchorYSource",
                    ),
                ),
            ):
                if item is not None and shiboken6.isValid(item):
                    for name in names:
                        if pool is not None and name in (
                            "anchorXSource",
                            "anchorYSource",
                        ):
                            # The scene-owned atlas slots are cleared below.
                            continue
                        item.setProperty(name, QUrl())
                    if names[0] == "u0Source" and "nodeIdSource" in names:
                        # The next selection is published at the camera that
                        # produced it; a cleared layer starts unshifted.
                        item.setProperty("anchorOffsetX", 0.0)
                        item.setProperty("anchorOffsetY", 0.0)
                    if self._native_weather_fields:
                        item.setProperty("nativeReady", False)
                        item.setProperty("nativeFrameObserved", False)
            if any(
                slot is not None
                for slot in getattr(self, "_shared_anchor_items", None) or ()
            ):
                # A partially initialised pool still owns loaded textures: clear
                # every declared slot rather than only a complete pool.
                self._clear_shared_anchors()

        def _refresh(self, generation):
            if generation != self._source_generation or self._disposed:
                return
            if self._camera is not None:
                self.set_camera(self._camera)
                if getattr(self, "_chart_model", None) is not None:
                    self._restart_chart_settle()

        def _set_weather_visible(self, visible):
            """Chart scenes remain visible even when weather has no data."""
            if not self._native_weather_fields:
                self._image_weather_visible = visible
                if not visible:
                    self._cancel_image_fill()
            if (
                getattr(self, "_chart_layer", False)
                or getattr(self, "_enc_chart_layer", False)
                or self._native_weather_fields
            ):
                self._weather.setVisible(True)
                self._barbs.setVisible(visible)
                if self._fill is not None:
                    self._fill.setVisible(
                        visible
                        and self._wind_gradient_enabled
                        and (
                            self._native_weather_fields
                            or self._image_fill_matches_requested()
                        )
                    )
                if not visible and self._native_weather_fields:
                    self._barbs_back.setVisible(False)
                    if self._fill_back is not None:
                        self._fill_back.setVisible(False)
            else:
                self._weather.setVisible(visible)
                if self._fill is not None:
                    self._fill.setVisible(
                        visible
                        and self._wind_gradient_enabled
                        and self._image_fill_matches_requested()
                    )

        def _image_fill_matches_requested(self):
            layer = self._publication_manager.layers["fill"]
            return layer.visible is not None and layer.visible_resources.get(
                "key"
            ) == getattr(self, "_image_fill_requested_key", None)

        def set_camera(self, camera):
            """Adopt a camera and publish the portrayal it may show."""
            if self._disposed:
                return "unavailable"
            began = time.perf_counter()
            state = "loading"
            try:
                previous_camera = self._camera
                current_dpr = self._native_publication_dpr()
                previous_dpr = getattr(self, "_camera_dpr", None)
                self._camera_dpr = current_dpr
                # Retire anchors encoded for the frame this camera replaces
                # before the shared map reference (and any rebase offset) is
                # rewritten. A zoom, resize or ratio change cannot be placed;
                # a pan or rebase keeps its anchors displayable.
                self._retire_barb_anchor_frame(previous_camera, camera, previous_dpr)
                self._camera = camera
                map_anchor_changed = self._update_shared_map_pan(camera)
                self._place_world(camera)
                # After the shared map reference has been reset for this
                # camera, a zoom may keep the retained front visible through
                # the temporary preview transform instead of hiding it.
                self._maybe_arm_barb_zoom_preview(
                    previous_camera, camera, previous_dpr, current_dpr
                )
                if previous_camera is not None and (
                    previous_camera.zoom != camera.zoom
                    or previous_camera.width != camera.width
                    or previous_camera.height != camera.height
                ):
                    preview = getattr(self, "_preview_retained_imagery", None)
                    if preview is not None:
                        preview(camera)
                self._marine_layer.set_camera(camera)
                self._icoads_layer.set_camera(camera)
                if self._chart_model is not None:
                    previous_chart_camera = self._chart_camera
                    if self._chart_camera != camera:
                        self._chart_camera = camera
                        self._chart_camera_changed = time.perf_counter()
                        self._restart_chart_settle()
                        self._request_interactive_chart()
                    # Show retained image as a scaled preview during zoom when ENC is off.
                    # (Desktop viewport sets enc_chart_layer=True as a capability;
                    # the preview should activate whenever ENC isn't actually enabled.)
                    enc_enabled = getattr(self._chart_model, "_enc_enabled", False)
                    if (
                        not enc_enabled
                        and previous_chart_camera is not None
                        and previous_chart_camera.zoom != camera.zoom
                        and self._chart_model._published is not None
                    ):
                        # Show the retained image as a scaled preview while
                        # the new native-scale composition waits for the view
                        # to settle. The QML status makes the preview explicit.
                        cast(QObject, self._chart).setProperty("chartLoading", True)
                    # Only withdraw the chart image immediately when ENC is actually
                    # enabled. When ENC is off, keep the retained image as a scaled
                    # preview (the non-ENC path above) to avoid blanking the basemap
                    # on zoom/time changes.
                    if enc_enabled and chart_withdrawn_on_camera_change(
                        previous_chart_camera,
                        camera,
                        self._chart_model._published is not None,
                    ):
                        # Withdraw old-scale/time ENC immediately, not after
                        # the settle timer or the replacement worker finishes.
                        self._clear_chart()
                # Check if the camera change invalidates the pan reference
                # The wave layer is optional: resolve its members before the
                # rebase and refresh branches read or write them.
                _prepare_wave_layer(self)
                dpr = self.devicePixelRatio()
                reference = self._publication_manager.reference_camera
                if reference is not None:
                    ref_pan = PanReference.from_camera(
                        reference, getattr(reference, "dpr", 1.0)
                    )
                    change = ref_pan.classify(camera, dpr)
                    if change == CameraChange.PAN:
                        # Keep the retained layers in one shared reference.
                        # PanReference.translation_to keeps longitude
                        # continuous across the antimeridian.
                        pass
                    else:
                        # Reference invalidated - rebase all layers
                        if not self._native_weather_fields:
                            self._cancel_image_fill(withdraw=True)
                        new_gen = self._publication_manager.current_generation + 1
                        self._publication_manager.set_generation(new_gen)
                        self._publication_manager.rebase_all(camera, new_gen)
                        # The rebase withdrew the wave frame's generation: do
                        # not keep drawing it while the new one is staged.
                        self._wave_token = None
                        for item in (
                            getattr(self, "_wave_fill", None),
                            getattr(self, "_wave_fill_back", None),
                        ):
                            if item is not None:
                                item.setVisible(False)
                        # The rebase withdrew the wave barbs' generation as
                        # well: hide the stale arrows until they restage.
                        self._wave_barbs_token = None
                        for item in (
                            getattr(self, "_wave_barbs", None),
                            getattr(self, "_wave_barbs_back", None),
                        ):
                            if item is not None:
                                item.setVisible(False)
                else:
                    # No reference yet, create one
                    self._publication_manager.set_reference_camera(camera)

                if getattr(self, "_wave_visible", False):
                    self._update_wave(camera)
                if getattr(self, "_wave_arrows_visible", False):
                    self._update_wave_arrows(camera)

                if map_anchor_changed or getattr(self, "_map_content", None) is None:
                    self._place_chart()
                # Publish chart and route placement in the same camera pass,
                # before weather updates can delay the cameraChanged signal.
                self._place_route_overlay(camera, map_anchor_changed)
                self._publish_streamlines(camera)
                if self._wind_style == "streamlines":
                    self._set_weather_visible(False)
                    self.cameraChanged.emit(camera)
                    return self._state
                state, field, selection = self._model.update(
                    camera, self._barb_reference_view(camera)
                )
                if state != "ready":
                    previous = self._weather_display_camera
                    preview = (
                        state == "loading"
                        and previous is not None
                        and previous.time != camera.time
                        and self._same_weather_view(previous, camera)
                    )
                    zoom_preview = (
                        state == "loading"
                        and getattr(self, "_barb_zoom_preview", None) is not None
                        and self._barb_zoom_preview_holds(camera, current_dpr)
                    )
                    if zoom_preview:
                        # The retained front is the previous frame, placed by
                        # the preview transform.  It is explicitly a loading
                        # preview: never report it as the current selection.
                        self._set_weather_visible(True)
                        self._show_state(
                            state,
                            "Updating wind — showing previous-frame zoom preview",
                        )
                    elif preview:
                        # Keep the last complete portrayal during time-only
                        # staging. Label it explicitly so previous-time data
                        # cannot be mistaken for the requested forecast.
                        self._set_weather_visible(True)
                        self._show_state(
                            state, "Updating wind — showing previous forecast time"
                        )
                    else:
                        self._clear_barb_zoom_preview()
                        self._set_weather_visible(False)
                        self._show_state(state)
                    self.cameraChanged.emit(camera)
                    return state
                # Do not clear the zoom preview before publishing: it must stay
                # visible while the replacement stages as pending.  It is
                # cleared only when the commit swaps the back into the front
                # (_commit_native_weather).
                if self._publish(field, selection, camera) is False:
                    return "unavailable"
                if not self._native_weather_fields:
                    self._weather_display_camera = camera
                    self._weather_display_dpr = current_dpr
                self._set_weather_visible(True)
                if self._native_weather_fields:
                    self._update_native_state()
                    if self._native_pending_camera is not None:
                        self._show_state(
                            "loading", "Updating wind — showing previous forecast time"
                        )
                else:
                    self._show_state(
                        "ready"
                        if self._barbs.property("ready")
                        and (
                            not self._wind_gradient_enabled
                            or self._fill is None
                            or self._image_fill_matches_requested()
                        )
                        else "loading"
                    )
                self.cameraChanged.emit(camera)
                return state
            finally:
                self._presentation_spans.append((began, time.perf_counter(), state))
                if len(self._presentation_spans) > 4096:
                    del self._presentation_spans[: len(self._presentation_spans) - 4096]

        def _update_shared_map_pan(self, camera):
            """Move chart-scene map content with one parent transform on pan."""
            parent = getattr(self, "_map_content", None)
            if parent is None:
                return False
            dpr = self.devicePixelRatio()
            reference = self._map_pan_reference
            anchor_changed = False
            if reference is None:
                reference = PanReference.from_camera(camera, dpr)
                self._map_pan_reference = reference
                self._map_anchor_camera = camera
                anchor_changed = True
                self._map_pan_window = PaddedPanWindow(
                    reference, 0.5 * max(float(camera.width), float(camera.height))
                )
            elif reference.classify(camera, dpr) != CameraChange.PAN:
                if (
                    reference.zoom == camera.zoom
                    and reference.width == camera.width
                    and reference.height == camera.height
                ):
                    # A DPR-only invalidation changes the pixel resources,
                    # not the geographic placement. Preserve the retained
                    # image/fill anchors until the host supplies new DPR data.
                    dx, dy = reference.translation_to(camera, reference.dpr)
                    self._rebase_retained_imagery(dx, dy)
                reference = PanReference.from_camera(camera, dpr)
                self._map_pan_reference = reference
                self._map_anchor_camera = camera
                anchor_changed = True
                self._map_pan_window = PaddedPanWindow(
                    reference, 0.5 * max(float(camera.width), float(camera.height))
                )
            else:
                _dx, _dy, rebase = self._map_pan_window.placement(camera, dpr)
                if rebase:
                    # Compensate front and pending placements before resetting
                    # the parent. Do not invalidate the unrelated weather/barb
                    # publication generation on a translation-only pan.
                    self._rebase_retained_imagery(_dx, _dy)
                    reference = PanReference.from_camera(camera, dpr)
                    self._map_pan_reference = reference
                    self._map_anchor_camera = camera
                    anchor_changed = True
                    self._map_pan_window = PaddedPanWindow(
                        reference, 0.5 * max(float(camera.width), float(camera.height))
                    )
            dx, dy = reference.translation_to(camera, dpr)
            parent.setProperty("x", dx)
            parent.setProperty("y", dy)
            return anchor_changed

        @staticmethod
        def _same_weather_view(first, second):
            """Whether an old portrayal remains spatially aligned for a time preview."""
            return (
                first.center_lat,
                first.center_lon,
                first.zoom,
                first.width,
                first.height,
                first.density,
                first.over_land,
                first.geographic,
            ) == (
                second.center_lat,
                second.center_lon,
                second.zoom,
                second.width,
                second.height,
                second.density,
                second.over_land,
                second.geographic,
            )

        def observations(self):
            """Presentation spans and the generations actually published."""
            return {
                "presentation_spans": tuple(self._presentation_spans),
                "published_generations": tuple(sorted(self._published_generations)),
                "weather_ready_at": self._weather_ready_at,
                "chart_ready_at": self._chart_ready_at,
                "chart_state": self._chart_state,
                "chart_requested_after_weather": self._chart_requested_after_weather,
                "chart_layer": (
                    None
                    if self._chart_model is None
                    else self._chart_model.observations()
                ),
            }

        def _publish(self, field, selection, camera):
            # Reject before publishing either namespace or touching QML sources.
            validate_binding(field, selection)
            dpr = self._native_publication_dpr()
            if (
                self._native_weather_fields
                and self._native_retained_view_is_current(camera, dpr)
                and self._selection_is_displayed(field, selection)
            ):
                # The same symbols of the same resident field at the same
                # forecast time, only moved by a covered pan: the retained
                # anchors are already placed for this camera.  Skip the
                # back-slot swap so nothing reloads, and keep the displayed
                # camera current.
                self._native_active_camera = camera
                self._weather_display_camera = camera
                return True
            if self._native_weather_fields and (
                (
                    self._native_active_camera == camera
                    and self._native_pending_camera is None
                    and self._native_active_identity == field.identity
                    and getattr(self, "_native_active_dpr", None) == dpr
                )
                or (
                    self._native_pending_camera == camera
                    and self._native_pending_identity == field.identity
                    and getattr(self, "_native_pending_dpr", None) == dpr
                )
            ):
                return True
            if self._native_weather_fields and self._native_active_camera is not None:
                self._native_pending_camera = camera
                self._barbs_back.setOpacity(0.01)
                self._barbs_back.setVisible(True)
                if self._fill_back is not None:
                    self._fill_back.setOpacity(
                        0.01 if self._wind_gradient_enabled else 0.0
                    )
                    self._fill_back.setVisible(True)
            if self._native_weather_fields:
                self._native_pending_identity = field.identity
                self._native_pending_selection = selection
                self._native_pending_dpr = dpr
            if self._native_weather_fields:
                required = field.texture_bytes + (
                    0
                    if self._fill is None or field.grid is None
                    else sum(image.sizeInBytes() for image in field.grid.images)
                )
                if required > self._native_budget.byte_limit:
                    self._native_error = (
                        "Current weather layers exceed native texture budget"
                    )
                    self._set_weather_visible(False)
                    self._show_state("unavailable")
                    self.nativeError.emit(self._native_error)
                    return False
                field_urls = ()
            else:
                try:
                    # The barbs' node field must remain servable while the
                    # gridded fill prepares on the same bounded provider.
                    field_urls = self._provider.reserve_field(
                        (self, "barbs"), field.images, field.generation
                    )
                except ValueError:
                    self._show_state("unavailable")
                    return False
            selection_urls = self._provider.publish_selection(
                selection.images, selection.generation
            )
            # The resident time window's fields are published once, so a
            # forecast-time change inside the window publishes nothing and
            # re-encodes nothing.
            if not self._native_weather_fields:
                for resident in self._model.window_fields():
                    try:
                        self._provider.publish_field(
                            resident.images, resident.generation
                        )
                    except ValueError:
                        # Lookahead is optional; protected displayed fields win.
                        continue
            # The two layers are staged on their own inputs: the fill sees the
            # resident field and the temporal blend, the barbs see the resident
            # field and the current-view selection.
            self._native_publishing = True
            try:
                if self._publish_fill(field, field_urls, camera) is False:
                    if not self._native_weather_fields:
                        self._provider.cancel_field((self, "barbs"))
                    return False
                self._publish_barbs(
                    field, selection, field_urls, selection_urls, camera
                )
                if not self._native_weather_fields:
                    self._provider.promote_field((self, "barbs"))
            finally:
                self._native_publishing = False
            self._published = selection.generation
            self._published_generations.add(selection.generation)

        def _image_fill_changed(self, item):
            if not self._disposed and item is self._fill_back:
                self._try_commit_image_fill(getattr(self, "_image_fill_token", None))

        def _cancel_image_fill(self, withdraw=False):
            self._image_fill_token = None
            self._image_fill_pending_epoch = None
            self._publication_manager.reject_pending(LayerRole.FILL)
            self._provider.cancel_field(self)
            if self._fill_back is not None:
                self._fill_back.setVisible(False)
                for name in ("u0Source", "v0Source", "u1Source", "v1Source"):
                    self._fill_back.setProperty(name, QUrl())
            if withdraw:
                if self._fill is not None:
                    self._fill.setVisible(False)
                    for name in ("u0Source", "v0Source", "u1Source", "v1Source"):
                        self._fill.setProperty(name, QUrl())
                self._publication_manager.withdraw_visible(LayerRole.FILL)
                self._provider.release_fields(self)
                self._provider.release_fields((self, "barbs"))
                self._published_fill = None
                self._image_fill_requested_key = None
                self._image_fill_failed = None

        def _stage_image_fill(self, field, camera):
            grid = field.grid
            if self._fill_back is None or grid is None or len(grid.images) != 4:
                self._image_fill_requested_key = None
                self._fill.setVisible(False)
                self._show_state("unavailable")
                return False
            key = f"{field.generation}/grid"
            self._image_fill_requested_key = key
            layer = self._publication_manager.layers["fill"]
            epoch = self._publication_manager.current_generation
            if getattr(self, "_image_fill_failed", None) == (key, epoch):
                self._show_state("unavailable")
                return False
            if layer.visible is not None and not self._image_fill_matches_requested():
                # The old forecast's fill must not accompany new barbs as if
                # both belonged to the requested time/field.
                self._fill.setVisible(False)
            if (
                self._published_fill == key
                and layer.visible is not None
                and layer.pending is None
            ):
                item = self._fill
                token = None
            else:
                old_token = getattr(self, "_image_fill_token", None)
                reuse_pending = (
                    old_token is not None
                    and layer.pending is not None
                    and layer.resources.get("key") == key
                    and layer.pending.source_key[0] == field.identity
                    and getattr(self, "_image_fill_pending_epoch", None) == epoch
                )
                budget = sum(image.sizeInBytes() for image in grid.images)
                delta = max(0, budget - layer.pending_budget_bytes)
                if not self._publication_manager.budget.can_allocate("fill", delta):
                    self._show_state("unavailable")
                    return False
                try:
                    urls = self._provider.reserve_field(self, grid.images, key)
                except ValueError:
                    self._show_state("unavailable")
                    return False
                token = old_token if reuse_pending else object()
                identity = LayerIdentity(
                    role=LayerRole.FILL,
                    generation=field.generation,
                    source_key=(field.identity, key, camera.stamp()),
                    content_hash=getattr(grid, "content_hash", str(grid.images)),
                )
                if not self._publication_manager.stage_resources(
                    LayerRole.FILL,
                    identity,
                    LayerPlacement(),
                    {ResourceKind.IMAGE: grid.images, "key": key, "urls": urls},
                    budget,
                    completion_token=token,
                ):
                    self._provider.cancel_field(self)
                    self._show_state("unavailable")
                    return False
                self._image_fill_token = token
                self._image_fill_pending_epoch = epoch
                item = self._fill_back
            self._fill_staging = True
            try:
                if token is not None:
                    item.setVisible(False)
                item.setProperty("blend", field.staged.weight(camera.stamp()))
                map_camera = getattr(self, "_map_anchor_camera", None)
                reference = self._publication_manager.reference_camera
                if map_camera is not None:
                    rect = _grid_rect(map_camera.view(), grid.lattice)
                elif reference is not None:
                    ref_pan = PanReference.from_camera(
                        reference, getattr(reference, "dpr", 1.0)
                    )
                    dx, dy = ref_pan.translation_to(camera)
                    rect = _grid_rect(reference.view(), grid.lattice)
                    rect = type(rect)(
                        rect.x() + dx, rect.y() + dy, rect.width(), rect.height()
                    )
                else:
                    rect = _grid_rect(camera.view(), grid.lattice)
                self._place_fill_rect(item, rect)
                if token is not None:
                    for name, url in zip(
                        ("u0Source", "v0Source", "u1Source", "v1Source"), urls
                    ):
                        item.setProperty(name, QUrl(url))
            finally:
                self._fill_staging = False
            if token is not None:
                self._try_commit_image_fill(token)
                if not reuse_pending and isinstance(self, QObject):
                    # Some backends do not compile an effect inside a fully
                    # hidden subtree. Do not retain an unready field forever.
                    QTimer.singleShot(
                        10000, lambda staged=token: self._image_fill_timeout(staged)
                    )
            return token

        def _image_fill_timeout(self, token):
            if self._disposed or token is not getattr(self, "_image_fill_token", None):
                return
            layer = self._publication_manager.layers["fill"]
            self._image_fill_failed = (
                layer.resources.get("key"),
                self._publication_manager.current_generation,
            )
            self._cancel_image_fill()
            if not self._image_fill_matches_requested():
                self._fill.setVisible(False)
            self._show_state("unavailable")

        def _try_commit_image_fill(self, token):
            if (
                self._disposed
                or getattr(self, "_fill_staging", False)
                or token is None
                or token is not getattr(self, "_image_fill_token", None)
            ):
                return
            layer = self._publication_manager.layers["fill"]
            if layer.pending is None or layer.completion_token is not token:
                return
            if self._fill_back.property("shaderError") or self._fill_back.property(
                "sourceError"
            ):
                self._image_fill_failed = (
                    layer.resources.get("key"),
                    self._publication_manager.current_generation,
                )
                self._cancel_image_fill()
                if not self._image_fill_matches_requested():
                    self._fill.setVisible(False)
                self._show_state("unavailable")
                return
            if not self._fill_back.property("prepared"):
                return
            adopted, _ = self._publication_manager.verify_and_adopt(
                LayerRole.FILL,
                lambda resources: len(resources.get(ResourceKind.IMAGE, ())) == 4
                and len(resources.get("urls", ())) == 4,
                expected_token=token,
            )
            if not adopted:
                self._cancel_image_fill()
                if not self._image_fill_matches_requested():
                    self._fill.setVisible(False)
                self._show_state("unavailable")
                return
            self._provider.promote_field(self)
            self._image_fill_token = None
            self._image_fill_pending_epoch = None
            self._image_fill_failed = None
            old = self._fill
            old.setVisible(False)
            self._fill, self._fill_back = self._fill_back, old
            self._published_fill = layer.visible_resources["key"]
            self._fill.setOpacity(self._wind_gradient_opacity)
            self._fill.setVisible(
                self._wind_gradient_enabled
                and getattr(self, "_image_weather_visible", True)
            )
            self.counters["fill_publications"] += 1
            self.counters["fill_uploads"] += 1
            if not getattr(self, "_native_publishing", False):
                self._show_state(
                    "ready" if self._barbs.property("ready") else "loading"
                )

        def _publish_fill(self, field, field_urls, camera):
            """Stage the fill layer on its own inputs.

            The fill consumes the field's *gridded form* and the temporal
            blend.  It never consumes the selection, so a selection change
            cannot restage it.  The gridded images are published under their
            own key, so they are the fill's own GPU resource and the barbs'
            node form is untouched.
            """
            if self._fill is None:
                return
            grid = field.grid
            if grid is None:
                return False if self._wind_gradient_enabled else None
            if not self._native_weather_fields:
                return self._stage_image_fill(field, camera)
            key = f"{field.generation}/grid"
            item = (
                self._fill_back
                if self._native_weather_fields
                and self._native_pending_camera is not None
                else self._fill
            )
            if self._native_weather_fields:
                resources = {ResourceKind.IMAGE: grid.images}
                budget_bytes = sum(
                    size()
                    for img in grid.images
                    if callable(size := getattr(img, "sizeInBytes", None))
                )
                identity = LayerIdentity(
                    role=LayerRole.FILL,
                    generation=field.generation,
                    source_key=(field.identity, key, camera.stamp()),
                    content_hash=(
                        grid.content_hash
                        if hasattr(grid, "content_hash")
                        else str(grid.images)
                    ),
                )
                token = object()
                staged = self._publication_manager.stage_resources(
                    LayerRole.FILL,
                    identity,
                    LayerPlacement(),
                    resources,
                    budget_bytes,
                    completion_token=token,
                )
                if not staged:
                    self._publication_manager.reject_pending(LayerRole.FILL)
                self._bind_native_layer(
                    "fill",
                    field.identity,
                    grid.images,
                    pending=self._native_pending_camera is not None,
                    completion_token=token if staged else None,
                )
            item.setProperty("blend", field.staged.weight(camera.stamp()))
            # Use reference camera for grid placement (shared pan)
            map_camera = getattr(self, "_map_anchor_camera", None)
            reference = self._publication_manager.reference_camera
            if map_camera is not None:
                rect = _grid_rect(map_camera.view(), grid.lattice)
            elif reference is not None:
                ref_pan = PanReference.from_camera(
                    reference, getattr(reference, "dpr", 1.0)
                )
                dx, dy = ref_pan.translation_to(camera)
                rect = _grid_rect(reference.view(), grid.lattice)
                rect = type(rect)(
                    rect.x() + dx, rect.y() + dy, rect.width(), rect.height()
                )
            else:
                rect = _grid_rect(camera.view(), grid.lattice)
            self._place_fill_rect(item, rect)

        def publish_fill(self, images, key):
            """Publish an image set to the fill layer alone.

            This is the fill layer's own publication path: it stages the fill
            without touching the barbs' namespace, the barbs' sources or the
            barbs' counters.  It is how an independent fill source would be
            published, and how a native probe observes that a fill change does
            not re-stage the barbs.
            """
            if self._fill is None:
                raise RuntimeError("The viewport has no numerical fill layer")
            if self._native_weather_fields:
                self._bind_native_layer("fill", ("manual", key), images)
                return ()
            if self._fill_back is None or len(images) != 4:
                raise ValueError("The fill requires four image resources")
            images = tuple(images)
            self._image_fill_requested_key = key
            layer = self._publication_manager.layers["fill"]
            if layer.visible is not None and not self._image_fill_matches_requested():
                self._fill.setVisible(False)
            if self._published_fill == key and layer.visible is not None:
                return layer.visible_resources["urls"]
            bytes_needed = sum(image.sizeInBytes() for image in images)
            delta = max(0, bytes_needed - layer.pending_budget_bytes)
            if not self._publication_manager.budget.can_allocate("fill", delta):
                self._show_state("unavailable")
                return ()
            try:
                urls = self._provider.reserve_field(self, images, key)
            except ValueError:
                self._show_state("unavailable")
                return ()
            token = object()
            identity = LayerIdentity(
                role=LayerRole.FILL,
                generation=self._publication_manager.current_generation,
                source_key=("manual", key),
                content_hash=str(key),
            )
            if not self._publication_manager.stage_resources(
                LayerRole.FILL,
                identity,
                LayerPlacement(),
                {ResourceKind.IMAGE: images, "key": key, "urls": urls},
                bytes_needed,
                completion_token=token,
            ):
                self._provider.cancel_field(self)
                self._show_state("unavailable")
                return ()
            self._image_fill_token = token
            self._image_fill_pending_epoch = (
                self._publication_manager.current_generation
            )
            self._fill_staging = True
            try:
                self._fill_back.setVisible(False)
                for name, url in zip(
                    ("u0Source", "v0Source", "u1Source", "v1Source"), urls
                ):
                    self._fill_back.setProperty(name, QUrl(url))
            finally:
                self._fill_staging = False
            self._try_commit_image_fill(token)
            if isinstance(self, QObject):
                QTimer.singleShot(
                    10000, lambda staged=token: self._image_fill_timeout(staged)
                )
            return urls

        def _find_shared_anchor_items(self):
            """The scene's shared anchor Image slots, when it declares a pool.

            Both barbs sample the same provider Images, so an anchor atlas is
            uploaded once per slot instead of once per layer.  A scene without
            them keeps the historical per-item anchor sources.
            """
            weather = getattr(self, "_weather", None)
            if weather is None:
                return (None, None)
            slots = []
            for slot in (0, 1):
                x_item = weather.findChild(QObject, f"sharedAnchorX{slot}")
                y_item = weather.findChild(QObject, f"sharedAnchorY{slot}")
                if x_item is None or y_item is None:
                    slots.append(None)
                else:
                    slots.append((x_item, y_item))
            return tuple(slots)

        def _shared_anchor_pool(self):
            """Both atlas slots, or ``None`` when the scene declares none."""
            items = getattr(self, "_shared_anchor_items", None)
            if items is None or any(slot is None for slot in items):
                return None
            return items

        def _shared_anchor_slot(self, urls, pending):
            """The atlas slot that serves ``urls``, staging it when necessary.

            The node atlas is field-node-indexed, so a mesh slot permutation
            reuses an already staged pair without touching either Image.  A new
            anchor frame is written into the slot the front layer is not
            displaying, so a pending frame never replaces what is on screen.
            """
            items = self._shared_anchor_pool()
            if items is None:
                return None
            pair = (urls[0], urls[1])
            stored = self._shared_anchor_urls
            front = self._shared_anchor_front
            candidates = (0, 1) if front is None else (front, 1 - front)
            slot = next(
                (candidate for candidate in candidates if stored[candidate] == pair),
                None,
            )
            if slot is None:
                slot = 0 if front is None else 1 - front
                x_item, y_item = items[slot]
                x_item.setProperty("source", QUrl(pair[0]))
                y_item.setProperty("source", QUrl(pair[1]))
                stored[slot] = pair
            if pending:
                self._shared_anchor_pending = slot
            else:
                self._shared_anchor_front = slot
            return slot

        def _adopt_shared_anchor_front(self):
            """Hand the staged atlas slot to the layer that now displays it."""
            pending = getattr(self, "_shared_anchor_pending", None)
            if pending is not None:
                self._shared_anchor_front = pending
                self._shared_anchor_pending = None

        def _clear_shared_anchors(self):
            """Release both atlas slots and forget which one the front shows."""
            self._shared_anchor_urls = [None, None]
            self._shared_anchor_front = None
            self._shared_anchor_pending = None
            items = getattr(self, "_shared_anchor_items", None) or ()
            for slot in items:
                if slot is None:
                    continue
                for item in slot:
                    if shiboken6.isValid(item):
                        item.setProperty("source", QUrl())

        def _publish_barbs(self, field, selection, field_urls, selection_urls, camera):
            """Stage the barbs layer on its own inputs.

            The barbs consume the resident field's endpoint images and the
            current-view selection.  A fill change never reaches this path.
            """
            item = (
                self._barbs_back
                if self._native_weather_fields
                and self._native_pending_camera is not None
                else self._barbs
            )
            if self._native_weather_fields:
                # The bound item starts from identity preview uniforms.  When
                # this publishes the pending back, the displayed front keeps
                # its zoom preview until the commit swaps the layers.
                self._reset_barb_zoom_preview_item(item)
            if self._native_weather_fields:
                resources = {ResourceKind.IMAGE: field.images}
                budget_bytes = sum(
                    size()
                    for img in field.images
                    if callable(size := getattr(img, "sizeInBytes", None))
                )
                identity = LayerIdentity(
                    role=LayerRole.BARBS,
                    generation=field.generation,
                    source_key=(field.identity, str(selection.shape), camera.stamp()),
                    content_hash=(
                        field.content_hash
                        if hasattr(field, "content_hash")
                        else str(field.images)
                    ),
                )
                token = object()
                staged = self._publication_manager.stage_resources(
                    LayerRole.BARBS,
                    identity,
                    LayerPlacement(),
                    resources,
                    budget_bytes,
                    completion_token=token,
                )
                if not staged:
                    self._publication_manager.reject_pending(LayerRole.BARBS)
                self._bind_native_layer(
                    "barbs",
                    field.identity,
                    field.images,
                    pending=self._native_pending_camera is not None,
                    completion_token=token if staged else None,
                )
                self._native_pending_selection_urls = selection_urls
            elif self._published != field.generation:
                for name, url in zip(
                    ("u0Source", "v0Source", "u1Source", "v1Source"), field_urls
                ):
                    self._barbs.setProperty(name, QUrl(url))
                self._published = field.generation
                self.counters["barbs_field_publications"] += 1
                self.counters["barbs_field_uploads"] += 1
            # Selection anchors are encoded in the coordinates of the camera
            # that published them.  The layer is placed in shared map-content
            # space, so the item compensates with its reference-local offset:
            # a pan inside one reference changes neither the anchors nor the
            # offset, and a rebase shifts it in _rebase_retained_imagery.
            offset = self._barb_anchor_offset(camera, selection)
            # A rebase or zoom can overtake a staged publication: its anchors
            # then belong to a frame this camera cannot be placed against.
            # Mark the frame explicitly invalid so the scene keeps the layer
            # hidden instead of drawing stale anchors at a guessed offset.
            item.setProperty("anchorFrameValid", offset is not None)
            if offset is not None:
                item.setProperty("anchorOffsetX", offset[0])
                item.setProperty("anchorOffsetY", offset[1])
            native_pending = (
                self._native_weather_fields and self._native_pending_camera is not None
            )
            pool = self._shared_anchor_pool()
            atlas_slot = None
            if pool is not None and len(selection_urls) >= 3:
                # Anchors are the node-indexed atlas: slot changes reuse it and
                # only the node-id indirection image is per-publication.
                atlas_slot = self._shared_anchor_slot(
                    selection_urls[1:3], pending=native_pending
                )
            for name, url in zip(
                ("nodeIdSource", "anchorXSource", "anchorYSource"), selection_urls
            ):
                if pool is not None and name != "nodeIdSource":
                    continue
                item.setProperty(name, QUrl(url))
            if atlas_slot is not None:
                item.setProperty("atlasSlot", atlas_slot)
            if native_pending:
                # The back layer binds these before the swap; keep them
                # servable until the commit pins them as the front.
                retain = getattr(
                    getattr(self, "_provider", None), "retain_selection", None
                )
                if retain is not None:
                    retain(selection_urls)
            self.counters["barbs_selection_publications"] += 1
            self.counters["barbs_selection_uploads"] += 1
            rows, cols = selection.shape
            item.setProperty("columns", cols)
            item.setProperty("rows", rows)
            item.setProperty("nodeIdIndirection", True)
            item.setProperty("fieldColumns", FIELD_COLUMNS)
            item.setProperty("blend", field.staged.weight(camera.stamp()))
            if not self._native_weather_fields or self._native_pending_camera is None:
                item.setOpacity(self._wind_opacity)

        def _maybe_arm_barb_zoom_preview(
            self, previous_camera, camera, previous_dpr, current_dpr
        ):
            """Keep the previous front visible through a zoom while it stages.

            The preview is only valid when the zoom is the sole change: the
            viewport size, device ratio and forecast time must match, and the
            displayed front must still carry a coherent published field.  A
            source or forecast change therefore never previews; it retires
            the front as before.  Once armed, a later pan at the same target
            zoom refreshes the placement against the newly rebased map
            reference instead of hiding the preview.
            """
            if not getattr(self, "_native_weather_fields", False) or camera is None:
                self._clear_barb_zoom_preview()
                return False
            active_camera = getattr(self, "_native_active_camera", None)
            identity = getattr(self, "_native_active_identity", None)
            selection = getattr(self, "_native_active_selection", None)
            anchor = getattr(selection, "anchor_reference", None)
            if (
                active_camera is None
                or identity is None
                or anchor is None
                or len(anchor) != 5
            ):
                self._clear_barb_zoom_preview()
                return False
            if not self._barb_zoom_dpr_matches(previous_dpr, current_dpr):
                self._clear_barb_zoom_preview()
                return False
            try:
                if float(camera.width) != float(anchor[3]) or float(
                    camera.height
                ) != float(anchor[4]):
                    self._clear_barb_zoom_preview()
                    return False
                stamp = camera.stamp()
            except (AttributeError, TypeError, ValueError):
                self._clear_barb_zoom_preview()
                return False
            if getattr(self, "_barb_zoom_preview", None) is not None:
                if self._barb_zoom_preview_holds(camera, current_dpr):
                    # A pan at the target zoom: the map reference moved, so
                    # re-place the same old anchors instead of reloading.
                    # Re-anchor to the current camera so the preview transform
                    # is computed against the new map reference at the same
                    # zoom, keeping the preview visible through pending staging.
                    self._barb_zoom_preview["anchor"] = PanReference.from_camera(
                        camera, current_dpr
                    )
                    return self._apply_barb_zoom_preview()
                # A continuing zoom animation changes the target zoom on every
                # tick, so the preview never "holds" mid-animation.  Renew the
                # same retained frame instead of dropping it: clearing here
                # blanks the barbs until the replacement commits, which reads
                # as a flash on each zoom tick.
                frame = self._barb_zoom_preview
                try:
                    requested_source = getattr(camera, "source", None)
                    renew = (
                        float(camera.width) == frame["width"]
                        and float(camera.height) == frame["height"]
                        and camera.stamp() == frame["stamp"]
                        and self._barb_zoom_dpr_matches(frame.get("dpr"), current_dpr)
                        and (
                            requested_source is None
                            or identity.source == requested_source
                        )
                    )
                except (AttributeError, TypeError, ValueError):
                    renew = False
                if renew:
                    self._barb_zoom_preview["zoom"] = float(camera.zoom)
                    return self._apply_barb_zoom_preview()
                self._clear_barb_zoom_preview()
            if previous_camera is None:
                return False
            try:
                unchanged_frame = (
                    float(camera.zoom) != float(previous_camera.zoom)
                    and float(camera.width) == float(previous_camera.width)
                    and float(camera.height) == float(previous_camera.height)
                    and camera.stamp() == previous_camera.stamp()
                )
            except (AttributeError, TypeError, ValueError):
                return False
            if not unchanged_frame:
                return False
            try:
                # Source and forecast must match the requested camera: a source
                # or time change never previews; it retires the front as before.
                requested_source = getattr(camera, "source", None)
                if not identity.contains(stamp) or (
                    requested_source is not None and identity.source != requested_source
                ):
                    return False
            except (AttributeError, TypeError, ValueError):
                return False
            # The front must have been displayable at the frame it portrays.
            if not self._barb_anchor_frame_places(
                active_camera, previous_dpr, previous_camera, current_dpr
            ):
                return False
            self._barb_zoom_preview = {
                "anchor": PanReference(*anchor, dpr=current_dpr or 1.0),
                "zoom": float(camera.zoom),
                "stamp": stamp,
                "width": float(camera.width),
                "height": float(camera.height),
                "dpr": current_dpr,
            }
            return self._apply_barb_zoom_preview()

        @staticmethod
        def _barb_zoom_dpr_matches(previous_dpr, current_dpr):
            if previous_dpr is None or current_dpr is None:
                return True
            return float(previous_dpr) == float(current_dpr)

        def _barb_zoom_preview_holds(self, camera, dpr=None):
            """Whether the armed preview still belongs to ``camera``."""
            frame = getattr(self, "_barb_zoom_preview", None)
            if frame is None or camera is None:
                return False
            if dpr is None:
                dpr = self._native_publication_dpr()
            recorded = frame.get("dpr")
            if recorded is not None and dpr is not None:
                if float(recorded) != float(dpr):
                    return False
            try:
                return (
                    float(camera.zoom) == frame["zoom"]
                    and float(camera.width) == frame["width"]
                    and float(camera.height) == frame["height"]
                    and camera.stamp() == frame["stamp"]
                )
            except (AttributeError, TypeError, ValueError):
                return False

        def _apply_barb_zoom_preview(self):
            """Place the retained front's anchors in the current map reference."""
            frame = getattr(self, "_barb_zoom_preview", None)
            if frame is None:
                return False
            target = getattr(self, "_map_pan_reference", None)
            if target is None:
                camera = self._camera
                if camera is None:
                    return False
                target = PanReference.from_camera(
                    camera, getattr(self, "_native_publication_dpr", lambda: 1.0)()
                )
            transform = zoom_preview_transform(frame["anchor"], target)
            item = getattr(self, "_barbs", None)
            if transform is None or item is None:
                return False
            scale, offset_x, offset_y = transform
            try:
                item.setProperty("previewScale", float(scale))
                item.setProperty("previewOffsetX", float(offset_x))
                item.setProperty("previewOffsetY", float(offset_y))
                # The preview transform already lands in the map reference,
                # so the reference-local pan compensation is not added again.
                item.setProperty("anchorOffsetX", 0.0)
                item.setProperty("anchorOffsetY", 0.0)
            except RuntimeError:
                return False
            self._set_barb_anchor_frame_valid(item, True)
            return True

        @staticmethod
        def _reset_barb_zoom_preview_item(item):
            if item is None:
                return
            try:
                item.setProperty("previewScale", 1.0)
                item.setProperty("previewOffsetX", 0.0)
                item.setProperty("previewOffsetY", 0.0)
            except RuntimeError:
                # The QML item was destroyed between frames.
                pass

        def _clear_barb_zoom_preview(self, reset_items=True):
            """Drop the preview state and restore identity shader uniforms."""
            frame = getattr(self, "_barb_zoom_preview", None)
            self._barb_zoom_preview = None
            if frame is None or not reset_items:
                return
            for item in (
                getattr(self, "_barbs", None),
                getattr(self, "_barbs_back", None),
            ):
                self._reset_barb_zoom_preview_item(item)

        def _retire_barb_anchor_frame(self, previous_camera, camera, previous_dpr=None):
            """Withdraw barb anchors this camera cannot place.

            The displayed front is judged against the frame it actually
            portrays (its adopted native camera, or the previous camera), so
            returning to that frame keeps it displayable.  A pan -- including
            a rebase the caller compensates -- preserves the anchors; a zoom,
            viewport resize or device-pixel-ratio change retires them until a
            publication republishes the layer.  A pending back layer staged
            for another incompatible frame is retired too, so it can never be
            committed as the current frame.
            """
            if previous_camera is None or camera is None:
                return False
            current_dpr = self._native_publication_dpr()
            active_camera = getattr(self, "_native_active_camera", None)
            displayed_camera = active_camera or getattr(
                self, "_weather_display_camera", None
            )
            if displayed_camera is None:
                displayed_camera = previous_camera
            front_dpr = getattr(self, "_native_active_dpr", None)
            if front_dpr is None:
                front_dpr = getattr(self, "_weather_display_dpr", None)
            if front_dpr is None:
                front_dpr = previous_dpr
            front = getattr(self, "_barbs", None)
            if self._barb_zoom_preview_holds(camera, current_dpr):
                # The front is the previous frame, placed by the zoom
                # preview transform.  A pan while the replacement is pending
                # follows the map reference instead of retiring it.
                self._set_barb_anchor_frame_valid(front, True)
            else:
                self._set_barb_anchor_frame_valid(
                    front,
                    self._barb_anchor_frame_places(
                        displayed_camera, front_dpr, camera, current_dpr
                    ),
                )
            pending_camera = getattr(self, "_native_pending_camera", None)
            if pending_camera is not None and not self._barb_anchor_frame_places(
                pending_camera,
                getattr(self, "_native_pending_dpr", None),
                camera,
                current_dpr,
            ):
                self._set_barb_anchor_frame_valid(
                    getattr(self, "_barbs_back", None), False
                )
            return not self._barb_anchor_frame_places(
                previous_camera, previous_dpr, camera, current_dpr
            )

        @staticmethod
        def _barb_anchor_frame_places(frame_camera, frame_dpr, camera, current_dpr):
            """Whether anchors encoded for one frame can be placed at a camera."""
            if frame_camera is None or camera is None:
                return False
            if current_dpr is None:
                current_dpr = 1.0
            if frame_dpr is None:
                frame_dpr = current_dpr
            try:
                reference = PanReference.from_camera(frame_camera, frame_dpr)
                return reference.classify(camera, current_dpr) == CameraChange.PAN
            except (AttributeError, TypeError, ValueError):
                return False

        @staticmethod
        def _set_barb_anchor_frame_valid(item, valid):
            """Set the anchor-frame flag, tolerating an item already destroyed."""
            if item is None:
                return
            try:
                item.setProperty("anchorFrameValid", bool(valid))
            except RuntimeError:
                # The QML item was destroyed between the camera handoff and
                # the property write.
                pass

        def _barb_anchor_offset(self, camera, selection=None):
            """The reference-local shift of a selection published at ``camera``.

            The selection anchors are reference-local: they are projected at
            the map pan reference, which the shared parent translates to the
            camera.  The item therefore needs only ``anchor reference ->
            map reference``; it is zero whenever both agree.  With no recorded
            anchor reference (the historical path) the anchors are in the
            camera's own pixels and the item supplies the inverse pan.

            ``None`` means the anchors belong to an incompatible frame (zoom,
            viewport size or dpr): there is no correct placement, so the caller
            must refuse to present them rather than guess an offset.
            """
            reference = getattr(self, "_map_pan_reference", None)
            if reference is None:
                return 0.0, 0.0
            anchor = getattr(selection, "anchor_reference", None)
            try:
                if anchor is not None:
                    anchor_reference = PanReference(*anchor, dpr=reference.dpr)
                    ax, ay = anchor_reference.translation_to(camera, reference.dpr)
                    bx, by = reference.translation_to(camera, reference.dpr)
                    return ax - bx, ay - by
                dx, dy = reference.translation_to(camera, reference.dpr)
            except (TypeError, ValueError):
                # A zoom, size or dpr change is a new frame: the retained
                # anchors cannot be placed against this camera.
                return None
            return -dx, -dy

        def _barb_reference_view(self, camera):
            """The map pan reference, as the view anchors are encoded at."""
            view = camera.view()
            reference = getattr(self, "_map_pan_reference", None)
            if reference is None:
                return view
            try:
                change = reference.classify(camera, self.devicePixelRatio())
            except (TypeError, ValueError):
                return view
            if change != CameraChange.PAN:
                return view
            return BarbView(
                view.width,
                view.height,
                reference.center_lat,
                reference.center_lon,
                reference.zoom,
                view.density,
                view.over_land,
                view.geographic,
            )

        def _native_publication_dpr(self):
            """The GUI's pixel ratio, or None when the host cannot report one."""
            read = getattr(self, "devicePixelRatio", None)
            if read is None:
                return None
            try:
                return float(read())
            except (TypeError, ValueError):
                return None

        def _native_retained_view_is_current(self, camera, dpr=None):
            """Whether retained anchors still portray this camera and time exactly.

            Only a translation of an already published camera may reuse them.
            A forecast-time step changes the blend weights and sampled values
            even when the resident node identities match, and a zoom, resize
            or device-pixel-ratio change means the pixel resources were made
            for another frame, so each of those must publish again.
            """
            active = getattr(self, "_native_active_camera", None)
            if active is None or camera is None:
                return False
            if dpr is None:
                dpr = self._native_publication_dpr()
            # A publication made at another ratio cannot be reused: its
            # pixel resources were composed for the earlier frame.
            recorded = getattr(self, "_native_active_dpr", None)
            if recorded is None or recorded != dpr:
                return False
            active_stamp = getattr(active, "stamp", None)
            camera_stamp = getattr(camera, "stamp", None)
            if active_stamp is None or camera_stamp is None:
                return False
            try:
                if active_stamp() != camera_stamp():
                    return False
                reference = PanReference.from_camera(active, dpr)
                return reference.classify(camera, dpr) == CameraChange.PAN
            except (AttributeError, TypeError, ValueError):
                return False

        def _selection_is_displayed(self, field, selection):
            """Whether the front layer already portrays these exact symbols."""
            if not self._native_weather_fields:
                return False
            active = getattr(self, "_native_active_selection", None)
            matches = getattr(active, "matches", None)
            return bool(
                self._native_pending_camera is None
                and self._native_active_camera is not None
                and self._native_active_identity == field.identity
                and matches is not None
                and matches(selection)
            )

        def publication_counters(self):
            """The published traffic, counted apart by kind and by layer."""
            return {
                "field_publications": self._provider.field_publications,
                "selection_publications": self._provider.selection_publications,
                "field_bytes": self._provider.field_bytes(),
                **self.counters,
            }

        def pan_pixels(self, dx, dy):
            if self._camera is None:
                return
            view = self._camera.view()
            self.set_camera(
                replace(
                    self._camera,
                    center_lon=view._x_to_lon(
                        view._lon_to_x(self._camera.center_lon) - dx
                    ),
                    center_lat=view._y_to_lat(
                        view._lat_to_y(self._camera.center_lat) - dy
                    ),
                )
            )

        def _set_zoom_around(self, zoom, x, y, anchor=None):
            camera = self._camera
            if camera is None:
                return
            if anchor is None:
                view = camera.view()
                anchor = (
                    view._y_to_lat(
                        view._lat_to_y(camera.center_lat) + y - camera.height / 2
                    ),
                    view._x_to_lon(
                        view._lon_to_x(camera.center_lon) + x - camera.width / 2
                    ),
                )
            lat, lon = anchor
            zoomed = replace(camera, zoom=max(1.0, min(20.0, zoom)))
            target = zoomed.view()
            self.set_camera(
                replace(
                    zoomed,
                    center_lon=target._x_to_lon(
                        target._lon_to_x(lon) - x + camera.width / 2
                    ),
                    center_lat=target._y_to_lat(
                        target._lat_to_y(lat) - y + camera.height / 2
                    ),
                )
            )

        def is_moving(self):
            return bool(
                self._drag is not None
                or self._pan_inertia is not None
                or self._pan_inertia_tick.isActive()
                or self._wheel_zoom_target is not None
                or self._wheel_zoom_tick.isActive()
                or self._wheel_trackpad_panning
                or getattr(self, "_native_gesture_active", False)
            )

        def cancel_motion(self):
            """Stop pending camera motion at its current presented state."""
            self._wheel_zoom_tick.stop()
            self._wheel_zoom_settle.stop()
            self._wheel_scroll_settle.stop()
            self._wheel_zoom_target = None
            self._wheel_zoom_anchor = None
            self._wheel_zoom_last_tick = None
            self._wheel_trackpad_panning = False
            self._native_gesture_active = False
            self._drag = None
            self._pan_inertia_tick.stop()
            self._pan_inertia = None
            self._pan_inertia_last_tick = None

        def finish_motion(self):
            """Finish an anchored wheel zoom, then clear pending motion state."""
            camera = self._camera
            target = self._wheel_zoom_target
            anchor = self._wheel_zoom_anchor
            if camera is not None and target is not None:
                if anchor is None:
                    self.set_camera(replace(camera, zoom=target))
                else:
                    x, y, anchor_lat, anchor_lon = anchor
                    self._set_zoom_around(target, x, y, (anchor_lat, anchor_lon))
            self.cancel_motion()

        def _advance_pan_inertia(self):
            motion = self._pan_inertia
            if motion is None or self._camera is None:
                self._pan_inertia_tick.stop()
                self._pan_inertia = None
                self._pan_inertia_last_tick = None
                return
            now = time.monotonic()
            elapsed = min(
                0.05,
                max(1.0 / 120.0, now - (self._pan_inertia_last_tick or now)),
            )
            self._pan_inertia_last_tick = now
            if not motion.active:
                self._pan_inertia_tick.stop()
                self._pan_inertia = None
                self._pan_inertia_last_tick = None
                return
            dx, dy = motion.step(elapsed)
            self.pan_pixels(dx, dy)
            if not motion.active:
                self._pan_inertia_tick.stop()
                self._pan_inertia = None
                self._pan_inertia_last_tick = None

        def _advance_wheel_zoom(self):
            if self._camera is None or self._wheel_zoom_target is None:
                self._wheel_zoom_tick.stop()
                return
            now = time.perf_counter()
            elapsed = min(
                0.05, max(1.0 / 120.0, now - (self._wheel_zoom_last_tick or now))
            )
            self._wheel_zoom_last_tick = now
            remaining = self._wheel_zoom_target - self._camera.zoom
            fraction = 1.0 - math.exp(-14.0 * elapsed)
            zoom = (
                self._wheel_zoom_target
                if abs(remaining) < 0.004
                else self._camera.zoom + remaining * fraction
            )
            anchor = self._wheel_zoom_anchor
            if anchor is None:
                self._wheel_zoom_tick.stop()
                return
            x, y, anchor_lat, anchor_lon = anchor
            self._set_zoom_around(zoom, x, y, (anchor_lat, anchor_lon))
            if zoom == self._wheel_zoom_target:
                self._wheel_zoom_tick.stop()
                self._wheel_zoom_last_tick = None

        def _finish_wheel_zoom(self):
            if self._wheel_zoom_tick.isActive():
                self._wheel_zoom_settle.start(250)
                return
            self._wheel_zoom_target = None
            self._wheel_zoom_anchor = None
            self._wheel_zoom_last_tick = None

        def _handle_desktop_wheel(self, event):
            if getattr(self, "_native_gesture_active", False):
                event.accept()
                return
            phase = event.phase()
            pixels = event.pixelDelta()
            modifiers = event.modifiers()
            if not pixels.isNull() and not modifiers & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
            ):
                self._wheel_zoom_tick.stop()
                self._wheel_zoom_settle.stop()
                self._wheel_zoom_target = None
                self._wheel_zoom_anchor = None
                self._wheel_trackpad_panning = True
                self.pan_pixels(pixels.x(), pixels.y())
                if phase == Qt.ScrollPhase.ScrollEnd:
                    self._wheel_trackpad_panning = False
                    self._wheel_scroll_settle.stop()
                elif phase == Qt.ScrollPhase.NoScrollPhase:
                    self._wheel_scroll_settle.start(250)
                event.accept()
                return
            delta = event.angleDelta().y() / 120 or pixels.y() / 60
            if not delta or self._camera is None:
                event.accept()
                return
            position = event.position()
            x, y = position.x(), position.y()
            view = self._camera.view()
            anchor = (
                view._y_to_lat(
                    view._lat_to_y(self._camera.center_lat)
                    + y
                    - self._camera.height / 2
                ),
                view._x_to_lon(
                    view._lon_to_x(self._camera.center_lon) + x - self._camera.width / 2
                ),
            )
            target = self._wheel_zoom_target or self._camera.zoom
            self._wheel_zoom_target = max(1.0, min(20.0, target + delta * 0.25))
            self._wheel_zoom_anchor = (x, y, *anchor)
            if not self._smooth_zoom:
                self._set_zoom_around(self._wheel_zoom_target, x, y, anchor)
                self._finish_wheel_zoom()
            else:
                self._wheel_zoom_last_tick = (
                    self._wheel_zoom_last_tick or time.perf_counter()
                )
                self._wheel_zoom_tick.start()
                self._wheel_zoom_settle.start(250)
            event.accept()

        def zoom_at(self, steps, x=None, y=None):
            if self._camera is None:
                return
            camera = self._camera
            x = camera.width / 2 if x is None else x
            y = camera.height / 2 if y is None else y
            view = camera.view()
            lon = view._x_to_lon(
                view._lon_to_x(camera.center_lon) + x - camera.width / 2
            )
            lat = view._y_to_lat(
                view._lat_to_y(camera.center_lat) + y - camera.height / 2
            )
            zoomed = replace(camera, zoom=max(1.0, min(20.0, camera.zoom + steps)))
            target = zoomed.view()
            self.set_camera(
                replace(
                    zoomed,
                    center_lon=target._x_to_lon(
                        target._lon_to_x(lon) - x + camera.width / 2
                    ),
                    center_lat=target._y_to_lat(
                        target._lat_to_y(lat) - y + camera.height / 2
                    ),
                )
            )

        def set_time(self, instant):
            if self._camera is not None:
                self.set_camera(replace(self._camera, time=instant))

        def showEvent(self, event):  # noqa: N802 - Qt virtual method name
            super().showEvent(event)
            # The display's own ratio is only known once the widget is on a
            # screen: the chart composition is made at that ratio.
            self._adopt_device_pixel_ratio()
            self._restart_chart_settle()

        def event(self, event):
            if (
                getattr(self, "_chart_without_weather", False)
                and event.type() == QEvent.Type.NativeGesture
            ):
                return self._native_gesture_event(event)
            result = super().event(event)
            if event.type() == QEvent.Type.DevicePixelRatioChange:
                # The display's ratio changed: the composition was made at
                # the old one and may not be resampled, so it is composed
                # again at the new one.  The base class has already adopted
                # the new ratio by the time the ratio is read here.
                self._adopt_device_pixel_ratio()
            return result

        def _native_gesture_event(self, event):
            """Own desktop trackpad pinch and update the viewport camera directly."""
            kind = event.gestureType()
            if kind == Qt.NativeGestureType.BeginNativeGesture:
                self._native_gesture_active = True
            elif kind == Qt.NativeGestureType.EndNativeGesture:
                self._native_gesture_active = False
            elif kind == Qt.NativeGestureType.ZoomNativeGesture:
                scale = 1.0 + event.value()
                if math.isfinite(scale) and scale > 0:
                    self._native_gesture_active = True
                    position = event.position()
                    self.zoom_at(math.log2(scale), position.x(), position.y())
            else:
                # Keep the map north-up; rotation has no viewport behavior.
                return bool(getattr(self, "_native_gesture_active", False))
            event.accept()
            return True

        def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
            super().resizeEvent(event)
            if self._camera is not None:
                self.set_camera(
                    replace(self._camera, width=self.width(), height=self.height())
                )

        def mousePressEvent(self, event):  # noqa: N802 - Qt virtual method name
            if event.button() == Qt.MouseButton.LeftButton:
                parent_widget = getattr(self, "parentWidget", None)
                canvas = parent_widget() if parent_widget else None
                record_measurement = getattr(canvas, "_record_measurement_click", None)
                if record_measurement and record_measurement(
                    event.position().x(), event.position().y()
                ):
                    event.accept()
                    return
                self.cancel_motion()
                self._drag = event.position()
                self._drag_velocity = DragVelocity()
                self._drag_velocity.add(
                    time.monotonic(), self._drag.x(), self._drag.y()
                )
                event.accept()
            else:
                super().mousePressEvent(event)

        def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual method name
            parent_widget = getattr(self, "parentWidget", None)
            canvas = parent_widget() if parent_widget else None
            update_preview = getattr(canvas, "_update_measurement_preview", None)
            if update_preview and update_preview(
                event.position().x(), event.position().y()
            ):
                event.accept()
                return
            if self._drag is not None:
                position = event.position()
                delta = position - self._drag
                self._drag = position
                self._drag_velocity.add(time.monotonic(), position.x(), position.y())
                self.pan_pixels(delta.x(), delta.y())
                event.accept()
            else:
                super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):  # noqa: N802 - Qt virtual method name
            if event.button() != Qt.MouseButton.LeftButton or self._drag is None:
                super().mouseReleaseEvent(event)
                return
            position = event.position()
            self._drag = None
            if self._pan_inertia_enabled:
                vx, vy = self._drag_velocity.release(
                    time.monotonic(), position.x(), position.y()
                )
                motion = PanInertia(vx, vy)
                if motion.active:
                    self._pan_inertia = motion
                    self._pan_inertia_last_tick = time.monotonic()
                    self._pan_inertia_tick.start()
            event.accept()

        def wheelEvent(self, event):  # noqa: N802 - Qt virtual method name
            if self._chart_without_weather:
                self._handle_desktop_wheel(event)
                return
            pixels = event.pixelDelta()
            if not pixels.isNull() and not (
                event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                self.pan_pixels(pixels.x(), pixels.y())
            else:
                self.zoom_at(
                    event.angleDelta().y() / 120 * 0.25,
                    event.position().x(),
                    event.position().y(),
                )
            event.accept()

        def closeEvent(self, event):  # noqa: N802 - Qt virtual method name
            self.dispose()
            super().closeEvent(event)

    return GribViewport


def saved_selection(data_dir):
    """Read the saved ordered selection without creating or modifying app data."""
    directory = Path(data_dir).expanduser()
    manager = json.loads((directory / "grib-manager.json").read_text())
    names = manager.get("opened", [])
    if not isinstance(names, list) or not names:
        raise ValueError("No saved forecasts selected; open forecasts in the app first")
    paths = []
    for name in names:
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("Saved forecast selection contains an invalid filename")
        path = directory / "grib" / name
        if not path.is_file():
            raise ValueError(f"Selected forecast is missing: {path}")
        paths.append(str(path))
    settings_path = directory / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    return tuple(paths), settings


def saved_chart_palette(data_dir, settings=None):
    """The user's own chart palette, read without creating or modifying data.

    The palette is the application's own: an unknown name falls back to the
    application's default rather than failing the composition.
    """
    from sailworks.qt.gribviewportchart import CHART_PALETTE
    from sailworks.rendering.style import Style

    if not settings:
        path = Path(data_dir).expanduser() / "settings.json"
        try:
            settings = json.loads(path.read_text())
        except (OSError, ValueError):
            settings = {}
    palette = settings.get("chartPalette") if isinstance(settings, dict) else None
    if palette in Style.chart_palettes:
        return palette
    return CHART_PALETTE


def main(argv=None):
    """Launch a read-only standalone viewport; no paths means saved selection."""
    import argparse

    from PySide6.QtCore import QDateTime, Qt
    from PySide6.QtWidgets import (
        QApplication,
        QDateTimeEdit,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    parser = argparse.ArgumentParser(description="Independent GRIB viewport")
    parser.add_argument(
        "paths", nargs="*", help="local GRIB files; default: saved selection"
    )
    parser.add_argument(
        "--data-dir", type=Path, help="saved app data directory (read only)"
    )
    parser.add_argument("--center-lat", type=float, default=48.31)
    parser.add_argument("--center-lon", type=float, default=-123.09)
    parser.add_argument("--zoom", type=float, default=9.8)
    parser.add_argument("--time", default="", help="forecast instant (ISO 8601)")
    parser.add_argument(
        "--resident-time-window",
        action="store_true",
        help=(
            "keep a bounded resident time window of forecast intervals over "
            "the presented spatial window (default: off)"
        ),
    )
    parser.add_argument(
        "--native-weather-fields",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Retain native field textures (default: on); "
            "--no-native-weather-fields restores Image sources"
        ),
    )
    parser.add_argument(
        "--screen-barbs",
        action="store_true",
        help=(
            "restore the previous screen-anchored barb layout, which re-places "
            "barbs on every pan (default: geographic layout)"
        ),
    )
    parser.add_argument(
        "--gpu-weather-fill",
        action="store_true",
        help=(
            "paint the numerical weather fill behind the wind barbs from the "
            "resident field (default: off)"
        ),
    )
    parser.add_argument(
        "--chart-layer",
        action="store_true",
        help=(
            "add the chart layer: the application's chart composition as an "
            "independent generation behind the weather (default: off)"
        ),
    )
    parser.add_argument(
        "--enc-chart-layer",
        action="store_true",
        help=(
            "add the installed and enabled ENC cells to the chart layer, "
            "drawn by the application's own ENC renderer (default: off; "
            "implies the chart layer)"
        ),
    )
    args = parser.parse_args(argv)
    settings = {}
    try:
        if args.paths:
            paths = tuple(str(Path(path).expanduser()) for path in args.paths)
            if not all(Path(path).is_file() for path in paths):
                raise ValueError("One or more supplied GRIB files do not exist")
        else:
            directory = args.data_dir
            if directory is None:
                from sailworks.core.storage import DATA_DIR

                directory = Path(DATA_DIR)
            paths, settings = saved_selection(directory)
        instant = (
            datetime.fromisoformat(args.time)
            if args.time
            else datetime.now(timezone.utc)
        )
        if instant.tzinfo is not None:
            instant = instant.astimezone(timezone.utc).replace(tzinfo=None)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))

    logging.basicConfig(level=logging.INFO)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    model = GribViewportModel(
        paths,
        resident_time_window=args.resident_time_window,
        grid=args.gpu_weather_fill,
    )
    chart_data_dir = args.data_dir
    if chart_data_dir is None:
        from sailworks.core.storage import DATA_DIR

        chart_data_dir = Path(DATA_DIR)
    # The chart composition uses the user's own palette where it is exposed.
    chart_palette = saved_chart_palette(chart_data_dir, settings)
    viewport = _viewport_widget_class()(
        model,
        gpu_weather_fill=args.gpu_weather_fill,
        chart_layer=args.chart_layer or args.enc_chart_layer,
        chart_data_dir=chart_data_dir,
        native_weather_fields=args.native_weather_fields,
        enc_chart_layer=args.enc_chart_layer,
        chart_palette=chart_palette,
    )
    window = QWidget()
    window.setWindowTitle(f"GRIB viewport — {len(paths)} forecasts")
    layout = QVBoxLayout(window)
    controls = QHBoxLayout()
    controls.addWidget(QLabel("Forecast UTC"))
    initial = QDateTime(instant)
    initial.setTimeSpec(Qt.TimeSpec.UTC)
    clock = QDateTimeEdit(initial)
    clock.setDisplayFormat("yyyy-MM-dd HH:mm 'UTC'")
    clock.setCalendarPopup(True)
    controls.addWidget(clock)
    for label, hours in (("−1 h", -1), ("+1 h", 1)):
        button = QPushButton(label)
        button.clicked.connect(
            lambda _checked=False, h=hours: clock.setDateTime(
                clock.dateTime().addSecs(h * 3600)
            )
        )
        controls.addWidget(button)
    controls.addStretch()
    controls.addWidget(QLabel("Drag: pan · Wheel: zoom · Trackpad: pan"))
    layout.addLayout(controls)
    layout.addWidget(viewport, 1)
    window.resize(1000, 780)
    window.show()
    camera = ViewportCamera(
        center_lat=args.center_lat,
        center_lon=args.center_lon,
        zoom=args.zoom,
        width=viewport.width(),
        height=viewport.height(),
        time=instant,
        density=settings.get("gribBarbDensity", "standard"),
        over_land=True,
        geographic=not args.screen_barbs,
    )
    viewport.set_camera(camera)
    clock.dateTimeChanged.connect(
        lambda value: viewport.set_time(value.toPython().replace(tzinfo=None))
    )
    # Closing a parent window does not send closeEvent to every child widget.
    app.aboutToQuit.connect(model.close)
    try:
        return app.exec()
    finally:
        viewport.close()


if __name__ == "__main__":
    raise SystemExit(main())
