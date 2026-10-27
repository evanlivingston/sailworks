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

from gweatherrouting.core.mapmotion import DragVelocity, PanInertia
from gweatherrouting.qt.gpuweatherfields import (
    FIELD_COLUMNS,
    FieldIdentity,
    FieldStore,
    GeographicField,
    validate_binding,
)
from gweatherrouting.rendering.weatherbarbs import (
    BarbView,
    FIELD_LATTICE_PX,
    field_window,
    _view_bounds,
    select_resident,
    stage_field,
)
from gweatherrouting.rendering.weatherstaging import NodeSampleMemo, time_window
from gweatherrouting.rendering.windpainter import shoreline_quality

logger = logging.getLogger("gweatherrouting")

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
    ):
        self.paths = tuple(str(path) for path in paths)
        if display_identity is not None and len(self.paths) != 1:
            raise ValueError("A display identity must belong to exactly one file")
        self.display_identity = display_identity
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
    def update(self, camera):  # noqa: C901 - atomic coverage/generation transition
        """Adopt a camera and return the portrayal that may be shown.

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
        if field is not None and _covers(field, camera):
            try:
                selection = select_resident(
                    field, camera.stamp(), camera.view()
                ).selection
            except Exception as exc:  # coverage or time escape
                logger.debug("Resident selection unavailable: %s", exc)
                selection = None
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
            from gweatherrouting.core.grib import Grib
            from gweatherrouting.core.gribsampling import WeatherSampler
            from gweatherrouting.rendering.windrenderer import WindLandMask
            from gweatherrouting.rendering.weathercache import (
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
                                *sampler._times[identity, "wind"],
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
            before, after = time_window(self._sampler, camera.stamp())
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
                parameter="wind",
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


def _viewport_widget_class():  # noqa: C901 - lazy factory includes Qt event methods
    """Build the render-context widget class on demand.

    Qt is imported lazily so the model and the tests stay usable headless.
    """
    from PySide6.QtCore import QEvent, QObject, QTimer, QUrl, Qt, Signal
    from PySide6.QtGui import QSurfaceFormat, QVector2D
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QLabel
    import shiboken6

    from gweatherrouting.qt.gpuweather import WeatherImageProvider
    from gweatherrouting.qt.gribviewportchart import (
        CHART_PALETTE,
        CHART_SETTLE_MS,
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
        marineStateChanged = Signal(str)  # noqa: N815

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
        ):
            super().__init__(parent)
            self._model = model
            self._source_generation = 0
            self._disposed = False
            self._wind_opacity = 1.0
            self._wind_style = "barbs"
            self._camera = None
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
            self._navigation_overlay = None
            self._navigation_key = None
            self._dashboard_overlay = None
            self._dashboard_key = None
            self._chart_state = "disabled"
            self._chart_viewport = None
            self._chart_camera = None
            self._chart_camera_changed = time.perf_counter()
            self._chart_ready_at = None
            self._chart_request_state = None
            self._chart_publish_state = None
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
                source = shaders / "WeatherBarbs.qml"
            self.setSource(QUrl.fromLocalFile(str(source)))
            self._weather: Any = self.rootObject()
            if self._weather is None:
                raise RuntimeError("The GRIB viewport could not load its shader")
            from .marinequick import MarineQuickLayer
            from .icoadsquick import IcoadsQuickLayer

            self._marine_layer = MarineQuickLayer(self._weather, self)
            self._marine_layer.stateChanged.connect(self.marineStateChanged.emit)
            self._icoads_layer = IcoadsQuickLayer(self._weather, self)
            self._barbs = self._weather
            self._fill = None
            if self._gpu_weather_fill or self._chart_layer:
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
                self._fill = self._weather.findChild(QObject, "weatherFill")
                if self._fill is None:
                    raise RuntimeError(
                        "The GRIB viewport could not load its weather scene"
                    )
                self._weather.setProperty("fillEnabled", True)
            if self._chart_layer:
                self._chart = self._weather.findChild(QObject, "chartLayer")
                self._chart_status_item = self._weather.findChild(
                    QObject, "chartStatus"
                )
                if self._chart is None or self._chart_status_item is None:
                    raise RuntimeError(
                        "The GRIB viewport could not load its chart scene"
                    )
                self._chart_model = ViewportChartLayer(
                    paths=model.paths,
                    data_dir=chart_data_dir,
                    palette=self._chart_palette,
                    worker_idle=model.busy,
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
            if self._native_weather_fields:
                from .gpuweathertextures import (
                    NativeWeatherBudget,
                    native_texture_context,
                )

                if self._native_budget is None:
                    self._native_budget = NativeWeatherBudget()
                for role, item in (("barbs", self._barbs), ("fill", self._fill)):
                    if item is None:
                        continue
                    context = native_texture_context(
                        self.quickWindow(), self._weather, self._native_budget
                    )
                    self._native_contexts[role] = context
                    item.setProperty("nativeFields", True)
                    for name, source_item in zip(
                        ("nativeU0", "nativeV0", "nativeU1", "nativeV1"),
                        context.sources,
                    ):
                        item.setProperty(name, source_item)
                    context.stateChanged.connect(
                        lambda status, layer=role: self._native_status(layer, status)
                    )
                    if role == "fill":
                        getattr(item, "readyChanged").connect(self._readiness_changed)
                    getattr(item, "shaderErrorChanged").connect(self._readiness_changed)

                def release_native_owners(
                    _obj=None, owners=tuple(self._native_contexts.values())
                ):
                    # QQuickWidget's scene graph has invalidated before QObject
                    # destruction. Keep GUI receivers alive until that boundary.
                    for owner in owners:
                        owner.close()
                        owner.deleteLater()

                self.destroyed.connect(release_native_owners)
            self._status = QLabel("Wind loading…", self)
            self._status.setStyleSheet(
                "background: rgba(0,0,0,190); color: white; padding: 6px"
            )
            self._status.move(8, 8)
            self._status.adjustSize()
            # The chart layer composes for a settled view.  The settle timer
            # spans more than fifteen of the gate's 30 inputs/s, so an
            # interaction never triggers a composition: the chart can never
            # compete with the weather pipeline for the worker.
            self._chart_timer = QTimer(self)
            self._chart_timer.setInterval(CHART_SETTLE_MS)
            self._chart_timer.timeout.connect(self._request_chart)
            self._chart_timer.start()
            self.prepared.connect(self._refresh, Qt.ConnectionType.QueuedConnection)
            self.chartPublished.connect(
                self._publish_chart, Qt.ConnectionType.QueuedConnection
            )
            self.chartStateChanged.connect(
                self._chart_state_changed, Qt.ConnectionType.QueuedConnection
            )
            self._attach_model()
            from .viewportstreamlines import ViewportStreamlineModel

            self._streamline_model = ViewportStreamlineModel(
                model.paths, model.display_identity
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
            self._streamline_settle = QTimer(self)
            self._streamline_settle.setSingleShot(True)
            self._streamline_settle.setInterval(120)
            self._streamline_settle.timeout.connect(self._retrace_streamlines)
            self.streamlinesPrepared.connect(
                self._refresh_streamlines, Qt.ConnectionType.QueuedConnection
            )
            self._streamlines = self._weather.findChild(QObject, "weatherStreamlines")
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

        def _show_state(self, state):
            self._state = state
            self._status.setText(
                "Wind loading…"
                if state == "loading"
                else (self._model.error or self._native_error or "Wind unavailable")
            )
            self._status.adjustSize()
            self._status.setVisible(state != "ready")
            self._status.raise_()
            if state == "ready" and self._weather_ready_at is None:
                # The weather is presented: the chart layer may now be asked
                # for its own generation.
                self._weather_ready_at = time.perf_counter()
            self.stateChanged.emit(state)

        def _chart_state_changed(self, state):
            """The chart layer's own state; it never changes the weather's."""
            self._chart_state = state
            if self._chart is not None:
                self._chart.setProperty("chartLoading", state == "loading")
                if state == "loading":
                    # The chart layer's own loading state was shown.
                    self._chart_loading_shown = True
                if state == "unavailable" and self._enc_chart_layer:
                    # A failed composition never leaves the previous ENC on
                    # screen: it is not current, and it is not presented as
                    # if it were.
                    cast(QObject, self._chart).setProperty("source", QUrl(""))
            self._update_enc_status()

        def _publish_chart(self, payload):
            """Publish a composed chart image (GUI thread)."""
            generation, image, metadata = payload
            if self._disposed:
                # A disposed viewport never shows a composition.
                return
            self._chart_viewport = metadata["viewport"]
            url = cast(Any, self._chart_provider).publish(generation, image)
            cast(QObject, self._chart).setProperty("source", QUrl(url))
            self.counters["chart_publications"] += 1
            self._place_chart()
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
                cast(QObject, chart).setProperty("source", QUrl(""))
            self._update_enc_status()
            camera = getattr(self, "_camera", None)
            if camera is not None and not getattr(self, "_disposed", False):
                model.request(camera, replace=True)

        def _place_chart(self):
            """Place the retained chart image for the current camera."""
            if self._chart is None or self._chart_viewport is None:
                return
            if self._camera is None:
                return
            x, y, width, height = chart_placement(self._chart_viewport, self._camera)
            self._chart.setProperty("imageX", x)
            self._chart.setProperty("imageY", y)
            self._chart.setProperty("imageWidth", width)
            self._chart.setProperty("imageHeight", height)

        def set_base(self, image, target):
            """Compose the app's basemap inside Quick, below ENC and weather."""
            if self._base is None or self._base_provider is None:
                return
            key = image.cacheKey()
            if key != self._base_key:
                url = self._base_provider.publish(key, image)
                self._base.setProperty("source", QUrl(url))
                self._base_key = key
            for name, value in zip(
                ("imageX", "imageY", "imageWidth", "imageHeight"),
                (target.x(), target.y(), target.width(), target.height()),
            ):
                self._base.setProperty(name, value)

        def set_environment(self, image, target):
            """Present the app's retained above-ENC layers without changing order."""
            if self._environment is None or self._environment_provider is None:
                return
            key = image.cacheKey()
            if key != self._environment_key:
                url = self._environment_provider.publish(key, image)
                self._environment.setProperty("source", QUrl(url))
                self._environment_key = key
            for name, value in zip(
                ("imageX", "imageY", "imageWidth", "imageHeight"),
                (target.x(), target.y(), target.width(), target.height()),
            ):
                self._environment.setProperty(name, value)

        def set_route_overlay(self, overlay):
            """Present app route/preview geometry in the viewport scene."""
            item = self._route_overlay
            camera = self._camera
            if item is None or camera is None:
                return
            for name, value in (
                ("routePlan", overlay["route_plan"]),
                ("routePreview", overlay["route_preview"]),
                ("selectedPoint", overlay["selected_point"]),
                ("trackPoints", overlay["track_points"]),
                ("trackPosition", overlay["track_position"]),
                ("trackHeading", overlay.get("track_heading")),
                ("trackAwa", overlay.get("track_awa")),
                ("captionText", overlay["caption"]),
                ("dashboardVisible", overlay["dashboard_visible"]),
                ("centerLatitude", camera.center_lat),
                ("centerLongitude", camera.center_lon),
                ("zoom", camera.zoom),
            ):
                item.setProperty(name, value)

        def set_marine(self, sources, instant, visible):
            if self._camera is not None:
                self._marine_layer.request(sources, instant, visible, self._camera)

        def set_ship_observations(self, reports, visible):
            if self._camera is not None:
                self._icoads_layer.present(reports, visible, self._camera)

        def set_navigation_overlay(self, image, target):
            """Present retained application navigation imagery in Quick."""
            item = self._navigation_overlay
            provider = self._navigation_provider
            if item is None or provider is None:
                return
            key = image.cacheKey()
            if key != self._navigation_key:
                url = provider.publish(key, image)
                item.setProperty("source", QUrl(url))
                self._navigation_key = key
            for name, value in zip(
                ("imageX", "imageY", "imageWidth", "imageHeight"),
                (target.x(), target.y(), target.width(), target.height()),
            ):
                item.setProperty(name, value)

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

        def _request_chart(self):
            """Ask the chart layer for its own generation.

            The chart is composed for a settled view: the view must not have
            moved for the settle window, the weather must already be
            presented and the weather pipeline must be idle.  While any of
            those is untrue the chart asks again, so the chart never delays
            the weather and never composes during an interaction.
            """
            if self._chart_model is None or self._camera is None:
                return
            if (
                self._weather_ready_at is None
                and not getattr(self, "_chart_without_weather", False)
                and not (self._enc_chart_layer and self._model.state == "unavailable")
            ):
                # The chart is added after the weather is presented.
                return
            if (time.perf_counter() - self._chart_camera_changed) * 1000.0 < (
                CHART_SETTLE_MS
            ):
                # The view is still moving: the chart waits for it to settle.
                return
            if self._model.busy():
                # The weather pipeline is working: the chart waits.
                return
            self.counters["chart_requests"] += 1
            self._chart_requested_after_weather = True
            self._chart_request_state = self._chart_state_now()
            self._chart_model.request(self._camera)

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

        def _native_status(self, role, status):
            if self._disposed:
                return
            item = self._barbs if role == "barbs" else self._fill
            if item is None or not shiboken6.isValid(item):
                return
            if status.get("invalidated"):
                self._native_bindings.pop(role, None)
                item.setProperty("nativeReady", False)
                item.setProperty("nativeFrameObserved", False)
                if self._camera is not None:
                    QTimer.singleShot(
                        0,
                        lambda: (
                            self.set_camera(self._camera)
                            if not self._disposed and self.isVisible()
                            else None
                        ),
                    )
                return
            if status["key"].identity != self._native_bindings.get(role):
                return
            if status.get("error"):
                self._native_error = status["error"]
                self._set_weather_visible(False)
                self._show_state("unavailable")
                self.nativeError.emit(self._native_error)
                return
            item.setProperty("nativeReady", status["ready"])
            item.setProperty("nativeFrameObserved", bool(status.get("frame_observed")))
            self._update_native_state()

        def _update_native_state(self):
            if self._wind_style == "streamlines":
                return
            if self._disposed or self._camera is None:
                return
            items = [self._barbs] + ([self._fill] if self._fill is not None else [])
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
                item.property("ready") for item in items
            ):
                if self._state != "ready":
                    self._show_state("ready")
            elif self._model.state == "ready" and self._state != "loading":
                self._show_state("loading")

        def _bind_native_layer(self, role, identity, images):
            item = self._barbs if role == "barbs" else self._fill
            if item is None:
                raise RuntimeError(f"Viewport has no native {role} layer")
            context = self._native_contexts[role]
            if self._native_bindings.get(role) == identity and context.ready:
                return
            self._native_bindings[role] = identity
            self._native_error = ""
            item.setProperty("nativeFrameObserved", False)
            warm = context.request(
                identity, images, "nodes" if role == "barbs" else "grid"
            )
            item.setProperty("nativeReady", warm)
            self.counters["native_field_requests"] += 1

        def native_observations(self):
            return {
                "enabled": self._native_weather_fields,
                "layers": {
                    role: context.snapshot()
                    for role, context in self._native_contexts.items()
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
            self._clear_weather_sources()
            for context in self._native_contexts.values():
                context.clear()
            self._native_bindings.clear()
            self._native_error = ""
            old = self._model
            old.on_change = None
            self._source_generation += 1
            old.close()  # nonjoining; the owning worker releases private handles
            from .viewportstreamlines import ViewportStreamlineModel

            self._streamline_model.close()
            self._streamline_model = ViewportStreamlineModel(
                model.paths, model.display_identity
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

        def _refresh_streamlines(self):
            if not self._disposed and self._camera is not None:
                self._publish_streamlines(self._camera, force=True)

        def _update_streamline_camera(self, camera):
            view = camera.view()
            size = view.TILE_SIZE * 2.0**camera.zoom
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
            self._update_streamline_camera(camera)
            camera_changed = camera != self._streamline_last_camera
            self._streamline_last_camera = camera
            prior = self._streamline_display_camera
            same_trace = prior is not None and (
                prior.time,
                prior.density,
                prior.over_land,
            ) == (camera.time, camera.density, camera.over_land)
            if (
                same_trace
                and not force
                and self._streamline_publication is not None
                and self._streamline_urls
            ):
                self._streamlines.setVisible(enabled)
                self._streamlines.setOpacity(
                    self._wind_opacity * (0.6 if prior.time != camera.time else 1.0)
                )
                if not camera_changed:
                    self._streamline_readiness_changed()
                    return "ready"
                self._streamline_settle.start()
                if prior.time == camera.time:
                    self._streamline_readiness_changed()
                    return "ready"
            else:
                self._streamline_settle.stop()
            state, paths = self._streamline_model.update(camera, enabled)
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
            from gweatherrouting.rendering.streamlinepaths import (
                paths_to_render_buffers,
            )
            from gweatherrouting.rendering.windpainter import barb_bucket_colors
            from PySide6.QtGui import QColor
            from .viewportstreamlines import MAX_BATCH_SIZE

            if state != "ready" or paths is None:
                source = self._streamline_display_camera
                retain_previous = (
                    state == "loading"
                    and enabled
                    and source is not None
                    and source.over_land == camera.over_land
                )
                if retain_previous:
                    self._streamlines.setOpacity(
                        self._wind_opacity
                        * (0.6 if source.time != camera.time else 1.0)
                    )
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
                    if retain_previous:
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
                self._streamlines.setOpacity(self._wind_opacity)
                self._streamlines.setVisible(enabled)
                self._streamline_readiness_changed()
                return state
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
            self._streamlines.setOpacity(
                self._wind_opacity
                * (
                    0.6
                    if self._streamline_display_camera
                    and self._streamline_display_camera.time != camera.time
                    else 1.0
                )
            )
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

        def dispose(self):
            if self._disposed:
                return
            self._disposed = True
            self._marine_layer.close()
            self._icoads_layer.close()
            self._streamline_model.close()
            self._streamline_settle.stop()
            self._streamlines.setVisible(False)
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
            if shiboken6.isValid(self._chart_timer):
                self._chart_timer.stop()
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
            if shiboken6.isValid(self._weather):
                self._weather.setVisible(False)

        def _clear_weather_sources(self):
            # Clearing the CPU provider alone leaves QML Images holding the
            # previous source's native textures. Retire both ownership layers.
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
                (self._fill, ("u0Source", "v0Source", "u1Source", "v1Source")),
            ):
                if item is not None and shiboken6.isValid(item):
                    for name in names:
                        item.setProperty(name, QUrl())
                    if self._native_weather_fields:
                        item.setProperty("nativeReady", False)
                        item.setProperty("nativeFrameObserved", False)

        def _refresh(self, generation):
            if generation != self._source_generation or self._disposed:
                return
            if self._camera is not None:
                self.set_camera(self._camera)

        def _set_weather_visible(self, visible):
            """Chart scenes remain visible even when weather has no data."""
            if getattr(self, "_chart_layer", False) or getattr(
                self, "_enc_chart_layer", False
            ):
                self._weather.setVisible(True)
                self._barbs.setVisible(visible)
                if self._fill is not None:
                    self._fill.setVisible(visible)
            else:
                self._weather.setVisible(visible)

        def set_camera(self, camera):
            """Adopt a camera and publish the portrayal it may show."""
            if self._disposed:
                return "unavailable"
            began = time.perf_counter()
            state = "loading"
            try:
                self._camera = camera
                self._marine_layer.set_camera(camera)
                self._icoads_layer.set_camera(camera)
                if self._chart_model is not None:
                    # The chart is composed for a settled view: the settle
                    # timer only asks for a composition when the view has
                    # not moved for the settle window, so an interaction
                    # never triggers a composition and the chart can never
                    # compete with the weather pipeline.  The retained
                    # composition is placed for the new camera either way.
                    previous_chart_camera = self._chart_camera
                    if self._chart_camera != camera:
                        self._chart_camera = camera
                        self._chart_camera_changed = time.perf_counter()
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
                        cast(QObject, self._chart).setProperty("source", QUrl(""))
                self._place_chart()
                self._publish_streamlines(camera)
                if self._wind_style == "streamlines":
                    self._set_weather_visible(False)
                    self.cameraChanged.emit(camera)
                    return self._state
                state, field, selection = self._model.update(camera)
                if state != "ready":
                    # A miss shows the loading state; it never shows stale content.
                    self._set_weather_visible(False)
                    self._show_state(state)
                    self.cameraChanged.emit(camera)
                    return state
                if self._publish(field, selection, camera) is False:
                    return "unavailable"
                self._set_weather_visible(True)
                if self._native_weather_fields:
                    self._update_native_state()
                else:
                    self._show_state(
                        "ready" if self._barbs.property("ready") else "loading"
                    )
                self.cameraChanged.emit(camera)
                return state
            finally:
                self._presentation_spans.append((began, time.perf_counter(), state))
                if len(self._presentation_spans) > 4096:
                    del self._presentation_spans[: len(self._presentation_spans) - 4096]

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
                field_urls = self._provider.publish_field(
                    field.images, field.generation
                )
            selection_urls = self._provider.publish_selection(
                selection.images, selection.generation
            )
            # The resident time window's fields are published once, so a
            # forecast-time change inside the window publishes nothing and
            # re-encodes nothing.
            if not self._native_weather_fields:
                for resident in self._model.window_fields():
                    self._provider.publish_field(resident.images, resident.generation)
            # The two layers are staged on their own inputs: the fill sees the
            # resident field and the temporal blend, the barbs see the resident
            # field and the current-view selection.
            self._publish_fill(field, field_urls, camera)
            self._publish_barbs(field, selection, field_urls, selection_urls, camera)
            self._published = selection.generation
            self._published_generations.add(selection.generation)

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
                return
            key = f"{field.generation}/grid"
            if self._native_weather_fields:
                self._bind_native_layer("fill", field.identity, grid.images)
                self._published_fill = key
            elif self._published_fill != key:
                urls = self._provider.publish_field(grid.images, key)
                for name, url in zip(
                    ("u0Source", "v0Source", "u1Source", "v1Source"), urls
                ):
                    self._fill.setProperty(name, QUrl(url))
                self._published_fill = key
                self.counters["fill_publications"] += 1
                self.counters["fill_uploads"] += 1
            self._fill.setProperty("blend", field.staged.weight(camera.stamp()))
            rect = _grid_rect(camera.view(), grid.lattice)
            for name, value in (
                ("gridX", rect.x()),
                ("gridY", rect.y()),
                ("gridWidth", rect.width()),
                ("gridHeight", rect.height()),
            ):
                if self._fill.setProperty(name, value) is False:
                    logger.warning("The numerical fill rejected %s", name)

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
            urls = self._provider.publish_field(images, key)
            for name, url in zip(
                ("u0Source", "v0Source", "u1Source", "v1Source"), urls
            ):
                self._fill.setProperty(name, QUrl(url))
            self._published_fill = key
            self.counters["fill_publications"] += 1
            self.counters["fill_uploads"] += 1
            return urls

        def _publish_barbs(self, field, selection, field_urls, selection_urls, camera):
            """Stage the barbs layer on its own inputs.

            The barbs consume the resident field's endpoint images and the
            current-view selection.  A fill change never reaches this path.
            """
            if self._native_weather_fields:
                self._bind_native_layer("barbs", field.identity, field.images)
            elif self._published != field.generation:
                for name, url in zip(
                    ("u0Source", "v0Source", "u1Source", "v1Source"), field_urls
                ):
                    self._barbs.setProperty(name, QUrl(url))
                self.counters["barbs_field_publications"] += 1
                self.counters["barbs_field_uploads"] += 1
            for name, url in zip(
                ("nodeIdSource", "anchorXSource", "anchorYSource"), selection_urls
            ):
                self._barbs.setProperty(name, QUrl(url))
            self.counters["barbs_selection_publications"] += 1
            self.counters["barbs_selection_uploads"] += 1
            rows, cols = selection.shape
            self._barbs.setProperty("columns", cols)
            self._barbs.setProperty("rows", rows)
            self._barbs.setProperty("nodeIdIndirection", True)
            self._barbs.setProperty("fieldColumns", FIELD_COLUMNS)
            self._barbs.setProperty("blend", field.staged.weight(camera.stamp()))
            self._barbs.setOpacity(self._wind_opacity)

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
    from gweatherrouting.qt.gribviewportchart import CHART_PALETTE
    from gweatherrouting.rendering.style import Style

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
                from gweatherrouting.core.storage import DATA_DIR

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
        from gweatherrouting.core.storage import DATA_DIR

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
