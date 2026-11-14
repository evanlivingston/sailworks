"""Per-member execution helpers for historical ensemble routing.

One :class:`HistoricalMemberPlan` becomes one :class:`MemberExecution`: the
route request the native engine will run, the frozen forecast it samples, and
the current-field retiming that puts both members on the same time axis.
Outcomes come back as :class:`MemberOutcome` records so the coordinator can
aggregate without knowing which engine ran the member.
"""

from __future__ import annotations

import dataclasses
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sailworks.core.marine.nativerequest import NativeRequest, freeze_native_requests
from sailworks.core.routingjob import HorizonExceeded, RouteRequest, horizon_deadline


@dataclass(frozen=True)
class MemberExecution:
    """Everything one member needs to run, frozen before dispatch."""

    member_id: str
    year: int
    offset_days: int
    request: RouteRequest
    # A ``ResolvedForecast`` in practice. Typed as ``object`` so this module
    # stays importable, and testable, without a native build.
    forecast: object
    current_selection: object | None
    current_time_shift_s: float
    current_hold_epoch_s: float | None
    computation_key: str
    binding_digest: str


@dataclass(frozen=True)
class MemberOutcome:
    """What a member produced; never conflates a failure with a no-finish."""

    member_id: str
    outcome: str
    duration_hours: float | None
    error: str = ""
    route_uid: str = ""


def member_route_name(plan) -> str:
    """Distinguish a member in the route list by year, offset and wall clock."""
    wall_clock = plan.intended_wall_datetime
    return f"{plan.year} · {plan.offset_days:+d}d · {wall_clock:%Y-%m-%d %H:%M}"


def build_member_request(
    base: RouteRequest,
    plan,
    *,
    max_duration_hours: int,
    use_currents: bool,
    current_priority: str,
) -> RouteRequest:
    """Clone ``base`` as one member's request on its resolved departure.

    The horizon is validated by the same helper the native job uses, so an
    invalid value fails here with the message the user already knows.
    """
    request = dataclasses.replace(
        base,
        name=member_route_name(plan),
        departure=plan.resolved_departure_utc,
        max_duration_hours=max_duration_hours,
        use_currents=use_currents,
        current_priority=current_priority,
    )
    horizon_deadline(request)
    return request


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")


def current_time_shift(plan, target_departure_utc: datetime) -> float:
    """Seconds to add to the target timeline to reach this member's timeline.

    A member departing before the target yields a negative shift.
    """
    _require_aware("plan.resolved_departure_utc", plan.resolved_departure_utc)
    _require_aware("target_departure_utc", target_departure_utc)
    return (plan.resolved_departure_utc - target_departure_utc).total_seconds()


def current_hold_epoch(plan, target_departure_utc: datetime, coverage_end_utc: datetime) -> float:
    """The instant, in member time, at which the current field stops advancing.

    The native engine hands the callback member epochs, so the hold is the
    member's coverage end shifted by the same amount as the member departure:
    ``coverage_end_utc + current_time_shift(plan, target_departure_utc)``.
    Returned as a whole-second float.
    """
    shift = current_time_shift(plan, target_departure_utc)
    shifted = coverage_end_utc + timedelta(seconds=shift)
    return float(int(shifted.timestamp()))


def classify_outcome(result: object | None, error: BaseException | None) -> str:
    """Map a member's result/error pair onto one of three outcome strings.

    ``HorizonExceeded`` is the only error that means "no finish". Every other
    exception is a failure and must never be reported as a passage that ran
    out of horizon.
    """
    if result is not None and error is not None:
        raise ValueError("A member cannot both finish and raise an exception.")
    if result is None and error is None:
        raise ValueError("A member must either finish or raise an exception.")
    if error is None:
        return "finished"
    if isinstance(error, HorizonExceeded):
        return "no_finish"
    return "failed"


def elapsed_hours(plan, arrival_utc: datetime) -> float:
    """Hours from the member's departure to its arrival."""
    _require_aware("plan.resolved_departure_utc", plan.resolved_departure_utc)
    _require_aware("arrival_utc", arrival_utc)
    hours = (arrival_utc - plan.resolved_departure_utc).total_seconds() / 3600.0
    if hours < 0:
        raise ValueError("arrival_utc cannot precede the member departure.")
    return hours


