"""An independent GRIB viewport: its own camera, scheduler and GPU context.

The viewport is a standalone subsystem.  It does not instantiate the
application's Core, SessionController, TrackMap, MapFrame, MapScene or
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
import os
from pathlib import Path
import sys
from threading import Condition, Event, Thread
import time

from gweatherrouting.qt.gpuweatherfields import (
    FIELD_COLUMNS,
    FieldIdentity,
    FieldStore,
    validate_binding,
)
from gweatherrouting.rendering.weatherbarbs import (
    BarbView,
    field_window,
    _view_bounds,
    select_resident,
    stage_field,
)
from gweatherrouting.rendering.weatherstaging import time_window
from gweatherrouting.rendering.windpainter import shoreline_quality

logger = logging.getLogger("gweatherrouting")

# The residency budget of the viewport's field store and of the published
# images.  Both are declared ceilings, not measured optima.  On the recorded
# 900x700 fixture the store held 33 prepared fields in 58.8 MB with no
# evictions, so the standalone store uses the same 128 MiB ceiling as the
# decoded-band cache.  It is a CPU payload budget, not VRAM, and larger views
# retain fewer fields.
FIELD_BYTE_LIMIT = 128 * 1024 * 1024
PREPARED_VIEW_FACTOR = 1.5
REFILL_VIEW_FACTOR = 1.25


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

    field: object
    generation: tuple
    demand: tuple
    slot: int
    span: tuple


class GribViewportModel:
    """The viewport's field scheduler and current-selection owner.

    The worker thread owns the GDAL handles and prepares geographic fields.
    The render context owns the GPU resources.  A covered camera move is
    served from the resident field without touching the worker, so a stalled
    preparation never stops camera updates.
    """

    def __init__(self, paths, field_bytes=FIELD_BYTE_LIMIT, land_mask=None):
        self.paths = tuple(str(path) for path in paths)
        self._land_mask = land_mask
        self._condition = Condition()
        self._pending = None
        self._active = None
        self._desired_key = None
        self._failed_key = None
        self._field = None
        self._sources = ()
        self._sampler = None
        self._land = None
        self._thread = None
        self._closed = False
        self._camera = None
        self._state = "loading"
        self._error = ""
        self._store = FieldStore(field_bytes)
        self._events = ()
        # Two prepared-resource slots: the front slot is what the viewport may
        # present, the back slot is what the worker fills.  At most two requests
        # are in flight (one active, one pending); a replaced request is
        # cancelled and can never publish.
        self._slots = [None, None]
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

    # ----------------------------------------------------------- camera
    def update(self, camera):
        """Adopt a camera and return the portrayal that may be shown.

        Returns ``(state, field, selection)``.  ``state`` is ``ready`` only
        when the selection belongs to the field that is actually published.
        The front slot is presented; the back slot is a prepared N+1 that may
        be handed forward without waiting for the worker.
        """
        with self._condition:
            if self._closed:
                return ("unavailable", None, None)
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
            guard = _expanded(camera, REFILL_VIEW_FACTOR)
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
            field_window(_expanded(camera, PREPARED_VIEW_FACTOR).view()),
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
        """Promote a prepared back-slot resource to the presented front."""
        self._field = resource.field
        self._front_index = resource.slot
        self._store.acquire(resource.field.identity)
        self._store.protect(resource.field.identity)
        self._failed_key = None
        self._error = ""
        self._state = "ready"
        self.counters["slot_handoffs"] += 1

    def _request(self, camera):
        """Queue the latest demand; a replaced request is cancelled.

        Only one request is active and one is pending, so at most two are in
        flight.  A pending request is always replaceable.  The active request
        is cancelled only when the time window or land policy makes its field
        fundamentally different; a pan keeps it so a 30 Hz input stream cannot
        starve preparation.
        """
        key = self._request_key(camera)
        self._desired_key = key
        if self._active is not None and self._active[2] == key:
            if self._pending is not None and self._pending[2] != key:
                self._pending[1].set()
                self.counters["cancelled"] += 1
                self._pending = None
            return
        if self._pending is not None:
            if self._pending[2] == key:
                return
            self._pending[1].set()
            self.counters["cancelled"] += 1
            self._pending = None
        if self._active is not None:
            old, active_cancel, _key = self._active
            if self._time_key(old.stamp()) != self._time_key(camera.stamp()) or (
                old.over_land != camera.over_land
            ):
                if not active_cancel.is_set():
                    active_cancel.set()
                    self.counters["cancelled"] += 1
        cancel = Event()
        self._pending = (camera, cancel, key)
        if self._thread is None:
            self._thread = Thread(target=self._run, name="grib-viewport", daemon=True)
            self._thread.start()
        self._condition.notify_all()

    def observations(self):
        """Preparation spans and publication-rejection evidence for the gate."""
        with self._condition:
            return {
                "preparation_spans": tuple(self._preparation_spans),
                "published_generations": tuple(sorted(self._published_generations)),
                "rejected_generations": tuple(sorted(self._rejected_generations)),
                "published_demands": tuple(sorted(self._published_demands, key=repr)),
                "rejected_demands": tuple(sorted(self._rejected_demands, key=repr)),
                "generation_rejected": self.counters["generation_rejected"],
                "superseded_rejected": self.counters["stale_rejected"],
                "slot_handoffs": self.counters["slot_handoffs"],
                "slots_filled": sum(resource is not None for resource in self._slots),
                "front_slot": self._front_index,
            }

    def _run(self):
        sources = []
        try:
            from gweatherrouting.core.grib import Grib
            from gweatherrouting.core.gribsampling import WeatherSampler
            from gweatherrouting.rendering.windrenderer import WindLandMask
            from gweatherrouting.rendering.weathercache import (
                DecodedBandCache,
                DisplayWeatherSource,
            )

            bands = DecodedBandCache(128 * 1024 * 1024)
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
                    )
                )
            # Opening/indexing must never happen while holding the caller's lock.
            sampler = WeatherSampler(sources)
            if len(sampler.indices) != len(sources) or not sources:
                raise ValueError("A selected forecast is unavailable")
            self._sources = tuple(sources)
            self._sampler = sampler
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
                        self._condition.wait()
                    if self._closed:
                        return
                    request, self._pending = self._pending, None
                    self._active = request
                camera, cancel, key = request
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
                            # The request was superseded: it may never publish.
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
                            if (
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
                callback = None if self._closed else self.on_change
            if callback is not None:
                callback()
        finally:
            for source in sources:
                source.close()
            with self._condition:
                self._sources, self._sampler, self._land = (), None, None
                self._store.clear()
                self._condition.notify_all()

    def _prepare(self, camera, cancel):
        """Prepare the geographic field of the camera's window."""
        if cancel.is_set():
            return None
        try:
            view = camera.view()
            with self._condition:
                geometry = next(
                    (
                        field
                        for field in reversed(self._store.fields())
                        if _spatially_covers(
                            field, _expanded(camera, REFILL_VIEW_FACTOR)
                        )
                    ),
                    None,
                )
            window = (
                geometry.identity.window
                if geometry is not None
                else field_window(_expanded(camera, PREPARED_VIEW_FACTOR).view())
            )
            source = tuple(_source_key(item) for item in self._sources)
            before, after = time_window(self._sampler, camera.stamp())
            resolution = (
                tuple(
                    sorted(
                        {index.nominal_step_degrees for index in self._sampler.indices}
                    )
                ),
                None if camera.over_land else shoreline_quality(view.get_scale()),
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
        version,
    )


