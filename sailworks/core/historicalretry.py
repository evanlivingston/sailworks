"""Member re-runs for historical routing jobs.

A finished or ``no_finish`` member is determinate; only a failure re-runs. The
retrier resets exactly the indeterminate members, re-acquires only the wind
their units need, rebuilds only their executions and recomputes the summary over
every member. Currents come from the job's stored binding — enabling them
without one is a refusal, never a silent wind-only downgrade.

The coordinator owns the sequence and its shared checkpoint helpers; this module
owns the retry rules and calls back into those helpers, so a rule has exactly one
implementation.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

from sailworks.core.historicaljob import (
    CurrentBinding,
    PreparedJob,
    _plan_units,
    summarize_job,
)
from sailworks.core.historicalpublication import CoordinatorError
from sailworks.core.historicalrouting import (
    HistoricalRoutingSpec,
    JobState,
    plan_members,
)

__all__ = [
    "RETRYABLE_MEMBER_STATES",
    "RETRYABLE_UNIT_STATES",
    "HistoricalRetrier",
]

#: Member states a retry may re-run; ``no_finish`` is determinate and never is.
RETRYABLE_MEMBER_STATES = frozenset({"failed_execution", "blocked_weather"})

#: Acquisition states a unit retry may re-run; routing is never implied.
RETRYABLE_UNIT_STATES = frozenset({"failed", "interrupted"})

#: Spec fields serialised as ISO strings that must come back as datetimes.
_DATETIME_SPEC_FIELDS = ("created_at_utc", "selected_departure_local")


def _progress(on_progress):
    """The coordinator's downloader adapter, imported lazily to break a cycle."""
    from sailworks.core.historicalcoordinator import _forward_progress

    return _forward_progress(on_progress)


def _notify_progress(on_progress, *args):
    from sailworks.core.historicalcoordinator import _notify

    _notify(on_progress, *args)


@dataclasses.dataclass(frozen=True)
class _OutcomeView:
    """Outcome-shaped view of a member checkpointed before this retry."""

    member_id: str
    outcome: str
    duration_hours: object = None


@dataclasses.dataclass(frozen=True)
class _StoredCurrents:
    """Presents a record's stored current binding to execution assembly."""

    binding: object


def _is_retryable(member) -> bool:
    """A member is indeterminate only if it failed and did not reach no-finish."""
    if str(member.outcome) == "no_finish":
        return False
    return (
        str(member.state) in RETRYABLE_MEMBER_STATES or str(member.outcome) == "failed"
    )


def _spec_from_record(record) -> HistoricalRoutingSpec:
    """Rebuild the planning spec from a record, ignoring non-spec bookkeeping."""
    stored = dict(record.spec or {})
    kwargs = {}
    for field in dataclasses.fields(HistoricalRoutingSpec):
        if field.name not in stored:
            continue
        value = stored[field.name]
        if isinstance(value, list):
            value = tuple(value)
        if field.name in _DATETIME_SPEC_FIELDS and isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        kwargs[field.name] = value
    try:
        return HistoricalRoutingSpec(**kwargs)
    except TypeError as exc:
        raise CoordinatorError(f"stored spec cannot be reloaded: {exc}") from exc


def _unique_units(units) -> tuple:
    """Deduplicate acquisition units by id, sorted for a stable call."""
    indexed = {str(getattr(unit, "unit_id", "")): unit for unit in units or ()}
    return tuple(indexed[key] for key in sorted(indexed))


def _stored_currents(record):
    """The job's current binding as an acquisition-like object, or ``None``."""
    stored = dict(record.current_binding or {})
    if not stored.get("enabled"):
        return None

    def instant(name):
        value = stored.get(name)
        return (
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )

    binding = CurrentBinding(
        enabled=True,
        target_departure_utc=instant("target_departure_utc"),
        coverage_end_utc=instant("coverage_end_utc"),
        priority=str(stored.get("priority", "")),
        field_ids=tuple(str(value) for value in (stored.get("field_ids") or ())),
        provider=str(stored.get("provider", "")),
    )
    return _StoredCurrents(binding=binding)


