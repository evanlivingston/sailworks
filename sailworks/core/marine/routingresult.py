"""Backend-neutral schema-v1 routing results, conversion and eligibility.

This module is standalone.  It does not import the legacy routing engine, the
native bridge, Qt, or any persistence/export wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping, Optional, Tuple
import uuid

from .routingrecord import (
    ArtifactRef,
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Observation,
    Outcome,
    Point,
    Preview,
    Provenance,
    SCHEMA_CAPABILITIES,
    SCHEMA_VERSION,
    STATUSES,
    SchemaError,
    StaleResultError,
    Trace,
    Validation,
    VALIDATION_STATES,
    canonical_utc,
    check_utc_epoch,
    epoch_from_utc,
    is_int,
    text,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _reject_constant(value: str) -> None:
    raise SchemaError(f"non-finite JSON constant {value} is not valid")


def _object(
    value: Any,
    label: str,
    *,
    required: Tuple[str, ...],
    optional: Tuple[str, ...] = (),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{label} must be an object")
    keys = set(value)
    missing = set(required) - keys
    extra = keys - set(required) - set(optional)
    if missing:
        raise SchemaError(f"{label} is missing keys {sorted(missing)}")
    if extra:
        raise SchemaError(f"{label} has unknown keys {sorted(extra)}")
    return value


def _array(value: Any, label: str) -> list:
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"{label} must be an array")
    return list(value)


def _point_from_dict(value: Any, label: str) -> Point:
    data = _object(value, label, required=("lat", "lon"))
    return Point(data["lat"], data["lon"])


def _point_from_native(value: Any, label: str) -> Point:
    pair = _array(value, label)
    if len(pair) != 2:
        raise SchemaError(f"{label} must be a [lat, lon] pair")
    return Point(pair[0], pair[1])


def _observation_from_dict(value: Any, label: str) -> Observation:
    data = _object(value, label, required=("value", "unit", "availability"))
    return Observation(data["value"], data["unit"], data["availability"])


def _observation_from_native(value: Any, label: str) -> Observation:
    if value is None:
        return Observation(None, None, "unknown")
    if isinstance(value, Mapping):
        data = _object(value, label, required=("value", "unit", "availability"))
        unit = data["unit"]
        availability = data["availability"]
        if availability not in {"reported", "unknown"}:
            raise SchemaError(f"{label} availability is invalid")
        raw = data["value"]
        if raw is None:
            return Observation(None, unit, "unknown")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise SchemaError(f"{label} value must be a number or null")
        if not math.isfinite(raw):
            return Observation(None, unit, "unknown")
        if availability == "unknown":
            raise SchemaError(f"{label} unknown availability cannot carry a value")
        return Observation(float(raw), unit, "reported")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{label} must be a number, observation or null")
    if not math.isfinite(value):
        return Observation(None, None, "unknown")
    return Observation(float(value), None, "reported")


def _environment_from_dict(value: Any, label: str) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SchemaError(f"{label} must be an object or null")
    environment = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise SchemaError(f"{label} keys must be non-empty strings")
        environment[key] = _observation_from_dict(item, f"{label}.{key}")
    return environment


def _environment_from_native(value: Any, label: str) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SchemaError(f"{label} must be an object or null")
    environment = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise SchemaError(f"{label} keys must be non-empty strings")
        environment[key] = _observation_from_native(item, f"{label}.{key}")
    return environment


def _environment_to_native(environment) -> Optional[dict]:
    if environment is None:
        return None
    native = {}
    for key, observation in environment.items():
        if observation.availability == "unknown" and observation.unit is None:
            native[key] = None
        elif observation.availability == "reported" and observation.unit is None:
            native[key] = observation.value
        else:
            native[key] = observation.to_dict()
    return native


def _engine_from_dict(value: Any) -> EngineIdentity:
    data = _object(
        value,
        "engine_identity",
        required=(
            "source_repo",
            "source_pin",
            "vendor_version",
            "cmake_project_version",
            "tree_sha256",
            "upstream_baseline",
        ),
    )
    return EngineIdentity(
        data["source_repo"],
        data["source_pin"],
        data["vendor_version"],
        data["cmake_project_version"],
        data["tree_sha256"],
        data["upstream_baseline"],
    )


def _validation_from_dict(value: Any, label: str) -> Validation:
    data = _object(value, label, required=("state", "independent"))
    return Validation(data["state"], data["independent"])


def _outcome_from_dict(value: Any) -> Outcome:
    data = _object(
        value,
        "outcome",
        required=("accepted", "delivered", "validation", "diagnostics"),
    )
    return Outcome(
        data["accepted"],
        data["delivered"],
        _validation_from_dict(data["validation"], "outcome.validation"),
        data["diagnostics"],
    )


def _provenance_from_dict(value: Any) -> Provenance:
    data = _object(value, "provenance", required=("created_utc", "route_environment"))
    return Provenance(data["created_utc"], data["route_environment"])


def _capabilities_from_dict(value: Any) -> Capabilities:
    data = _object(value, "capabilities", required=("schema", "backend"))
    schema_data = _object(
        data["schema"],
        "capabilities.schema",
        required=tuple(SCHEMA_CAPABILITIES),
    )
    if dict(schema_data) != dict(SCHEMA_CAPABILITIES):
        raise SchemaError("schema capabilities are fixed invariants")
    backend = _object(
        data["backend"],
        "capabilities.backend",
        required=("producer", "trace_times", "validation"),
        optional=("features",),
    )
    validation = _object(
        backend["validation"],
        "capabilities.backend.validation",
        required=("mode", "sample_count", "cadence"),
    )
    return Capabilities(
        backend["producer"],
        backend["trace_times"],
        validation["mode"],
        validation["sample_count"],
        validation["cadence"],
        backend.get("features"),
    )


def _artifact_from_dict(value: Any) -> ArtifactRef:
    data = _object(
        value,
        "artifact_ref",
        required=(
            "artifact_version",
            "artifact_id",
            "sha256",
            "byte_length",
            "media_type",
            "uri",
        ),
    )
    return ArtifactRef(
        data["artifact_version"],
        data["artifact_id"],
        data["sha256"],
        data["byte_length"],
        data["media_type"],
        data["uri"],
    )


def _contour_from_dict(value: Any, label: str) -> Contour:
    data = _object(value, label, required=("contour_id", "closed", "points"))
    points = tuple(
        _point_from_dict(item, f"{label}.points[{index}]")
        for index, item in enumerate(_array(data["points"], f"{label}.points"))
    )
    return Contour(data["contour_id"], data["closed"], points)


def _layer_from_dict(value: Any, label: str) -> Layer:
    data = _object(
        value,
        label,
        required=("layer_id", "time_epoch_s", "time_utc", "reverse", "contours"),
    )
    contours = tuple(
        _contour_from_dict(item, f"{label}.contours[{index}]")
        for index, item in enumerate(_array(data["contours"], f"{label}.contours"))
    )
    return Layer(
        data["layer_id"],
        data["time_epoch_s"],
        data["time_utc"],
        data["reverse"],
        contours,
    )


def _leg_from_dict(value: Any, label: str) -> Leg:
    data = _object(
        value,
        label,
        required=(
            "leg_id",
            "start",
            "end",
            "start_epoch_s",
            "start_utc",
            "end_epoch_s",
            "end_utc",
            "environment",
        ),
        optional=("readout",),
    )
    return Leg(
        data["leg_id"],
        _point_from_dict(data["start"], f"{label}.start"),
        _point_from_dict(data["end"], f"{label}.end"),
        data["start_epoch_s"],
        data["start_utc"],
        data["end_epoch_s"],
        data["end_utc"],
        _environment_from_dict(data["environment"], f"{label}.environment"),
        data.get("readout"),
    )


def _trace_from_dict(value: Any, label: str) -> Trace:
    data = _object(value, label, required=("trace_id", "endpoint", "route", "times"))
    route = tuple(
        _point_from_dict(item, f"{label}.route[{index}]")
        for index, item in enumerate(_array(data["route"], f"{label}.route"))
    )
    times = data["times"]
    if times is not None:
        times = tuple(_array(times, f"{label}.times"))
    return Trace(
        data["trace_id"],
        _point_from_dict(data["endpoint"], f"{label}.endpoint"),
        route,
        times,
    )


def _preview_from_dict(value: Any, label: str) -> Preview:
    data = _object(
        value,
        label,
        required=(
            "preview_id",
            "generation",
            "sequence",
            "provisional",
            "route",
        ),
    )
    route = tuple(
        _point_from_dict(item, f"{label}.route[{index}]")
        for index, item in enumerate(_array(data["route"], f"{label}.route"))
    )
    return Preview(
        data["preview_id"],
        data["generation"],
        data["sequence"],
        route,
        data["provisional"],
    )


def _contour_from_native(value: Any, label: str) -> Contour:
    data = _object(value, label, required=("id", "closed", "points"))
    points = tuple(
        _point_from_native(item, f"{label}.points[{index}]")
        for index, item in enumerate(_array(data["points"], f"{label}.points"))
    )
    return Contour(data["id"], data["closed"], points)


def _layer_from_native(value: Any, label: str) -> Layer:
    data = _object(
        value,
        label,
        required=("id", "time_epoch_s", "reverse", "contours"),
        optional=("time_utc",),
    )
    epoch_s = data["time_epoch_s"]
    utc = data.get("time_utc")
    if utc is None:
        utc = canonical_utc(epoch_s)
    else:
        check_utc_epoch(epoch_s, utc, f"{label} time")
    contours = tuple(
        _contour_from_native(item, f"{label}.contours[{index}]")
        for index, item in enumerate(_array(data["contours"], f"{label}.contours"))
    )
    return Layer(data["id"], epoch_s, utc, data["reverse"], contours)


def _leg_from_native(value: Any, label: str) -> Leg:
    data = _object(
        value,
        label,
        required=("id", "start", "end", "start_epoch_s", "end_epoch_s"),
        optional=("start_utc", "end_utc", "environment", "readout"),
    )
    start_epoch = data["start_epoch_s"]
    end_epoch = data["end_epoch_s"]
    start_utc = data.get("start_utc")
    end_utc = data.get("end_utc")
    if start_utc is None:
        start_utc = canonical_utc(start_epoch)
    if end_utc is None:
        end_utc = canonical_utc(end_epoch)
    check_utc_epoch(start_epoch, start_utc, f"{label} start")
    check_utc_epoch(end_epoch, end_utc, f"{label} end")
    return Leg(
        data["id"],
        _point_from_native(data["start"], f"{label}.start"),
        _point_from_native(data["end"], f"{label}.end"),
        start_epoch,
        start_utc,
        end_epoch,
        end_utc,
        _environment_from_native(data.get("environment"), f"{label}.environment"),
        data.get("readout"),
    )


def _trace_from_native(value: Any, label: str) -> Trace:
    data = _object(value, label, required=("id", "endpoint", "route", "times"))
    route = tuple(
        _point_from_native(item, f"{label}.route[{index}]")
        for index, item in enumerate(_array(data["route"], f"{label}.route"))
    )
    times = data["times"]
    if times is not None:
        times = tuple(_array(times, f"{label}.times"))
    return Trace(
        data["id"],
        _point_from_native(data["endpoint"], f"{label}.endpoint"),
        route,
        times,
    )


def _preview_from_native(value: Any, label: str) -> Preview:
    data = _object(
        value,
        label,
        required=("id", "generation", "sequence", "provisional", "route"),
    )
    route = tuple(
        _point_from_native(item, f"{label}.route[{index}]")
        for index, item in enumerate(_array(data["route"], f"{label}.route"))
    )
    return Preview(
        data["id"],
        data["generation"],
        data["sequence"],
        route,
        data["provisional"],
    )


def _provenance_from_native(value: Any) -> Provenance:
    data = _object(
        value,
        "provenance",
        required=("created_epoch_s", "route_environment"),
        optional=("created_utc",),
    )
    epoch_s = data["created_epoch_s"]
    utc = data.get("created_utc")
    if utc is None:
        utc = canonical_utc(epoch_s)
    else:
        check_utc_epoch(epoch_s, utc, "provenance created")
    return Provenance(utc, data["route_environment"])


@dataclass(frozen=True)
class RoutingResult:
    calculation_id: str
    generation: int
    sequence: int
    status: str
    outcome: Outcome
    engine_identity: EngineIdentity
    provenance: Provenance
    legs: Tuple[Leg, ...]
    layers: Tuple[Layer, ...]
    capabilities: Capabilities
    traces: Tuple[Trace, ...] = ()
    previews: Tuple[Preview, ...] = ()
    artifact_ref: Optional[ArtifactRef] = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not is_int(self.schema_version) or self.schema_version != 1:
            raise SchemaError("only schema_version 1 is supported")
        text(self.calculation_id, "calculation_id")
        try:
            parsed = uuid.UUID(self.calculation_id)
        except (AttributeError, ValueError) as exc:
            raise SchemaError("calculation_id must be a UUID") from exc
        if str(parsed) != self.calculation_id:
            raise SchemaError("calculation_id must use canonical lowercase UUID text")
        if not is_int(self.generation) or self.generation < 0:
            raise SchemaError("generation must be a non-negative integer")
        if not is_int(self.sequence) or self.sequence < 0:
            raise SchemaError("sequence must be a non-negative integer")
        if not isinstance(self.status, str) or self.status not in STATUSES:
            raise SchemaError(f"unknown status {self.status!r}")
        if not isinstance(self.outcome, Outcome):
            raise SchemaError("outcome must be an Outcome record")
        if not isinstance(self.engine_identity, EngineIdentity):
            raise SchemaError("engine_identity must be an EngineIdentity record")
        if not isinstance(self.provenance, Provenance):
            raise SchemaError("provenance must be a Provenance record")
        if not isinstance(self.capabilities, Capabilities):
            raise SchemaError("capabilities must be a Capabilities record")
        if self.artifact_ref is not None and not isinstance(
            self.artifact_ref, ArtifactRef
        ):
            raise SchemaError("artifact_ref must be an ArtifactRef record or null")

        legs = tuple(self.legs)
        layers = tuple(self.layers)
        traces = tuple(self.traces)
        previews = tuple(self.previews)
        if not all(isinstance(item, Leg) for item in legs):
            raise SchemaError("legs must be Leg records")
        if not all(isinstance(item, Layer) for item in layers):
            raise SchemaError("layers must be Layer records")
        if not all(isinstance(item, Trace) for item in traces):
            raise SchemaError("traces must be Trace records")
        if not all(isinstance(item, Preview) for item in previews):
            raise SchemaError("previews must be Preview records")
        object.__setattr__(self, "legs", legs)
        object.__setattr__(self, "layers", layers)
        object.__setattr__(self, "traces", traces)
        object.__setattr__(self, "previews", previews)

        if self.status in {
            "cancelled",
            "failed",
            "incomplete",
            "preview",
            "no_feasible_route",
            "resource_limit_reached",
            "internal_error",
        }:
            if self.outcome.accepted or self.outcome.delivered:
                raise SchemaError(
                    f"{self.status} results cannot be accepted or delivered"
                )
        if self.capabilities.trace_times == "absent":
            if any(trace.times is not None for trace in traces):
                raise SchemaError("trace times contradict absent capability")
        if self.provenance.route_environment == "absent":
            if any(leg.environment for leg in legs):
                raise SchemaError("absent route environment cannot carry values")

        identifiers = []
        for leg in legs:
            identifiers.append(leg.leg_id)
        for layer in layers:
            identifiers.append(layer.layer_id)
            for contour in layer.contours:
                identifiers.append(contour.contour_id)
        for trace in traces:
            identifiers.append(trace.trace_id)
        for preview in previews:
            identifiers.append(preview.preview_id)
        if len(set(identifiers)) != len(identifiers):
            raise SchemaError("routing identifiers must be unique per calculation")

    def ensure_supersedes(self, previous: "RoutingResult") -> None:
        if not isinstance(previous, RoutingResult):
            raise TypeError("previous must be a RoutingResult")
        if self.calculation_id != previous.calculation_id:
            raise StaleResultError("supersession requires the same calculation_id")
        if self.generation < previous.generation:
            raise StaleResultError("generation moved backwards")
        if self.generation == previous.generation:
            if self.sequence <= previous.sequence:
                raise StaleResultError("stale sequence for the current generation")

    def to_dict(self) -> dict:
        data = {
            "schema_version": self.schema_version,
            "calculation_id": self.calculation_id,
            "generation": self.generation,
            "sequence": self.sequence,
            "status": self.status,
            "outcome": self.outcome.to_dict(),
            "engine_identity": self.engine_identity.to_dict(),
            "provenance": self.provenance.to_dict(),
            "legs": [leg.to_dict() for leg in self.legs],
            "layers": [layer.to_dict() for layer in self.layers],
            "capabilities": self.capabilities.to_dict(),
        }
        if self.traces:
            data["traces"] = [trace.to_dict() for trace in self.traces]
        if self.previews:
            data["previews"] = [preview.to_dict() for preview in self.previews]
        if self.artifact_ref is not None:
            data["artifact_ref"] = self.artifact_ref.to_dict()
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: Any) -> "RoutingResult":
        if not isinstance(value, str):
            raise SchemaError("routing result JSON must be text")
        try:
            data = json.loads(value, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise SchemaError("routing result JSON is invalid") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, value: Any) -> "RoutingResult":
        data = _object(
            value,
            "routing result",
            required=(
                "schema_version",
                "calculation_id",
                "generation",
                "sequence",
                "status",
                "outcome",
                "engine_identity",
                "provenance",
                "legs",
                "layers",
                "capabilities",
            ),
            optional=("traces", "previews", "artifact_ref"),
        )
        if data["schema_version"] != 1:
            raise SchemaError("only schema_version 1 is supported")
        legs = tuple(
            _leg_from_dict(item, f"legs[{index}]")
            for index, item in enumerate(_array(data["legs"], "legs"))
        )
        layers = tuple(
            _layer_from_dict(item, f"layers[{index}]")
            for index, item in enumerate(_array(data["layers"], "layers"))
        )
        traces = tuple(
            _trace_from_dict(item, f"traces[{index}]")
            for index, item in enumerate(_array(data.get("traces", []), "traces"))
        )
        previews = tuple(
            _preview_from_dict(item, f"previews[{index}]")
            for index, item in enumerate(_array(data.get("previews", []), "previews"))
        )
        artifact = data.get("artifact_ref")
        return cls(
            calculation_id=data["calculation_id"],
            generation=data["generation"],
            sequence=data["sequence"],
            status=data["status"],
            outcome=_outcome_from_dict(data["outcome"]),
            engine_identity=_engine_from_dict(data["engine_identity"]),
            provenance=_provenance_from_dict(data["provenance"]),
            legs=legs,
            layers=layers,
            capabilities=_capabilities_from_dict(data["capabilities"]),
            traces=traces,
            previews=previews,
            artifact_ref=None if artifact is None else _artifact_from_dict(artifact),
            schema_version=data["schema_version"],
        )

    @classmethod
    def from_native(cls, value: Any) -> "RoutingResult":
        data = _object(
            value,
            "native routing payload",
            required=(
                "schema_version",
                "calculation",
                "status",
                "outcome",
                "engine",
                "provenance",
                "legs",
                "layers",
                "capabilities",
            ),
            optional=("traces", "previews", "artifact_ref"),
        )
        if data["schema_version"] != 1:
            raise SchemaError("only schema_version 1 is supported")
        calculation = _object(
            data["calculation"],
            "calculation",
            required=("id", "generation", "sequence"),
        )
        provenance = _provenance_from_native(data["provenance"])
        legs = tuple(
            _leg_from_native(item, f"legs[{index}]")
            for index, item in enumerate(_array(data["legs"], "legs"))
        )
        layers = tuple(
            _layer_from_native(item, f"layers[{index}]")
            for index, item in enumerate(_array(data["layers"], "layers"))
        )
        traces = tuple(
            _trace_from_native(item, f"traces[{index}]")
            for index, item in enumerate(_array(data.get("traces", []), "traces"))
        )
        previews = tuple(
            _preview_from_native(item, f"previews[{index}]")
            for index, item in enumerate(_array(data.get("previews", []), "previews"))
        )
        artifact = data.get("artifact_ref")
        return cls(
            calculation_id=calculation["id"],
            generation=calculation["generation"],
            sequence=calculation["sequence"],
            status=data["status"],
            outcome=_outcome_from_dict(data["outcome"]),
            engine_identity=_engine_from_dict(data["engine"]),
            provenance=provenance,
            legs=legs,
            layers=layers,
            capabilities=_capabilities_from_dict(data["capabilities"]),
            traces=traces,
            previews=previews,
            artifact_ref=None if artifact is None else _artifact_from_dict(artifact),
            schema_version=data["schema_version"],
        )

    def to_native(self) -> dict:
        data = {
            "schema_version": self.schema_version,
            "calculation": {
                "id": self.calculation_id,
                "generation": self.generation,
                "sequence": self.sequence,
            },
            "status": self.status,
            "outcome": self.outcome.to_dict(),
            "engine": self.engine_identity.to_dict(),
            "provenance": {
                "created_epoch_s": epoch_from_utc(self.provenance.created_utc),
                "route_environment": self.provenance.route_environment,
            },
            "legs": [
                {
                    "id": leg.leg_id,
                    "start": [leg.start.lat, leg.start.lon],
                    "end": [leg.end.lat, leg.end.lon],
                    "start_epoch_s": leg.start_epoch_s,
                    "end_epoch_s": leg.end_epoch_s,
                    "environment": _environment_to_native(leg.environment),
                    "readout": None if leg.readout is None else dict(leg.readout),
                }
                for leg in self.legs
            ],
            "layers": [
                {
                    "id": layer.layer_id,
                    "time_epoch_s": layer.time_epoch_s,
                    "reverse": layer.reverse,
                    "contours": [
                        {
                            "id": contour.contour_id,
                            "closed": contour.closed,
                            "points": [
                                [point.lat, point.lon] for point in contour.points
                            ],
                        }
                        for contour in layer.contours
                    ],
                }
                for layer in self.layers
            ],
            "capabilities": self.capabilities.to_dict(),
        }
        if self.traces:
            data["traces"] = [
                {
                    "id": trace.trace_id,
                    "endpoint": [trace.endpoint.lat, trace.endpoint.lon],
                    "route": [[point.lat, point.lon] for point in trace.route],
                    "times": None if trace.times is None else list(trace.times),
                }
                for trace in self.traces
            ]
        if self.previews:
            data["previews"] = [
                {
                    "id": preview.preview_id,
                    "generation": preview.generation,
                    "sequence": preview.sequence,
                    "provisional": preview.provisional,
                    "route": [[point.lat, point.lon] for point in preview.route],
                }
                for preview in self.previews
            ]
        if self.artifact_ref is not None:
            data["artifact_ref"] = self.artifact_ref.to_dict()
        return data


def export_blockers(result: RoutingResult) -> Tuple[str, ...]:
    if not isinstance(result, RoutingResult):
        raise TypeError("result must be a RoutingResult")
    reasons = []
    if result.status != "complete":
        reasons.append(f"status:{result.status}")
    if not result.outcome.accepted:
        reasons.append("outcome:not-accepted")
    if not result.outcome.delivered:
        reasons.append("outcome:not-delivered")
    validation = result.outcome.validation
    if validation.state != "passed":
        reasons.append(f"validation:{validation.state}")
    if not validation.independent:
        reasons.append("validation:not-independent")
    if not result.legs:
        reasons.append("route:no-legs")
    return tuple(reasons)


def export_eligible(result: RoutingResult) -> bool:
    return not export_blockers(result)


navigation_eligible = export_eligible


def legacy_validation_state(summary: Any) -> Validation:
    """Read only an explicit legacy validation state.

    Boolean ``passed`` flags are deliberately ignored: a legacy summary that
    lacks explicit validation metadata remains unknown and cannot become
    export eligible.  No legacy module is imported, so heavy optional
    dependencies stay outside this standalone adapter.
    """
    state = "unknown"
    if isinstance(summary, Mapping):
        validation = summary.get("validation")
        if isinstance(validation, Mapping):
            candidate = validation.get("state")
            if candidate in VALIDATION_STATES and candidate != "unknown":
                state = candidate
    return Validation(state, False)