def _viewport_widget_class():
    """Build the render-context widget class on demand.

    Qt is imported lazily so the model and the tests stay usable headless.
    """
    from PySide6.QtCore import QUrl, Qt, Signal
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QLabel

    from gweatherrouting.qt.gpuweather import WeatherImageProvider

    class GribViewport(QQuickWidget):
        """The viewport's render context: the GPU side of the split.

        The field's endpoint images are published once and stay resident; the
        current-view selection is published for every view.  Only a selection
        that belongs to the published field is shown as current.
        """

        prepared = Signal()
        cameraChanged = Signal(object)
        stateChanged = Signal(str)

        def __init__(self, model, parent=None):
            super().__init__(parent)
            self._model = model
            self._camera = None
            self._drag = None
            self._state = "loading"
            self._presentation_spans = []
            format_ = QSurfaceFormat()
            format_.setSamples(4)
            format_.setAlphaBufferSize(8)
            self.setFormat(format_)
            self.setResizeMode(QQuickWidget.SizeRootObjectToView)
            self._provider = WeatherImageProvider()
            self.engine().addImageProvider("weather", self._provider)
            source = Path(__file__).parent / "shaders" / "WeatherBarbs.qml"
            self.setSource(QUrl.fromLocalFile(str(source)))
            self._weather = self.rootObject()
            if self._weather is None:
                raise RuntimeError("The GRIB viewport could not load its shader")
            self._published = None
            self._published_generations = set()
            self._status = QLabel("Wind loading…", self)
            self._status.setStyleSheet(
                "background: rgba(0,0,0,190); color: white; padding: 6px"
            )
            self._status.move(8, 8)
            self._status.adjustSize()
            self.prepared.connect(self._refresh, Qt.QueuedConnection)
            self._model.on_change = self.prepared.emit
            self._weather.readyChanged.connect(self._readiness_changed)

        def _show_state(self, state):
            self._state = state
            self._status.setText(
                "Wind loading…"
                if state == "loading"
                else (self._model.error or "Wind unavailable")
            )
            self._status.adjustSize()
            self._status.setVisible(state != "ready")
            self._status.raise_()
            self.stateChanged.emit(state)

        def _readiness_changed(self):
            if self._weather.property("ready") and self._model.state == "ready":
                self._show_state("ready")

        def _refresh(self):
            if self._camera is not None:
                self.set_camera(self._camera)

        def set_camera(self, camera):
            """Adopt a camera and publish the portrayal it may show."""
            began = time.perf_counter()
            state = "loading"
            try:
                self._camera = camera
                state, field, selection = self._model.update(camera)
                if state != "ready":
                    # A miss shows the loading state; it never shows stale content.
                    self._weather.setVisible(False)
                    self._show_state(state)
                    self.cameraChanged.emit(camera)
                    return state
                self._publish(field, selection, camera)
                self._weather.setVisible(True)
                self._show_state(
                    "ready" if self._weather.property("ready") else "loading"
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
            }

        def _publish(self, field, selection, camera):
            # Reject before publishing either namespace or touching QML sources.
            validate_binding(field, selection)
            field_urls = self._provider.publish_field(field.images, field.generation)
            selection_urls = self._provider.publish_selection(
                selection.images, selection.generation
            )
            if self._published != field.generation:
                for name, url in zip(
                    ("u0Source", "v0Source", "u1Source", "v1Source"), field_urls
                ):
                    self._weather.setProperty(name, QUrl(url))
            for name, url in zip(
                ("nodeIdSource", "anchorXSource", "anchorYSource"), selection_urls
            ):
                self._weather.setProperty(name, QUrl(url))
            rows, cols = selection.shape
            self._weather.setProperty("columns", cols)
            self._weather.setProperty("rows", rows)
            self._weather.setProperty("nodeIdIndirection", True)
            self._weather.setProperty("fieldColumns", FIELD_COLUMNS)
            self._weather.setProperty("blend", field.staged.weight(camera.stamp()))
            self._weather.setOpacity(1.0)
            self._published = selection.generation
            self._published_generations.add(selection.generation)

        def publication_counters(self):
            """The published traffic, counted apart by kind."""
            return {
                "field_publications": self._provider.field_publications,
                "selection_publications": self._provider.selection_publications,
                "field_bytes": self._provider.field_bytes(),
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

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if self._camera is not None:
                self.set_camera(
                    replace(self._camera, width=self.width(), height=self.height())
                )

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                self._drag = event.position()
                event.accept()
            else:
                super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if self._drag is not None:
                position = event.position()
                delta = position - self._drag
                self._drag = position
                self.pan_pixels(delta.x(), delta.y())
                event.accept()
            else:
                super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            self._drag = None
            super().mouseReleaseEvent(event)

        def wheelEvent(self, event):
            pixels = event.pixelDelta()
            if not pixels.isNull() and not (event.modifiers() & Qt.ControlModifier):
                self.pan_pixels(pixels.x(), pixels.y())
            else:
                self.zoom_at(
                    event.angleDelta().y() / 120 * 0.25,
                    event.position().x(),
                    event.position().y(),
                )
            event.accept()

        def closeEvent(self, event):
            self._model.close()
            self._provider.clear()
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
    model = GribViewportModel(paths)
    viewport = _viewport_widget_class()(model)
    window = QWidget()
    window.setWindowTitle(f"GRIB viewport — {len(paths)} forecasts")
    layout = QVBoxLayout(window)
    controls = QHBoxLayout()
    controls.addWidget(QLabel("Forecast UTC"))
    initial = QDateTime(instant)
    initial.setTimeSpec(Qt.UTC)
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
        over_land=settings.get("gribArrowOnGround", False),
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
