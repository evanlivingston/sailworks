"""Spawn-safe, lossless input snapshot for a native user calculation.

This is an input contract, not a solver: no route is accepted until the native
bridge and its independent replay honor every field in this snapshot.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from sailworks.core.routingjob import RouteRequest
from sailworks.core.scenariopool import provider_descriptors
from sailworks.core.tranchestore import ResolvedForecast


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


def freeze_native_requests(
    request: RouteRequest,
    scenarios: tuple[RouteRequest, ...],
    forecast: ResolvedForecast,
    providers,
    current_selection,
    marine_directory=None,
    *,
    engine: str,
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
        unchanged = tuple(
            (str(key), value) for key, value in dict(item.parameters).items()
        )
        frozen.append(
            NativeRequest(
                version=1,
                engine=engine,
                calculation_id=str(uuid4()),
                name=item.name,
                start=_point(start, "start"),
                destination=_point(item.destination, "destination"),
                departure_epoch_s=int(departure.astimezone(timezone.utc).timestamp()),
                waypoints=tuple(_point(point, "waypoint") for point in waypoints),
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
            )
        )
    return tuple(frozen)
