"""Run existing routing engines with frozen inputs and injected UI/persistence hooks."""

from dataclasses import dataclass
from functools import lru_cache
from copy import deepcopy
import logging
from datetime import datetime, timedelta as TimeDelta
from threading import Event, RLock
import math
from typing import Optional

from weatherrouting import IsoPoint, RoutingNoWindError
from weatherrouting.routers.linearbestisorouter import LinearBestIsoRouter

_original_linear_route = LinearBestIsoRouter._route


def _linear_route_with_missing_destination_wind(
    self, lastlog, time, timedelta, start, end, iso_f
):
    try:
        result = _original_linear_route(
            self, lastlog, time, timedelta, start, end, iso_f
        )
        if result.path:
            candidate = result.path[-1]
            from gweatherrouting.core.marine.routing import GEOD

            _, _, metres = GEOD.inv(candidate.pos[1], candidate.pos[0], end[1], end[0])
            if metres <= 1:
                candidate.pos = tuple(end)
                candidate.next_wp_dist = 0
                result.position = tuple(end)
                return result
            terminal = _integrate_wind_terminal_leg(self, candidate, end)
            if terminal:
                result.path.extend(terminal)
                result.position = tuple(end)
                result.time = terminal[-1].time
            else:
                # Do not let a nearby dependency candidate consume the waypoint.
                result.path = []
                result.position = tuple(start)
        return result
    except AttributeError as exc:
        if "isochrones" in str(exc):
            raise RoutingNoWindError(
                "Load a wind forecast covering the destination position and time."
            ) from exc
        raise


def _wind_terminal_velocity(router, position, instant, end):
    """Return a polar-derived ground velocity on the direct wind-only course."""
    from gweatherrouting.core.marine.routing import KNOT, distance_bearing

    wind = router.grib.get_wind_at(instant, *position)
    if wind is None or len(wind) < 2 or not all(math.isfinite(v) for v in wind[:2]):
        raise RoutingNoWindError(
            "Load a wind forecast covering the final route segment and arrival time."
        )
    wind_from, wind_ms = wind[:2]
    tws_kn = wind_ms / KNOT
    _, course = distance_bearing(position, end)
    wind_from = math.radians(wind_from)
    twa = abs((course - wind_from + math.pi) % (2 * math.pi) - math.pi)
    speed_kn = router.polar.get_speed(tws_kn, twa)
    if not math.isfinite(speed_kn) or speed_kn <= 0:
        return None
    speed_ms = speed_kn * KNOT
    return (
        speed_ms * math.sin(course),
        speed_ms * math.cos(course),
        speed_kn,
        wind_from,
        tws_kn,
        math.degrees(course) % 360,
    )


def _valid_wind_terminal_segment(router, start, end):
    """Apply the configured point and full-segment land predicates."""
    points = getattr(router, "points_validity", None)
    if points and not all(points([end])):
        return False
    point = getattr(router, "point_validity", None)
    if point and not point(*end):
        return False
    lines = getattr(router, "lines_validity", None)
    if lines and not all(lines([[*start, *end]])):
        return False
    line = getattr(router, "line_validity", None)
    return not line or line(*start, *end)


