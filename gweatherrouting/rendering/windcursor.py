"""Asynchronous cursor weather queries with private, bounded GRIB handles."""

from collections import OrderedDict
from threading import Thread

from .windrenderer import DisplayGrib
from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import WeatherSampler


class CursorGrib(DisplayGrib):
    """Keep forecast-hour metadata and GDAL resources local to the cursor worker."""

    def __init__(self, source):
        super().__init__(source)
        self._hours = Grib._get_available_forecast_hours.__wrapped__(self)

    def _get_available_forecast_hours(self):  # type: ignore[override]
        return self._hours

    def close(self):
        self._arrays.clear()
        super().close()


class WindCursorSampler:
    def __init__(self, dispatch, display_grib=CursorGrib):
        self._dispatch = dispatch
        self._display_grib = display_grib
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
        self._start()

    def cancel(self):
        self._generation += 1
        self._pending = None

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
        if self._running or self._pending is None or self._closed:
            return False
        request, self._pending = self._pending, None
        self._running = True

        def worker():
            try:
                result = self._sample_weather(*request[1:5])
            except Exception:
                result = (None, None)
            if self._closed:
                self._clear_gribs()
            self._dispatch(self._ready, request, result)

        Thread(target=worker, name="wind-cursor", daemon=True).start()
        return False

    def _sample(self, sources, forecast, lat, lon):
        """Compatibility API returning wind only."""
        return self._sample_weather(sources, forecast, lat, lon)[0]

    def _sample_weather(self, sources, forecast, lat, lon):
        private = []
        try:
            for source in sources:
                try:
                    grib = self._gribs.get(source)
                    if grib is None:
                        grib = self._display_grib(source)
                        self._gribs[source] = grib
                    self._gribs.move_to_end(source)
                    private.append(grib)
                except Exception:
                    continue
            if private and all(isinstance(grib, Grib) for grib in private):
                sampler = WeatherSampler(private)
                wind = sampler.wind_at(forecast, lat, lon)
                gust = (
                    sampler.gust_at(forecast, lat, lon)
                    if any(
                        record.element == "GUST"
                        for index in sampler.indices
                        for record in index.records
                    )
                    else None
                )
                return wind, gust
            return self._sample_providers(private, forecast, lat, lon)
        finally:
            # Keep every enabled file alive until cross-file sampling completes.
            while len(self._gribs) > 2:
                _, evicted = self._gribs.popitem(last=False)
                self._release_grib(evicted)

    @staticmethod
    def _sample_providers(private, forecast, lat, lon):
        wind = gust = None
        for grib in private:
            if wind is None:
                try:
                    wind = grib.get_wind_at(forecast, lat, lon)
                except Exception:
                    pass
            if gust is None:
                try:
                    get_gust = getattr(grib, "get_gust_at", None)
                    if get_gust is not None:
                        gust = get_gust(forecast, lat, lon)
                except Exception:
                    pass
            if wind is not None and gust is not None:
                break
        return wind, gust

    def _ready(self, request, result):
        self._running = False
        if not self._closed and request[0] == self._generation:
            request[5](result)
        if self._closed:
            self._clear_gribs()
        elif self._pending is not None:
            self._start()
        return False
