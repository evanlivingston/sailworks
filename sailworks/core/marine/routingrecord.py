"""Immutable schema-v1 routing result records and strict value objects.

The values here are backend neutral.  They preserve engine emission order and
explicit unknown state; they never repair geometry, infer trace times or turn
unavailable observations into zero.
"""

from __future__ import annotations

from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import urlsplit

SCHEMA_VERSION = 1

STATUSES = frozenset({"complete", "cancelled", "failed", "incomplete", "preview"})
VALIDATION_STATES = frozenset({"passed", "failed", "unknown"})
VALIDATION_MODES = frozenset({"sampled", "exhaustive", "none"})
FEATURE_STATES = frozenset({"complete", "sampled", "absent", "unsupported"})
TRACE_TIME_CAPABILITIES = frozenset({"absent", "per_position"})
ROUTE_ENVIRONMENTS = frozenset({"recorded", "absent"})

SCHEMA_CAPABILITIES = MappingProxyType(
    {
        "contour_order": "emitted",
        "closed": "explicit",
        "trace_times": "nullable",
    }
)

_CANONICAL_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RoutingResultError(ValueError):
    """Base error for routing result schema and integrity failures."""


class SchemaError(RoutingResultError):
    """A schema-v1 record violated a required invariant."""


class StaleResultError(RoutingResultError):
    """A provisional update did not supersede its predecessor."""


class ArtifactIntegrityError(RoutingResultError):
    """A stored artifact failed digest or byte-length verification."""


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaError(f"{label} must be a non-empty string")
    return value


def sha256_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SchemaError(f"{label} must be a lowercase SHA-256 digest")
    return value


