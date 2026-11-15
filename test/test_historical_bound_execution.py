"""Bound-run engine: per-member isolation, cancellation and status mapping."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from sailworks.core import historicalexecution
from sailworks.core.historicalexecution import (
    BoundRunCancelled,
    classify_status,
    run_bound_members,
)

MARINE_DIRECTORY = "/marine"


class FakeJob:
    def __init__(self, member_id):
        self.calculation_id = f"calc-{member_id}"


class FakeStatus:
    def __init__(self, state, payload=None, message=""):
        self.state = state
        self.payload = payload
        self.message = message


class FakeWorker:
    """Duck-typed stand-in for ``NativeRoutingWorker`` with no native bridge."""

    def __init__(self, statuses, progress=()):
        self._statuses = list(statuses)
        self._progress = list(progress)
        self.submitted = []
        self.shutdown_called = False

    def submit(self, job):
        self.submitted.append(job)
        return len(self.submitted)

    def poll(self, timeout=0.0):
        return self._statuses.pop(0)

    def take_progress(self):
        return self._progress.pop(0) if self._progress else None

    def shutdown(self):
        self.shutdown_called = True


def _execution(member_id):
    return SimpleNamespace(member_id=member_id, request=object())


def _patch_requests(monkeypatch):
    monkeypatch.setattr(
        historicalexecution,
        "build_native_request",
        lambda execution, **kwargs: FakeJob(execution.member_id),
    )


def _echo(payload, job):
    return payload


def test_member_failure_does_not_abort_batch(monkeypatch):
    _patch_requests(monkeypatch)
    executions = [_execution(name) for name in ("a", "b", "c")]
    worker = FakeWorker(
        [
            FakeStatus("completed", payload={"member": "a"}),
            FakeStatus("failed", message="engine exploded"),
            FakeStatus("completed", payload={"member": "c"}),
        ]
    )
    saved = []

    def save(execution, result):
        saved.append((execution.member_id, result["member"]))
        return f"route-{execution.member_id}", 12.0

    outcomes = run_bound_members(
        executions,
        worker=worker,
        save=save,
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
    )

    assert [item.member_id for item in outcomes] == ["a", "b", "c"]
    assert [item.outcome for item in outcomes] == ["finished", "failed", "finished"]
    assert outcomes[1].error == "engine exploded"
    assert outcomes[1].route_uid == ""
    assert saved == [("a", "a"), ("c", "c")]
    assert len(worker.submitted) == 3


def test_conversion_failure_is_a_member_failure(monkeypatch):
    _patch_requests(monkeypatch)
    worker = FakeWorker([FakeStatus("completed", payload={"member": "a"})])
    saved = []

    def convert(payload, job):
        raise ValueError("native calculation did not produce a validated route: landfall")

    outcomes = run_bound_members(
        [_execution("a")],
        worker=worker,
        save=lambda execution, result: saved.append(result) or ("uid", 1.0),
        marine_directory=MARINE_DIRECTORY,
        convert=convert,
    )

    assert [item.outcome for item in outcomes] == ["failed"]
    assert "validated route" in outcomes[0].error
    assert saved == []


def test_cancelled_status_stops_the_batch(monkeypatch):
    _patch_requests(monkeypatch)
    worker = FakeWorker([FakeStatus("cancelled")])

    with pytest.raises(BoundRunCancelled) as caught:
        run_bound_members(
            [_execution("a"), _execution("b")],
            worker=worker,
            save=lambda execution, result: ("uid", 1.0),
            marine_directory=MARINE_DIRECTORY,
            convert=_echo,
        )

    assert caught.value.outcomes == ()
    assert len(worker.submitted) == 1


def test_check_cancel_stops_before_the_next_submit(monkeypatch):
    _patch_requests(monkeypatch)
    worker = FakeWorker(
        [
            FakeStatus("completed", payload={"member": "a"}),
            FakeStatus("completed", payload={"member": "b"}),
        ]
    )
    saved = []

    class Cancelled(Exception):
        pass

    def check_cancel():
        if saved:
            raise Cancelled("stop")

    with pytest.raises(Cancelled):
        run_bound_members(
            [_execution("a"), _execution("b")],
            worker=worker,
            save=lambda execution, result: (saved.append(execution.member_id) or ("uid", 1.0)),
            marine_directory=MARINE_DIRECTORY,
            convert=_echo,
            check_cancel=check_cancel,
        )

    assert saved == ["a"]
    assert len(worker.submitted) == 1


def test_progress_updates_are_forwarded_in_order(monkeypatch):
    _patch_requests(monkeypatch)
    worker = FakeWorker(
        [FakeStatus("completed", payload={})],
        progress=[{"stage": "preflight"}, {"stage": "complete"}],
    )
    seen = []

    run_bound_members(
        [_execution("a")],
        worker=worker,
        save=lambda execution, result: ("uid", 1.0),
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
        on_progress=seen.append,
    )

    assert seen == [{"stage": "preflight"}, {"stage": "complete"}]


@pytest.mark.parametrize(
    ("state", "expected"),
    [("completed", "finished"), ("failed", "failed"), ("cancelled", "cancelled")],
)
def test_classify_status_maps_terminal_states(state, expected):
    assert classify_status(state) == expected


@pytest.mark.parametrize("state", ["queued", "running", "bogus", None])
def test_classify_status_rejects_non_terminal_or_unknown(state):
    with pytest.raises(ValueError):
        classify_status(state)


def test_unknown_status_becomes_a_failed_outcome(monkeypatch):
    _patch_requests(monkeypatch)
    worker = FakeWorker([FakeStatus("teleported")])

    outcomes = run_bound_members(
        [_execution("a")],
        worker=worker,
        save=lambda execution, result: ("uid", 1.0),
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
    )

    assert [item.outcome for item in outcomes] == ["failed"]
    assert "teleported" in outcomes[0].error


def test_saved_route_identity_and_duration_are_reported(monkeypatch):
    _patch_requests(monkeypatch)
    worker = FakeWorker([FakeStatus("completed", payload={"member": "a"})])

    outcomes = run_bound_members(
        [_execution("a")],
        worker=worker,
        save=lambda execution, result: ("route-uid-7", 31.5),
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
    )

    assert outcomes[0].route_uid == "route-uid-7"
    assert outcomes[0].duration_hours == 31.5


def _member_of(job):
    return job.calculation_id.removeprefix("calc-")


class ParallelWorker:
    """Per-slot worker whose latency, failures and cancels are per member."""

    def __init__(self, delays=None, fail_for=(), cancel_for=()):
        self.delays = dict(delays or {})
        self.fail_for = set(fail_for)
        self.cancel_for = set(cancel_for)
        self.job = None
        self.in_use = False
        self.guard = threading.Lock()
        self.submitted = []
        self.shutdown_called = False

    def submit(self, job):
        with self.guard:
            if self.in_use:
                raise RuntimeError("worker used by two threads at once")
            self.in_use = True
        self.job = job
        self.submitted.append(_member_of(job))
        return len(self.submitted)

    def poll(self, timeout=0.0):
        member = _member_of(self.job)
        time.sleep(self.delays.get(member, 0.0))
        with self.guard:
            self.in_use = False
        if member in self.cancel_for:
            return FakeStatus("cancelled")
        if member in self.fail_for:
            return FakeStatus("failed", message=f"{member} exploded")
        return FakeStatus("completed", payload={"member": member})

    def take_progress(self):
        return None

    def shutdown(self):
        self.shutdown_called = True


def _factory_of(created, **kwargs):
    def factory():
        created.append(ParallelWorker(**kwargs))
        return created[-1]

    return factory


def test_parallel_outcomes_follow_input_order(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c", "d", "e", "f"]
    # Later members finish first so completion order cannot be mistaken for
    # input order.
    delays = {name: 0.05 * (len(names) - index) for index, name in enumerate(names)}
    created = []
    saved = []
    save_lock = threading.Lock()

    def save(execution, result):
        with save_lock:
            saved.append((execution.member_id, result["member"]))
        return f"route-{execution.member_id}", 1.0

    outcomes = run_bound_members(
        [_execution(name) for name in names],
        worker_factory=_factory_of(created, delays=delays),
        concurrency=3,
        save=save,
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
    )

    assert [item.member_id for item in outcomes] == names
    assert all(item.outcome == "finished" for item in outcomes)
    assert len(saved) == len(names)
    assert sorted(member for member, _ in saved) == names
    assert len(created) == 3
    assert all(worker.shutdown_called is False for worker in created)


def test_each_slot_owns_a_distinct_worker(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c", "d", "e", "f"]
    created = []

    outcomes = run_bound_members(
        [_execution(name) for name in names],
        worker_factory=_factory_of(created, delays={name: 0.01 for name in names}),
        concurrency=3,
        save=lambda execution, result: ("uid", 1.0),
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
    )

    assert len(outcomes) == len(names)
    assert len(created) == 3
    assert len({id(worker) for worker in created}) == 3
    # ``in_use`` would have raised from inside a slot had a worker been shared.
    assert all(worker.in_use is False for worker in created)


def test_member_failure_does_not_abort_other_slots(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c", "d", "e", "f"]
    created = []
    saved = []
    save_lock = threading.Lock()

    def save(execution, result):
        with save_lock:
            saved.append(execution.member_id)
        return ("uid", 1.0)

    outcomes = run_bound_members(
        [_execution(name) for name in names],
        worker_factory=_factory_of(created, fail_for={"b"}),
        concurrency=3,
        save=save,
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
    )

    assert [item.member_id for item in outcomes] == names
    failed = [item for item in outcomes if item.outcome == "failed"]
    assert [item.member_id for item in failed] == ["b"]
    assert failed[0].error == "b exploded"
    assert sorted(saved) == ["a", "c", "d", "e", "f"]


def test_cancellation_from_one_slot_stops_the_others(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c", "d", "e", "f"]
    created = []

    with pytest.raises(BoundRunCancelled):
        run_bound_members(
            [_execution(name) for name in names],
            worker_factory=_factory_of(created, delays={"b": 0.2}, cancel_for={"a"}),
            concurrency=2,
            save=lambda execution, result: ("uid", 1.0),
            marine_directory=MARINE_DIRECTORY,
            convert=_echo,
        )

    submitted = [member for worker in created for member in worker.submitted]
    assert set(submitted) <= {"a", "b"}
    assert "f" not in submitted


def test_single_worker_path_is_unchanged(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c"]
    worker = FakeWorker([FakeStatus("completed", payload={"member": name}) for name in names])
    saved = []

    outcomes = run_bound_members(
        [_execution(name) for name in names],
        worker=worker,
        concurrency=1,
        save=lambda execution, result: (saved.append(execution.member_id) or ("uid", 1.0)),
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
    )

    assert [item.member_id for item in outcomes] == names
    assert [item.outcome for item in outcomes] == ["finished"] * len(names)
    assert saved == names
    assert len(worker.submitted) == len(names)


class ProgressWorker(ParallelWorker):
    def __init__(self, delays=None):
        super().__init__(delays)
        self.pending = []

    def poll(self, timeout=0.0):
        status = super().poll(timeout)
        member = _member_of(self.job)
        self.pending = [{"member": member, "n": 1}, {"member": member, "n": 2}]
        return status

    def take_progress(self):
        return self.pending.pop(0) if self.pending else None


def test_progress_from_parallel_slots_is_complete(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c", "d", "e", "f"]
    created = []
    seen = []
    overlaps = []
    active = {"flag": False}

    def progress(update):
        if active["flag"]:
            overlaps.append(update)
        active["flag"] = True
        time.sleep(0.001)
        seen.append(update)
        active["flag"] = False

    def factory():
        created.append(ProgressWorker({name: 0.01 for name in names}))
        return created[-1]

    outcomes = run_bound_members(
        [_execution(name) for name in names],
        worker_factory=factory,
        concurrency=3,
        save=lambda execution, result: ("uid", 1.0),
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
        on_progress=progress,
    )

    assert len(outcomes) == len(names)
    assert overlaps == []
    expected = [{"member": name, "n": number} for name in names for number in (1, 2)]
    assert sorted(seen, key=lambda item: (item["member"], item["n"])) == expected


def test_member_completion_reports_serial_indices_in_order(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c"]
    worker = FakeWorker(
        [
            FakeStatus("completed", payload={"member": "a"}),
            FakeStatus("failed", message="engine exploded"),
            FakeStatus("completed", payload={"member": "c"}),
        ]
    )
    reported = []

    outcomes = run_bound_members(
        [_execution(name) for name in names],
        worker=worker,
        save=lambda execution, result: ("uid", 1.0),
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
        on_member_complete=lambda index, outcome: reported.append((index, outcome)),
    )

    assert [index for index, _ in reported] == [0, 1, 2]
    assert [outcome for _, outcome in reported] == outcomes
    assert [outcome.outcome for _, outcome in reported] == ["finished", "failed", "finished"]
    assert [outcome.member_id for _, outcome in reported] == names


def test_member_completion_reports_each_parallel_member_once(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c", "d"]
    created = []
    reported = {}
    guard = threading.Lock()

    def complete(index, outcome):
        with guard:
            reported.setdefault(index, []).append(outcome)

    outcomes = run_bound_members(
        [_execution(name) for name in names],
        worker_factory=_factory_of(
            created, delays={name: 0.05 * (len(names) - index) for index, name in enumerate(names)}
        ),
        concurrency=2,
        save=lambda execution, result: ("uid", 1.0),
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
        on_member_complete=complete,
    )

    assert sorted(reported) == [0, 1, 2, 3]
    assert all(len(items) == 1 for items in reported.values())
    for index, items in reported.items():
        assert items[0].member_id == names[index]
        assert items[0] == outcomes[index]


def test_member_completion_callback_failure_does_not_abort_batch(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c"]
    worker = FakeWorker([FakeStatus("completed", payload={"member": name}) for name in names])

    def complete(index, outcome):
        raise RuntimeError("progress sink exploded")

    outcomes = run_bound_members(
        [_execution(name) for name in names],
        worker=worker,
        save=lambda execution, result: ("uid", 1.0),
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
        on_member_complete=complete,
    )

    assert [item.member_id for item in outcomes] == names
    assert [item.outcome for item in outcomes] == ["finished"] * len(names)
    assert len(worker.submitted) == len(names)


def test_cancelled_run_does_not_report_members_that_never_started(monkeypatch):
    _patch_requests(monkeypatch)
    names = ["a", "b", "c"]
    worker = FakeWorker([FakeStatus("completed", payload={"member": name}) for name in names])
    saved = []
    reported = []

    class Cancelled(Exception):
        pass

    def check_cancel():
        if saved:
            raise Cancelled("stop")

    with pytest.raises(Cancelled):
        run_bound_members(
            [_execution(name) for name in names],
            worker=worker,
            save=lambda execution, result: (saved.append(execution.member_id) or ("uid", 1.0)),
            marine_directory=MARINE_DIRECTORY,
            convert=_echo,
            check_cancel=check_cancel,
            on_member_complete=lambda index, outcome: reported.append(index),
        )

    assert saved == ["a"]
    assert len(worker.submitted) == 1
    assert reported == [0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"worker": FakeWorker([]), "worker_factory": lambda: FakeWorker([])},
        {"worker": FakeWorker([]), "concurrency": 2},
        {"worker_factory": lambda: FakeWorker([]), "concurrency": 0},
    ],
)
def test_invalid_worker_configurations_raise(monkeypatch, kwargs):
    _patch_requests(monkeypatch)
    with pytest.raises(ValueError):
        run_bound_members(
            [_execution("a")],
            save=lambda execution, result: ("uid", 1.0),
            marine_directory=MARINE_DIRECTORY,
            convert=_echo,
            **kwargs,
        )
