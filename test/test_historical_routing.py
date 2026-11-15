"""Historical-year ensemble planning: calendar semantics and validation."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError

import pytest

from sailworks.core.historicalrouting import (
    CalendarAdjustment,
    HistoricalRoutingSpec,
    YearPlan,
    YearState,
    effective_members,
    effective_plans,
    member_id,
    plan_members,
    plan_years,
    reference_year,
    resolve_local_departure,
    summarize_years,
    validate_spec,
)

UTC = timezone.utc


def make_spec(**overrides) -> HistoricalRoutingSpec:
    values = {
        "job_id": "job-1",
        "created_at_utc": datetime(2026, 1, 1, tzinfo=UTC),
        "selected_departure_local": datetime(2019, 6, 1, 12, 0),
        "timezone_id": "UTC",
        "reference_year": 2019,
        "years": 10,
        "offset_days": 3,
        "max_duration_hours": 240,
        "region": (-10.0, 40.0, 5.0, 50.0),
    }
    values.update(overrides)
    return HistoricalRoutingSpec(**values)


def adjustments_of_kind(member, kind):
    return [item for item in member.calendar_adjustments if item.kind == kind]


def test_member_count_order_and_ids_are_deterministic():
    spec = make_spec(years=3, offset_days=2)
    members = plan_members(spec)
    assert len(members) == 3 * (2 * 2 + 1)
    assert members == plan_members(spec)
    assert [item.member_id for item in members] == [item.member_id for item in plan_members(spec)]
    assert len({item.member_id for item in members}) == len(members)
    pairs = [(item.year, item.offset_days) for item in members]
    assert pairs == sorted(pairs)
    assert {item.offset_days for item in members} == {-2, -1, 0, 1, 2}


def test_reference_year_comes_from_the_selected_departure():
    spec = make_spec(selected_departure_local=datetime(2019, 6, 1, 12, 0), years=10)
    assert reference_year(spec.selected_departure_local, spec.timezone_id) == 2019
    members = plan_members(make_spec(offset_days=0))
    assert sorted({item.year for item in members}) == list(range(2010, 2020))


def test_offsets_are_calendar_days_across_a_month_boundary():
    spec = make_spec(
        selected_departure_local=datetime(2020, 1, 1, 12, 0),
        reference_year=2020,
        years=1,
        offset_days=1,
    )
    members = {item.offset_days: item for item in plan_members(spec)}
    assert members[-1].intended_wall_datetime == datetime(2019, 12, 31, 12, 0)
    assert members[-1].resolved_departure_utc == datetime(2019, 12, 31, 12, 0, tzinfo=UTC)
    assert members[1].intended_wall_datetime == datetime(2020, 1, 2, 12, 0)


def test_february_29_substitution_happens_before_offsets():
    spec = make_spec(
        selected_departure_local=datetime(2020, 2, 29, 12, 0),
        reference_year=2020,
        years=2,
        offset_days=1,
    )
    members = plan_members(spec)
    leap_years = [item for item in members if item.year == 2020]
    common_years = [item for item in members if item.year == 2019]
    assert leap_years and common_years
    leap = {item.offset_days: item.intended_wall_datetime for item in leap_years}
    assert leap[0] == datetime(2020, 2, 29, 12, 0)
    assert leap[-1] == datetime(2020, 2, 28, 12, 0)
    assert leap[1] == datetime(2020, 3, 1, 12, 0)
    assert not any(adjustments_of_kind(item, "leap_day") for item in leap_years)
    anchor = next(item for item in common_years if item.offset_days == 0)
    others = [item for item in common_years if item.offset_days != 0]
    assert adjustments_of_kind(anchor, "leap_day")
    assert not any(adjustments_of_kind(item, "leap_day") for item in others)
    common = {item.offset_days: item.intended_wall_datetime for item in common_years}
    assert common[0] == datetime(2019, 2, 28, 12, 0)
    assert common[1] == datetime(2019, 3, 1, 12, 0)
    assert common[-1] == datetime(2019, 2, 27, 12, 0)


def test_leap_day_resolution_never_raises_for_common_years():
    resolved, adjustments = resolve_local_departure(2023, 2, 29, 12, 0, "UTC")
    assert resolved == datetime(2023, 2, 28, 12, 0, tzinfo=UTC)
    assert [item.kind for item in adjustments] == ["leap_day"]


def test_unknown_timezone_propagates_from_resolution():
    with pytest.raises(ZoneInfoNotFoundError):
        resolve_local_departure(2026, 6, 1, 12, 0, "Not/AZone")


def test_dst_gap_shifts_forward_and_records_adjustment():
    resolved, adjustments = resolve_local_departure(2026, 3, 8, 2, 30, "America/New_York")
    assert resolved.tzinfo is not None
    assert resolved == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert [item.kind for item in adjustments] == ["dst_gap"]
    assert isinstance(adjustments[0], CalendarAdjustment)


def test_dst_fold_uses_the_earlier_occurrence():
    resolved, adjustments = resolve_local_departure(2026, 11, 1, 1, 30, "America/New_York")
    assert resolved == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert [item.kind for item in adjustments] == ["dst_fold"]


def test_coverage_window_is_whole_utc_hours():
    spec = make_spec(max_duration_hours=72)
    for member in plan_members(spec):
        start = member.coverage_start_utc
        end = member.coverage_end_utc
        assert start.minute == start.second == start.microsecond == 0
        assert end.minute == end.second == end.microsecond == 0
        assert start == member.resolved_departure_utc.replace(minute=0, second=0, microsecond=0)
        assert end - start == timedelta(hours=72)


def test_coverage_ceil_rounds_a_mid_hour_departure():
    spec = make_spec(
        selected_departure_local=datetime(2019, 6, 1, 12, 30),
        years=1,
        offset_days=0,
        max_duration_hours=72,
    )
    (member,) = plan_members(spec)
    assert member.resolved_departure_utc == datetime(2019, 6, 1, 12, 30, tzinfo=UTC)
    assert member.coverage_start_utc == datetime(2019, 6, 1, 12, 0, tzinfo=UTC)
    assert member.coverage_end_utc == datetime(2019, 6, 4, 13, 0, tzinfo=UTC)
    assert member.coverage_end_utc - member.coverage_start_utc == timedelta(hours=73)


def test_validate_spec_rejects_invalid_inputs():
    assert validate_spec(make_spec()) == []
    assert validate_spec(make_spec(years=30, offset_days=5))
    assert validate_spec(make_spec(years=31, offset_days=0))
    assert validate_spec(make_spec(years=0))
    assert validate_spec(make_spec(offset_days=-1))
    assert validate_spec(make_spec(offset_days=15))
    assert validate_spec(make_spec(max_duration_hours=5))
    assert validate_spec(make_spec(max_duration_hours=250))
    assert validate_spec(make_spec(max_duration_hours=721))
    assert validate_spec(make_spec(backend="legacy"))
    assert validate_spec(make_spec(timezone_id="Not/AZone"))
    assert validate_spec(make_spec(region=(10.0, 40.0, 5.0, 50.0)))


def test_validate_spec_enforces_the_reference_year():
    assert validate_spec(make_spec(reference_year=2018))
    with pytest.raises(ValueError):
        plan_members(make_spec(reference_year=2018))


def test_validate_spec_requires_an_aware_creation_time_and_whole_minutes():
    assert validate_spec(make_spec(created_at_utc=datetime(2026, 1, 1)))
    assert validate_spec(make_spec(selected_departure_local=datetime(2019, 6, 1, 12, 0, 30)))
    assert validate_spec(make_spec(selected_departure_local=datetime(2019, 6, 1, 12, 0, 0, 500)))


def test_plan_members_rejects_an_invalid_spec():
    with pytest.raises(ValueError):
        plan_members(make_spec(backend="legacy"))


def test_plan_years_excludes_the_whole_year_on_one_missing_window():
    spec = make_spec(years=3, offset_days=1)
    members = plan_members(spec)
    offender = next(item for item in members if item.year == 2017 and item.offset_days == 0)

    def unavailable(start, end):
        if (start, end) == (offender.coverage_start_utc, offender.coverage_end_utc):
            return "no archived wind"
        return None

    plans = plan_years(spec, unavailable)
    assert [plan.year for plan in plans] == [2017, 2018, 2019]
    excluded = next(plan for plan in plans if plan.year == 2017)
    assert excluded.state is YearState.EXCLUDED
    assert len(excluded.members) == 3
    assert offender.member_id in excluded.excluded_reason
    assert "no archived wind" in excluded.excluded_reason
    for plan in plans:
        if plan.year != 2017:
            assert plan.state is YearState.ELIGIBLE
            assert plan.excluded_reason == ""
    assert summarize_years(plans) == (3, 2, 1, 0)

    no_exclusions = plan_years(spec, lambda start, end: None)
    assert all(plan.state is YearState.ELIGIBLE for plan in no_exclusions)
    assert summarize_years(no_exclusions) == (3, 3, 0, 0)


def test_effective_accessors_drop_excluded_years():
    spec = make_spec(years=3, offset_days=1)
    members = plan_members(spec)
    offender = next(item for item in members if item.year == 2017 and item.offset_days == 1)

    def unavailable(start, end):
        if (start, end) == (offender.coverage_start_utc, offender.coverage_end_utc):
            return "no archived wind"
        return None

    plans = plan_years(spec, unavailable)
    assert [plan.year for plan in effective_plans(plans)] == [2018, 2019]
    effective = effective_members(plans)
    assert len(effective) == (spec.years - 1) * (2 * spec.offset_days + 1)
    assert all(member.year != 2017 for member in effective)


def test_summarize_years_counts_complete_years():
    spec = make_spec(years=2, offset_days=0)
    plans = plan_years(spec, lambda start, end: None)
    completed = YearPlan(year=plans[0].year, state=YearState.COMPLETE)
    assert summarize_years((completed,)) == (1, 1, 0, 1)


def test_member_id_is_unique_and_stable():
    identifier = member_id(2019, 3)
    assert identifier == member_id(2019, 3)
    assert identifier.startswith("2019-")
    assert identifier.endswith("+03")
    assert len(identifier) == 4 + 1 + 3
    assert member_id(2019, 3) != member_id(2019, -3)
    assert member_id(2019, 0) != member_id(2020, 0)
    identifiers = {member_id(year, offset) for year in range(2010, 2020) for offset in range(-3, 4)}
    assert len(identifiers) == 70
