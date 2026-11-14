"""Whole-job orchestration for historical-year ensemble routing.

:mod:`sailworks.core.historicaljob` owns planning, execution assembly and
summarising; this module owns the *sequence*: preflight, acquisition, assembly,
run, summarise and the durable checkpoint between each stage. Every external
effect — wind acquisition, current acquisition, the bound runner and the scene —
is injected, so the sequence runs headless with no Qt import and no network I/O.

Scene publication, deletion and recovery live in
:mod:`sailworks.core.historicalpublication`; this module delegates to them and
re-exports their names. Whole years are the exclusion unit: a year without
coverage is dropped entire and the job still runs, while a member that fails
stays local to that member. Currents are never silently disabled — enabling
them without an acquisition is an error that parks the job in
``needs_attention``.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from sailworks.core.historicaljob import (
    JobProgress,
    build_executions,
    format_group_name,
    prepare_job,
    summarize_job,
)
from sailworks.core.historicalpublication import (
    DEFAULT_ROUTING_GROUP,
    GROUP_PREFIX,
    PUBLISHED_ROUTE_PREFIX,
    CoordinatorError,
    HistoricalPublisher,
)
from sailworks.core.historicalretry import HistoricalRetrier
from sailworks.core.historicalrouting import (
    JobState,
    YearState,
    resolve_local_departure,
)
from sailworks.core.historicalroutingrecords import YearRecord

__all__ = [
    "DEFAULT_ROUTING_GROUP",
    "GROUP_PREFIX",
    "PUBLISHED_ROUTE_PREFIX",
    "CoordinatorError",
    "HistoricalPublisher",
    "HistoricalRetrier",
    "HistoricalRoutingCoordinator",
]

#: Outcome kinds that make the job itself indeterminate, not just one member.
INDETERMINATE_OUTCOMES = frozenset({"failed", "cancelled"})

_MEMBER_STATES = {
    "finished": "finished",
    "no_finish": "no_finish_within_horizon",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _jsonable(value):
    """Plain-JSON projection of a record, spec or exclusion."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    return value


def _progress_event(update) -> JobProgress:
    """Normalise a downloader progress update into a :class:`JobProgress`."""
    get = (
        update.get
        if isinstance(update, dict)
        else lambda name: getattr(update, name, None)
    )
    return JobProgress(
        stage=str(get("stage") or "acquiring"),
        completed=int(get("completed") or 0),
        total=int(get("total") or 0),
        message=str(get("message") or ""),
    )


def _forward_progress(on_progress):
    """Wrap a UI callback so injected downloaders see ``JobProgress`` objects."""
    if on_progress is None:
        return None
    return lambda update: on_progress(_progress_event(update))


def _notify(on_progress, stage, completed, total, message=""):
    if on_progress is not None:
        on_progress(JobProgress(str(stage), int(completed), int(total), str(message)))