def canonical_utc(epoch_s: int) -> str:
    if not is_int(epoch_s):
        raise SchemaError("epoch seconds must be an integer")
    try:
        instant = datetime.fromtimestamp(epoch_s, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise SchemaError("epoch seconds are out of range") from exc
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_from_utc(value: Any) -> int:
    if not isinstance(value, str) or not _CANONICAL_UTC.fullmatch(value):
        raise SchemaError(
            "UTC timestamps must be whole-second YYYY-MM-DDTHH:MM:SSZ values"
        )
    try:
        instant = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SchemaError("UTC timestamp is not a real instant") from exc
    return timegm(instant.timetuple())


def check_utc_epoch(epoch_s: Any, utc: Any, label: str) -> None:
    if not is_int(epoch_s):
        raise SchemaError(f"{label} epoch seconds must be an integer")
    if epoch_from_utc(utc) != epoch_s:
        raise SchemaError(f"{label} UTC text and epoch seconds disagree")


def _json_value(value: Any, label: str) -> Any:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return value
    if is_int(value):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError(f"{label} cannot contain non-finite numbers")
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_json_value(item, label) for item in value)
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError(f"{label} keys must be strings")
            frozen[key] = _json_value(item, label)
        return MappingProxyType(frozen)
    raise SchemaError(f"{label} must contain JSON-safe values")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float

    def __post_init__(self) -> None:
        for name, value, limit in (
            ("lat", self.lat, 90.0),
            ("lon", self.lon, 180.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SchemaError(f"coordinate {name} must be a finite number")
            if not math.isfinite(value):
                raise SchemaError(f"coordinate {name} must be finite")
            if value < -limit or value > limit:
                raise SchemaError(f"coordinate {name} is out of range")
            object.__setattr__(self, name, float(value))

    def to_dict(self) -> dict:
        return {"lat": self.lat, "lon": self.lon}


@dataclass(frozen=True)
class Observation:
    value: Optional[float]
    unit: Optional[str] = None
    availability: str = "reported"

    def __post_init__(self) -> None:
        if not isinstance(self.availability, str) or self.availability not in {
            "reported",
            "unknown",
        }:
            raise SchemaError("observation availability must be reported or unknown")
        if self.unit is not None and not isinstance(self.unit, str):
            raise SchemaError("observation unit must be a string or null")
        if self.availability == "unknown":
            if self.value is not None:
                raise SchemaError("unknown observations cannot carry a value")
            return
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise SchemaError("reported observations need a finite value")
        if not math.isfinite(self.value):
            raise SchemaError("reported observations need a finite value")
        object.__setattr__(self, "value", float(self.value))

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "availability": self.availability,
        }


@dataclass(frozen=True)
class ArtifactRef:
    artifact_version: int
    artifact_id: str
    sha256: str
    byte_length: int
    media_type: str
    uri: str

    def __post_init__(self) -> None:
        if not is_int(self.artifact_version) or self.artifact_version != 1:
            raise SchemaError("artifact_version must be 1")
        sha256_text(self.artifact_id, "artifact_id")
        sha256_text(self.sha256, "sha256")
        if self.artifact_id != self.sha256:
            raise SchemaError("artifact_id must be the content SHA-256 digest")
        if not is_int(self.byte_length) or self.byte_length < 0:
            raise SchemaError("byte_length must be a non-negative integer")
        text(self.media_type, "media_type")
        text(self.uri, "uri")
        parsed = urlsplit(self.uri)
        has_dot_segment = any(
            segment in {".", ".."}
            for component in (parsed.netloc, parsed.path)
            for segment in component.split("/")
        )
        netloc = parsed.netloc
        authority_traversal = netloc.startswith("..") or netloc.endswith("..")
        if "\\" in self.uri or has_dot_segment or authority_traversal:
            raise SchemaError("artifact uri must not contain traversal components")

    def to_dict(self) -> dict:
        return {
            "artifact_version": self.artifact_version,
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "uri": self.uri,
        }


@dataclass(frozen=True)
class EngineIdentity:
    source_repo: str
    source_pin: str
    vendor_version: Optional[str]
    cmake_project_version: Optional[str]
    tree_sha256: str
    upstream_baseline: Optional[str]

    def __post_init__(self) -> None:
        text(self.source_repo, "source_repo")
        text(self.source_pin, "source_pin")
        sha256_text(self.tree_sha256, "tree_sha256")
        for label in ("vendor_version", "cmake_project_version", "upstream_baseline"):
            value = getattr(self, label)
            if value is not None:
                text(value, label)

    def to_dict(self) -> dict:
        return {
            "source_repo": self.source_repo,
            "source_pin": self.source_pin,
            "vendor_version": self.vendor_version,
            "cmake_project_version": self.cmake_project_version,
            "tree_sha256": self.tree_sha256,
            "upstream_baseline": self.upstream_baseline,
        }


@dataclass(frozen=True)
class Validation:
    state: str
    independent: bool

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or self.state not in VALIDATION_STATES:
            raise SchemaError("validation state must be passed, failed or unknown")
        if not isinstance(self.independent, bool):
            raise SchemaError("validation independence must be a boolean")
        if self.state == "unknown" and self.independent:
            raise SchemaError("unknown validation cannot claim independence")

    def to_dict(self) -> dict:
        return {"state": self.state, "independent": self.independent}


@dataclass(frozen=True)
class Outcome:
    accepted: bool
    delivered: bool
    validation: Validation
    diagnostics: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise SchemaError("outcome accepted must be a boolean")
        if not isinstance(self.delivered, bool):
            raise SchemaError("outcome delivered must be a boolean")
        if self.delivered and not self.accepted:
            raise SchemaError("a delivered outcome must also be accepted")
        if not isinstance(self.validation, Validation):
            raise SchemaError("outcome validation must be a Validation record")
        frozen = {} if self.diagnostics is None else self.diagnostics
        object.__setattr__(
            self, "diagnostics", _json_value(frozen, "outcome diagnostics")
        )

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "delivered": self.delivered,
            "validation": self.validation.to_dict(),
            "diagnostics": thaw_json(self.diagnostics),
        }


@dataclass(frozen=True)
class Provenance:
    created_utc: str
    route_environment: str

    def __post_init__(self) -> None:
        epoch_from_utc(self.created_utc)
        if (
            not isinstance(self.route_environment, str)
            or self.route_environment not in ROUTE_ENVIRONMENTS
        ):
            raise SchemaError("route_environment must be recorded or absent")

    def to_dict(self) -> dict:
        return {
            "created_utc": self.created_utc,
            "route_environment": self.route_environment,
        }


