"""Debounced cursor weather queries with private, bounded GRIB handles."""

from collections import OrderedDict
from threading import Thread

from .windrenderer import DisplayGrib
from gweatherrouting.core.grib import Grib


class CursorGrib(DisplayGrib):
    """Keep forecast-hour metadata and GDAL resources local to the cursor worker."""

    def __init__(self, source):
        super().__init__(source)
        self._hours = Grib._get_available_forecast_hours.__wrapped__(self)

    def _get_available_forecast_hours(self):  # type: ignore[override]
        return self._hours

    def close(self):
        self._arrays.clear()
        self.dataset = None


class WindCursorSampler:
    def __init__(self, call_later, cancel_later, dispatch, display_grib=CursorGrib):
        self._call_later = call_later
        self._cancel_later = cancel_later
        self._dispatch = dispatch
        self._display_grib = display_grib
        self._timer = 0
        self._generation = 0
        self._pending = None
        self._running = False
        self._closed = False
        self._gribs = OrderedDict()

    def request(self, sources, forecast, lat, lon, callback):
        self.cancel()
        if self._closed or not sources:
            return
        self._pending = (self._generation, tuple(sources), forecast, lat, lon, callback)
        self._timer = self._call_later(120, self._start)

    def cancel(self):
        self._generation += 1
        self._pending = None
        if self._timer:
            self._cancel_later(self._timer)
            self._timer = 0

    def close(self, *args):
        self.cancel()
        self._closed = True
        if not self._running:
            self._clear_gribs()

    def _clear_gribs(self):
        for grib in self._gribs.values():
            self._release_grib(grib)
        self._gribs.clear()

    @staticmethod
    def _release_grib(grib):
        close = getattr(grib, "close", None)
        if close is not None:
            close()

    def _start(self):
        self._timer = 0
        if self._running or self._pending is None or self._closed:
            return False
        request, self._pending = self._pending, None
        self._running = True

        def worker():
            try:
                result = self._sample(*request[1:5])
            except Exception:
                result = None
            if self._closed:
                self._clear_gribs()
            self._dispatch(self._ready, request, result)

        Thread(target=worker, name="wind-cursor", daemon=True).start()
        return False

    def _sample(self, sources, forecast, lat, lon):
        for source in sources:
            try:
                grib = self._gribs.get(source)
                if grib is None:
                    grib = self._display_grib(source)
                    self._gribs[source] = grib
                    if len(self._gribs) > 2:
                        _, evicted = self._gribs.popitem(last=False)
                        self._release_grib(evicted)
                self._gribs.move_to_end(source)
                wind = grib.get_wind_at(forecast, lat, lon)
                if wind is not None:
                    return wind
            except Exception:
                # Match the manager: try other enabled sources for this point.
                continue
        return None

    def _ready(self, request, result):
        self._running = False
        if not self._closed and request[0] == self._generation:
            request[5](result)
        if self._closed:
            self._clear_gribs()
        elif self._pending is not None and not self._timer:
            self._start()
        return False
