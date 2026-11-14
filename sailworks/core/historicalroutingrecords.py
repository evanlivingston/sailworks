"""Durable record types for historical routing jobs.

Pure data: JSON-only round-tripping with no filesystem access, so a record can
be serialised, inspected or version-checked independently of the store.
"""

from dataclasses import dataclass, field

__all__ = [
    "SCHEMA_VERSION",
    "AcquisitionRecord",
    "MemberRecord",
    "YearRecord",
    "JobRecord",
    "computation_key",
    "str_tuple",
]


SCHEMA_VERSION = 1


def _strings(values) -> list:
    return [str(value) for value in (values or ())]


def _ints(values) -> list:
    return [int(value) for value in (values or ())]


def str_tuple(values) -> tuple:
    """JSON round-trip helper: a tuple of strings, treating ``None`` as empty."""
    return tuple(str(value) for value in (values or ()))


def _int_tuple(values) -> tuple:
    return tuple(int(value) for value in (values or ()))


def _dicts(values) -> list:
    return [dict(value) for value in (values or ())]


def computation_key(job_id: str, member_id: str, generation: int) -> str:
    """Stable publication identity for one member computation generation."""
    return f"{job_id}:{member_id}:{generation}"


@dataclass
class AcquisitionRecord:
    """One acquisition unit's durable state."""

    unit_id: str
    state: str
    start_utc: str
    end_utc: str
    artifact_hashes: tuple = ()
    error: str = ""
    attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "state": self.state,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "artifact_hashes": _strings(self.artifact_hashes),
            "error": self.error,
            "attempts": int(self.attempts),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AcquisitionRecord":
        return cls(
            unit_id=str(data["unit_id"]),
            state=str(data["state"]),
            start_utc=str(data["start_utc"]),
            end_utc=str(data["end_utc"]),
            artifact_hashes=str_tuple(data.get("artifact_hashes")),
            error=str(data.get("error", "")),
            attempts=int(data.get("attempts", 0)),
        )