def _load_active(store, job_id):
    """Load a job a retry may touch; archived and running jobs are refusals."""
    try:
        record = store.load(job_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CoordinatorError(f"unknown job {job_id!r}") from exc
    state = str(record.state)
    if state == "archived":
        raise CoordinatorError(f"job {job_id!r} is archived")
    if state in {"running", JobState.ACQUIRING.value}:
        raise CoordinatorError(f"job {job_id!r} is already running")
    return record


def _planned_members(record, spec) -> tuple:
    """The job's stored members, re-planned from the spec in planned order."""
    return tuple(
        plan for plan in plan_members(spec) if plan.member_id in record.members
    )


def _summary_outcomes(record, retried) -> tuple:
    """Outcome views for every reported member, retried ones taking priority."""
    durations = {}
    for entry in record.attempt_history:
        member_id = entry.get("member_id")
        if member_id and entry.get("outcome"):
            durations[str(member_id)] = entry.get("duration_hours")
    latest = {}
    for member in record.members.values():
        if not member.outcome:
            continue
        latest[member.member_id] = _OutcomeView(
            member.member_id, str(member.outcome), durations.get(member.member_id)
        )
    for outcome in retried:
        member_id = str(getattr(outcome, "member_id", ""))
        latest[member_id] = _OutcomeView(
            member_id,
            str(getattr(outcome, "outcome", "")),
            getattr(outcome, "duration_hours", None),
        )
    return tuple(latest.values())


def _retry_final_state(record, retry_ids) -> str:
    """Settle the job on what the retried members actually became."""
    for member_id in retry_ids:
        member = record.members.get(member_id)
        if member is None or str(member.outcome) not in {"finished", "no_finish"}:
            return JobState.NEEDS_ATTENTION.value
    if record.excluded_years:
        return JobState.COMPLETE_WITH_EXCLUSIONS.value
    return JobState.COMPLETE.value


class HistoricalRetrier:
    """Re-run the indeterminate parts of an existing historical job."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store

    def retry_failed(
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
    ) -> list:
        """Re-run only the members a previous attempt left indeterminate.

        Finished and ``no_finish`` members are determinate: they keep their
        route, attempt count and outcome, and are never planned again. Wind is
        re-acquired only for the retryable members' units, and currents come
        from the stored binding — never from a silent wind-only downgrade.
        """
        record = _load_active(self.store, job_id)
        retryable = [
            member for member in record.members.values() if _is_retryable(member)
        ]
        if not retryable:
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

        spec = _spec_from_record(record)
        retry_ids = {member.member_id for member in retryable}
        retry_plans = tuple(
            plan for plan in plan_members(spec) if plan.member_id in retry_ids
        )
        missing = retry_ids - {plan.member_id for plan in retry_plans}
        if missing:
            raise CoordinatorError(
                f"retryable members are no longer planned: {sorted(missing)!r}"
            )

        use_currents = bool(getattr(base_request, "use_currents", False)) or (
            "currents" in tuple(getattr(spec, "required_fields", ()) or ())
        )
        acquisition = _stored_currents(record) if use_currents else None
        if use_currents:
            if acquisition is None:
                raise CoordinatorError(
                    "currents are enabled but the job has no stored current binding"
                )
            if current_selection_factory is None:
                raise CoordinatorError(
                    "current selection factory required to retry a currents-enabled job"
                )

        # 1. Reset only the indeterminate members; save before any bytes move.
        at = self.coordinator._stamp()
        for member in retryable:
            previous_state = member.state
            previous_outcome = member.outcome
            member.state = "waiting_weather"
            member.outcome = ""
            member.error = ""
            member.route_uid = ""
            member.attempts = int(member.attempts) + 1
            record.attempt_history = tuple(record.attempt_history) + (
                {
                    "event": "retry",
                    "member_id": member.member_id,
                    "previous_state": str(previous_state),
                    "previous_outcome": str(previous_outcome),
                    "attempt": int(member.attempts),
                    "at": at,
                },
            )
        record.last_checkpoint_utc = at
        self.store.save(record)

        # 2. Re-acquire only the wind the retryable members need.
        units = _unique_units(_plan_units(retry_plans, spec))
        self.coordinator._set_state(record, JobState.ACQUIRING)
        try:
            ensure_wind(units, progress=_progress(on_progress))
        except Exception as exc:
            self.coordinator._fail(record, "acquiring", exc)
            raise

        # 3. Rebuild only the retryable members' executions.
        prepared = PreparedJob(
            spec=spec,
            members=retry_plans,
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

        # 4. Run and checkpoint every outcome, without double-counting attempts.
        self.coordinator._set_state(record, JobState.RUNNING)
        collected = self.coordinator._run_members(
            record,
            executions,
            run_bound=run_bound,
            concurrency=concurrency,
            on_outcome=on_outcome,
            increment_attempts=False,
        )

        # 5. Summarise over every member: preserved ones and the retried ones.
        summary = summarize_job(
            dataclasses.replace(
                prepared, members=_planned_members(record, spec), units=()
            ),
            _summary_outcomes(record, collected),
        )
        record.spec["summary"] = summary
        record.last_checkpoint_utc = self.coordinator._stamp()
        self.store.save(record)
        self.coordinator._set_state(record, _retry_final_state(record, retry_ids))
        _notify_progress(
            on_progress,
            record.state,
            len(collected),
            len(retry_plans),
            summary.get("completion_label", ""),
        )
        return collected

    def rerun_failed_units(self, job_id, *, ensure_wind, on_progress=None) -> int:
        """Re-acquire only failed or interrupted units; never routes anything."""
        record = _load_active(self.store, job_id)
        if ensure_wind is None:
            raise CoordinatorError("wind acquisition requires an ensure_wind callable")
        failed = {
            str(unit_id)
            for unit_id, unit in record.acquisitions.items()
            if str(unit.state) in RETRYABLE_UNIT_STATES
        }
        if not failed:
            return 0
        spec = _spec_from_record(record)
        planned = _unique_units(_plan_units(tuple(plan_members(spec)), spec))
        units = tuple(
            unit for unit in planned if str(getattr(unit, "unit_id", "")) in failed
        )
        if not units:
            return 0

        for unit in units:
            record.acquisitions[str(unit.unit_id)].attempts += 1
        record.last_checkpoint_utc = self.coordinator._stamp()
        self.store.save(record)
        try:
            ensure_wind(units, progress=_progress(on_progress))
        except Exception as exc:
            for unit in units:
                entry = record.acquisitions[str(unit.unit_id)]
                entry.state = "failed"
                entry.error = str(exc)
            record.last_checkpoint_utc = self.coordinator._stamp()
            self.store.save(record)
            raise
        for unit in units:
            entry = record.acquisitions[str(unit.unit_id)]
            entry.state = "complete"
            entry.error = ""
        record.last_checkpoint_utc = self.coordinator._stamp()
        self.store.save(record)
        return len(units)
