"""Orchestration for historical-year ensemble routing.

The Qt coordinator owns the network, the native bridge and the marine store.
This module owns none of those: it accepts callables for everything it cannot
own and returns plain frozen records. That keeps preparation, execution
assembly and summarising testable without a tranche store, a downloader or a
native build.

Coverage is half-open everywhere: a member's window is
``[coverage_start_utc, coverage_end_utc)`` and is passed to the tranche
builder unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sailworks.core import historicalroutingstats
from sailworks.core.historicalbinding import CurrentBinding, WeatherBindingDescriptor
from sailworks.core.historicalexecution import (
    MemberExecution,
    build_member_request,
    current_hold_epoch,
    current_time_shift,
)
from sailworks.core.historicalrouting import (
    HistoricalMemberPlan,
    HistoricalRoutingSpec,
    YearPlan,
    YearState,
    effective_plans,
    plan_members,
    plan_years,
    summarize_years,
    validate_spec,
)
from sailworks.core.historicalroutingrecords import computation_key
from sailworks.core.historicaltranche import wind_tranche_for_window
from sailworks.core.historicalweather import (
    AcquisitionUnit,
    CoverageWindow,
    YearExclusion,
    availability_reason,
    estimate_decoded_bytes,
    plan_acquisition_units,
)

BINDING_VERSION = 1
TARGET_COMPLETION_HOURS = 168.0

__all__ = [
    "JobProgress",
    "PreparedJob",
    "build_executions",
    "format_compare_summary",
    "format_group_name",
    "prepare_job",
    "summarize_job",
]


@dataclass(frozen=True)
class PreparedJob:
    """Everything planning decided, before any bytes or routes exist."""

    spec: HistoricalRoutingSpec
    # Effective members only, ordered by (year ascending, offset ascending).
    members: tuple[HistoricalMemberPlan, ...]
    # Every requested year, effective or not, with its state.
    years: tuple[YearPlan, ...]
    units: tuple[AcquisitionUnit, ...]
    exclusions: tuple[YearExclusion, ...]
    requested_years: int
    effective_years: int
    excluded_years: int
    estimated_bytes: int


@dataclass(frozen=True)
class JobProgress:
    """One stage transition the coordinator can forward to the UI."""

    stage: str
    completed: int
    total: int
    message: str = ""


def prepare_job(spec, *, now=None, availability=None) -> PreparedJob:
    """Plan members, drop whole unavailable years and size the acquisition.

    ``availability`` is a callable ``(CoverageWindow) -> str | None``. The
    default wraps :func:`historicalweather.availability_reason` with ``now``.
    A year is excluded if any one of its members is unavailable; a reduced
    offset set is never produced.
    """
    errors = validate_spec(spec)
    if errors:
        raise ValueError("; ".join(errors))

    if now is None:
        now = datetime.now(timezone.utc)
    if availability is None:
        availability = lambda window: availability_reason(window, now=now)  # noqa: E731

    def check(start_utc: datetime, end_utc: datetime) -> str | None:
        return availability(CoverageWindow(start_utc, end_utc))

    years = plan_years(spec, check)
    requested, effective_count, excluded_count, _complete = summarize_years(years)

    effective = effective_plans(years)
    members = tuple(member for plan in effective for member in plan.members)
    units = _plan_units(members, spec) if members else ()
    estimated = estimate_decoded_bytes(units, spec.region) if units else 0

    return PreparedJob(
        spec=spec,
        members=members,
        years=years,
        units=units,
        exclusions=_exclusions(years, check),
        requested_years=requested,
        effective_years=effective_count,
        excluded_years=excluded_count,
        estimated_bytes=estimated,
    )


def _plan_units(members: tuple[HistoricalMemberPlan, ...], spec) -> tuple:
    windows = [
        CoverageWindow(member.coverage_start_utc, member.coverage_end_utc) for member in members
    ]
    return plan_acquisition_units(windows, spec.region, fields=spec.required_fields)


def _exclusions(years, check) -> tuple[YearExclusion, ...]:
    """Name the first offending member of every excluded year."""
    exclusions: list[YearExclusion] = []
    for plan in years:
        if plan.state is not YearState.EXCLUDED:
            continue
        member = None
        reason = plan.excluded_reason
        for candidate in plan.members:
            detail = check(candidate.coverage_start_utc, candidate.coverage_end_utc)
            if detail:
                member = candidate
                reason = detail
                break
        if member is None:  # defensive: the year was excluded, so keep it represented
            member = plan.members[0]
        exclusions.append(
            YearExclusion(
                year=plan.year,
                member_id=member.member_id,
                offset_days=member.offset_days,
                reason=reason,
            )
        )
    return tuple(exclusions)


def build_executions(
    prepared,
    base_request,
    *,
    store,
    target_departure_utc,
    current_binding=None,
    use_currents=False,
    tranche_factory=None,
    current_selection_factory=None,
    max_duration_hours=None,
) -> tuple[MemberExecution, ...]:
    """Freeze one execution per effective member, in planned order.

    The wind tranche is republished per member from that member's half-open
    window. Currents are one shared target-date snapshot: enabling them without
    a binding, or without a way to select the fields, is an error rather than a
    silent downgrade to wind-only routes.
    """
    spec = prepared.spec
    horizon = spec.max_duration_hours if max_duration_hours is None else max_duration_hours
    if use_currents:
        if current_binding is None or not current_binding.enabled:
            raise ValueError("currents are enabled but no current binding was supplied")
        if current_selection_factory is None:
            raise ValueError("current selection factory required")
        currents = current_binding
        priority = current_binding.priority
    else:
        currents = CurrentBinding(enabled=False, target_departure_utc=None, coverage_end_utc=None)
        priority = base_request.current_priority

    executions: list[MemberExecution] = []
    for member in prepared.members:
        forecast = (
            wind_tranche_for_window(
                store,
                member.coverage_start_utc,
                member.coverage_end_utc,
                # ``spec.region`` is our (west, south, east, north) convention;
                # the tranche converts it for the ERA5 catalog itself.
                spec.region,
            )
            if tranche_factory is None
            else tranche_factory(member)
        )
        request = build_member_request(
            base_request,
            member,
            max_duration_hours=horizon,
            use_currents=use_currents,
            current_priority=priority,
        )
        if use_currents:
            shift = current_time_shift(member, target_departure_utc)
            hold = current_hold_epoch(member, target_departure_utc, currents.coverage_end_utc)
            selection = current_selection_factory(currents)
        else:
            shift = 0.0
            hold = None
            selection = None
        executions.append(
            MemberExecution(
                member_id=member.member_id,
                year=member.year,
                offset_days=member.offset_days,
                request=request,
                forecast=forecast,
                current_selection=selection,
                current_time_shift_s=shift,
                current_hold_epoch_s=hold,
                computation_key=computation_key(spec.job_id, member.member_id, 1),
                binding_digest=_binding_digest(spec, member, forecast, currents, horizon),
            )
        )
    return tuple(executions)


def _wind_artifact_hashes(forecast) -> tuple[str, ...]:
    entry = getattr(forecast, "entry", None)
    artifacts = tuple(getattr(entry, "artifacts", ()) or ()) if entry is not None else ()
    if not artifacts:
        return ("",)
    return (str(getattr(artifacts[0], "sha256", "") or ""),)


def _binding_digest(spec, member, forecast, currents, horizon) -> str:
    descriptor = WeatherBindingDescriptor(
        binding_version=BINDING_VERSION,
        source_id=spec.source_id,
        wind_artifact_hashes=_wind_artifact_hashes(forecast),
        wind_coverage_start_utc=member.coverage_start_utc,
        wind_coverage_end_utc=member.coverage_end_utc,
        wind_region=tuple(float(value) for value in spec.region),
        member_departure_utc=member.resolved_departure_utc,
        currents=currents,
        max_duration_hours=horizon,
    )
    return descriptor.digest()


def summarize_job(prepared, outcomes) -> dict:
    """JSON-safe aggregate over the members that reported an outcome.

    Statistics reuse :mod:`historicalroutingstats`, so complete years are the
    primary sample, ``no_finish`` stays in the denominator, and incomplete or
    excluded years never count as non-finishes.
    """
    spec = prepared.spec
    by_member = {member.member_id: member for member in prepared.members}
    latest = {}
    for outcome in outcomes:
        latest[str(outcome.member_id)] = outcome

    counts = {"finished": 0, "no_finish": 0, "failed": 0, "cancelled": 0}
    results = []
    for member_id, outcome in latest.items():
        member = by_member.get(member_id)
        if member is None:  # an outcome from a different job or a stale member
            continue
        kind = str(outcome.outcome)
        counts[kind] = counts.get(kind, 0) + 1
        results.append(
            historicalroutingstats.MemberResult(
                member_id=member.member_id,
                year=member.year,
                offset_days=member.offset_days,
                outcome=kind,
                duration_hours=outcome.duration_hours,
            )
        )

    complete, mapping = historicalroutingstats.complete_years(results, spec.offset_days)
    target_hours = min(TARGET_COMPLETION_HOURS, float(spec.max_duration_hours))
    curve = historicalroutingstats.completion_curve(
        mapping, spec.offset_days, spec.max_duration_hours
    )
    target = historicalroutingstats.target_completion(mapping, spec.offset_days, target_hours)
    return {
        "requested_years": int(prepared.requested_years),
        "effective_years": int(prepared.effective_years),
        "excluded_years": int(prepared.excluded_years),
        "complete_years": len(complete),
        "finished": counts["finished"],
        "no_finish": counts["no_finish"],
        "failed": counts["failed"],
        "cancelled": counts["cancelled"],
        "completion_curve": [[float(moment), float(fraction)] for moment, fraction in curve],
        "target_hours": target_hours,
        "target_completion": float(target),
        "sample_guardrails": historicalroutingstats.sample_guardrails(len(complete)),
        "completion_label": historicalroutingstats.completion_label(len(complete), target_hours),
    }


def format_group_name(spec) -> str:
    """Scene group name: target departure plus the requested year span."""
    selected = spec.selected_departure_local
    first = spec.reference_year - spec.years + 1
    return (
        f"Historical wind · {selected:%b} {selected.day} {selected.year} · "
        f"requested {first}–{spec.reference_year}"
    )


def format_compare_summary(prepared) -> str:
    """One-line plan summary for the Compare tab."""
    return (
        f"Requested {prepared.requested_years} years · "
        f"Effective {prepared.effective_years} · "
        f"Excluded {prepared.excluded_years} · "
        f"{len(prepared.members)} routes"
    )