def _integrate_wind_terminal_leg(router, candidate, end):
    """Integrate a bounded, wind-sampled final leg; return points or no connection.

    The endpoint shortcut in the dependency used a nearby point and its current
    polar speed as an implicit arrival. This small RK2 connector instead samples
    start, midpoint and endpoint wind, applies the configured polar/safety wrapper,
    and checks each emitted segment. It is limited to one hour, matching the
    dependency's existing max-reach acceptance horizon.
    """
    from gweatherrouting.core.marine.routing import (
        KNOT,
        distance_bearing,
        move,
    )

    if candidate.time is None:
        return None
    position = tuple(candidate.pos)
    instant = candidate.time
    elapsed = 0.0
    points = []
    budget = 3600.0
    for _ in range(12):
        distance, _ = distance_bearing(position, end)
        if distance <= 1.0:
            if distance == 0:
                return points
            final = _wind_terminal_velocity(router, position, instant, end)
            if final is None:
                return None
            duration = distance / (final[2] * KNOT)
            if elapsed + duration > budget:
                return None
            if not _valid_wind_terminal_segment(router, position, end):
                return None
            arrival = instant + TimeDelta(seconds=duration)
            wind = router.grib.get_wind_at(arrival, *end)
            if wind is None or not all(math.isfinite(v) for v in wind[:2]):
                raise RoutingNoWindError(
                    "Load a wind forecast covering the final route segment and arrival time."
                )
            end_speed = router.polar.get_speed(
                wind[1] / KNOT,
                abs(
                    (math.radians(final[5]) - math.radians(wind[0]) + math.pi)
                    % (2 * math.pi)
                    - math.pi
                ),
            )
            if not math.isfinite(end_speed) or end_speed <= 0:
                return None
            points.append(
                IsoPoint(
                    tuple(end),
                    time=arrival,
                    twd=math.radians(wind[0]),
                    tws=wind[1] / KNOT,
                    speed=final[2],
                    brg=final[5],
                    next_wp_dist=0,
                )
            )
            return points
        initial = _wind_terminal_velocity(router, position, instant, end)
        if initial is None:
            return None
        remaining = budget - elapsed
        if remaining <= 0:
            return None
        duration = min(300.0, remaining, distance / (initial[2] * KNOT))
        midpoint = move(position, initial[0], initial[1], duration / 2)
        if not _valid_wind_terminal_segment(router, position, midpoint):
            return None
        middle_time = instant + TimeDelta(seconds=duration / 2)
        middle = _wind_terminal_velocity(router, midpoint, middle_time, end)
        if middle is None:
            return None

        # If the direct course can cover the remainder in this substep, refine
        # the midpoint estimate and integrate exactly that final duration.
        final_duration = distance / (middle[2] * KNOT)
        if final_duration <= min(300.0, remaining):
            converged = False
            for _ in range(8):
                midpoint = move(
                    position,
                    initial[0],
                    initial[1],
                    final_duration / 2,
                )
                if not _valid_wind_terminal_segment(router, position, midpoint):
                    return None
                middle_time = instant + TimeDelta(seconds=final_duration / 2)
                middle = _wind_terminal_velocity(router, midpoint, middle_time, end)
                if middle is None:
                    return None
                refined = distance / (middle[2] * KNOT)
                if abs(refined - final_duration) < 0.005:
                    final_duration = refined
                    converged = True
                    break
                final_duration = refined
            if not converged:
                return None
            if final_duration <= min(300.0, remaining):
                if not _valid_wind_terminal_segment(router, position, end):
                    return None
                if not _valid_wind_terminal_segment(router, midpoint, end):
                    return None
                arrival = instant + TimeDelta(seconds=final_duration)
                end_wind = router.grib.get_wind_at(arrival, *end)
                if (
                    end_wind is None
                    or len(end_wind) < 2
                    or not all(math.isfinite(v) for v in end_wind[:2])
                ):
                    raise RoutingNoWindError(
                        "Load a wind forecast covering the final route segment and arrival time."
                    )
                end_tws = end_wind[1] / KNOT
                end_twa = abs(
                    (math.radians(middle[5]) - math.radians(end_wind[0]) + math.pi)
                    % (2 * math.pi)
                    - math.pi
                )
                end_speed = router.polar.get_speed(end_tws, end_twa)
                if not math.isfinite(end_speed) or end_speed <= 0:
                    return None
                points.append(
                    IsoPoint(
                        tuple(end),
                        time=arrival,
                        twd=math.radians(end_wind[0]),
                        tws=end_tws,
                        speed=middle[2],
                        brg=middle[5],
                        next_wp_dist=0,
                    )
                )
                return points

        destination = move(position, middle[0], middle[1], duration)
        travelled, course = distance_bearing(position, destination)
        if travelled <= 0 or travelled >= distance:
            return None
        if not _valid_wind_terminal_segment(router, position, destination):
            return None
        if not _valid_wind_terminal_segment(router, midpoint, destination):
            return None
        arrival = instant + TimeDelta(seconds=duration)
        end_wind = router.grib.get_wind_at(arrival, *destination)
        if (
            end_wind is None
            or len(end_wind) < 2
            or not all(math.isfinite(v) for v in end_wind[:2])
        ):
            raise RoutingNoWindError(
                "Load a wind forecast covering the final route segment and arrival time."
            )
        remaining_distance, _ = distance_bearing(destination, end)
        points.append(
            IsoPoint(
                destination,
                time=arrival,
                twd=middle[3],
                tws=middle[4],
                speed=middle[2],
                brg=math.degrees(course) % 360,
                next_wp_dist=remaining_distance / 1852,
            )
        )
        position, instant = destination, arrival
        elapsed += duration
    return None