class HistoricalRoutingCoordinator:
    """Run one historical job end to end, checkpointing every stage.

    ``store`` is required; ``scene`` is an optional SceneCollection and ``now``
    an injected clock (a callable, or a fixed datetime). ``ensure_wind`` has the
    contract ``ensure_wind(units, progress=None)``: the ``progress`` keyword is
    always passed (a JobProgress callback, or ``None``), so injected downloaders
    must accept it.
    """

    def __init__(self, *, store, scene=None, now=None):
        self.store = store
        self.scene = scene
        if now is None:
            self._clock = lambda: datetime.now(timezone.utc)
        elif callable(now):
            self._clock = now
        else:
            fixed = now
            self._clock = lambda: fixed
        self.publisher = HistoricalPublisher(store=store, scene=scene, now=self._clock)
        self.retrier = HistoricalRetrier(self)

    def preflight(self, spec, base_request):
        """Plan members, exclusions and the acquisition, without side effects."""
        return prepare_job(spec, now=self._clock())

    def start(
        self,
        spec,
        base_request,
        *,
        ensure_wind=None,
        acquire_currents=None,
        current_selection_factory=None,
        run_bound=None,
        concurrency=None,
        on_progress=None,
        on_outcome=None,
        tranche_factory=None,
    ) -> str:
        """Run the whole job and return its id, parked states included."""
        prepared = self.preflight(spec, base_request)
        record = self._create_record(spec, prepared)

        if prepared.effective_years == 0:
            self._set_state(record, JobState.AWAITING_AVAILABILITY)
            _notify(on_progress, "awaiting_availability", 0, prepared.requested_years)
            return record.job_id

        if ensure_wind is None:
            raise CoordinatorError("wind acquisition requires an ensure_wind callable")

        # 2. Acquire wind: ensure_wind(units, progress=None).
        self._set_state(record, JobState.ACQUIRING)
        try:
            ensure_wind(prepared.units, progress=_forward_progress(on_progress))
        except Exception as exc:
            self._fail(record, "acquiring", exc)
            raise

        # 3. Acquire currents, or refuse: enabling them is a hard requirement.
        use_currents = bool(getattr(base_request, "use_currents", False)) or (
            "currents" in tuple(spec.required_fields)
        )
        acquisition = None
        if use_currents:
            acquisition = self._acquire_currents(record, prepared, acquire_currents)

        # 4. Freeze one execution per effective member.
        executions = self._build_executions(
            record,
            prepared,
            base_request,
            spec=spec,
            use_currents=use_currents,
            acquisition=acquisition,
            tranche_factory=tranche_factory,
            current_selection_factory=current_selection_factory,
        )

        # 5. Run, checkpointing every outcome as it reports.
        self._set_state(record, JobState.RUNNING)
        collected = self._run_members(
            record,
            executions,
            run_bound=run_bound,
            concurrency=concurrency,
            on_outcome=on_outcome,
        )

        # 6. Summarise, publish, settle.
        summary = summarize_job(prepared, collected)
        record.spec["summary"] = summary
        record.last_checkpoint_utc = self._stamp()
        self.publisher.ensure_group(record)
        self._set_state(record, self._final_state(prepared, collected))
        _notify(
            on_progress,
            record.state,
            len(collected),
            len(prepared.members),
            summary.get("completion_label", ""),
        )
        return record.job_id

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
        """See :meth:`historicalretry.HistoricalRetrier.retry_failed`."""
        return self.retrier.retry_failed(
            job_id,
            base_request,
            ensure_wind=ensure_wind,
            run_bound=run_bound,
            concurrency=concurrency,
            on_progress=on_progress,
            on_outcome=on_outcome,
            tranche_factory=tranche_factory,
            current_selection_factory=current_selection_factory,
        )

    def rerun_failed_units(self, job_id, *, ensure_wind, on_progress=None) -> int:
        """See :meth:`historicalretry.HistoricalRetrier.rerun_failed_units`."""
        return self.retrier.rerun_failed_units(
            job_id, ensure_wind=ensure_wind, on_progress=on_progress
        )

    def publish_routes(self, job_id, routes, *, entries=None):
        """See :meth:`historicalpublication.HistoricalPublisher.publish_routes`."""
        return self.publisher.publish_routes(job_id, routes, entries=entries)

    def delete_job(self, job_id, routes, *, collection=None):
        """See :meth:`historicalpublication.HistoricalPublisher.delete_job`."""
        return self.publisher.delete_job(job_id, routes, collection=collection)

    def recover(self):
        """What a restarted process should do; never writes."""
        return self.publisher.recover()

    def _stamp(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    def _create_record(self, spec, prepared):
        payload = _jsonable(dataclasses.asdict(spec))
        payload["group_name"] = format_group_name(spec)
        payload["exclusions"] = [_jsonable(item) for item in prepared.exclusions]
        record = self.store.create(payload, prepared.members)
        record.requested_years = tuple(plan.year for plan in prepared.years)
        record.effective_years = tuple(
            plan.year for plan in prepared.years if plan.state is YearState.ELIGIBLE
        )
        record.excluded_years = tuple(
            plan.year for plan in prepared.years if plan.state is YearState.EXCLUDED
        )
        record.years = {
            plan.year: YearRecord(
                year=plan.year, state=str(plan.state.value), reason=plan.excluded_reason
            )
            for plan in prepared.years
        }
        record.last_checkpoint_utc = self._stamp()
        self.store.save(record)
        return record

    def _set_state(self, record, state) -> None:
        self.store.set_job_state(record, str(getattr(state, "value", state)))

    def _fail(self, record, stage, error) -> None:
        """Record a stage failure and park the job; never swallow the error."""
        record.spec["last_error"] = f"{stage}: {type(error).__name__}: {error}"
        record.attempt_history = tuple(record.attempt_history) + (
            {
                "stage": str(stage),
                "error": str(error),
                "kind": type(error).__name__,
                "at": self._stamp(),
            },
        )
        record.last_checkpoint_utc = self._stamp()
        self.store.save(record)
        self._set_state(record, JobState.NEEDS_ATTENTION)

    def _acquire_currents(self, record, prepared, acquire_currents):
        if acquire_currents is None:
            error = CoordinatorError(
                "currents are enabled but no current acquisition was supplied"
            )
            self._fail(record, "currents", error)
            raise error
        try:
            acquisition = acquire_currents(prepared)
        except Exception as exc:
            self._fail(record, "currents", exc)
            raise
        if acquisition is None:
            error = CoordinatorError("current acquisition returned no binding")
            self._fail(record, "currents", error)
            raise error
        binding = getattr(acquisition, "binding", None)
        record.current_binding = {
            "enabled": bool(getattr(binding, "enabled", False)),
            "priority": str(getattr(binding, "priority", "")),
            "target_departure_utc": _jsonable(
                getattr(binding, "target_departure_utc", None)
            ),
            "coverage_end_utc": _jsonable(
                getattr(acquisition, "coverage_end_utc", None)
            ),
            "hold_seconds": float(getattr(acquisition, "hold_seconds", 0.0) or 0.0),
            "provider": str(getattr(acquisition, "provider", "")),
            "field_ids": [str(value) for value in acquisition.field_ids],
            "field_hashes": [str(value) for value in acquisition.field_hashes],
        }
        hashes = tuple(str(value) for value in acquisition.field_hashes)
        if hashes:
            self.store.pin(record, hashes)
        else:
            self.store.save(record)
        return acquisition

    def _target_departure(self, spec) -> datetime:
        """UTC instant of the selected local departure, anchored on the spec year."""
        wall = spec.selected_departure_local
        resolved, _adjustments = resolve_local_departure(
            spec.reference_year,
            wall.month,
            wall.day,
            wall.hour,
            wall.minute,
            spec.timezone_id,
        )
        return resolved

    def _build_executions(
        self,
        record,
        prepared,
        base_request,
        *,
        spec,
        use_currents,
        acquisition,
        tranche_factory,
        current_selection_factory,
    ):
        try:
            executions = build_executions(
                prepared,
                base_request,
                store=self.store,
                target_departure_utc=self._target_departure(spec),
                current_binding=getattr(acquisition, "binding", None),
                use_currents=use_currents,
                tranche_factory=tranche_factory,
                current_selection_factory=current_selection_factory,
            )
        except Exception as exc:
            self._fail(record, "assembly", exc)
            raise CoordinatorError(f"could not freeze executions: {exc}") from exc
        for execution in executions:
            member = record.members.get(execution.member_id)
            if member is not None and execution.binding_digest:
                member.binding_digest = str(execution.binding_digest)
        record.last_checkpoint_utc = self._stamp()
        self.store.save(record)
        return executions

    def _run_members(
        self,
        record,
        executions,
        *,
        run_bound,
        concurrency,
        on_outcome,
        increment_attempts=True,
    ):
        if run_bound is None:
            raise CoordinatorError("running a job requires a run_bound callable")
        if concurrency is not None and (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or concurrency < 1
        ):
            raise CoordinatorError("concurrency must be an integer of at least 1")
        collected: list = []

        def handle(outcome) -> None:
            self._apply_outcome(record, outcome, increment_attempts=increment_attempts)
            collected.append(outcome)
            if on_outcome is not None:
                on_outcome(outcome)

        kwargs = {"on_outcome": handle}
        if concurrency is not None:
            kwargs["concurrency"] = int(concurrency)
        try:
            returned = run_bound(executions, **kwargs)
        except Exception as exc:
            self._fail(record, "running", exc)
            raise
        for outcome in returned or ():
            if outcome is not None and not any(outcome is item for item in collected):
                handle(outcome)
        return collected

    def _apply_outcome(self, record, outcome, *, increment_attempts=True) -> None:
        member_id = str(getattr(outcome, "member_id", ""))
        member = record.members.get(member_id)
        if member is None:
            raise CoordinatorError(
                f"outcome for unknown member {member_id!r} in job {record.job_id}"
            )
        kind = str(getattr(outcome, "outcome", ""))
        self.store.update_member(
            record,
            member_id,
            state=_MEMBER_STATES.get(kind, kind or "failed"),
            outcome=kind,
            route_uid=str(getattr(outcome, "route_uid", "") or "") or None,
            error=str(getattr(outcome, "error", "") or "") or None,
            attempts=int(member.attempts) + (1 if increment_attempts else 0),
        )
        record.attempt_history = tuple(record.attempt_history) + (
            {
                "member_id": member_id,
                "outcome": kind,
                "duration_hours": getattr(outcome, "duration_hours", None),
                "at": self._stamp(),
            },
        )
        record.last_checkpoint_utc = self._stamp()
        self.store.save(record)

    def _final_state(self, prepared, outcomes) -> str:
        kinds = {str(getattr(outcome, "outcome", "")) for outcome in outcomes}
        if kinds & INDETERMINATE_OUTCOMES:
            return JobState.NEEDS_ATTENTION.value
        if prepared.excluded_years:
            return JobState.COMPLETE_WITH_EXCLUSIONS.value
        return JobState.COMPLETE.value
