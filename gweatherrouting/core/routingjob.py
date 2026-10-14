"""Run the existing wind-only routing engine without UI or persistence access."""

from dataclasses import dataclass
from functools import lru_cache
from copy import deepcopy
import logging
from datetime import datetime
from threading import Event

from weatherrouting import RoutingNoWindError

from gweatherrouting.core.geo.poi import POI
from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribmanager import GribManager
from gweatherrouting.core.marine.routing import routing_completion


class RoutingCancelledError(Exception):
    pass


@dataclass(frozen=True)
class WindFile:
    path: str
    name: str
    centre: str
    bounds: object
    start_time: datetime
    last_forecast: int


def freeze_wind_sources(gribs):
    """Capture selected files before the worker starts; open private GDAL handles."""
    return tuple(
        (
            WindFile(
                g.path,
                g.name,
                g.centre,
                deepcopy(g.bounds),
                g.start_time,
                g.last_forecast,
            )
            if isinstance(g, Grib)
            else g
        )
        for g in gribs
    )


class WorkerGrib(Grib):
    """Keep legacy sampling with bounded, run-local caches and private handles."""

    def __init__(self, source):
        super().__init__(
            source.path,
            source.name,
            source.centre,
            source.bounds,
            source.start_time,
            source.last_forecast,
        )
        self._hours = Grib._get_available_forecast_hours.__wrapped__(self)
        self._index_data = lru_cache(maxsize=2)(
            lambda hour: Grib.get_rindex_data.__wrapped__(self, hour)
        )

    def _get_available_forecast_hours(self):  # type: ignore[override]
        return self._hours

    def get_rindex_data(self, hour):  # type: ignore[override]
        return self._index_data(hour)

    def close(self):
        self._index_data.cache_clear()
        self.dataset = None


class RoutingWind:
    def __init__(self, sources, cancel):
        self.cancel = cancel
        self.manager = GribManager.__new__(GribManager)
        self.manager.gribs = []
        for source in sources:
            try:
                private = WorkerGrib(source) if isinstance(source, WindFile) else source
            except Exception as exc:
                logging.getLogger("gweatherrouting").warning(
                    "Unable to open routing forecast %s: %s", source.path, exc
                )
                continue
            self.manager.gribs.append(private)

    def get_wind_at(self, instant, lat, lon):
        # Check outside GribManager's missing-source exception handler so a
        # cancellation interrupts the search rather than becoming absent wind.
        self.check_cancelled()
        result = self.manager.get_wind_at(instant, lat, lon)
        self.check_cancelled()
        return result

    def close(self):
        for source in self.manager.gribs:
            if isinstance(source, WorkerGrib):
                source.close()
        self.manager.gribs.clear()

    def check_cancelled(self):
        if self.cancel.is_set():
            raise RoutingCancelledError()


@dataclass(frozen=True)
class RouteRequest:
    name: str
    start: tuple[float, float]
    destination: tuple[float, float]
    departure: datetime
    polar_file: str
    algorithm: object
    coastline_checks: bool = True


@dataclass(frozen=True)
class CalculatedRoute:
    points: tuple
    isochrones: tuple
    provenance: dict


def calculate_route(core, request, sources, providers, cancel: Event, progress):
    """Keep GTK's wind-only algorithm, sampling, parameters and result format."""
    wind = RoutingWind(sources, cancel)
    try:
        return _calculate_with_wind(core, request, providers, wind, progress)
    finally:
        wind.close()


def _calculate_with_wind(core, request, providers, wind, progress):
    wind.check_cancelled()
    routing = core.create_routing(
        request.algorithm,
        request.polar_file,
        POI("destination", request.destination),
        request.departure,
        request.start,
        providers,
        not request.coastline_checks,
        grib_override=wind,
    )
    if wind.get_wind_at(request.departure, *request.start) is None:
        raise RoutingNoWindError(
            "Load a wind forecast covering the departure and start position."
        )
    progress(0, ())
    result = None
    while not routing.end:
        wind.check_cancelled()
        result = routing.step()
        path = result.path or routing.get_current_best_path()
        progress(routing_completion(routing), tuple(tuple(p.pos) for p in path))
    wind.check_cancelled()
    if result is None or not result.path:
        raise ValueError("The routing engine returned no route.")
    points = tuple(
        (
            p.pos[0],
            p.pos[1],
            p.time.strftime("%m/%d/%Y, %H:%M:%S"),
            p.twd,
            p.tws,
            p.speed,
            p.brg,
        )
        for p in result.path
    )
    return CalculatedRoute(
        points,
        tuple(tuple(group) for group in result.isochrones),
        dict(schema_version=1, routing="wind-only", currents="disabled"),
    )