@dataclass(frozen=True)
class Capabilities:
    producer: str
    trace_times: str
    validation_mode: str
    sample_count: Optional[int]
    cadence: Optional[int]
    features: Optional[Mapping[str, str]] = None

    def __post_init__(self) -> None:
        text(self.producer, "capabilities producer")
        if (
            not isinstance(self.trace_times, str)
            or self.trace_times not in TRACE_TIME_CAPABILITIES
        ):
            raise SchemaError("trace_times must be absent or per_position")
        if self.producer.casefold() == "standard" and self.trace_times != "absent":
            raise SchemaError("the standard producer cannot claim trace times")
        if (
            not isinstance(self.validation_mode, str)
            or self.validation_mode not in VALIDATION_MODES
        ):
            raise SchemaError("validation mode must be sampled, exhaustive or none")
        for label in ("sample_count", "cadence"):
            value = getattr(self, label)
            if value is not None and (not is_int(value) or value < 0):
                raise SchemaError(f"{label} must be a non-negative integer or null")
        if self.validation_mode == "sampled":
            if (
                self.sample_count is None
                or self.sample_count < 1
                or self.cadence is None
                or self.cadence < 1
            ):
                raise SchemaError("sampled validation needs a count and cadence")
        elif self.sample_count is not None or self.cadence is not None:
            raise SchemaError(
                f"{self.validation_mode} validation cannot report samples"
            )
        if self.features is not None:
            if not isinstance(self.features, Mapping):
                raise SchemaError("feature capabilities must be an object or null")
            frozen = {}
            for key, state in self.features.items():
                if not isinstance(key, str) or not key:
                    raise SchemaError("feature names must be non-empty strings")
                if not isinstance(state, str) or state not in FEATURE_STATES:
                    raise SchemaError(
                        "feature state must be complete, sampled, absent or unsupported"
                    )
                frozen[key] = state
            object.__setattr__(self, "features", MappingProxyType(frozen))

    def to_dict(self) -> dict:
        return {
            "schema": dict(SCHEMA_CAPABILITIES),
            "backend": {
                "features": None if self.features is None else dict(self.features),
                "producer": self.producer,
                "trace_times": self.trace_times,
                "validation": {
                    "mode": self.validation_mode,
                    "sample_count": self.sample_count,
                    "cadence": self.cadence,
                },
            },
        }


@dataclass(frozen=True)
class Leg:
    leg_id: str
    start: Point
    end: Point
    start_epoch_s: int
    start_utc: str
    end_epoch_s: int
    end_utc: str
    environment: Optional[Mapping[str, Observation]] = None
    readout: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        text(self.leg_id, "leg_id")
        if not isinstance(self.start, Point) or not isinstance(self.end, Point):
            raise SchemaError("leg endpoints must be Point records")
        check_utc_epoch(self.start_epoch_s, self.start_utc, "leg start")
        check_utc_epoch(self.end_epoch_s, self.end_utc, "leg end")
        if self.environment is not None:
            if not isinstance(self.environment, Mapping):
                raise SchemaError("leg environment must be an object or null")
            frozen = {}
            for key, observation in self.environment.items():
                if not isinstance(key, str) or not key:
                    raise SchemaError("environment keys must be non-empty strings")
                if not isinstance(observation, Observation):
                    raise SchemaError("environment values must be observations")
                frozen[key] = observation
            object.__setattr__(self, "environment", MappingProxyType(frozen))
        if self.readout is not None:
            if not isinstance(self.readout, Mapping):
                raise SchemaError("leg readout must be an object or null")
            frozen_readout = {}
            for key, value in self.readout.items():
                if not isinstance(key, str) or not key:
                    raise SchemaError("readout keys must be non-empty strings")
                if value is None or isinstance(value, (str, bool, int, float)):
                    frozen_readout[key] = value
                else:
                    raise SchemaError("readout values must be scalars or null")
            object.__setattr__(self, "readout", MappingProxyType(frozen_readout))

    def to_dict(self) -> dict:
        environment = None
        if self.environment is not None:
            environment = {
                key: observation.to_dict()
                for key, observation in self.environment.items()
            }
        return {
            "leg_id": self.leg_id,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "start_epoch_s": self.start_epoch_s,
            "start_utc": self.start_utc,
            "end_epoch_s": self.end_epoch_s,
            "end_utc": self.end_utc,
            "environment": environment,
            "readout": None if self.readout is None else dict(self.readout),
        }


