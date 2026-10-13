"""Immutable marine datasets and explicit, station-scoped sampling results."""

import bisect
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Tuple


class Capability(str, Enum):
    WATER_LEVEL = "water_level"
    STATION_CURRENT = "station_current"
    PASSAGE_EVENTS = "passage_events"
    CURRENT_FIELD = "current_field"


def utc_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Marine data timestamps require a UTC offset")
    return value.astimezone(timezone.utc)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Station:
    id: str
    code: str
    name: str
    latitude: float
    longitude: float
    series_codes: Tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "series_codes", tuple(self.series_codes))
        if (
            not self.id
            or not self.name
            or not (-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180)
        ):
            raise ValueError("Invalid marine station metadata")

    @classmethod
    def from_json(cls, value):
        return cls(**dict(value, series_codes=tuple(value["series_codes"])))

    def to_json(self):
        return dict(self.__dict__, series_codes=list(self.series_codes))


@dataclass(frozen=True)
class Sample:
    time: datetime
    value: Optional[float]
    qc_flag: str
    qualifier: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "time", utc_time(self.time))
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("Non-finite marine sample")

    def to_json(self):
        return dict(self.__dict__, time=self.time.isoformat())


@dataclass(frozen=True)
class Series:
    code: str
    unit: str
    samples: Tuple[Sample, ...]
    source_code: str = ""

    def __post_init__(self):
        object.__setattr__(self, "samples", tuple(self.samples))
        if any(b.time <= a.time for a, b in zip(self.samples, self.samples[1:])):
            raise ValueError("Marine samples must have unique, increasing timestamps")

    def to_json(self):
        return dict(
            code=self.code,
            unit=self.unit,
            source_code=self.source_code,
            samples=[s.to_json() for s in self.samples],
        )

    @classmethod
    def from_json(cls, value):
        return cls(
            value["code"],
            value["unit"],
            tuple(Sample(**s) for s in value["samples"]),
            value.get("source_code", ""),
        )


@dataclass(frozen=True)
class Dataset:
    provider: str
    station: Station
    retrieved_at: datetime
    requested_start: datetime
    requested_end: datetime
    series: Tuple[Series, ...]
    metadata_json: str
    sources_json: str
    attribution: str
    licence_url: str
    schema_version: int = 1

    def __post_init__(self):
        object.__setattr__(self, "series", tuple(self.series))
        if self.schema_version != 1:
            raise ValueError("Unsupported marine dataset version")
        for field in ("retrieved_at", "requested_start", "requested_end"):
            object.__setattr__(self, field, utc_time(getattr(self, field)))
        if self.requested_start >= self.requested_end:
            raise ValueError("Invalid dataset time interval")
        if len({s.code for s in self.series}) != len(self.series):
            raise ValueError("Duplicate marine series")
        json.loads(self.metadata_json)
        json.loads(self.sources_json)

    @property
    def id(self):
        return hashlib.sha256(canonical_json(self.to_json()).encode()).hexdigest()

    def get_series(self, code):
        return next((s for s in self.series if s.code == code), None)

    @property
    def capabilities(self):
        codes = {s.code for s in self.series if s.samples}
        capabilities = []
        if "water_level" in codes:
            capabilities.append(Capability.WATER_LEVEL)
        if {"current_speed", "current_direction"}.issubset(codes):
            capabilities.append(Capability.STATION_CURRENT)
        if "current_events" in codes:
            capabilities.append(Capability.PASSAGE_EVENTS)
        return tuple(capabilities)

    def to_json(self):
        return dict(
            self.__dict__,
            station=self.station.to_json(),
            retrieved_at=self.retrieved_at.isoformat(),
            requested_start=self.requested_start.isoformat(),
            requested_end=self.requested_end.isoformat(),
            series=[s.to_json() for s in self.series],
        )

    @classmethod
    def from_json(cls, value):
        return cls(
            **dict(
                value,
                station=Station.from_json(value["station"]),
                series=tuple(Series.from_json(s) for s in value["series"]),
            )
        )

    def manifest(self):
        return dict(
            dataset_id=self.id,
            provider=self.provider,
            station=self.station.to_json(),
            retrieved_at=self.retrieved_at.isoformat(),
            requested_start=self.requested_start.isoformat(),
            requested_end=self.requested_end.isoformat(),
            capabilities=[c.value for c in self.capabilities],
            attribution=self.attribution,
            licence_url=self.licence_url,
            sources=json.loads(self.sources_json),
            metadata=json.loads(self.metadata_json),
        )


