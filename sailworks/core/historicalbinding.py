"""Immutable two-source binding identity for historical weather.

A binding states exactly which wind artifacts, region and coverage a member's
route was computed against, plus whether a currents field joined it. The digest
is the only thing a route or publication record needs to store.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = ["CurrentBinding", "WeatherBindingDescriptor"]


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_utc(label: str, value) -> None:
    offset = value.utcoffset() if isinstance(value, datetime) else None
    if not isinstance(value, datetime) or value.tzinfo is None or offset != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC.")


def _parse_iso(text: str) -> datetime:
    """Parse an ISO-8601 instant, normalising any explicit offset to UTC.

    A naive string is ambiguous and therefore rejected; ``+05:00`` is accepted
    and converted, so comparisons and digests only ever see UTC.
    """
    value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{text!r} must carry an explicit UTC offset.")
    value = value.astimezone(timezone.utc)
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{text!r} did not normalise to UTC.")
    return value


@dataclass(frozen=True)
class CurrentBinding:
    """The currents half of a two-source binding; disabled means wind only."""

    enabled: bool
    target_departure_utc: datetime | None
    coverage_end_utc: datetime | None
    priority: str = "finest"
    field_ids: tuple = ()
    # For currents these are content-addressed CurrentField ids, not ERA5 GRIB digests.
    artifact_hashes: tuple = ()
    provider: str = ""
    hold_policy: str = "final_spatial_field"

    def __post_init__(self) -> None:
        if self.target_departure_utc is not None:
            _require_utc("target_departure_utc", self.target_departure_utc)
        if self.coverage_end_utc is not None:
            _require_utc("coverage_end_utc", self.coverage_end_utc)

    def to_dict(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "target_departure_utc": (
                None if self.target_departure_utc is None else _iso_z(self.target_departure_utc)
            ),
            "coverage_end_utc": (
                None if self.coverage_end_utc is None else _iso_z(self.coverage_end_utc)
            ),
            "priority": self.priority,
            "field_ids": [str(value) for value in self.field_ids],
            "artifact_hashes": [str(value) for value in self.artifact_hashes],
            "provider": self.provider,
            "hold_policy": self.hold_policy,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CurrentBinding":
        target = data.get("target_departure_utc")
        coverage = data.get("coverage_end_utc")
        return cls(
            enabled=bool(data.get("enabled", False)),
            target_departure_utc=None if target is None else _parse_iso(target),
            coverage_end_utc=None if coverage is None else _parse_iso(coverage),
            priority=str(data.get("priority", "finest")),
            field_ids=tuple(str(value) for value in data.get("field_ids", ())),
            artifact_hashes=tuple(str(value) for value in data.get("artifact_hashes", ())),
            provider=str(data.get("provider", "")),
            hold_policy=str(data.get("hold_policy", "final_spatial_field")),
        )


@dataclass(frozen=True)
class WeatherBindingDescriptor:
    """What a member's route was actually computed against; hashed, not mutable."""

    binding_version: int
    source_id: str
    wind_artifact_hashes: tuple
    wind_coverage_start_utc: datetime
    wind_coverage_end_utc: datetime
    wind_region: tuple
    member_departure_utc: datetime
    currents: CurrentBinding
    max_duration_hours: int
    sampler_version: str = "1"

    def __post_init__(self) -> None:
        _require_utc("wind_coverage_start_utc", self.wind_coverage_start_utc)
        _require_utc("wind_coverage_end_utc", self.wind_coverage_end_utc)
        _require_utc("member_departure_utc", self.member_departure_utc)
        if self.wind_coverage_start_utc >= self.wind_coverage_end_utc:
            raise ValueError("wind coverage must start before it ends.")
        if len(self.wind_region) != 4:
            raise ValueError("wind_region must be (west, south, east, north).")

    def to_dict(self) -> dict:
        return {
            "binding_version": int(self.binding_version),
            "source_id": self.source_id,
            "wind_artifact_hashes": [str(value) for value in self.wind_artifact_hashes],
            "wind_coverage_start_utc": _iso_z(self.wind_coverage_start_utc),
            "wind_coverage_end_utc": _iso_z(self.wind_coverage_end_utc),
            "wind_region": [float(value) for value in self.wind_region],
            "member_departure_utc": _iso_z(self.member_departure_utc),
            "currents": self.currents.to_dict(),
            "max_duration_hours": int(self.max_duration_hours),
            "sampler_version": self.sampler_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WeatherBindingDescriptor":
        return cls(
            binding_version=int(data["binding_version"]),
            source_id=str(data["source_id"]),
            wind_artifact_hashes=tuple(
                str(value) for value in data.get("wind_artifact_hashes", ())
            ),
            wind_coverage_start_utc=_parse_iso(data["wind_coverage_start_utc"]),
            wind_coverage_end_utc=_parse_iso(data["wind_coverage_end_utc"]),
            wind_region=tuple(float(value) for value in data["wind_region"]),
            member_departure_utc=_parse_iso(data["member_departure_utc"]),
            currents=CurrentBinding.from_dict(data.get("currents", {})),
            max_duration_hours=int(data["max_duration_hours"]),
            sampler_version=str(data.get("sampler_version", "1")),
        )

    def digest(self) -> str:
        """SHA-256 over the canonical binding payload."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf8")).hexdigest()