@dataclass(frozen=True)
class Contour:
    contour_id: str
    closed: bool
    points: Tuple[Point, ...]

    def __post_init__(self) -> None:
        text(self.contour_id, "contour_id")
        if not isinstance(self.closed, bool):
            raise SchemaError("contour closed must be a boolean")
        points = tuple(self.points)
        if not points:
            raise SchemaError("contours need at least one emitted point")
        if not all(isinstance(point, Point) for point in points):
            raise SchemaError("contour points must be Point records")
        object.__setattr__(self, "points", points)

    def to_dict(self) -> dict:
        return {
            "contour_id": self.contour_id,
            "closed": self.closed,
            "points": [point.to_dict() for point in self.points],
        }


@dataclass(frozen=True)
class Layer:
    layer_id: str
    time_epoch_s: int
    time_utc: str
    reverse: bool
    contours: Tuple[Contour, ...]

    def __post_init__(self) -> None:
        text(self.layer_id, "layer_id")
        check_utc_epoch(self.time_epoch_s, self.time_utc, "layer time")
        if not isinstance(self.reverse, bool):
            raise SchemaError("layer reverse must be a boolean")
        contours = tuple(self.contours)
        if not all(isinstance(contour, Contour) for contour in contours):
            raise SchemaError("layer contours must be Contour records")
        object.__setattr__(self, "contours", contours)

    def to_dict(self) -> dict:
        return {
            "layer_id": self.layer_id,
            "time_epoch_s": self.time_epoch_s,
            "time_utc": self.time_utc,
            "reverse": self.reverse,
            "contours": [contour.to_dict() for contour in self.contours],
        }


@dataclass(frozen=True)
class Trace:
    trace_id: str
    endpoint: Point
    route: Tuple[Point, ...]
    times: Optional[Tuple[int, ...]] = None

    def __post_init__(self) -> None:
        text(self.trace_id, "trace_id")
        if not isinstance(self.endpoint, Point):
            raise SchemaError("trace endpoint must be a Point record")
        route = tuple(self.route)
        if not route:
            raise SchemaError("traces need at least one route point")
        if not all(isinstance(point, Point) for point in route):
            raise SchemaError("trace route points must be Point records")
        object.__setattr__(self, "route", route)
        if self.times is None:
            return
        times = tuple(self.times)
        if not all(is_int(value) for value in times):
            raise SchemaError("trace times must be whole epoch seconds")
        if len(times) != len(route):
            raise SchemaError("trace times must align with route positions")
        if any(later < earlier for earlier, later in zip(times, times[1:])):
            raise SchemaError("trace times must be non-decreasing")
        object.__setattr__(self, "times", times)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "endpoint": self.endpoint.to_dict(),
            "route": [point.to_dict() for point in self.route],
            "times": None if self.times is None else list(self.times),
        }


@dataclass(frozen=True)
class Preview:
    preview_id: str
    generation: int
    sequence: int
    route: Tuple[Point, ...]
    provisional: bool = True

    def __post_init__(self) -> None:
        text(self.preview_id, "preview_id")
        if not is_int(self.generation) or self.generation < 0:
            raise SchemaError("preview generation must be a non-negative integer")
        if not is_int(self.sequence) or self.sequence < 0:
            raise SchemaError("preview sequence must be a non-negative integer")
        if self.provisional is not True:
            raise SchemaError("previews must be provisional")
        route = tuple(self.route)
        if not all(isinstance(point, Point) for point in route):
            raise SchemaError("preview route points must be Point records")
        object.__setattr__(self, "route", route)

    def to_dict(self) -> dict:
        return {
            "preview_id": self.preview_id,
            "generation": self.generation,
            "sequence": self.sequence,
            "provisional": self.provisional,
            "route": [point.to_dict() for point in self.route],
        }
