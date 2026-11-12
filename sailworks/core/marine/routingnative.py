"""Convert native routing bridge payloads into schema-v1 result records.

The native bridge returns plain dictionaries. This module is a pure adapter: it
never runs a calculation, never invents geometry and never mutates its input.
Missing or unavailable values stay unknown; contour order and open/closed state
are passed through exactly as the engine emitted them.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from .routingrecord import (
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Observation,
    Outcome,
    Point,
    Provenance,
    SchemaError,
    Trace,
    Validation,
)
from .routingresult import RoutingResult, canonical_utc

# Statuses that cannot carry a reliable validation verdict. Their outcome stays
# unknown rather than being reported as a failure of the searched route.
UNVALIDATED_STATUSES = frozenset(
    {
        "cancelled",
        "incomplete",
        "no_feasible_route",
        "resource_limit_reached",
        "internal_error",
    }
)


def polar_rows_from_polar(polar: Any) -> list[dict]:
    """Convert a Sailworks/weatherrouting polar into bridge polar rows.

    The library stores true wind angles in radians and wind/boat speeds in
    metres per second. The bridge works in degrees and knots, so both are
    converted here once; no interpolation or efficiency change is applied.
    """

    factor = 3600.0 / 1852.0
    twa_degrees = [math.degrees(float(angle)) for angle in polar.twa]
    rows = []
    for wind_index, tws_ms in enumerate(polar.tws):
        points = [
            [twa_degrees[angle_index], float(row[wind_index]) * factor]
            for angle_index, row in enumerate(polar.speed_table)
        ]
        rows.append({"true_wind_speed_knots": float(tws_ms) * factor, "points": points})
    return rows


def current_callable_from_selection(selection: Any, *, include_provenance=False):
    """Adapt a Sailworks current selection into the native current callback.

    ``selection.sample(lat, lon, utc)`` returns the established record with a
    status and toward-east/toward-north components in metres per second. Any
    status other than ``available`` is reported as unavailable so the engine
    keeps its missing-current policy instead of being given calm water.
    """

    def current(latitude: float, longitude: float, epoch_s: int):
        instant = datetime.fromtimestamp(epoch_s, timezone.utc)
        sample = selection.sample(latitude, longitude, instant)
        if getattr(sample, "status", None) != "available":
            return (0.0, 0.0, False)
        east, north = sample.values
        if include_provenance:
            if not getattr(sample, "dataset_id", None):
                raise ValueError("Selected current sample has no dataset identity")
            return (float(east), float(north), True, sample.dataset_id)
        return (float(east), float(north), True)

    return current


def wind_callable_from_sampler(sampler: Any):
    """Adapt ``WeatherSampler.wind_at`` into the native bridge wind callback.

    The bridge expects ``(from_degrees, speed_ms, available)`` — exactly the
    established Sailworks sampling representation, so no unit conversion happens
    on this side. A missing sample is reported as unavailable rather than calm.
    """

    def wind(latitude: float, longitude: float, epoch_s: int):
        instant = datetime.fromtimestamp(epoch_s, timezone.utc)
        value = sampler.wind_at(instant, latitude, longitude)
        if value is None:
            return (0.0, 0.0, False)
        from_degrees, speed_ms = value
        return (float(from_degrees), float(speed_ms), True)

    return wind


def to_routing_result(
    native_run: Mapping[str, Any],
    *,
    calculation_id: str,
    generation: int,
    sequence: int,
    created_epoch_s: int,
    identity: Mapping[str, Any],
) -> RoutingResult:
    """Build a schema-v1 result from one native bridge run payload."""

    run = _mapping(native_run, "native run")
    engine_identity = _engine_identity(identity)
    status = _text(run.get("status"), "native run status")
    accepted = _bool(run.get("accepted"), "native run accepted")
    legs = tuple(
        _leg(item, f"legs[{index}]")
        for index, item in enumerate(_sequence(run.get("legs", ()), "legs"))
    )
    layers = tuple(
        _layer(item, f"layers[{index}]")
        for index, item in enumerate(_sequence(run.get("layers", ()), "layers"))
    )
    traces = tuple(
        trace
        for layer_index, layer in enumerate(_sequence(run.get("layers", ()), "layers"))
        for trace in _layer_traces(layer, f"layers[{layer_index}]")
    )
    validation = _mapping(run.get("validation"), "native validation")
    passed = _bool(validation.get("passed"), "native validation passed")
    state = _validation_state(status, passed)
    independent = state == "passed" and run.get("validation_pass_flags_equal") is True
    outcome = Outcome(
        accepted=accepted,
        delivered=accepted and bool(legs),
        validation=Validation(state, independent),
        diagnostics=_diagnostics(run),
    )
    return RoutingResult(
        calculation_id=calculation_id,
        generation=generation,
        sequence=sequence,
        status=status,
        outcome=outcome,
        engine_identity=engine_identity,
        provenance=Provenance(
            canonical_utc(created_epoch_s),
            "recorded" if _environment_recorded(legs) else "absent",
        ),
        legs=legs,
        layers=layers,
        capabilities=_capabilities(run, traces, identity),
        traces=traces,
    )


def _validation_state(status: str, passed: bool) -> str:
    if passed:
        return "passed"
    if status in UNVALIDATED_STATUSES:
        return "unknown"
    return "failed"


def _environment_recorded(legs: Sequence[Leg]) -> bool:
    return any(
        observation.availability == "reported"
        for leg in legs
        for observation in (leg.environment or {}).values()
    )


def _diagnostics(run: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for key in (
        "warnings",
        "metrics",
        "diagnostics",
        "environment_usage",
        "request",
        "solver_path",
        "digest",
        "inspection_geometry",
        "independent_validation",
        "request_kind",
        "input_fingerprints",
        "scenario",
        "legacy_algorithm",
    ):
        if key in run:
            diagnostics[key] = run[key]
    if "validation" in run:
        diagnostics["engine_validation"] = run["validation"]
    return diagnostics


def _engine_identity(identity: Mapping[str, Any]) -> EngineIdentity:
    source = _mapping(identity.get("source"), "identity source")
    versions = _mapping(identity.get("versions"), "identity versions")
    return EngineIdentity(
        source_repo=_text(source.get("repository"), "identity repository"),
        source_pin=_text(source.get("commit"), "identity commit"),
        vendor_version=_optional_text(versions.get("engine")),
        cmake_project_version=_optional_text(versions.get("engine_cmake_project")),
        tree_sha256=_text(source.get("tree_sha256"), "identity tree hash"),
        upstream_baseline=_optional_text(source.get("upstream_baseline")),
    )


def _feature_states(
    identity: Mapping[str, Any], run: Mapping[str, Any]
) -> Mapping[str, str]:
    """Explicit per-feature state so absent services are never implied."""

    inspection = run.get("inspection_geometry")
    return {
        "weather_sampling": "sampled",
        "waves": "absent",
        "shoreline": "unsupported",
        "depth": "unsupported",
        "chart_hazards": "unsupported",
        "inspection_geometry": "sampled" if inspection else "absent",
    }


def _capabilities(
    run: Mapping[str, Any],
    traces: Sequence[Trace],
    identity: Mapping[str, Any],
) -> Capabilities:
    validation = _mapping(run.get("validation"), "native validation")
    samples = validation.get("samples")
    request = _mapping(run.get("request"), "native request")
    cadence = request.get("time_step_seconds")
    sampled = (
        isinstance(samples, int)
        and not isinstance(samples, bool)
        and samples >= 1
        and isinstance(cadence, int)
        and not isinstance(cadence, bool)
        and cadence >= 1
    )
    trace_times = (
        "per_position" if any(trace.times is not None for trace in traces) else "absent"
    )
    return Capabilities(
        producer=_text(run.get("engine", "unknown"), "native engine"),
        trace_times=trace_times,
        validation_mode="sampled" if sampled else "none",
        sample_count=samples if sampled else None,
        cadence=cadence if sampled else None,
        features=_feature_states(identity, run),
    )


def _leg(value: Any, label: str) -> Leg:
    data = _mapping(value, label)
    start_epoch = _epoch(data.get("start_time"), f"{label}.start_time")
    end_epoch = _epoch(data.get("end_time"), f"{label}.end_time")
    return Leg(
        leg_id=_text(data.get("leg_id", label.replace("legs[", "leg-")), f"{label}.id"),
        start=_point(data.get("start"), f"{label}.start"),
        end=_point(data.get("end"), f"{label}.end"),
        start_epoch_s=start_epoch,
        start_utc=canonical_utc(start_epoch),
        end_epoch_s=end_epoch,
        end_utc=canonical_utc(end_epoch),
        environment=_leg_environment(data, label),
        readout=_leg_readout(data),
    )


def _leg_environment(
    leg: Mapping[str, Any], label: str
) -> Optional[Mapping[str, Observation]]:
    environment: dict[str, Observation] = {}
    for prefix in ("wind", "current"):
        east = _optional_number(leg.get(f"{prefix}_east_knots"))
        north = _optional_number(leg.get(f"{prefix}_north_knots"))
        available = leg.get(f"{prefix}_available") is True
        available = available and east is not None and north is not None
        environment[f"{prefix}_east_kn"] = Observation(
            east if available else None,
            "kn",
            "reported" if available else "unknown",
        )
        environment[f"{prefix}_north_kn"] = Observation(
            north if available else None,
            "kn",
            "reported" if available else "unknown",
        )
    # The bridge reports wave availability but not recorded wave values. Do not
    # invent a height: absent waves stay unknown.
    if leg.get("waves_available") is not None:
        environment["waves_height_m"] = Observation(None, "m", "unknown")
    if not any(
        observation.availability == "reported" for observation in environment.values()
    ):
        # No recorded value at all: the leg has no route environment rather than
        # a set of unknown observations that could be misread as recorded data.
        return None
    return environment


READOUT_NUMBERS = (
    "heading_degrees",
    "course_through_water_degrees",
    "course_over_ground_degrees",
    "speed_through_water_knots",
    "speed_over_ground_knots",
    "true_wind_speed_knots",
    "true_wind_angle_degrees",
    "wind_gust_knots",
    "estimated_fuel_litres",
)
READOUT_TEXT = ("propulsion_mode", "tack", "profile_identity", "profile_role")


def _leg_readout(leg: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Record per-leg readouts the engine supplied; absent values stay absent."""
    readout: dict[str, Any] = {}
    for key in READOUT_NUMBERS:
        value = _optional_number(leg.get(key))
        if value is not None:
            readout[key] = value
    for key in READOUT_TEXT:
        value = leg.get(key)
        if isinstance(value, str) and value:
            readout[key] = value
    if leg.get("stationary_wait") is not None:
        readout["stationary_wait"] = leg.get("stationary_wait") is True
    for prefix in ("wind", "current"):
        source = leg.get(f"{prefix}_source")
        if isinstance(source, Mapping):
            dataset = source.get("dataset_identity")
            if isinstance(dataset, str) and dataset:
                readout[f"{prefix}_dataset_identity"] = dataset
    warnings = leg.get("warnings")
    if isinstance(warnings, (list, tuple)):
        codes = [
            item.get("code")
            for item in warnings
            if isinstance(item, Mapping) and isinstance(item.get("code"), str)
        ]
        readout["warning_count"] = len(warnings)
        readout["warning_codes"] = ";".join(codes)
    margins = leg.get("safety_margins")
    if isinstance(margins, Mapping):
        for name, value in margins.items():
            number = _optional_number(value)
            if isinstance(name, str) and number is not None:
                readout[f"margin_{name}"] = number
    return readout or None