def build_native_request(
    execution: MemberExecution,
    *,
    marine_directory: str,
    providers: tuple = (),
    engine: str = "main",
) -> NativeRequest:
    """Freeze one member into a spawn-safe native request with retiming set."""
    frozen = freeze_native_requests(
        execution.request,
        (),
        execution.forecast,
        providers,
        execution.current_selection,
        marine_directory,
        engine=engine,
    )
    if len(frozen) != 1:
        raise ValueError(f"Expected exactly one frozen native request, got {len(frozen)}.")
    request = frozen[0]
    if execution.request.use_currents:
        return dataclasses.replace(
            request,
            current_time_shift_s=execution.current_time_shift_s,
            current_hold_epoch_s=execution.current_hold_epoch_s,
        )
    if execution.current_time_shift_s != 0.0 or execution.current_hold_epoch_s is not None:
        raise ValueError("Inactive currents cannot carry a time shift or hold.")
    return request


class BoundRunCancelled(Exception):
    """A bound-run batch stopped because the caller cancelled it.

    ``outcomes`` carries the members that already finished before the stop so
    the caller can still report them; it is empty when nothing completed.
    """

    def __init__(self, message: str = "bound run was cancelled", outcomes=()):
        super().__init__(message)
        self.outcomes = tuple(outcomes)


_TERMINAL_STATES = {
    "completed": "finished",
    "failed": "failed",
    "cancelled": "cancelled",
}


def classify_status(state: object) -> str:
    """Map a terminal native job state onto the bound-run vocabulary.

    Queued/running states are not terminal and are rejected here: the polling
    loop only calls this once the worker has published a final status.
    """
    try:
        return _TERMINAL_STATES[state]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown native job state: {state!r}") from None


def _no_cancel() -> None:
    return None


def _no_progress(update) -> None:
    return None


def _default_convert(payload, job, *, sequence: int):
    """Convert and validate one payload exactly as the controller does."""
    from sailworks.core.marine.routingnative import to_routing_result
    from sailworks.core.marine.routingresult import export_blockers

    identity = payload.get("engine_identity") if isinstance(payload, dict) else None
    if not isinstance(identity, dict):
        raise ValueError("Native routing result did not report its engine identity")
    result = to_routing_result(
        payload,
        calculation_id=job.calculation_id,
        generation=int(getattr(job, "generation", 0) or 0),
        sequence=sequence,
        created_epoch_s=int(time.time()),
        identity=identity,
    )
    blockers = export_blockers(result)
    if blockers:
        raise ValueError(
            "native calculation did not produce a validated route: " + ", ".join(blockers)
        )
    return result


def _run_one_member(
    worker,
    cancel,
    progress,
    *,
    index: int,
    execution,
    save,
    marine_directory,
    providers,
    convert,
) -> MemberOutcome:
    """Run one member through ``worker`` and return its outcome.

    ``cancel`` and ``progress`` are already-bound callables so the caller can
    supply thread-aware wrappers without changing per-member behaviour. A
    cancelled native job raises :class:`BoundRunCancelled`; everything else
    that goes wrong stays local to this member.
    """
    cancel()
    try:
        job = build_native_request(
            execution, marine_directory=marine_directory, providers=providers
        )
    except Exception as exc:
        return MemberOutcome(execution.member_id, "failed", None, error=str(exc))
    cancel()
    worker.submit(job)
    status = None
    while status is None:
        cancel()
        status = worker.poll(0.1)
        update = worker.take_progress()
        if update is not None:
            progress(update)
    update = worker.take_progress()
    if update is not None:
        progress(update)
    try:
        kind = classify_status(getattr(status, "state", None))
    except ValueError as exc:
        return MemberOutcome(execution.member_id, "failed", None, error=str(exc))
    if kind == "cancelled":
        raise BoundRunCancelled()
    if kind == "failed":
        return MemberOutcome(
            execution.member_id,
            "failed",
            None,
            error=getattr(status, "message", "") or "native worker did not publish a result",
        )
    try:
        result = (
            _default_convert(status.payload, job, sequence=index)
            if convert is None
            else convert(status.payload, job)
        )
    except Exception as exc:
        return MemberOutcome(execution.member_id, "failed", None, error=str(exc))
    try:
        route_uid, duration_hours = save(execution, result)
    except (ValueError, OSError) as exc:
        return MemberOutcome(execution.member_id, "failed", None, error=str(exc))
    return MemberOutcome(
        execution.member_id,
        "finished",
        duration_hours,
        route_uid=route_uid or "",
    )


