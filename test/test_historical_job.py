"""Tests for historical job orchestration.

No network, no native bridge, no Qt: the tranche, store and current selection
are all injected fakes.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from sailworks.core.historicalbinding import CurrentBinding
from sailworks.core.historicalexecution import MemberOutcome, member_route_name
from sailworks.core.historicaljob import (
    JobProgress,
    build_executions,
    format_compare_summary,
    format_group_name,
    prepare_job,
    summarize_job,
)
from sailworks.core.historicalrouting import HistoricalRoutingSpec, YearState, plan_members
from sailworks.core.routingjob import RouteRequest

UTC = timezone.utc
REGION = (-10.0, 40.0, 5.0, 50.0)
NOW = datetime(2026, 6, 15, 12, tzinfo=UTC)
TARGET = datetime(2026, 6, 15, 12, tzinfo=UTC)


def make_spec(years=3, offset_days=1, **overrides):
    values = dict(
        job_id="job-210",
        created_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        selected_departure_local=datetime(2026, 6, 15, 12, 0),
        timezone_id="UTC",
        reference_year=2026,
        years=years,
        offset_days=offset_days,
        max_duration_hours=240,
        region=REGION,
    )
    values.update(overrides)
    return HistoricalRoutingSpec(**values)


def available(_window):
    return None


def make_request():
    return RouteRequest(
        name="base",
        start=None,
        destination=(0.0, 0.0),
        departure=TARGET,
        polar_file="",
        algorithm=None,
    )


class FakeArtifact:
    sha256 = "a" * 64


class FakeForecast:
    def __init__(self, sha256="a" * 64):
        self.entry = type("Entry", (), {"artifacts": (FakeArtifact(),)})
        self.entry.artifacts[0].sha256 = sha256


def fake_tranche(member, digest="a" * 64):
    return FakeForecast(digest)


def make_binding(**overrides):
    values = dict(
        enabled=True,
        target_departure_utc=TARGET,
        coverage_end_utc=TARGET + timedelta(hours=120),
        priority="finest",
        field_ids=("field-a",),
        artifact_hashes=("field-a",),
        provider="chs",
    )
    values.update(overrides)
    return CurrentBinding(**values)


def test_prepare_job_all_available_counts_units_and_order():
    prepared = prepare_job(make_spec(), now=NOW, availability=available)

    assert prepared.requested_years == 3
    assert prepared.effective_years == 3
    assert prepared.excluded_years == 0
    assert prepared.exclusions == ()
    assert [plan.year for plan in prepared.years] == [2024, 2025, 2026]
    assert all(plan.state is YearState.ELIGIBLE for plan in prepared.years)

    assert len(prepared.members) == 9
    assert [(member.year, member.offset_days) for member in prepared.members] == [
        (year, offset) for year in (2024, 2025, 2026) for offset in (-1, 0, 1)
    ]

    # Adjacent offsets overlap, so days are shared instead of fetched twice.
    assert len(prepared.units) == 39
    assert len({unit.unit_id for unit in prepared.units}) == len(prepared.units)
    assert len(prepared.units) < 3 * 3 * 11
    assert prepared.estimated_bytes > 0
    assert all(str(unit.utc_day.year) in {"2024", "2025", "2026"} for unit in prepared.units)


def test_prepare_job_excludes_a_whole_year_and_names_first_member():
    def availability(window):
        if window.start_utc.year == 2025:
            return "ERA5 does not cover this window yet"
        return None

    spec = make_spec()
    prepared = prepare_job(spec, now=NOW, availability=availability)

    assert prepared.requested_years == 3
    assert prepared.effective_years == 2
    assert prepared.excluded_years == 1

    year_2025 = next(plan for plan in prepared.years if plan.year == 2025)
    assert year_2025.state is YearState.EXCLUDED
    assert 2025 not in {member.year for member in prepared.members}

    first_offender = next(member for member in plan_members(spec) if member.year == 2025)
    assert len(prepared.exclusions) == 1
    exclusion = prepared.exclusions[0]
    assert exclusion.year == 2025
    assert exclusion.member_id == first_offender.member_id
    assert exclusion.offset_days == first_offender.offset_days
    assert exclusion.reason == "ERA5 does not cover this window yet"


def test_prepare_job_with_every_year_excluded_is_empty_but_populated():
    prepared = prepare_job(make_spec(), now=NOW, availability=lambda window: "no coverage")

    assert prepared.requested_years == 3
    assert prepared.effective_years == 0
    assert prepared.excluded_years == 3
    assert prepared.members == ()
    assert prepared.units == ()
    assert prepared.estimated_bytes == 0
    assert [plan.state for plan in prepared.years] == [YearState.EXCLUDED] * 3
    assert len(prepared.exclusions) == 3


def test_build_executions_one_per_member_in_order_with_stable_digest():
    prepared = prepare_job(make_spec(), now=NOW, availability=available)
    base = make_request()

    def run():
        return build_executions(
            prepared,
            base,
            store="unused",
            target_departure_utc=TARGET,
            tranche_factory=fake_tranche,
        )

    executions = run()
    assert len(executions) == len(prepared.members) == 9
    assert [(item.year, item.offset_days) for item in executions] == [
        (member.year, member.offset_days) for member in prepared.members
    ]

    first = executions[0]
    planned = prepared.members[0]
    assert first.member_id == planned.member_id
    assert first.request.departure == planned.resolved_departure_utc
    assert first.request.name == member_route_name(planned)
    assert first.request.max_duration_hours == 240
    assert first.request.use_currents is False
    assert first.current_selection is None
    assert first.current_time_shift_s == 0.0
    assert first.current_hold_epoch_s is None
    assert first.computation_key

    # Wind-only runs repeat byte for byte.
    assert [item.binding_digest for item in run()] == [item.binding_digest for item in executions]


def test_build_executions_currents_require_binding_and_factory():
    prepared = prepare_job(make_spec(), now=NOW, availability=available)
    base = make_request()

    with pytest.raises(ValueError, match="no current binding"):
        build_executions(
            prepared,
            base,
            store="unused",
            target_departure_utc=TARGET,
            use_currents=True,
            tranche_factory=fake_tranche,
            current_selection_factory=lambda binding: "selection",
        )

    with pytest.raises(ValueError, match="no current binding"):
        build_executions(
            prepared,
            base,
            store="unused",
            target_departure_utc=TARGET,
            use_currents=True,
            current_binding=make_binding(enabled=False),
            tranche_factory=fake_tranche,
            current_selection_factory=lambda binding: "selection",
        )

    binding = make_binding()
    executions = build_executions(
        prepared,
        base,
        store="unused",
        target_departure_utc=TARGET,
        use_currents=True,
        current_binding=binding,
        tranche_factory=fake_tranche,
        current_selection_factory=lambda current: ("selection", current.field_ids),
    )
    first = executions[0]
    planned = prepared.members[0]
    assert first.current_selection == ("selection", ("field-a",))
    assert first.request.use_currents is True
    shift = (planned.resolved_departure_utc - TARGET).total_seconds()
    assert first.current_time_shift_s == shift < 0
    expected_hold = int((binding.coverage_end_utc + timedelta(seconds=shift)).timestamp())
    assert first.current_hold_epoch_s == pytest.approx(expected_hold)
    assert executions[-1].current_time_shift_s > 0


def test_build_executions_without_currents_ignores_the_factory():
    prepared = prepare_job(make_spec(), now=NOW, availability=available)

    def explode(binding):
        raise AssertionError("current selection factory must not run")

    executions = build_executions(
        prepared,
        make_request(),
        store="unused",
        target_departure_utc=TARGET,
        use_currents=False,
        current_binding=make_binding(),
        tranche_factory=fake_tranche,
        current_selection_factory=explode,
    )
    assert all(item.current_selection is None for item in executions)
    assert all(item.current_time_shift_s == 0.0 for item in executions)
    assert all(item.current_hold_epoch_s is None for item in executions)


def _mixed_outcomes(prepared):
    durations = {2024: (100.0, 120.0, 140.0), 2025: (110.0, 130.0, None), 2026: (90.0, None, None)}
    kinds = {
        2024: ("finished", "finished", "finished"),
        2025: ("finished", "finished", "no_finish"),
        2026: ("finished", "failed", "failed"),
    }
    outcomes = []
    for member in prepared.members:
        index = member.offset_days + 1
        outcomes.append(
            MemberOutcome(
                member_id=member.member_id,
                outcome=kinds[member.year][index],
                duration_hours=durations[member.year][index],
            )
        )
    return tuple(outcomes)


def test_summarize_job_uses_complete_years_and_keeps_no_finish_mass():
    prepared = prepare_job(make_spec(), now=NOW, availability=available)
    summary = summarize_job(prepared, _mixed_outcomes(prepared))

    assert summary["requested_years"] == 3
    assert summary["effective_years"] == 3
    assert summary["excluded_years"] == 0
    assert summary["complete_years"] == 2  # 2026 has indeterminate members
    assert summary["finished"] == 6
    assert summary["no_finish"] == 1
    assert summary["failed"] == 2
    assert summary["cancelled"] == 0
    assert summary["target_hours"] == 168.0
    # 2024: 3/3, 2025: 2/3 -> the no-finish stays in the denominator.
    assert summary["target_completion"] == pytest.approx(5.0 / 6.0)
    assert summary["sample_guardrails"] == "descriptive"
    assert summary["completion_curve"][-1] == [240.0, pytest.approx(5.0 / 6.0)]
    assert summary["completion_label"].endswith("across 2 complete years")

    assert json.loads(json.dumps(summary)) == summary


def test_format_group_name_and_compare_summary():
    spec = make_spec(years=10, offset_days=3)
    prepared = prepare_job(spec, now=NOW, availability=available)
    assert format_group_name(spec) == "Historical wind · Jun 15 2026 · requested 2017–2026"
    assert format_compare_summary(prepared) == (
        "Requested 10 years · Effective 10 · Excluded 0 · 70 routes"
    )

    def availability(window):
        return "late" if window.start_utc.year == 2025 else None

    excluded = prepare_job(make_spec(), now=NOW, availability=availability)
    assert format_compare_summary(excluded) == (
        "Requested 3 years · Effective 2 · Excluded 1 · 6 routes"
    )


def test_build_executions_propagates_tranche_failures():
    prepared = prepare_job(make_spec(), now=NOW, availability=available)

    def broken(member):
        raise RuntimeError(f"assembly failed for {member.member_id}")

    with pytest.raises(RuntimeError, match="assembly failed"):
        build_executions(
            prepared,
            make_request(),
            store="unused",
            target_departure_utc=TARGET,
            tranche_factory=broken,
        )


def test_job_progress_is_a_plain_frozen_record():
    progress = JobProgress(stage="planning", completed=0, total=63)
    assert progress.stage == "planning"
    assert progress.message == ""