def _layer(value: Any, label: str) -> Layer:
    data = _mapping(value, label)
    epoch_s = _epoch(data.get("time"), f"{label}.time")
    layer_id = _text(data.get("id", label), f"{label}.id")
    contours = tuple(
        _contour(item, f"{label}.contours[{index}]", layer_id, index)
        for index, item in enumerate(
            _sequence(data.get("contours", ()), f"{label}.contours")
        )
    )
    return Layer(
        layer_id=layer_id,
        time_epoch_s=epoch_s,
        time_utc=canonical_utc(epoch_s),
        reverse=_bool(data.get("reverse"), f"{label}.reverse"),
        contours=contours,
    )


def _contour(value: Any, label: str, layer_id: str, index: int) -> Contour:
    data = _mapping(value, label)
    points = tuple(
        _point(item, f"{label}.points[{position}]")
        for position, item in enumerate(
            _sequence(data.get("points", ()), f"{label}.points")
        )
    )
    contour_id = _text(data.get("id", f"{layer_id}-contour-{index}"), f"{label}.id")
    return Contour(contour_id, _bool(data.get("closed"), f"{label}.closed"), points)


def _layer_traces(value: Any, label: str) -> tuple[Trace, ...]:
    data = _mapping(value, label)
    layer_id = _text(data.get("id", label), f"{label}.id")
    traces = []
    for index, item in enumerate(_sequence(data.get("traces", ()), f"{label}.traces")):
        trace_label = f"{label}.traces[{index}]"
        trace = _mapping(item, trace_label)
        route = tuple(
            _point(point, f"{trace_label}.route[{position}]")
            for position, point in enumerate(
                _sequence(trace.get("route", ()), f"{trace_label}.route")
            )
        )
        times = trace.get("times")
        if times is not None:
            times = tuple(
                _epoch(item, f"{trace_label}.times[{position}]")
                for position, item in enumerate(
                    _sequence(times, f"{trace_label}.times")
                )
            )
            if len(times) != len(route):
                raise SchemaError(
                    f"{trace_label}.times must align with the route positions"
                )
        traces.append(
            Trace(
                _text(
                    trace.get("id", f"{layer_id}-trace-{index}"), f"{trace_label}.id"
                ),
                _point(trace.get("endpoint"), f"{trace_label}.endpoint"),
                route,
                times,
            )
        )
    return tuple(traces)


def _point(value: Any, label: str) -> Point:
    data = _mapping(value, label)
    return Point(
        _number(data.get("latitude"), f"{label}.latitude"),
        _number(data.get("longitude"), f"{label}.longitude"),
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise SchemaError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaError(f"{label} must be a non-empty string")
    return value


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return _text(value, "optional text")


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(f"{label} must be a boolean")
    return value


def _epoch(value: Any, label: str) -> int:
    number = _number(value, label)
    if not float(number).is_integer():
        raise SchemaError(f"{label} must be whole epoch seconds")
    return int(number)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise SchemaError(f"{label} must be a finite number")
    return number


def _optional_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