@dataclass(frozen=True)
class QueryResult:
    status: str
    dataset_id: Optional[str] = None
    values: Tuple[float, ...] = ()
    qc_flags: Tuple[str, ...] = ()
    detail: str = ""


def bracket(series, instant, max_gap_seconds):
    """No extrapolation, no bridging null/suspect samples or large time gaps."""
    if series is None or not series.samples:
        return None
    times = [s.time for s in series.samples]
    index = bisect.bisect_left(times, instant)
    if index < len(times) and times[index] == instant:
        left = right = series.samples[index]
    elif index == 0 or index == len(times):
        return None
    else:
        left, right = series.samples[index - 1 : index + 1]
    if (right.time - left.time).total_seconds() > max_gap_seconds:
        return None
    if any(s.value is None or s.qc_flag not in ("1", "2") for s in (left, right)):
        return None
    return left, right


def sample_station(dataset, capability, instant, max_gap_seconds=1800):  # noqa: C901
    """Sample a specifically chosen station; never a spatial current field.

    Values are metres for levels and east/north metres per second for currents.
    QC flag 2 predictions remain explicitly marked as unevaluated.
    """
    instant = utc_time(instant)
    if not dataset.requested_start <= instant <= dataset.requested_end:
        return QueryResult("outside_time", detail="Outside downloaded interval")
    codes = {
        Capability.WATER_LEVEL: ("water_level",),
        Capability.STATION_CURRENT: ("current_speed", "current_direction"),
    }
    if capability not in codes:
        return QueryResult(
            "unsupported", detail="This capability has no scalar station sampler"
        )
    required = [dataset.get_series(code) for code in codes[capability]]
    if any(series is None for series in required):
        return QueryResult("missing_series")
    if capability == Capability.WATER_LEVEL:
        if required[0].unit != "m":
            return QueryResult("unsupported_unit")
    else:
        if required[0].unit != "kn" or required[1].unit != "deg":
            return QueryResult("unsupported_unit")
        # Equal clocks are required before constructing paired current vectors.
        if tuple(s.time for s in required[0].samples) != tuple(
            s.time for s in required[1].samples
        ):
            return QueryResult("unpaired_components")
    pairs = [bracket(series, instant, max_gap_seconds) for series in required]
    if any(pair is None for pair in pairs):
        return QueryResult(
            "missing_samples", detail="Gap, invalid QC, null value or no time coverage"
        )
    flags = tuple(sorted({s.qc_flag for pair in pairs for s in pair}))
    left, right = pairs[0]
    duration = (right.time - left.time).total_seconds()
    weight = (instant - left.time).total_seconds() / duration if duration else 0
    if capability == Capability.WATER_LEVEL:
        values = (left.value + (right.value - left.value) * weight,)
    else:
        vectors = []
        for speed, direction in zip(pairs[0], pairs[1]):
            if speed.value < 0 or not 0 <= direction.value <= 360:
                return QueryResult("invalid_value")
            radians = math.radians(direction.value)
            magnitude = speed.value * 1852 / 3600
            vectors.append(
                (magnitude * math.sin(radians), magnitude * math.cos(radians))
            )
        values = tuple(a + (b - a) * weight for a, b in zip(*vectors))
    return QueryResult("available", dataset.id, values, flags)
