"""Pure ERA5 planning: acquisition units, availability and coverage helpers.

Nothing here performs I/O. Given planned members this module deduplicates their
coverage into UTC-day acquisition units and says which years ERA5 cannot serve
yet. Binding identity lives in :mod:`sailworks.core.historicalbinding`.

Coverage is half-open: a window covers ``[start_utc, end_utc)``. Every datetime
must be timezone-aware UTC.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from .historicalrouting import HistoricalMemberPlan
from .historicalbinding import CurrentBinding, WeatherBindingDescriptor  # noqa: F401

ERA5_EARLIEST_UTC = datetime(1940, 1, 1, tzinfo=timezone.utc)
ERA5_LATENCY_HOURS = 120
MAX_UNIT_DAYS = 31
DEFAULT_REGION_PADDING_DEG = 5.0
DATELINE_EPSILON = 1e-9
DATELINE_SPAN_MESSAGE = (
    "region spans more than 180 degrees of longitude; use split_region_at_dateline."
)

GRID_SPACING_DEG = 0.25


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_utc(label: str, value) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime.")
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC.")


def _check_region(region) -> tuple:
    try:
        west, south, east, north = (float(value) for value in region)
    except (TypeError, ValueError):
        raise ValueError("region must be (west, south, east, north).") from None
    if not west < east or not south < north:
        raise ValueError("region bounds must be ordered west < east and south < north.")
    if not (-180.0 <= west and east <= 180.0):
        raise ValueError("region longitudes must lie within [-180, 180].")
    if not (-90.0 <= south and north <= 90.0):
        raise ValueError("region latitudes must lie within [-90, 90].")
    return (west, south, east, north)


def era5_area(region) -> tuple[float, float, float, float]:
    """Convert our ``(west, south, east, north)`` region to ERA5's area order.

    The CDS API takes ``(north, west, south, east)``; the rest of the routing
    stack speaks ``(west, south, east, north)``.  This is the only conversion
    point, so a caller that reaches ERA5 passes a region through it instead of
    reordering by hand.  Malformed input is rejected rather than silently
    reordered.
    """
    west, south, east, north = _check_region(region)
    return (north, west, south, east)


def region_from_era5_area(area) -> tuple[float, float, float, float]:
    """Convert an ERA5 ``(north, west, south, east)`` area to our region order.

    Inverse of :func:`era5_area`; the result uses ``(west, south, east,
    north)`` and is validated like any other region.  Bounds that do not form
    a north-over-south, west-before-east box raise instead of being reordered.
    """
    try:
        values = tuple(float(value) for value in area)
    except (TypeError, ValueError):
        raise ValueError("area must be (north, west, south, east).") from None
    if len(values) != 4:
        raise ValueError("area must be (north, west, south, east).")
    north, west, south, east = values
    if not (-90.0 <= south < north <= 90.0 and -180.0 <= west < east <= 180.0):
        raise ValueError("area must be ordered north > south and west < east within valid bounds.")
    return (west, south, east, north)


@dataclass(frozen=True)
class CoverageWindow:
    """A half-open UTC coverage span: ``[start_utc, end_utc)``."""

    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        _require_utc("start_utc", self.start_utc)
        _require_utc("end_utc", self.end_utc)
        if self.end_utc <= self.start_utc:
            raise ValueError("coverage end must be after its start.")

    def hours(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds() / 3600.0


@dataclass(frozen=True)
class AcquisitionUnit:
    """One deduplicated acquisition request, identified by its canonical payload."""

    unit_id: str
    start_utc: datetime
    end_utc: datetime
    utc_day: date
    region: tuple
    fields: tuple

    def __post_init__(self) -> None:
        _require_utc("start_utc", self.start_utc)
        _require_utc("end_utc", self.end_utc)
        if self.end_utc <= self.start_utc:
            raise ValueError("unit end must be after its start.")
        if self.utc_day != self.start_utc.astimezone(timezone.utc).date():
            raise ValueError("utc_day must be the start's UTC calendar day.")
        if not isinstance(self.unit_id, str) or len(self.unit_id) != 64:
            raise ValueError("unit_id must be a SHA-256 hex digest.")
        if any(character not in "0123456789abcdef" for character in self.unit_id):
            raise ValueError("unit_id must be lowercase hexadecimal.")

    @staticmethod
    def canonical_payload(start_utc: datetime, end_utc: datetime, region, fields) -> dict:
        """Deterministic payload: sorted keys, ISO-8601 Z times, 6 dp region."""
        return {
            "end_utc": _iso_z(end_utc),
            "fields": sorted(str(value) for value in fields),
            "region": [round(float(value), 6) for value in region],
            "start_utc": _iso_z(start_utc),
            "utc_day": start_utc.astimezone(timezone.utc).date().isoformat(),
        }

    @classmethod
    def create(
        cls,
        *,
        start_utc: datetime,
        end_utc: datetime,
        region,
        fields: Iterable[str] = ("wind",),
    ) -> "AcquisitionUnit":
        box = _check_region(region)
        names = tuple(str(value) for value in fields)
        payload = cls.canonical_payload(start_utc, end_utc, box, names)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return cls(
            unit_id=hashlib.sha256(canonical.encode("utf8")).hexdigest(),
            start_utc=start_utc,
            end_utc=end_utc,
            utc_day=start_utc.astimezone(timezone.utc).date(),
            region=tuple(round(value, 6) for value in box),
            fields=names,
        )

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "start_utc": _iso_z(self.start_utc),
            "end_utc": _iso_z(self.end_utc),
            "utc_day": self.utc_day.isoformat(),
            "region": list(self.region),
            "fields": list(self.fields),
        }

    def hours(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds() / 3600.0


@dataclass(frozen=True)
class YearExclusion:
    """Why a whole year was dropped from the ensemble."""

    year: int
    member_id: str
    offset_days: int
    reason: str


# ---------------------------------------------------------------------------
# coverage


def merge_coverage(windows: Iterable[CoverageWindow]) -> tuple:
    """Union overlapping and touching windows, ordered by start."""
    ordered = sorted(windows, key=lambda window: (window.start_utc, window.end_utc))
    merged: list = []
    for window in ordered:
        if not isinstance(window, CoverageWindow):
            raise TypeError("expected CoverageWindow")
        if merged and window.start_utc <= merged[-1].end_utc:
            last = merged[-1]
            if window.end_utc > last.end_utc:
                merged[-1] = CoverageWindow(last.start_utc, window.end_utc)
            continue
        merged.append(window)
    return tuple(merged)


def _next_midnight(moment: datetime) -> datetime:
    midnight = moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight <= moment:
        midnight += timedelta(days=1)
    return midnight


def split_unit_days(windows: Iterable[CoverageWindow], *, max_days: int = MAX_UNIT_DAYS) -> tuple:
    """Split coverage into pieces that never cross UTC midnight or exceed ``max_days``."""
    if max_days < 1:
        raise ValueError("max_days must be at least one.")
    pieces: list = []
    for window in merge_coverage(windows):
        cursor = window.start_utc
        limit = timedelta(days=max_days)
        while cursor < window.end_utc:
            piece_end = min(window.end_utc, _next_midnight(cursor), cursor + limit)
            if piece_end <= cursor:  # defensive: never emit a zero-length piece
                raise ValueError("coverage splitting stalled.")
            pieces.append((cursor, piece_end))
            cursor = piece_end
    return tuple(pieces)


def plan_acquisition_units(
    windows: Iterable[CoverageWindow],
    region,
    fields: Iterable[str] = ("wind",),
    *,
    max_days: int = MAX_UNIT_DAYS,
) -> tuple:
    """Deduplicated acquisition units for every window, sorted by start then id."""
    names = tuple(str(value) for value in fields)
    units: dict = {}
    for start, end in split_unit_days(windows, max_days=max_days):
        unit = AcquisitionUnit.create(start_utc=start, end_utc=end, region=region, fields=names)
        units.setdefault(unit.unit_id, unit)
    return tuple(sorted(units.values(), key=lambda unit: (unit.start_utc, unit.unit_id)))


# ---------------------------------------------------------------------------
# availability


def availability_reason(
    window: CoverageWindow,
    *,
    now: datetime,
    earliest: datetime = ERA5_EARLIEST_UTC,
    latency_hours: int = ERA5_LATENCY_HOURS,
) -> str | None:
    """Why ERA5 cannot serve ``window`` right now, or ``None`` when it can."""
    _require_utc("now", now)
    _require_utc("earliest", earliest)
    if latency_hours < 0:
        raise ValueError("latency_hours must be non-negative.")
    if window.start_utc < earliest:
        return (
            f"coverage starts {_iso_z(window.start_utc)}, before ERA5 begins at "
            f"{_iso_z(earliest)}."
        )
    cutoff = now - timedelta(hours=latency_hours)
    if window.end_utc > cutoff:
        return (
            f"coverage ends {_iso_z(window.end_utc)}, after the ERA5 availability "
            f"cutoff {_iso_z(cutoff)} at {latency_hours} h latency."
        )
    return None


def _ordered_members(members: Iterable[HistoricalMemberPlan]) -> list:
    return sorted(members, key=lambda member: (member.year, member.offset_days, member.member_id))


def _member_window(member: HistoricalMemberPlan) -> CoverageWindow:
    return CoverageWindow(member.coverage_start_utc, member.coverage_end_utc)


def plan_member_availability(
    members: Iterable[HistoricalMemberPlan],
    *,
    now: datetime,
    earliest: datetime = ERA5_EARLIEST_UTC,
    latency_hours: int = ERA5_LATENCY_HOURS,
) -> dict:
    """``member_id`` -> reason, for unavailable member windows only."""
    reasons: dict = {}
    for member in _ordered_members(members):
        reason = availability_reason(
            _member_window(member), now=now, earliest=earliest, latency_hours=latency_hours
        )
        if reason:
            reasons[member.member_id] = reason
    return reasons


def plan_year_exclusions(
    members: Iterable[HistoricalMemberPlan],
    *,
    now: datetime,
    earliest: datetime = ERA5_EARLIEST_UTC,
    latency_hours: int = ERA5_LATENCY_HOURS,
) -> tuple:
    """One exclusion per year, naming the first offending member by (year, offset)."""
    excluded: dict = {}
    for member in _ordered_members(members):
        if member.year in excluded:
            continue
        reason = availability_reason(
            _member_window(member), now=now, earliest=earliest, latency_hours=latency_hours
        )
        if reason:
            excluded[member.year] = YearExclusion(
                year=member.year,
                member_id=member.member_id,
                offset_days=member.offset_days,
                reason=reason,
            )
    return tuple(excluded[year] for year in sorted(excluded))


# ---------------------------------------------------------------------------
# regions


def region_from_points(
    points: Iterable[tuple], padding_deg: float = DEFAULT_REGION_PADDING_DEG
) -> tuple:
    """Waypoint envelope plus padding, clipped to valid latitude/longitude.

    A degenerate envelope is refused, as is a span over 180 degrees of longitude:
    those waypoints straddle the dateline and need :func:`split_region_at_dateline`.
    """
    latitudes: list = []
    longitudes: list = []
    for latitude, longitude in points:
        latitudes.append(float(latitude))
        longitudes.append(float(longitude))
    if not latitudes:
        raise ValueError("at least one point is required.")
    if not math.isfinite(padding_deg) or padding_deg < 0:
        raise ValueError("padding must be a finite, non-negative number of degrees.")
    west = max(-180.0, min(longitudes) - padding_deg)
    east = min(180.0, max(longitudes) + padding_deg)
    south = max(-90.0, min(latitudes) - padding_deg)
    north = min(90.0, max(latitudes) + padding_deg)
    if not (east > west and north > south):
        raise ValueError("waypoints produce a degenerate region; widen the padding.")
    if east - west > 180.0 + DATELINE_EPSILON:
        raise ValueError(
            "waypoints span more than 180 degrees of longitude; use split_region_at_dateline."
        )
    return (west, south, east, north)


def split_dateline(region) -> tuple:
    """Validate an explicit region; refuse a span over 180 degrees of longitude.

    An explicit box never wraps, so nothing is split here. Such a span usually
    means ``west > east`` was written for a dateline crossing: use
    :func:`split_region_at_dateline` instead.
    """
    box = _check_region(region)
    west, _south, east, _north = box
    if east - west > 180.0 + DATELINE_EPSILON:
        raise ValueError(DATELINE_SPAN_MESSAGE)
    return (box,)


def split_region_at_dateline(west: float, south: float, east: float, north: float) -> tuple:
    """Split a span that crosses 180 (``west > east``) into non-wrapping boxes.

    ``west <= east`` returns one box unchanged. Zero-width halves are dropped.
    """
    west, south, east, north = (float(value) for value in (west, south, east, north))
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise ValueError("region longitudes must lie within [-180, 180].")
    if not (-90.0 <= south and north <= 90.0):
        raise ValueError("region latitudes must lie within [-90, 90].")
    if not south < north:
        raise ValueError("region bounds must be ordered south < north.")
    if west <= east:
        if east - west > 180.0 + DATELINE_EPSILON:
            raise ValueError(DATELINE_SPAN_MESSAGE)
        return ((west, south, east, north),)
    boxes = []
    if 180.0 - west > DATELINE_EPSILON:
        boxes.append((west, south, 180.0, north))
    if east + 180.0 > DATELINE_EPSILON:
        boxes.append((-180.0, south, east, north))
    return tuple(boxes)


# ---------------------------------------------------------------------------
# members and volume


def units_for_members(
    members: Iterable[HistoricalMemberPlan],
    region,
    fields: Iterable[str] = ("wind",),
    *,
    max_days: int = MAX_UNIT_DAYS,
) -> dict:
    """``member_id`` -> ordered unit ids intersecting its window, from one shared set.

    Members touching a UTC day boundary point at the same unit ids.
    """
    planned = list(members)
    names = tuple(str(value) for value in fields)
    windows = [_member_window(member) for member in planned]
    units = plan_acquisition_units(windows, region, names, max_days=max_days)
    mapping: dict = {}
    for member in planned:
        mapping[member.member_id] = tuple(
            unit.unit_id
            for unit in units
            if unit.start_utc < member.coverage_end_utc and unit.end_utc > member.coverage_start_utc
        )
    return mapping


def estimate_decoded_bytes(
    units: Iterable[AcquisitionUnit],
    region,
    *,
    components: int = 2,
    bytes_per_value: int = 4,
) -> int:
    """Rough decoded size: hours x 0.25-degree cells x components x bytes.

    A planning estimate: rectangular region, no packing, no compression.
    """
    west, south, east, north = _check_region(region)
    if components < 1 or bytes_per_value < 1:
        raise ValueError("components and bytes_per_value must be positive.")
    cells = math.ceil((east - west) / GRID_SPACING_DEG - DATELINE_EPSILON) * math.ceil(
        (north - south) / GRID_SPACING_DEG - DATELINE_EPSILON
    )
    hours = sum(unit.hours() for unit in units)
    return int(round(hours * cells * components * bytes_per_value))