@dataclass
class MemberRecord:
    """One planned member's durable state and its publication binding."""

    member_id: str
    year: int
    offset_days: int
    state: str
    departure_utc: str
    coverage_start_utc: str
    coverage_end_utc: str
    acquisition_unit_ids: tuple = ()
    binding_digest: str = ""
    route_uid: str = ""
    outcome: str = ""
    error: str = ""
    attempts: int = 0
    computation_generation: int = 1

    def to_dict(self) -> dict:
        return {
            "member_id": self.member_id,
            "year": int(self.year),
            "offset_days": int(self.offset_days),
            "state": self.state,
            "departure_utc": self.departure_utc,
            "coverage_start_utc": self.coverage_start_utc,
            "coverage_end_utc": self.coverage_end_utc,
            "acquisition_unit_ids": _strings(self.acquisition_unit_ids),
            "binding_digest": self.binding_digest,
            "route_uid": self.route_uid,
            "outcome": self.outcome,
            "error": self.error,
            "attempts": int(self.attempts),
            "computation_generation": int(self.computation_generation),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemberRecord":
        return cls(
            member_id=str(data["member_id"]),
            year=int(data["year"]),
            offset_days=int(data["offset_days"]),
            state=str(data["state"]),
            departure_utc=str(data["departure_utc"]),
            coverage_start_utc=str(data["coverage_start_utc"]),
            coverage_end_utc=str(data["coverage_end_utc"]),
            acquisition_unit_ids=str_tuple(data.get("acquisition_unit_ids")),
            binding_digest=str(data.get("binding_digest", "")),
            route_uid=str(data.get("route_uid", "")),
            outcome=str(data.get("outcome", "")),
            error=str(data.get("error", "")),
            attempts=int(data.get("attempts", 0)),
            computation_generation=int(data.get("computation_generation", 1)),
        )


@dataclass
class YearRecord:
    """Year-level eligibility, including why a previously eligible year changed."""

    year: int
    state: str
    reason: str = ""
    previous_reasons: tuple = ()

    def to_dict(self) -> dict:
        return {
            "year": int(self.year),
            "state": self.state,
            "reason": self.reason,
            "previous_reasons": _strings(self.previous_reasons),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "YearRecord":
        return cls(
            year=int(data["year"]),
            state=str(data["state"]),
            reason=str(data.get("reason", "")),
            previous_reasons=str_tuple(data.get("previous_reasons")),
        )


@dataclass
class JobRecord:
    """The complete durable state of one historical job."""

    schema_version: int
    job_id: str
    created_at_utc: str
    spec: dict
    state: str
    requested_years: tuple = ()
    effective_years: tuple = ()
    excluded_years: tuple = ()
    members: dict = field(default_factory=dict)
    acquisitions: dict = field(default_factory=dict)
    years: dict = field(default_factory=dict)
    # The current snapshot this job sampled: binding ids, hashes and coverage.
    # Older records without the key load as ``{}``.
    current_binding: dict = field(default_factory=dict)
    published: dict = field(default_factory=dict)
    pending_publications: dict = field(default_factory=dict)
    artifact_pins: tuple = ()
    attempt_history: tuple = ()
    last_checkpoint_utc: str = ""
    supersedes_job_id: str = ""
    scene_group_id: str = ""
    scene_detached: bool = False

    def to_dict(self) -> dict:
        return {
            "schema_version": int(self.schema_version),
            "job_id": self.job_id,
            "created_at_utc": self.created_at_utc,
            "spec": dict(self.spec),
            "state": self.state,
            "requested_years": _ints(self.requested_years),
            "effective_years": _ints(self.effective_years),
            "excluded_years": _ints(self.excluded_years),
            "members": {key: value.to_dict() for key, value in self.members.items()},
            "acquisitions": {key: value.to_dict() for key, value in self.acquisitions.items()},
            "years": {str(key): value.to_dict() for key, value in self.years.items()},
            "current_binding": dict(self.current_binding),
            "published": dict(self.published),
            "pending_publications": dict(self.pending_publications),
            "artifact_pins": _strings(self.artifact_pins),
            "attempt_history": _dicts(self.attempt_history),
            "last_checkpoint_utc": self.last_checkpoint_utc,
            "supersedes_job_id": self.supersedes_job_id,
            "scene_group_id": self.scene_group_id,
            "scene_detached": bool(self.scene_detached),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobRecord":
        members = {
            str(key): MemberRecord.from_dict(value)
            for key, value in dict(data.get("members", {})).items()
        }
        acquisitions = {
            str(key): AcquisitionRecord.from_dict(value)
            for key, value in dict(data.get("acquisitions", {})).items()
        }
        years = {
            int(key): YearRecord.from_dict(value)
            for key, value in dict(data.get("years", {})).items()
        }
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            job_id=str(data["job_id"]),
            created_at_utc=str(data["created_at_utc"]),
            spec=dict(data.get("spec", {})),
            state=str(data["state"]),
            requested_years=_int_tuple(data.get("requested_years")),
            effective_years=_int_tuple(data.get("effective_years")),
            excluded_years=_int_tuple(data.get("excluded_years")),
            members=members,
            acquisitions=acquisitions,
            years=years,
            current_binding=dict(data.get("current_binding") or {}),
            published={
                str(key): str(value) for key, value in dict(data.get("published", {})).items()
            },
            pending_publications={
                str(key): str(value)
                for key, value in dict(data.get("pending_publications", {})).items()
            },
            artifact_pins=str_tuple(data.get("artifact_pins")),
            attempt_history=tuple(_dicts(data.get("attempt_history"))),
            last_checkpoint_utc=str(data.get("last_checkpoint_utc", "")),
            supersedes_job_id=str(data.get("supersedes_job_id", "")),
            scene_group_id=str(data.get("scene_group_id", "")),
            scene_detached=bool(data.get("scene_detached", False)),
        )
