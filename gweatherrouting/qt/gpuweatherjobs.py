"""One private weather worker, one replaceable pending request per map."""

import logging
import os
from threading import Condition, Event, Thread

from PySide6.QtCore import QObject, Signal, Slot

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.core.routingjob import freeze_wind_sources
from gweatherrouting.qt.gpuweatherfields import FieldStore
from gweatherrouting.rendering.weatherbarbs import BarbView, stage_barbs
from gweatherrouting.rendering.weathercache import (
    DecodedBandCache,
    DisplayWeatherSource,
)
from gweatherrouting.rendering.windrenderer import WindLandMask

logger = logging.getLogger("gweatherrouting")

# The declared residency budget of the GPU-resident field store. It is the
# same 32 MiB the CPU staging path already allowed for one published pair, so
# the field store does not raise the memory ceiling of the presenter.
FIELD_BYTE_LIMIT = 32 * 1024 * 1024


def source_key(source):
    try:
        stat = os.stat(source.path)
        version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except OSError:
        version = None
    return (
        id(source),
        source.path,
        source.name,
        source.centre,
        source.start_time,
        source.last_forecast,
        repr(source.bounds),
        version,
    )


class _WeatherResources:
    """Created, used and closed exclusively by the numerical weather worker."""

    def __init__(self, byte_limit, field_limit=None, resident_fields=False):
        self.providers: list[DisplayWeatherSource] = []
        self.sampler: WeatherSampler | None = None
        self.signature: tuple | None = None
        self.band_cache = DecodedBandCache(byte_limit)
        self.land = WindLandMask()
        self.resident_fields = resident_fields
        self.fields = FieldStore(
            field_limit if field_limit is not None else FIELD_BYTE_LIMIT
        )

    def release(self):
        for provider in self.providers:
            provider.close()
        self.providers.clear()
        self.sampler = None
        self.signature = None
        # Resident fields belong to the selected sources; a source change
        # releases them instead of letting them occupy the budget.
        self.fields.clear()

    def _open(self, sources, signature, cancel):
        self.release()
        for source in sources:
            if cancel.is_set():
                return
            expected = signature[len(self.providers)][-1]
            if source_key(source)[-1] != expected:
                raise ValueError("Forecast file changed before staging")
            self.providers.append(
                DisplayWeatherSource(
                    source.path,
                    source.name,
                    source.centre,
                    source.bounds,
                    source.start_time,
                    source.last_forecast,
                    source.model_name,
                    band_cache=self.band_cache,
                )
            )
        if cancel.is_set():
            return
        self.sampler = WeatherSampler(self.providers)
        if len(self.sampler.indices) != len(self.providers):
            raise ValueError("A selected weather source is unavailable")
        self.signature = signature

    def prepare(self, key, stamp, sources, cancel):
        if self.signature != key[1]:
            self._open(sources, key[1], cancel)
        if cancel.is_set() or self.sampler is None:
            return None
        atlas = stage_barbs(
            self.sampler,
            stamp,
            key[0],
            self.land,
            cancel.is_set,
            **(
                {"store": self.fields, "source": key[1]} if self.resident_fields else {}
            ),
        )
        if any(
            source_key(source)[-1] != entry[-1]
            for source, entry in zip(sources, key[1])
        ):
            raise ValueError("Forecast file changed during staging")
        return atlas

    def close(self):
        self.release()
        self.land.contains.cache_clear()
        self.land._prepared.clear()
        self.land._tiles.clear()
        self.land._dataset = None


