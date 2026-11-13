"""Spawn-safe, lossless input snapshot for a native user calculation.

This is an input contract, not a solver: no route is accepted until the native
bridge and its independent replay honor every field in this snapshot.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sailworks.core.routingjob import RouteRequest
from sailworks.core.scenariopool import provider_descriptors
from sailworks.core.tranchestore import ResolvedForecast

WAVE_ROUTING_FIELDS = ("wave_height", "wave_period", "wave_direction")
WAVE_UNITS = {
    "wave_height": "m",
    "wave_period": "s",
    "wave_direction": "degrees (coming from true north)",
}


@dataclass(frozen=True)
class Rounding:
    """One pass-and-exit rule for a route mark."""

    side: str
    radius_nm: float | None = None
    exit_bearing_min_deg: float | None = None
    exit_bearing_max_deg: float | None = None
    name: str = ""

    def __post_init__(self):
        object.__setattr__(self, "name", str(self.name or "").strip())
        side = str(self.side).strip().lower()
        if side not in ("port", "starboard"):
            raise ValueError("Rounding side must be port or starboard")
        object.__setattr__(self, "side", side)
        # Radius is inert legacy metadata: it is parsed and round-tripped but
        # never constrains planning or acceptance, so any value is valid.
        radius = self.radius_nm
        if radius is None:
            object.__setattr__(self, "radius_nm", None)
        elif type(radius) not in (int, float) or not math.isfinite(radius):
            raise ValueError("Rounding radius must be a finite number or None")
        else:
            object.__setattr__(self, "radius_nm", float(radius))
        minimum = self.exit_bearing_min_deg
        maximum = self.exit_bearing_max_deg
        if (minimum is None) != (maximum is None):
            raise ValueError("Rounding exit bearing needs both a minimum and maximum")
        if minimum is not None:
            if (
                type(minimum) not in (int, float)
                or type(maximum) not in (int, float)
                or not math.isfinite(minimum)
                or not math.isfinite(maximum)
                or not 0 <= minimum <= maximum < 360
            ):
                raise ValueError(
                    "Rounding exit bearing range must satisfy 0 <= min <= max < 360"
                )
            object.__setattr__(self, "exit_bearing_min_deg", float(minimum))
            object.__setattr__(self, "exit_bearing_max_deg", float(maximum))

    def to_native(self) -> dict:
        # Radius is legacy point metadata, never a planning or acceptance
        # constraint: the native contract is side-of-track plus exit bearings.
        return dict(
            side=self.side,
            exit_bearing_min_deg=self.exit_bearing_min_deg,
            exit_bearing_max_deg=self.exit_bearing_max_deg,
            name=self.name,
        )


@dataclass(frozen=True)
class NativeRequest:
    version: int
    engine: str
    calculation_id: str
    name: str
    start: tuple[float, float]
    destination: tuple[float, float]
    departure_epoch_s: int
    waypoints: tuple[tuple[float, float], ...]
    forecast: ResolvedForecast
    current_field_ids: tuple[str, ...]
    current_priority: str
    marine_directory: str | None
    polar_rows: tuple
    polar_sha256: str
    polar_efficiency: float
    provider_descriptors: tuple
    coastline_checks: bool
    coastline_clearance_distance_m: float
    use_currents: bool
    auto_download: bool
    wind_speed_pct: float
    wind_dir_offset: float
    tws_max_kn: float | None
    min_sailing_speed_kn: float | None
    motor_speed_kn: float
    wave_correct: bool
    # The legacy algorithm selection is recorded, never executed. Its default
    # parameter values describe no user preference the native engine can honor;
    # a changed value is rejected instead of silently ignored.
    legacy_algorithm: str
    unchanged_legacy_parameters: tuple
    # Real wave fields are a separate published forecast entry from the wind
    # binding. They stay optional so an untouched calculation keeps today's
    # wind-only path; the wind-based proxy is still controlled by wave_correct.
    wave_forecast: ResolvedForecast | None = None
    # Aligned one-to-one with the chain marks after the start. Empty means no
    # rounding anywhere; None keeps exact-point behaviour for that mark.
    roundings: tuple[Rounding | None, ...] = ()


def changed_legacy_parameters(request: RouteRequest) -> tuple[str, dict]:
    """Return the selected legacy algorithm and parameters native cannot apply.

    The routing panel always sends the selected algorithm plus every ``PARAMS``
    widget value. The native engine runs its own search options, so unchanged
    defaults are recorded as *not executed* and any changed or unknown value is
    refused by name. Callers can use this before dispatch to show the same
    decision the frozen-job boundary enforces.
    """
    algorithm_name = getattr(request.algorithm, "__name__", "")
    if not algorithm_name:
        raise ValueError("Native routing requires a named legacy algorithm")
    declared = getattr(request.algorithm, "PARAMS", {}) or {}
    changed = {}
    for key, value in dict(request.parameters).items():
        parameter = declared.get(key)
        if parameter is None or value != parameter.default:
            changed[str(key)] = value
    return algorithm_name, changed


def unapplied_legacy_parameter_message(algorithm_name: str, changed: dict) -> str:
    return (
        "Native routing cannot apply changed legacy algorithm parameters "
        f"for {algorithm_name}: {', '.join(sorted(changed))}. "
        "Reset them or calculate with the legacy backend."
    )


def _point(value, label):
    if value is None or len(value) != 2:
        raise ValueError(f"Native {label} requires a (latitude, longitude) pair")
    latitude, longitude = value
    if (
        not all(type(part) in (int, float) and math.isfinite(part) for part in value)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        raise ValueError(f"Native {label} is outside valid coordinates")
    return (float(latitude), float(longitude))


def _distance_nm(left, right):
    """Great-circle distance for the native route-window estimate."""
    lat1, lon1 = (math.radians(value) for value in left)
    lat2, lon2 = (math.radians(value) for value in right)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * 3440.065 * math.asin(min(1.0, math.sqrt(h)))


def wave_field_times(entry):
    """Wave coverage recorded by the published entry, or an explicit gap."""
    field_times = dict(getattr(entry, "field_times", ()) or ())
    return {field: tuple(field_times.get(field) or ()) for field in WAVE_ROUTING_FIELDS}


def validate_wave_forecast(wave_forecast):
    """Require a deterministic, single-artifact entry with all wave fields."""
    if not isinstance(wave_forecast, ResolvedForecast):
        raise ValueError("Wave-aware routing requires a published wave forecast")
    entry = wave_forecast.entry
    if getattr(entry, "member", None) != "deterministic":
        raise ValueError("Wave routing requires a deterministic wave forecast")
    if len(getattr(entry, "artifacts", ())) != 1:
        raise ValueError("Wave routing requires exactly one wave artifact")
    times = wave_field_times(entry)
    missing = [field for field in WAVE_ROUTING_FIELDS if not times[field]]
    if missing:
        raise ValueError(
            "Selected wave forecast "
            f"{getattr(entry, 'model_id', 'unknown')} has no validated "
            + ", ".join(f"{field} ({WAVE_UNITS[field]})" for field in missing)
            + "; wave-aware routing needs height, period and direction"
        )
    return times


def _inside(region, point):
    south, west, north, east = (float(value) for value in region)
    latitude, longitude = point
    if not south <= latitude <= north:
        return False
    if west <= east:
        return west <= longitude <= east
    return longitude >= west or longitude <= east


def _check_wave_window(entry, start, destination, departure, wind_end=None):
    """Refuse a wave request that does not cover the estimated route window.

    This is the same endpoint/departure/window test the engine performs in
    preflight, repeated here so a native calculation fails before a worker is
    spawned. There is deliberately no fallback to the wind proxy.
    """
    region = getattr(entry, "region", None)
    times = wave_field_times(entry)["wave_height"]
    covered = bool(times)
    if region is not None:
        if not _inside(region, start) or not _inside(region, destination):
            covered = False
    if times:
        first, last = min(times), max(times)
        if departure < first or departure > last:
            covered = False
        nominal_seconds = _distance_nm(start, destination) / 3.0 * 3600.0
        available_seconds = (
            (wind_end - departure).total_seconds()
            if wind_end is not None and wind_end > departure
            else nominal_seconds
        )
        nominal_end = departure + timedelta(
            seconds=min(nominal_seconds, available_seconds)
        )
        if nominal_end > last:
            covered = False
    if covered:
        return
    model = getattr(entry, "model_id", "unknown")
    if times:
        coverage = f"{min(times).isoformat()} to {max(times).isoformat()}"
    else:
        coverage = "no wave times"
    raise ValueError(
        f"Wave data {model} does not cover the route window from "
        f"{departure.isoformat()} (coverage: {coverage}, area: {region}). "
        "No wave-aware route was calculated and the wind proxy was not "
        "substituted."
    )


def _start_and_waypoints(request):
    """Resolve the start the way the legacy track workflow does.

    Choosing "First waypoint" leaves ``start`` empty and lets the route begin at
    the track's first point. Native keeps that meaning: the first point becomes
    the start and the remaining points stay as the waypoint chain.
    """
    waypoints = tuple(request.waypoints or ())
    if request.start is not None:
        return request.start, waypoints
    if len(waypoints) >= 2:
        return waypoints[0], waypoints[1:]
    raise ValueError("Native routing requires an explicit start position")


def _rounding(value):
    if value is None:
        return None
    if isinstance(value, Rounding):
        return value
    if isinstance(value, dict):
        return Rounding(**value)
    raise ValueError("Rounding must be a Rounding record, mapping, or None")


def _resolve_roundings(item, start, waypoints):
    """Align panel roundings with the chain the bridge will build.

    The panel supplies one entry per track point (or one for a POI/map
    destination). The first track point becomes the start when Start is "First
    waypoint", so its entry cannot carry a rounding. Consecutive duplicate
    points are skipped by the bridge; a rounding on a skipped duplicate is
    rejected rather than silently dropped.
    """
    raw = tuple(getattr(item, "roundings", ()) or ())
    if not raw:
        return ()
    expected = len(item.waypoints) if item.waypoints else 1
    if len(raw) != expected:
        raise ValueError("Rounding specs must align one-to-one with the route points")
    normalized = tuple(_rounding(value) for value in raw)
    if not any(value is not None for value in normalized):
        return ()
    if item.start is None:
        if normalized[0] is not None:
            raise ValueError(
                "The first waypoint is the start; remove its rounding spec"
            )
        normalized = normalized[1:]

    if not waypoints:
        chain = [start, item.destination]
        marks = [(item.destination, normalized[0])]
    else:
        chain = [start]
        marks = []
        for point, rounding in zip(waypoints, normalized):
            if point == chain[-1]:
                if rounding is not None:
                    raise ValueError(
                        "Rounding is set on a duplicate waypoint that is skipped"
                    )
                continue
            chain.append(point)
            marks.append((point, rounding))
        if chain[-1] != item.destination:
            chain.append(item.destination)
            marks.append((item.destination, None))

    # Radius is legacy metadata only. It must not reject a compliant route or
    # shape the chain: side-of-track acceptance belongs to the shared winding
    # evaluator, so nearby marks and overlapping legacy radii are allowed here.
    return tuple(rounding for _, rounding in marks)


def freeze_native_requests(
    request: RouteRequest,
    scenarios: tuple[RouteRequest, ...],
    forecast: ResolvedForecast,
    providers,
    current_selection,
    marine_directory=None,
    *,
    engine: str,
    wave_forecast=None,
) -> tuple[NativeRequest, ...]:
    """Freeze every requested scenario before dispatch; never omit a provider.

    Call with a published analysis snapshot and selected current-field snapshot.
    Worker-side code must revalidate artifact hashes and field IDs before use.
    No live polar, chart, field, GDAL or Qt handle crosses the process boundary.
    """
    if not isinstance(forecast, ResolvedForecast):
        raise ValueError("Native routing requires a published forecast snapshot")
    if engine not in {"main", "standard", "original", "professional"}:
        raise ValueError("Choose an explicit supported native routing engine")
    if len(forecast.artifact_paths) != 1 or len(forecast.entry.artifacts) != 1:
        raise ValueError("Native routing requires one frozen forecast artifact")
    if request.wave_entry_id:
        if wave_forecast is None:
            raise ValueError(
                "Selected wave data is not available; choose a published "
                "wave forecast or set Wave data to None"
            )
        if wave_forecast.tranche.tranche_id != forecast.tranche.tranche_id:
            raise ValueError("Selected wave forecast is not in the active tranche")
        if wave_forecast.entry.entry_id != request.wave_entry_id:
            raise ValueError(
                "Selected wave forecast changed while freezing the request"
            )
        validate_wave_forecast(wave_forecast)
    elif wave_forecast is not None:
        raise ValueError("Wave forecast supplied without a selected wave entry")
    wind_times = tuple(
        dict(getattr(forecast.entry, "field_times", ()) or ()).get("wind") or ()
    )
    wind_end = max(wind_times) if wind_times else None
    descriptors = provider_descriptors(providers) if request.coastline_checks else ()
    if descriptors is None:
        raise ValueError("Native shoreline provider cannot be rebuilt in a worker")
    if any(descriptor.kind != "gshhs" for descriptor in descriptors):
        raise ValueError("Native shoreline provider type is not supported")
    if request.coastline_checks and not descriptors:
        raise ValueError("Native coastline checks require installed shoreline data")
    if request.use_currents and (current_selection is None or not marine_directory):
        raise ValueError(
            "Native currents require selected local fields and their store"
        )
    if request.use_currents and not current_selection.fields:
        raise ValueError("Native currents require at least one selected local field")
    if request.use_currents and current_selection.priority != request.current_priority:
        raise ValueError("Native current priority changed after selection")
    if not Path(request.polar_file).is_file():
        raise ValueError("Native routing requires a readable local polar")

    import weatherrouting

    polar_data = Path(request.polar_file).read_bytes()
    polar = weatherrouting.Polar(request.polar_file)
    # The installed weatherrouting.Polar parser retains the .pol wind and boat
    # speeds in knots. The generic adapter for m/s-backed polar objects must
    # not be applied here: that would multiply both axes by 3600/1852 twice.
    rows = tuple(
        (
            float(tws),
            tuple(
                (math.degrees(float(angle)), float(polar.speed_table[index][column]))
                for index, angle in enumerate(polar.twa)
            ),
        )
        for column, tws in enumerate(polar.tws)
    )
    if Path(request.polar_file).read_bytes() != polar_data:
        raise ValueError("Native polar changed while freezing its rows")
    items = tuple(scenarios) or (request,)
    frozen = []
    for item in items:
        if not isinstance(item, RouteRequest):
            raise TypeError("Native scenarios must be RouteRequest instances")
        if (
            item.polar_file != request.polar_file
            or item.use_currents != request.use_currents
            or item.current_priority != request.current_priority
            or item.coastline_checks != request.coastline_checks
            or getattr(item, "wave_entry_id", None) != request.wave_entry_id
        ):
            raise ValueError("Native scenario changed frozen source selection")
        start, waypoints = _start_and_waypoints(item)
        departure = item.departure
        if departure.tzinfo is None or departure.utcoffset() is None:
            # The panel's departure is a naive local wall time, and legacy
            # routes treat it that way. Attach the local offset instead of
            # refusing a selection legacy accepts.
            departure = departure.astimezone()
        algorithm_name, changed = changed_legacy_parameters(item)
        if changed:
            raise ValueError(
                unapplied_legacy_parameter_message(algorithm_name, changed)
            )
        destination = _point(item.destination, "destination")
        start_point = _point(start, "start")
        resolved_waypoints = tuple(_point(point, "waypoint") for point in waypoints)
        resolved_roundings = _resolve_roundings(item, start_point, resolved_waypoints)
        if engine not in {"main", "professional"} and any(
            rounding is not None for rounding in resolved_roundings
        ):
            raise ValueError(
                "Rounding constraints require the main or professional native engine"
            )
        if wave_forecast is not None:
            _check_wave_window(
                wave_forecast.entry, start_point, destination, departure, wind_end
            )
        unchanged = tuple(
            (str(key), value) for key, value in dict(item.parameters).items()
        )
        frozen.append(
            NativeRequest(
                version=1,
                engine=engine,
                calculation_id=str(uuid4()),
                name=item.name,
                start=start_point,
                destination=destination,
                departure_epoch_s=int(departure.astimezone(timezone.utc).timestamp()),
                waypoints=resolved_waypoints,
                forecast=forecast,
                current_field_ids=(
                    tuple(field.id for field in current_selection.fields)
                    if item.use_currents
                    else ()
                ),
                current_priority=item.current_priority,
                marine_directory=str(marine_directory) if item.use_currents else None,
                polar_rows=rows,
                polar_sha256=hashlib.sha256(polar_data).hexdigest(),
                polar_efficiency=item.polar_efficiency,
                provider_descriptors=descriptors,
                coastline_checks=item.coastline_checks,
                coastline_clearance_distance_m=item.coastline_clearance_distance_m,
                use_currents=item.use_currents,
                auto_download=item.auto_download,
                wind_speed_pct=item.wind_speed_pct,
                wind_dir_offset=item.wind_dir_offset,
                tws_max_kn=item.tws_max_kn,
                min_sailing_speed_kn=item.min_sailing_speed_kn,
                motor_speed_kn=item.motor_speed_kn,
                wave_correct=item.wave_correct,
                legacy_algorithm=algorithm_name,
                unchanged_legacy_parameters=unchanged,
                wave_forecast=wave_forecast,
                roundings=resolved_roundings,
            )
        )
    return tuple(frozen)
