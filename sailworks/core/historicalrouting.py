"""Pure planning and validation for historical-year ensemble routing.

A historical job replays one departure over the last ``years`` calendar years,
each at ``2 * offset_days + 1`` day offsets. This module resolves those members
into UTC instants, records calendar anomalies instead of raising, and decides
which whole years are eligible given the weather coverage that is available.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_YEARS = 1
MAX_YEARS = 30
DEFAULT_YEARS = 10

MIN_OFFSET_DAYS = 0
MAX_OFFSET_DAYS = 14
DEFAULT_OFFSET_DAYS = 3

MAX_MEMBERS = 256

MIN_HORIZON_HOURS = 6
MAX_HORIZON_HOURS = 720
DEFAULT_HORIZON_HOURS = 240

DEFAULT_REGION_PADDING_DEG = 5.0

HORIZON_STEP_HOURS = 6


class MemberState(str, Enum):
    PLANNED = "planned"
    WAITING_WEATHER = "waiting_weather"
    READY = "ready"
    RUNNING = "running"
    FINISHED = "finished"
    NO_FINISH_WITHIN_HORIZON = "no_finish_within_horizon"
    BLOCKED_WEATHER = "blocked_weather"
    FAILED_EXECUTION = "failed_execution"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class JobState(str, Enum):
    PLANNED = "planned"
    PREFLIGHT = "preflight"
    ACQUIRING = "acquiring"
    RUNNING = "running"
    COMPLETE = "complete"
    COMPLETE_WITH_EXCLUSIONS = "complete_with_exclusions"
    NEEDS_ATTENTION = "needs_attention"
    AWAITING_AVAILABILITY = "awaiting_availability"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    RECOVERING = "recovering"
    ARCHIVED = "archived"


class YearState(str, Enum):
    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


@dataclass(frozen=True)
class HistoricalRoutingSpec:
    job_id: str
    created_at_utc: datetime
    selected_departure_local: datetime
    timezone_id: str
    reference_year: int
    years: int
    offset_days: int
    max_duration_hours: int
    region: tuple[float, float, float, float]
    source_id: str = "ERA5"
    required_fields: tuple[str, ...] = ("wind",)
    backend: str = "native"
    requested_backend: str = "native"
    configuration_fingerprint: str = ""
    software_version: str = ""
    # The track this analysis belongs to, when the caller knows one. Empty for
    # records written before the field existed and for untracked analyses.
    track_name: str = ""


@dataclass(frozen=True)
class CalendarAdjustment:
    kind: str
    detail: str


@dataclass(frozen=True)
class HistoricalMemberPlan:
    """One planned member.

    ``intended_wall_datetime`` is naive local wall clock by design; the UTC
    fields are timezone-aware.
    """

    member_id: str
    year: int
    offset_days: int
    intended_wall_datetime: datetime
    resolved_departure_utc: datetime
    calendar_adjustments: tuple[CalendarAdjustment, ...]
    coverage_start_utc: datetime
    coverage_end_utc: datetime


@dataclass(frozen=True)
class YearPlan:
    year: int
    state: YearState
    excluded_reason: str = ""
    members: tuple[HistoricalMemberPlan, ...] = ()


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(value: datetime) -> datetime:
    floored = _floor_hour(value)
    if floored == value:
        return floored
    return floored + timedelta(hours=1)


def member_id(year: int, offset_days: int) -> str:
    """Stable identifier for one (year, offset) member within a job."""
    return f"{year:04d}-{offset_days:+03d}"


def validate_spec(spec: HistoricalRoutingSpec) -> list[str]:
    """Return human-readable problems with ``spec``; an empty list means valid."""
    errors: list[str] = []
    if not MIN_YEARS <= spec.years <= MAX_YEARS:
        errors.append(f"years must be between {MIN_YEARS} and {MAX_YEARS}.")
    if not MIN_OFFSET_DAYS <= spec.offset_days <= MAX_OFFSET_DAYS:
        errors.append(
            f"offset days must be between {MIN_OFFSET_DAYS} and {MAX_OFFSET_DAYS}."
        )
    count = spec.years * (2 * spec.offset_days + 1)
    if count > MAX_MEMBERS:
        errors.append(f"{count} members exceeds the {MAX_MEMBERS}-member limit.")
    if not MIN_HORIZON_HOURS <= spec.max_duration_hours <= MAX_HORIZON_HOURS:
        errors.append(
            f"max duration hours must be between {MIN_HORIZON_HOURS} and {MAX_HORIZON_HOURS}."
        )
    elif spec.max_duration_hours % HORIZON_STEP_HOURS != 0:
        errors.append(f"max duration hours must be a multiple of {HORIZON_STEP_HOURS}.")
    _validate_region(spec.region, errors)
    if spec.backend != "native":
        errors.append("historical routing requires the native backend.")
    if spec.created_at_utc.tzinfo is None:
        errors.append("created_at_utc must be timezone-aware.")
    if (
        spec.selected_departure_local.second
        or spec.selected_departure_local.microsecond
    ):
        errors.append("selected departure must have zero seconds and microseconds.")
    try:
        ZoneInfo(spec.timezone_id)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"unknown timezone: {spec.timezone_id}.")
    else:
        local_year = reference_year(spec.selected_departure_local, spec.timezone_id)
        if spec.reference_year != local_year:
            errors.append(
                f"reference year must be the selected departure's local year ({local_year})."
            )
    return errors


def _validate_region(
    region: tuple[float, float, float, float], errors: list[str]
) -> None:
    try:
        west, south, east, north = (float(value) for value in region)
    except (TypeError, ValueError):
        errors.append("region must be (west, south, east, north).")
        return
    if not west < east or not south < north:
        errors.append("region bounds must be ordered west < east and south < north.")
    if not (-180.0 <= west and east <= 180.0):
        errors.append("region longitudes must lie within [-180, 180].")
    if not (-90.0 <= south and north <= 90.0):
        errors.append("region latitudes must lie within [-90, 90].")


def reference_year(selected_departure_local: datetime, timezone_id: str) -> int:
    """Local calendar year of the selected departure, which anchors the ensemble."""
    zone = ZoneInfo(timezone_id)
    if selected_departure_local.tzinfo is None:
        return selected_departure_local.replace(tzinfo=zone).year
    return selected_departure_local.astimezone(zone).year


def resolve_local_departure(
    year: int, month: int, day: int, hour: int, minute: int, timezone_id: str
) -> tuple[datetime, tuple[CalendarAdjustment, ...]]:
    """Resolve a local wall clock to UTC, recording (never raising) calendar anomalies."""
    zone = ZoneInfo(timezone_id)
    adjustments: list[CalendarAdjustment] = []
    if (month, day) == (2, 29) and not _is_leap_year(year):
        adjustments.append(
            CalendarAdjustment(
                "leap_day",
                f"{year:04d}-02-29 does not exist; substituted {year:04d}-02-28.",
            )
        )
        day = 28
    wall = datetime(year, month, day, hour, minute)
    earlier = wall.replace(tzinfo=zone, fold=0)
    later = wall.replace(tzinfo=zone, fold=1)
    resolved = earlier.astimezone(timezone.utc)
    round_trip = resolved.astimezone(zone).replace(tzinfo=None)
    if round_trip != wall:
        adjustments.append(
            CalendarAdjustment(
                "dst_gap",
                f"{wall.isoformat()} does not exist in {timezone_id}; "
                f"shifted to {round_trip.isoformat()} ({resolved.isoformat()}).",
            )
        )
    elif earlier.utcoffset() != later.utcoffset():
        adjustments.append(
            CalendarAdjustment(
                "dst_fold",
                f"{wall.isoformat()} occurs twice in {timezone_id}; "
                f"using the earlier occurrence ({resolved.isoformat()}).",
            )
        )
    return resolved, tuple(adjustments)


def plan_members(spec: HistoricalRoutingSpec) -> tuple[HistoricalMemberPlan, ...]:
    """Expand the spec into members ordered by (year ascending, offset ascending)."""
    errors = validate_spec(spec)
    if errors:
        raise ValueError("; ".join(errors))
    base = spec.selected_departure_local
    if base.tzinfo is not None:
        base = base.astimezone(ZoneInfo(spec.timezone_id)).replace(tzinfo=None)
    february_29 = (base.month, base.day) == (2, 29)
    members: list[HistoricalMemberPlan] = []
    for year in range(spec.reference_year - spec.years + 1, spec.reference_year + 1):
        day = 28 if february_29 and not _is_leap_year(year) else base.day
        substitution = None
        if day != base.day:
            substitution = CalendarAdjustment(
                "leap_day",
                f"{year:04d}-02-29 does not exist; substituted {year:04d}-02-28.",
            )
        for offset in range(-spec.offset_days, spec.offset_days + 1):
            intended = datetime(
                year, base.month, day, base.hour, base.minute
            ) + timedelta(days=offset)
            resolved, adjustments = resolve_local_departure(
                intended.year,
                intended.month,
                intended.day,
                intended.hour,
                intended.minute,
                spec.timezone_id,
            )
            if substitution is not None and offset == 0:
                adjustments = (substitution,) + adjustments
            coverage_end = resolved + timedelta(hours=spec.max_duration_hours)
            members.append(
                HistoricalMemberPlan(
                    member_id=member_id(year, offset),
                    year=year,
                    offset_days=offset,
                    intended_wall_datetime=intended,
                    resolved_departure_utc=resolved,
                    calendar_adjustments=adjustments,
                    coverage_start_utc=_floor_hour(resolved),
                    coverage_end_utc=_ceil_hour(coverage_end),
                )
            )
    return tuple(members)


def plan_years(spec: HistoricalRoutingSpec, unavailable) -> tuple[YearPlan, ...]:
    """Group members by year and exclude a whole year if any member window is unavailable."""
    grouped: dict[int, list[HistoricalMemberPlan]] = {}
    for planned in plan_members(spec):
        grouped.setdefault(planned.year, []).append(planned)
    plans: list[YearPlan] = []
    for year in sorted(grouped):
        group = tuple(grouped[year])
        reason = ""
        for planned in group:
            detail = unavailable(planned.coverage_start_utc, planned.coverage_end_utc)
            if detail:
                reason = f"{planned.member_id}: {detail}"
                break
        state = YearState.EXCLUDED if reason else YearState.ELIGIBLE
        plans.append(
            YearPlan(year=year, state=state, excluded_reason=reason, members=group)
        )
    return tuple(plans)


def effective_plans(plans) -> tuple[YearPlan, ...]:
    """Return only the eligible year plans."""
    return tuple(plan for plan in plans if plan.state is YearState.ELIGIBLE)


def effective_members(plans) -> tuple[HistoricalMemberPlan, ...]:
    """Flatten the members of eligible years only."""
    return tuple(member for plan in effective_plans(plans) for member in plan.members)


def summarize_years(plans) -> tuple[int, int, int, int]:
    """Return (requested, effective, excluded, complete) year counts.

    ``effective = requested - excluded`` and may still include years later marked
    ``INCOMPLETE``; ``complete`` counts only ``YearState.COMPLETE``.
    """
    requested = len(plans)
    excluded = sum(1 for plan in plans if plan.state is YearState.EXCLUDED)
    complete = sum(1 for plan in plans if plan.state is YearState.COMPLETE)
    return requested, requested - excluded, excluded, complete