LinearBestIsoRouter._route = _linear_route_with_missing_destination_wind

from gweatherrouting.core.geo.poi import POI
from gweatherrouting.core.geo.track import Track
from gweatherrouting.core.modifiedgrib import ModifiedGribManager
from gweatherrouting.core.routingoptions import configured_algorithm
from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribmanager import GribManager
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
    land_avoidance: Optional[bool] = None
    coastline_clearance_distance_m: float = 0.0

    def __post_init__(self):
        if (
            not math.isfinite(self.coastline_clearance_distance_m)
            or self.coastline_clearance_distance_m < 0
        ):
            raise ValueError("Coastline clearance distance must be nonnegative")
        if self.land_avoidance is None:
            object.__setattr__(self, "land_avoidance", self.coastline_checks)
        else:
            object.__setattr__(self, "coastline_checks", self.land_avoidance)


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
    current_selection=None,
):
    """Run the existing engines and current-coverage workflow with injected UI hooks."""
    wind = RoutingWind(sources, cancel)
    routing = None
    try:
        routing = create_requested_routing(
            core, request, providers, wind, current_selection
        )
        from gweatherrouting.core.routingcoverage import prepare_coverage

        prepare_coverage(
            routing,
            core.marine_manager if request.use_currents else None,
            request.auto_download,
            cancel,
            report,
            confirm,
        )
        result = run_requested_routing(
            core, routing, request, wind, progress, report, save_fields
        )
        return result
    finally:
        close_requested_routing(routing)
        wind.close()


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
    for provider in providers if request.coastline_checks else ():
        provider.coastline_clearance_distance_m = request.coastline_clearance_distance_m
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
    routing = core.create_routing(
        algorithm,
        request.polar_file,
        target,
        request.departure,
        request.start,
        providers if request.coastline_checks else (),
        not request.coastline_checks,
        **options,
    )
    environment = getattr(routing, "current_environment", None)
    if environment is not None:
        environment.factors = {
            "currents": bool(request.use_currents),
            "land_avoidance": bool(request.coastline_checks),
            "wave_correction": bool(request.wave_correct),
        }
    return routing


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
    environment = getattr(routing, "current_environment", None)
    land_avoidance = bool(request.coastline_checks)
    if environment is not None and getattr(environment, "factors", None):
        land_avoidance = bool(environment.factors.get("land_avoidance", land_avoidance))
    provenance["factors"] = {
        "currents": bool(request.use_currents),
        "land_avoidance": land_avoidance,
        "wave_correction": bool(request.wave_correct),
    }
    provenance["active_factors"] = [
        name
        for name, enabled in (
            ("currents", request.use_currents),
            ("land_avoidance", request.coastline_checks),
            ("wave_correction", request.wave_correct),
        )
        if enabled
    ]
    if (
        request.parameters
        or request.waypoints
        or request.use_currents
        or request.polar_efficiency != 1
        or request.wind_speed_pct
        or request.wind_dir_offset
        or request.coastline_clearance_distance_m
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
            coastline_clearance_distance_m=request.coastline_clearance_distance_m,
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
            factors=provenance["factors"],
            active_factors=provenance["active_factors"],
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
    for job in (environment.prefetch,):
        if job is not None:
            job.close()
    for source in environment.wind.gribs:
        if isinstance(source, Grib):
            source.grib.close()
    environment.wind.sampler = None
    environment.wind.gribs = ()
