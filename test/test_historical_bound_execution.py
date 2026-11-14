"""Bound-run engine: per-member isolation, cancellation and status mapping."""

from __future__ import annotations

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
        raise ValueError(
            "native calculation did not produce a validated route: landfall"
        )

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
            save=lambda execution, result: (
                saved.append(execution.member_id) or ("uid", 1.0)
            ),
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
