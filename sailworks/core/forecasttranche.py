"""Immutable, versioned forecast refresh contracts; not semantic GRIB validation."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re

VERSION = 1
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIELDS = {
    "wind",
    "pressure",
    "gust",
    "wave_height",
    "wave_period",
    "wave_direction",
}
# Importing DownloadOptions here would execute gribsources/__init__.py, which
# imports every network provider. Keep its small pure validation contract local.


def _id(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _sequence(value, label):
    if isinstance(value, (str, bytes, dict)) or not isinstance(value, (tuple, list)):
        raise ValueError(f"{label} must be a sequence")
    return tuple(value)


def _region(value):
    region = _sequence(value, "region")
    if len(region) != 4 or any(
        type(x) not in (int, float) or not math.isfinite(x) for x in region
    ):
        raise ValueError("region must contain four finite bounds")
    south, west, north, east = region
    if not (
        -90 <= south < north <= 90
        and -180 <= west <= 180
        and -180 <= east <= 180
        and west != east
        and (west, east) != (180, -180)
    ):
        raise ValueError("invalid geographic region")
    return tuple(float(x) for x in region)


def _lon_segments(west, east):
    if (west, east) == (-180, 180):
        return ((0.0, 360.0),)
    start, end = (west + 180) % 360, (east + 180) % 360
    return ((start, end),) if start < end else ((start, 360.0), (0.0, end))


def _contains(actual, requested):
    south, west, north, east = actual
    rsouth, rwest, rnorth, reast = requested
    if south > rsouth or north < rnorth:
        return False
    actual_segments = _lon_segments(west, east)
    return all(
        any(a <= r0 and b >= r1 for a, b in actual_segments)
        for r0, r1 in _lon_segments(rwest, reast)
    )


def shared_requested_region(configurations):
    """Return the canonical common request area, rejecting per-model divergence."""
    configurations = tuple(configurations)
    if not configurations:
        raise ValueError("a shared requested region requires model configurations")
    region = configurations[0].region
    if any(configuration.region != region for configuration in configurations[1:]):
        raise ValueError("All configured models must share the same requested region")
    return region


def _times(values, label):
    result = []
    for value in _sequence(values, label):
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError(f"{label} must be timezone-aware")
        result.append(value.astimezone(timezone.utc))
    return tuple(result)


def _strings(values, label, nonempty=True):
    result = _sequence(values, label)
    if any(not isinstance(x, str) or not x for x in result) or (
        nonempty and not result
    ):
        raise ValueError(f"invalid {label}")
    if len(set(result)) != len(result):
        raise ValueError(f"duplicate {label}")
    return result


@dataclass(frozen=True)
class ModelConfiguration:
    model_id: str
    fields: tuple[str, ...]
    region: tuple[float, float, float, float]
    resolution: str
    forecast_hours: int
    steps: tuple[int, ...] = ()
    interval: int = 3

    def __post_init__(self):
        _id(self.model_id, "model id")
        fields = _strings(self.fields, "fields")
        if set(fields) - _FIELDS:
            raise ValueError("unsupported forecast field")
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "region", _region(self.region))
        if not isinstance(self.resolution, str) or not self.resolution:
            raise ValueError("resolution must be explicit")
        if type(self.forecast_hours) is not int or not 0 <= self.forecast_hours <= 384:
            raise ValueError("forecast hours must be in 0..384")
        if type(self.interval) is not int or self.interval not in (0, 1, 3, 6, 12):
            raise ValueError("unsupported forecast interval")
        steps = _sequence(self.steps, "steps")
        if any(type(x) is not int or x < 0 or x > self.forecast_hours for x in steps):
            raise ValueError("steps must be integer hours within forecast horizon")
        if tuple(sorted(set(steps))) != steps or (
            self.interval and any(x % self.interval for x in steps)
        ):
            raise ValueError("steps must be unique, sorted and interval-compatible")
        object.__setattr__(self, "steps", steps)

    def to_json(self):
        return {
            "model_id": self.model_id,
            "fields": list(self.fields),
            "region": list(self.region),
            "resolution": self.resolution,
            "forecast_hours": self.forecast_hours,
            "steps": list(self.steps),
            "interval": self.interval,
        }

    @classmethod
    def from_json(cls, data):
        return cls(
            data["model_id"],
            data["fields"],
            data["region"],
            data["resolution"],
            data["forecast_hours"],
            data.get("steps", ()),
            data.get("interval", 3),
        )


@dataclass(frozen=True)
class Artifact:
    path: str
    sha256: str
    size: int

    def __post_init__(self):
        if not isinstance(self.path, str) or not re.fullmatch(
            r"artifacts/[0-9a-f]{64}", self.path
        ):
            raise ValueError("artifact must use canonical content-addressed path")
        if (
            type(self.size) is not int
            or self.size < 0
            or self.sha256 != self.path.split("/")[-1]
        ):
            raise ValueError("invalid artifact digest or size")

    def to_json(self):
        return {"path": self.path, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_json(cls, data):
        return cls(data["path"], data["sha256"], data["size"])


@dataclass(frozen=True)
class ModelEntry:
    entry_id: str
    model_id: str
    run_time: datetime
    member: str
    fields: tuple[str, ...]
    levels: tuple[str, ...]
    region: tuple[float, float, float, float]
    resolution: str
    time_steps: tuple[datetime, ...]
    artifacts: tuple[Artifact, ...]
    acquired_at: datetime
    field_times: tuple[tuple[str, tuple[datetime, ...]], ...] = ()

    def __post_init__(self):
        _id(self.entry_id, "entry id")
        _id(self.model_id, "model id")
        if not isinstance(self.member, str) or not self.member:
            raise ValueError("member must be non-empty")
        if not isinstance(self.resolution, str) or not self.resolution:
            raise ValueError("resolution must be explicit")
        run = _times((self.run_time,), "run time")[0]
        object.__setattr__(self, "run_time", run)
        acquired = _times((self.acquired_at,), "acquisition time")[0]
        if acquired < run:
            raise ValueError("acquisition time cannot precede model run")
        object.__setattr__(self, "acquired_at", acquired)
        object.__setattr__(self, "fields", _strings(self.fields, "fields"))
        if set(self.fields) - _FIELDS:
            raise ValueError("unsupported forecast field")
        object.__setattr__(self, "levels", _strings(self.levels, "levels"))
        object.__setattr__(self, "region", _region(self.region))
        times = _times(self.time_steps, "time steps")
        if (
            not times
            or tuple(sorted(set(times))) != times
            or any(t < run for t in times)
        ):
            raise ValueError(
                "time steps must be unique, increasing and not precede run"
            )
        object.__setattr__(self, "time_steps", times)
        artifacts = _sequence(self.artifacts, "artifacts")
        if (
            not artifacts
            or any(not isinstance(x, Artifact) for x in artifacts)
            or len({artifact.path for artifact in artifacts}) != len(artifacts)
        ):
            raise ValueError("artifacts must be non-empty, unique Artifact values")
        object.__setattr__(self, "artifacts", artifacts)
        field_times = _sequence(self.field_times, "field times")
        if field_times:
            normalized = []
            names = []
            for item in field_times:
                pair = _sequence(item, "field time entry")
                if len(pair) != 2:
                    raise ValueError("field time entry must be a field and times")
                name = pair[0]
                if not isinstance(name, str) or not name:
                    raise ValueError("invalid field time name")
                times = _times(pair[1], "field times")
                if not times or tuple(sorted(set(times))) != times:
                    raise ValueError("field times must be non-empty, unique and sorted")
                if any(time not in self.time_steps for time in times):
                    raise ValueError("field times must be part of entry coverage")
                names.append(name)
                normalized.append((name, times))
            if set(names) != set(self.fields) or len(names) != len(set(names)):
                raise ValueError(
                    "field times must cover each declared field exactly once"
                )
            if set(time for _, times in normalized for time in times) != set(
                self.time_steps
            ):
                raise ValueError("field times union must equal entry coverage")
            object.__setattr__(self, "field_times", tuple(normalized))

    def to_json(self):
        return {
            "entry_id": self.entry_id,
            "model_id": self.model_id,
            "run_time": self.run_time.isoformat(),
            "acquired_at": self.acquired_at.isoformat(),
            "member": self.member,
            "fields": list(self.fields),
            "levels": list(self.levels),
            "region": list(self.region),
            "resolution": self.resolution,
            "time_steps": [t.isoformat() for t in self.time_steps],
            "artifacts": [a.to_json() for a in self.artifacts],
            "field_times": [
                [field, [time.isoformat() for time in times]]
                for field, times in self.field_times
            ],
        }

    @classmethod
    def from_json(cls, d):
        return cls(
            d["entry_id"],
            d["model_id"],
            datetime.fromisoformat(d["run_time"]),
            d["member"],
            d["fields"],
            d["levels"],
            d["region"],
            d["resolution"],
            tuple(datetime.fromisoformat(t) for t in d["time_steps"]),
            tuple(Artifact.from_json(a) for a in d["artifacts"]),
            datetime.fromisoformat(d["acquired_at"]),
            tuple(
                (field, tuple(datetime.fromisoformat(time) for time in times))
                for field, times in d.get("field_times", ())
            ),
        )


@dataclass(frozen=True)
class ForecastTranche:
    tranche_id: str
    refreshed_at: datetime
    configurations: tuple[ModelConfiguration, ...]
    entries: tuple[ModelEntry, ...]
    version: int = VERSION

    def __post_init__(self):
        _id(self.tranche_id, "tranche id")
        if type(self.version) is not int or self.version != VERSION:
            raise ValueError("unsupported tranche version")
        object.__setattr__(
            self, "refreshed_at", _times((self.refreshed_at,), "refresh time")[0]
        )
        configs, entries = _sequence(self.configurations, "configurations"), _sequence(
            self.entries, "entries"
        )
        if not configs or any(not isinstance(c, ModelConfiguration) for c in configs):
            raise ValueError(
                "configurations must be non-empty ModelConfiguration values"
            )
        shared_requested_region(configs)
        if not entries or any(not isinstance(e, ModelEntry) for e in entries):
            raise ValueError("entries must be non-empty ModelEntry values")
        if len({c.model_id for c in configs}) != len(configs) or len(
            {e.entry_id for e in entries}
        ) != len(entries):
            raise ValueError("duplicate configuration or entry")
        if len({e.model_id for e in entries}) != len(entries) or {
            c.model_id for c in configs
        } != {e.model_id for e in entries}:
            raise ValueError("each configured model must have exactly one entry")
        by_model = {c.model_id: c for c in configs}
        for entry in entries:
            if entry.acquired_at > self.refreshed_at:
                raise ValueError("acquisition time cannot follow tranche refresh time")
            config = by_model[entry.model_id]
            if not set(config.fields) <= set(entry.fields):
                raise ValueError("entry omits requested fields")
            if not _contains(entry.region, config.region):
                raise ValueError("actual region does not cover requested region")
            if config.resolution != "native" and entry.resolution != config.resolution:
                raise ValueError("effective resolution differs from fixed request")
            if entry.resolution == "native":
                raise ValueError("effective resolution must be concrete")
            if any(
                t > entry.run_time + timedelta(hours=config.forecast_hours)
                for t in entry.time_steps
            ):
                raise ValueError("entry exceeds requested forecast horizon")
            if config.steps and not set(config.steps) <= {
                (t - entry.run_time).total_seconds() / 3600 for t in entry.time_steps
            }:
                raise ValueError("entry omits requested steps")
        object.__setattr__(self, "configurations", configs)
        object.__setattr__(self, "entries", entries)

    @property
    def region(self):
        return shared_requested_region(self.configurations)

    def to_json(self):
        return {
            "version": self.version,
            "tranche_id": self.tranche_id,
            "refreshed_at": self.refreshed_at.isoformat(),
            "configurations": [c.to_json() for c in self.configurations],
            "entries": [e.to_json() for e in self.entries],
        }

    @classmethod
    def from_json(cls, d):
        if type(d.get("version")) is not int or d["version"] != VERSION:
            raise ValueError("unsupported tranche version")
        return cls(
            d["tranche_id"],
            datetime.fromisoformat(d["refreshed_at"]),
            tuple(ModelConfiguration.from_json(c) for c in d["configurations"]),
            tuple(ModelEntry.from_json(e) for e in d["entries"]),
            d["version"],
        )


@dataclass(frozen=True)
class AnalysisSelection:
    tranche_id: str
    entry_id: str

    def __post_init__(self):
        _id(self.tranche_id, "tranche id")
        _id(self.entry_id, "entry id")

    def to_json(self):
        return {"tranche_id": self.tranche_id, "entry_id": self.entry_id}

    @classmethod
    def from_json(cls, d):
        return cls(d["tranche_id"], d["entry_id"])
