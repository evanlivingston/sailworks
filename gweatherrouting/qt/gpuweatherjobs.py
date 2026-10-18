"""One private weather worker, one replaceable pending request per map."""

import logging
import os
from threading import Condition, Event, Thread

from PySide6.QtCore import QObject, Signal, Slot

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.core.routingjob import freeze_wind_sources
from gweatherrouting.rendering.weatherbarbs import BarbView, stage_barbs
from gweatherrouting.rendering.windrenderer import WindLandMask

logger = logging.getLogger("gweatherrouting")


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


class WeatherStager(QObject):
    changed = Signal()
    completed = Signal(object, object, str)

    def __init__(self, parent=None):
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
        self.completed.connect(self._accept)

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
            if self._active is not None:
                self._active[3].set()

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

    def _run(self):
        providers = []
        signature = None
        sampler = None
        land = WindLandMask()
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self.closed or self._pending is not None
                    )
                    if self.closed:
                        return
                    request, self._pending = self._pending, None
                    self._active = request
                key, stamp, sources, cancel = request
                atlas, error = None, ""
                try:
                    if signature != key[1]:
                        for provider in providers:
                            provider.close()
                        providers, sampler, signature = [], None, None
                        for source in sources:
                            if cancel.is_set():
                                break
                            expected = key[1][len(providers)][-1]
                            if source_key(source)[-1] != expected:
                                raise ValueError("Forecast file changed before staging")
                            providers.append(
                                Grib(
                                    source.path,
                                    source.name,
                                    source.centre,
                                    source.bounds,
                                    source.start_time,
                                    source.last_forecast,
                                    source.model_name,
                                )
                            )
                        if not cancel.is_set():
                            sampler = WeatherSampler(providers)
                            if len(sampler.indices) != len(providers):
                                raise ValueError(
                                    "A selected weather source is unavailable"
                                )
                            # Bound potential six-band-per-source float64 caches.
                            estimate = sum(
                                i.width * i.height * 8 * min(6, i.dataset.RasterCount)
                                for i in sampler.indices
                            )
                            if estimate > 128 * 1024 * 1024:
                                raise ValueError(
                                    "Selected forecasts exceed GPU worker band budget"
                                )
                            signature = key[1]
                    if not cancel.is_set() and sampler is not None:
                        atlas = stage_barbs(sampler, stamp, key[0], land, cancel.is_set)
                        if any(
                            source_key(source)[-1] != entry[-1]
                            for source, entry in zip(sources, key[1])
                        ):
                            raise ValueError("Forecast file changed during staging")
                except Exception as exc:
                    error = str(exc)
                with self._condition:
                    self._active = None
                    if (
                        atlas is not None
                        and not error
                        and not cancel.is_set()
                        and not self.closed
                    ):
                        self._prepared = key, atlas
                    # A time-only request already covered by this endpoint pair
                    # must not trigger a second staging pass after every scrub.
                    if atlas is not None and self._pending is not None:
                        pending_key, pending_stamp, _, _ = self._pending
                        if pending_key == key and atlas.staged.contains(pending_stamp):
                            self._pending = None
                if not cancel.is_set() and not self.closed:
                    self.completed.emit(key, atlas, error)
        finally:
            for provider in providers:
                provider.close()
            land.contains.cache_clear()
            land._prepared.clear()
            land._tiles.clear()
            land._dataset = None

    def close(self):
        self.invalidate()
        with self._condition:
            self.closed = True
            self._condition.notify()
        # Do not join a GDAL read on the GUI thread. The worker closes its own
        # handles in finally, never publishing a result after cancellation.