def run_bound_members(
    executions,
    *,
    worker=None,
    worker_factory=None,
    concurrency=1,
    save,
    marine_directory,
    providers=(),
    on_progress=None,
    check_cancel=None,
    convert=None,
) -> list[MemberOutcome]:
    """Run one native job per member, isolating failures member by member.

    Unlike the scenario runner, a failed job, a conversion that fails
    validation, or a save that raises ``ValueError``/``OSError`` becomes that
    member's ``failed`` outcome and the next member still runs. Only a
    cancelled job or a raising ``check_cancel`` ends the batch. The caller owns
    the worker lifecycle; this function never shuts a worker down and never
    calls ``worker.cancel()``.

    Exactly one worker source must be supplied. ``worker`` keeps the historical
    serial behaviour and requires ``concurrency == 1``. ``worker_factory`` is
    called once per slot; each slot runs its own worker on its own thread, so
    no worker instance is ever shared across threads. Outcomes are written back
    at the member's original index, so the returned order is the input order
    regardless of completion order.

    ``on_progress`` may be called concurrently from several slots and is
    serialized here with a lock. ``save`` may also be called concurrently:
    callers must make it thread-safe (the Qt controller's ``_ui_call`` already
    marshals to the GUI thread). ``check_cancel`` must be safe to call from
    several threads and should raise to stop the batch.
    """
    if (worker is None) == (worker_factory is None):
        raise ValueError("Provide exactly one of worker or worker_factory.")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ValueError("concurrency must be an integer of at least 1.")
    if worker is not None and concurrency != 1:
        raise ValueError("A single worker instance cannot run with concurrency > 1.")
    cancel = check_cancel or _no_cancel
    progress = on_progress or _no_progress

    if worker is not None:
        outcomes: list[MemberOutcome] = []
        for index, execution in enumerate(executions):
            try:
                outcomes.append(
                    _run_one_member(
                        worker,
                        cancel,
                        progress,
                        index=index,
                        execution=execution,
                        save=save,
                        marine_directory=marine_directory,
                        providers=providers,
                        convert=convert,
                    )
                )
            except BoundRunCancelled:
                raise BoundRunCancelled(outcomes=outcomes) from None
        return outcomes

    slots = [worker_factory() for _ in range(concurrency)]
    work: queue.SimpleQueue = queue.SimpleQueue()
    for index, execution in enumerate(executions):
        work.put((index, execution))
    results: list[MemberOutcome | None] = [None] * len(executions)
    stop = threading.Event()
    progress_lock = threading.Lock()
    report_lock = threading.Lock()
    errors: list[BaseException] = []
    cancelled: list[BoundRunCancelled] = []

    def cancel_checked() -> None:
        if stop.is_set():
            raise BoundRunCancelled()
        cancel()

    def report_progress(update) -> None:
        with progress_lock:
            progress(update)

    def run_slot(slot_worker) -> None:
        try:
            while not stop.is_set():
                try:
                    index, execution = work.get_nowait()
                except queue.Empty:
                    return
                results[index] = _run_one_member(
                    slot_worker,
                    cancel_checked,
                    report_progress,
                    index=index,
                    execution=execution,
                    save=save,
                    marine_directory=marine_directory,
                    providers=providers,
                    convert=convert,
                )
        except BoundRunCancelled as exc:
            stop.set()
            with report_lock:
                cancelled.append(exc)
        except BaseException as exc:  # noqa: BLE001 - re-raised after every slot joins
            stop.set()
            with report_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=run_slot, args=(slot,), name=f"bound-member-{number}", daemon=True)
        for number, slot in enumerate(slots)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if errors:
        raise errors[0]
    if cancelled:
        raise BoundRunCancelled(outcomes=tuple(item for item in results if item is not None))
    return [item for item in results if item is not None]


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def historical_provenance(
    plan,
    spec,
    *,
    binding_digest: str,
    source_id: str,
    artifact_hashes: tuple[str, ...],
    current_policy: str,
    effective_backend: str = "native",
) -> dict:
    """JSON-serializable record of how one member's route was computed."""
    return {
        "historical_routing": {
            "schema_version": 1,
            "job_id": spec.job_id,
            "member_id": plan.member_id,
            "year": int(plan.year),
            "offset_days": int(plan.offset_days),
            "intended_local": plan.intended_wall_datetime.isoformat(),
            "timezone_id": spec.timezone_id,
            "resolved_utc": _iso_z(plan.resolved_departure_utc),
            "calendar_adjustments": [
                {"kind": adjustment.kind, "detail": adjustment.detail}
                for adjustment in plan.calendar_adjustments
            ],
            "source_id": source_id,
            "artifact_hashes": [str(value) for value in artifact_hashes],
            "current_policy": current_policy,
            "max_duration_hours": int(spec.max_duration_hours),
            "region": [float(value) for value in spec.region],
            "requested_backend": spec.requested_backend,
            "effective_backend": effective_backend,
            "binding_digest": binding_digest,
        }
    }
