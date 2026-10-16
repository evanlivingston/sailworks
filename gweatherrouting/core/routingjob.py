"""Run existing routing engines with frozen inputs and injected UI/persistence hooks."""

from dataclasses import dataclass
from functools import lru_cache
from copy import deepcopy
import logging
from datetime import datetime
from threading import Event, RLock
from typing import Optional
import math

from weatherrouting import RoutingNoWindError

from gweatherrouting.core.geo.poi import POI
from gweatherrouting.core.geo.track import Track
from gweatherrouting.core.modifiedgrib import ModifiedGribManager
from gweatherrouting.core.routingoptions import configured_algorithm
from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribmanager import GribManager
from .forecastselection import ForecastSelection, forecast_source
from .modeldownloads import ModelForecastRecovery
from .marine.fields import Area
from .storage import GRIB_DIR
from gweatherrouting.core.marine.routing import routing_completion, routing_utc


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
    model_name: str = "Unknown"


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
                g.model_name,
            )
            if isinstance(g, Grib)
            else g
        )
        for g in gribs
    )


class WorkerGrib(Grib):
    """Shared interpolation with bounded, run-local caches and private handles."""

    def __init__(self, source):
        super().__init__(
            source.path,
            source.name,
            source.centre,
            source.bounds,
            source.start_time,
            source.last_forecast,
            source.model_name,
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
        super().close()


class RoutingWind:
    def __init__(self, sources, cancel):
        self.cancel = cancel
        self._query_lock = RLock()
        self.manager = GribManager.__new__(GribManager)
        self.manager.gribs = []
        self.recover_missing = None
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
        # Engine concurrency may issue simultaneous queries on these private GDAL files.
        with self._query_lock:
            self.check_cancelled()
            result = self.manager.get_wind_at(instant, lat, lon)
            if result is None and self.recover_missing is not None:
                self.recover_missing(instant, lat, lon)
                result = self.manager.get_wind_at(instant, lat, lon)
        self.check_cancelled()
        return result

    def close(self):
        for source in self.manager.gribs:
            if isinstance(source, Grib):
                source.close()
        self.manager.gribs.clear()

    def check_cancelled(self):
        if self.cancel.is_set():
            raise RoutingCancelledError()


@dataclass(frozen=True)
class RouteRequest:
    name: str
    start: Optional[tuple[float, float]]
    destination: tuple[float, float]
    departure: datetime
    polar_file: str
    algorithm: object
    coastline_checks: bool = True
    parameters: tuple = ()
    waypoints: tuple = ()
    polar_efficiency: float = 1.0
    use_currents: bool = False
    auto_download: bool = False
    current_priority: str = "finest"
    wind_speed_pct: float = 0
    wind_dir_offset: float = 0
    tws_max_kn: Optional[float] = None
    min_sailing_speed_kn: Optional[float] = None
    motor_speed_kn: float = 5.0
    wave_correct: bool = False
    forecast_selection: Optional[ForecastSelection] = None


class RoutingPreview(tuple):
    """Path-compatible progress payload with frozen isochrone coordinates."""

    isochrones: tuple

    def __new__(cls, path=(), isochrones=()):
        result = super().__new__(cls, path)
        result.isochrones = isochrones
        return result


class IsochroneSnapshot:
    """Reuse frozen completed frontiers; never queue live engine objects to Qt."""

    def __init__(self):
        self.frontiers: dict[int, tuple] = {}

    def capture(self, frontiers):
        current = {}
        for frontier in frontiers:
            cached = self.frontiers.get(id(frontier))
            if cached is None or len(cached[1]) != len(frontier):
                cached = (frontier, tuple(tuple(point.pos) for point in frontier))
            current[id(frontier)] = cached
        self.frontiers = current
        return tuple(current[id(frontier)][1] for frontier in frontiers)


@dataclass(frozen=True)
class CalculatedRoute:
    points: tuple
    isochrones: tuple
    provenance: dict


def calculate_route(
    core,
    request,
    sources,
    providers,
    cancel: Event,
    progress,
    *,
    report=lambda message: None,
    confirm=lambda report: False,
    save_fields=None,
    save_gribs=None,
    current_selection=None,
):
    """Run the existing engines and current-coverage workflow with injected UI hooks."""
    if request.forecast_selection is not None:
        sources = tuple(
            g
            for g in sources
            if forecast_source(g) in request.forecast_selection.models
        )
    wind = RoutingWind(sources, cancel)
    routing = None
    try:
        routing = create_requested_routing(
            core, request, providers, wind, current_selection
        )
        from gweatherrouting.core.routingcoverage import prepare_coverage

        recovery = prepare_selected_wind(
            request, routing, wind, cancel, report, save_gribs
        )

        prepare_coverage(
            routing,
            core.marine_manager if request.use_currents else None,
            request.auto_download,
            cancel,
            report,
            confirm,
            save_gribs,
            forecast_selection=request.forecast_selection,
        )
        result = run_requested_routing(
            core, routing, request, wind, progress, report, save_fields
        )
        if request.forecast_selection is not None:
            result.provenance["weather_models"] = list(
                request.forecast_selection.models
            )
            if recovery.issues:
                result.provenance["weather_warnings"] = sorted(set(recovery.issues))
        return result
    finally:
        close_requested_routing(routing)
        wind.close()


def prepare_selected_wind(request, routing, wind, cancel, report, save_gribs):
    if request.forecast_selection is None:
        return None
    recovery = ModelForecastRecovery(
        request.forecast_selection, GRIB_DIR, cancel, report
    )

    def install(added):
        known = {g.path for g in wind.manager.gribs if hasattr(g, "path")}
        added = [g for g in added if g.path not in known]
        wind.manager.gribs.extend(added)
        if routing.current_environment is not None:
            routing.current_environment.wind.add_gribs(added)
        if added and save_gribs is not None:
            save_gribs(added)

    install(recovery.cached(wind.manager.gribs))

    def recover(instant, lat, lon):
        points = [(lat, lon), *routing.track[routing.wp :]]
        area = Area.from_points(points, padding=1)
        install(recovery.download(wind.manager.gribs, points, instant, area))

    # Probe all checked models before starting, then fill actual search gaps.
    recover(request.departure, *routing.position)
    wind.recover_missing = recover
    if wind.manager.get_wind_at(request.departure, *routing.position) is None:
        details = "; ".join(sorted(set(recovery.issues)))
        raise RoutingNoWindError(
            "Selected weather models have no wind data at the departure position and time. "
            + details
        )
    return recovery


def create_requested_routing(core, request, providers, wind, current_selection):
    from gweatherrouting.core.safetypolicy import SafetyPolicy
    from gweatherrouting.core.marine.fields import normalize_priority

    wind.check_cancelled()
    priority = normalize_priority(getattr(request, "current_priority", "finest"))
    safety_policy = SafetyPolicy(
        tws_max_kn=request.tws_max_kn,
        min_sailing_speed_kn=request.min_sailing_speed_kn,
        motor_speed_kn=request.motor_speed_kn,
        wave_correct=request.wave_correct,
    )
    algorithm = (
        configured_algorithm(request.algorithm, request.parameters)
        if request.parameters
        else request.algorithm
    )
    target = (
        Track("destination", list(request.waypoints))
        if request.waypoints
        else POI("destination", request.destination)
    )
    override = wind
    if request.wind_speed_pct or request.wind_dir_offset:
        override = ModifiedGribManager(
            wind, 1 + request.wind_speed_pct / 100, request.wind_dir_offset
        )
    options = dict(
        grib_override=override,
        polar_efficiency=request.polar_efficiency,
        safety_policy=safety_policy,
    )
    if request.use_currents:
        options.update(
            use_currents=True,
            wind_sources=wind.manager,
            current_selection=current_selection,
            current_priority=priority,
        )
        # FrozenRoutingWind applies scenario modifiers to its own newest-run selection.
        options["grib_override"] = (
            override if isinstance(override, ModifiedGribManager) else None
        )
    return core.create_routing(
        algorithm,
        request.polar_file,
        target,
        request.departure,
        request.start,
        providers,
        not request.coastline_checks,
        **options,
    )


def run_requested_routing(core, routing, request, wind, progress, report, save_fields):
    from gweatherrouting.core.marine.routing import step_with_coverage

    wind.check_cancelled()
    if (
        not request.use_currents
        and routing.grib.get_wind_at(routing.time, *routing.position) is None
    ):
        raise RoutingNoWindError(
            "Load a wind forecast covering the departure and start position."
        )
    if request.use_currents:
        routing.current_environment.progress = (
            lambda distance, checked, total: progress(
                routing_completion(routing, distance), None
            )
        )
    progress(0, RoutingPreview())
    snapshot = IsochroneSnapshot()
    result = None
    while not routing.end:
        wind.check_cancelled()
        result = (
            step_with_coverage(
                routing, core.marine_manager, request.auto_download, report, save_fields
            )
            if request.use_currents
            else routing.step()
        )
        path = result.path or routing.get_current_best_path()
        progress(
            routing_completion(routing),
            RoutingPreview(
                tuple(tuple(p.pos) for p in path), snapshot.capture(result.isochrones)
            ),
        )
    wind.check_cancelled()
    if result is None or not result.path:
        raise ValueError("The routing engine returned no route.")
    provenance = routing_provenance(routing, result.path)
    if (
        request.parameters
        or request.waypoints
        or request.use_currents
        or request.polar_efficiency != 1
        or request.wind_speed_pct
        or request.wind_dir_offset
        or request.tws_max_kn is not None
        or request.min_sailing_speed_kn is not None
        or request.wave_correct
    ):
        provenance["options"] = dict(
            algorithm=request.algorithm.__name__,
            parameters=dict(request.parameters),
            polar_efficiency=request.polar_efficiency,
            departure=request.departure.isoformat(),
            wind_speed_pct=request.wind_speed_pct,
            wind_dir_offset=request.wind_dir_offset,
            coastline_checks=request.coastline_checks,
            waypoints=request.waypoints,
            tws_max_kn=request.tws_max_kn,
            min_sailing_speed_kn=request.min_sailing_speed_kn,
            motor_speed_kn=request.motor_speed_kn,
            wave_correct=request.wave_correct,
            wave_note=(
                "TWS-proxy reduction pending wave GRIBs"
                if request.wave_correct
                else None
            ),
        )
    return CalculatedRoute(
        serialize_routing_points(routing, result.path),
        tuple(tuple(group) for group in result.isochrones),
        provenance,
    )


def serialize_routing_points(routing, path):
    currents = routing.current_environment is not None
    return tuple(
        (
            p.pos[0],
            p.pos[1],
            (
                routing_utc(p.time).isoformat()
                if currents
                else p.time.strftime("%m/%d/%Y, %H:%M:%S")
            ),
            math.degrees(p.twd) % 360 if currents else p.twd,
            p.tws,
            p.speed,
            p.brg,
        )
        for p in path
    )


def routing_provenance(routing, path):
    environment = getattr(routing, "current_environment", None)
    if environment is None:
        return dict(schema_version=1, routing="wind-only", currents="disabled")
    value = environment.provenance(path)
    value["wind"] = environment.wind.provenance()
    value["wind"]["actual_search_sources"] = value["wind"]["actual_sources"]
    value["wind"]["actual_sources"] = sorted(
        {s["wind_source"] for s in value["samples"] if s.get("wind_source")}
    )
    return value


def close_requested_routing(routing):
    environment = getattr(routing, "current_environment", None)
    if environment is None:
        return
    environment.progress = None
    for job in (environment.prefetch, environment.wind_recovery):
        if job is not None:
            job.close()
    for source in environment.wind.gribs:
        if isinstance(source, Grib):
            source.grib.close()
    environment.wind.sampler = None
    environment.wind.gribs = ()
