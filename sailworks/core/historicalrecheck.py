"""Re-admitting whole excluded years to an existing historical job.

A year is the exclusion unit: one without coverage is dropped entire, so it has
no member records at all. When coverage reaches it later, re-checking inserts
fresh ``waiting_weather`` members for that year only and re-runs exactly those;
every member that already exists is left untouched. Currents follow the job's
stored binding — enabling them without one is a refusal, never a downgrade.

The mixin below is part of :class:`historicalretry.HistoricalRetrier`; it lives
here so the retry rules and the year re-check stay separately readable.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from sailworks.core.historicaljob import PreparedJob, _plan_units, summarize_job
from sailworks.core.historicalpublication import CoordinatorError
from sailworks.core.historicalrouting import JobState, plan_members
from sailworks.core.historicalroutingrecords import MemberRecord
from sailworks.core.historicalweather import CoverageWindow, availability_reason

__all__ = ["HistoricalRecheckMixin"]


def _refresh_year_tuples(record) -> None:
    """Recompute the effective/excluded tuples from the year records."""
    record.effective_years = tuple(
        sorted(
            year
            for year, entry in record.years.items()
            if str(entry.state) == "eligible"
        )
    )
    record.excluded_years = tuple(
        sorted(
            year
            for year, entry in record.years.items()
            if str(entry.state) == "excluded"
        )
    )


def _recheck_final_state(record, fresh_ids) -> str:
    """Settle the job on what the newly admitted members actually became."""
    for member_id in fresh_ids:
        member = record.members.get(member_id)
        if member is None or str(member.outcome) not in {"finished", "no_finish"}:
            return JobState.NEEDS_ATTENTION.value
    for member in record.members.values():
        if member.member_id in fresh_ids:
            continue
        if str(member.outcome) in {"failed", "cancelled"}:
            return JobState.NEEDS_ATTENTION.value
    if record.excluded_years:
        return JobState.COMPLETE_WITH_EXCLUSIONS.value
    return JobState.COMPLETE.value


class HistoricalRecheckMixin:
    """Re-check whole excluded years and admit the ones now available."""

    def recheck_excluded(
        self,
        job_id,
        base_request,
        *,
        ensure_wind,
        run_bound,
        concurrency=None,
        on_progress=None,
        on_outcome=None,
        tranche_factory=None,
        current_selection_factory=None,
        now=None,
    ) -> list:
        """Admit every excluded year whose whole coverage is available again.

        Years that remain unavailable stay excluded and are not retried. A year
        admitted here gets new ``waiting_weather`` members and a re-acquisition
        of only their units; the run then covers those members alone.
        """
        from sailworks.core import historicalretry as retry

        record = retry._load_active(self.store, job_id)
        spec = retry._spec_from_record(record)
        moment = now if now is not None else datetime.now(timezone.utc)
        plans_by_year = {}
        for plan in plan_members(spec):
            plans_by_year.setdefault(plan.year, []).append(plan)

        eligible = []
        for year in sorted(record.excluded_years):
            year_record = record.years.get(year)
            if year_record is None or str(year_record.state) != "excluded":
                continue
            plans = plans_by_year.get(year, [])
            if not plans:
                continue
            if all(
                availability_reason(
                    CoverageWindow(plan.coverage_start_utc, plan.coverage_end_utc),
                    now=moment,
                )
                is None
                for plan in plans
            ):
                eligible.append((year, tuple(plans)))
        if not eligible:
            return []
        if ensure_wind is None:
            raise CoordinatorError("wind acquisition requires an ensure_wind callable")
        if run_bound is None:
            raise CoordinatorError("running a job requires a run_bound callable")
        if concurrency is not None and (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or concurrency < 1
        ):
            raise CoordinatorError("concurrency must be an integer of at least 1")

        use_currents = bool(getattr(base_request, "use_currents", False)) or (
            "currents" in tuple(getattr(spec, "required_fields", ()) or ())
        )
        acquisition = retry._stored_currents(record) if use_currents else None
        if use_currents:
            if acquisition is None:
                raise CoordinatorError(
                    "currents are enabled but the job has no stored current binding"
                )
            if current_selection_factory is None:
                raise CoordinatorError(
                    "current selection factory required to recheck a currents-enabled job"
                )

        # 1. Admit each newly available year and insert its fresh members.
        at = self.coordinator._stamp()
        fresh = []
        for year, plans in eligible:
            year_record = record.years[year]
            previous_reason = str(year_record.reason)
            if previous_reason:
                year_record.previous_reasons = tuple(year_record.previous_reasons) + (
                    previous_reason,
                )
            year_record.state = "eligible"
            year_record.reason = ""
            for plan in plans:
                if plan.member_id in record.members:
                    continue
                record.members[plan.member_id] = MemberRecord(
                    member_id=plan.member_id,
                    year=plan.year,
                    offset_days=plan.offset_days,
                    state="waiting_weather",
                    departure_utc=plan.resolved_departure_utc.isoformat(),
                    coverage_start_utc=plan.coverage_start_utc.isoformat(),
                    coverage_end_utc=plan.coverage_end_utc.isoformat(),
                )
                fresh.append(plan)
            record.attempt_history = tuple(record.attempt_history) + (
                {
                    "event": "recheck",
                    "year": year,
                    "previous_reason": previous_reason,
                    "members": [plan.member_id for plan in plans],
                    "at": at,
                },
            )
        _refresh_year_tuples(record)
        record.last_checkpoint_utc = at
        self.store.save(record)
        fresh_plans = tuple(fresh)
        if not fresh_plans:
            return []

        # 2. Re-acquire only the wind the newly admitted members need.
        units = retry._unique_units(_plan_units(fresh_plans, spec))
        self.coordinator._set_state(record, JobState.ACQUIRING)
        try:
            ensure_wind(units, progress=retry._progress(on_progress))
        except Exception as exc:
            self.coordinator._fail(record, "acquiring", exc)
            raise

        # 3. Build and run executions for the newly admitted members only.
        prepared = PreparedJob(
            spec=spec,
            members=fresh_plans,
            years=(),
            units=units,
            exclusions=(),
            requested_years=len(record.requested_years),
            effective_years=len(record.effective_years),
            excluded_years=len(record.excluded_years),
            estimated_bytes=0,
        )
        executions = self.coordinator._build_executions(
            record,
            prepared,
            base_request,
            spec=spec,
            use_currents=use_currents,
            acquisition=acquisition,
            tranche_factory=tranche_factory,
            current_selection_factory=current_selection_factory,
        )
        self.coordinator._set_state(record, JobState.RUNNING)
        collected = self.coordinator._run_members(
            record,
            executions,
            run_bound=run_bound,
            concurrency=concurrency,
            on_outcome=on_outcome,
        )

        # 4. Summarise over every member, old and newly admitted.
        summary = summarize_job(
            dataclasses.replace(
                prepared, members=retry._planned_members(record, spec), units=()
            ),
            retry._summary_outcomes(record, collected),
        )
        record.spec["summary"] = summary
        record.last_checkpoint_utc = self.coordinator._stamp()
        self.store.save(record)
        self.coordinator._set_state(
            record,
            _recheck_final_state(record, {plan.member_id for plan in fresh_plans}),
        )
        retry._notify_progress(
            on_progress,
            record.state,
            len(collected),
            len(fresh_plans),
            summary.get("completion_label", ""),
        )
        return collected