class WeatherStager(QObject):
    BAND_BYTE_LIMIT = 128 * 1024 * 1024
    changed = Signal()
    completed = Signal(object, object, str)

    def __init__(self, parent=None, resident_fields=False):
        super().__init__(parent)
        self.closed = False
        self.frame = None
        self.latest = None
        self.failure = None
        self._condition = Condition()
        self._pending = None
        self._active = None
        self._prepared = None
        self._thread = None
        self._release_requested = False
        self._band_stats = {}
        self._field_stats = {}
        self.resident_fields = resident_fields
        self.completed.connect(self._accept)

    def band_cache_statistics(self):
        """Completed-request snapshot only; no worker handles/arrays cross threads."""
        with self._condition:
            return dict(self._band_stats)

    def field_store_statistics(self):
        """Completed-request snapshot of the resident field store."""
        with self._condition:
            return dict(self._field_stats)

    def request(self, canvas):
        if self.closed:
            return None
        settings = canvas.controller.settings
        sources = tuple(canvas.controller.core.grib_manager.gribs)
        # Non-GDAL providers, streamlines and unthinned mode keep the CPU path.
        eligible = (
            canvas.wind_visible
            and sources
            and all(isinstance(s, Grib) for s in sources)
            and getattr(settings, "gribWindStyle", "barbs") == "barbs"
            and getattr(settings, "gribBarbDensity", "standard") != "all"
        )
        if not eligible:
            self.invalidate()
            return None
        view = BarbView(
            canvas.width(),
            canvas.height(),
            canvas._center_lat,
            canvas._center_lon,
            canvas._zoom,
            getattr(settings, "gribBarbDensity", "standard"),
            bool(settings.gribArrowOnGround),
        )
        signature = tuple(source_key(s) for s in sources)
        key = (view, signature)
        stamp = canvas.time_control.time.timestamp()
        self.latest = (key, stamp)
        if self.frame is not None:
            frame_key, atlas = self.frame
            if frame_key == key and atlas.staged.contains(stamp):
                return atlas, atlas.staged.weight(stamp)
        if self.failure == (key, stamp):
            return None
        with self._condition:
            # Rendering can request another time after the worker finished but
            # before its queued GUI completion slot runs. Adopt the published
            # mailbox here instead of scheduling a duplicate atlas build.
            if self._prepared is not None:
                prepared_key, atlas = self._prepared
                if prepared_key == key and atlas.staged.contains(stamp):
                    self.frame = self._prepared
                    return atlas, atlas.staged.weight(stamp)
            if self._active is not None and self._active[0] != key:
                self._active[3].set()
            self._pending = (key, stamp, freeze_wind_sources(sources), Event())
            if self._thread is None:
                self._thread = Thread(target=self._run, name="gpu-weather", daemon=True)
                self._thread.start()
            self._condition.notify()
        return None

    def invalidate(self):
        self.latest = self.frame = self.failure = None
        with self._condition:
            self._pending = None
            self._prepared = None
            self._release_requested = True
            if self._active is not None:
                self._active[3].set()
            self._condition.notify()

    @Slot(object, object, str)
    def _accept(self, key, atlas, error):
        if self.closed or self.latest is None or key != self.latest[0]:
            return
        if error:
            self.failure = self.latest
            logger.warning("Numerical GPU wind using CPU fallback: %s", error)
        elif atlas is not None and atlas.staged.contains(self.latest[1]):
            self.frame = key, atlas
            self.failure = None
        self.changed.emit()

    def _take_request(self):
        with self._condition:
            self._condition.wait_for(
                lambda: self.closed
                or self._pending is not None
                or self._release_requested
            )
            if self.closed:
                return True, None
            if self._release_requested:
                self._release_requested = False
                return False, None
            request, self._pending = self._pending, None
            self._active = request
            return False, request

    def _finish_request(self, request, atlas, error, statistics, fields=None):
        key, cancel = request[0], request[3]
        with self._condition:
            self._band_stats = statistics
            if fields is not None:
                self._field_stats = fields
            self._active = None
            if (
                atlas is not None
                and not error
                and not cancel.is_set()
                and not self.closed
            ):
                self._prepared = key, atlas
            # A time-only request already covered by this endpoint pair must
            # not trigger a second staging pass after every scrub.
            if atlas is not None and self._pending is not None:
                pending_key, pending_stamp, _, _ = self._pending
                if pending_key == key and atlas.staged.contains(pending_stamp):
                    self._pending = None
        if not cancel.is_set() and not self.closed:
            self.completed.emit(key, atlas, error)

    def _run(self):
        resources = _WeatherResources(
            self.BAND_BYTE_LIMIT, resident_fields=self.resident_fields
        )
        try:
            while True:
                closed, request = self._take_request()
                if closed:
                    return
                if request is None:
                    resources.release()
                    with self._condition:
                        self._band_stats = resources.band_cache.statistics()
                        self._field_stats = resources.fields.statistics()
                    continue
                atlas, error = None, ""
                try:
                    atlas = resources.prepare(*request)
                except Exception as exc:
                    error = str(exc)
                self._finish_request(
                    request,
                    atlas,
                    error,
                    resources.band_cache.statistics(),
                    resources.fields.statistics(),
                )
        finally:
            resources.close()
            with self._condition:
                self._band_stats = resources.band_cache.statistics()
                self._field_stats = resources.fields.statistics()

    def close(self):
        self.invalidate()
        with self._condition:
            self.closed = True
            self._condition.notify()
        # Do not join a GDAL read on the GUI thread. The worker closes its own
        # handles in finally, never publishing a result after cancellation.
