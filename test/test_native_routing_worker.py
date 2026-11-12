"""Lifecycle tests for the bounded native routing worker."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        ROOT / "build" / "native-routing" / "python",
    )
)
if BUILD_PYTHON.is_dir():
    sys.path.insert(0, str(BUILD_PYTHON))

from sailworks.core.marine.nativeworker import (  # noqa: E402
    CANCEL_GRACE_SECONDS,
    NativeJobStatus,
    NativeProgressThrottle,
    NativeRoutingWorker,
)


def _wait(worker, timeout=90.0, progress=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = worker.poll(0.1)
        update = worker.take_progress()
        if progress is not None and update is not None:
            progress.append(update)
        if status is not None:
            return status
    pytest.fail("native worker did not publish a status in time")


def test_progress_throttle_coalesces_fast_updates_and_keeps_stage_changes():
    throttle = NativeProgressThrottle(interval=0.1)
    assert throttle.should_emit({"stage": "preflight"}, 0.0)
    assert not throttle.should_emit({"stage": "preflight"}, 0.01)
    assert throttle.should_emit({"stage": "forward_isochrone"}, 0.02)
    assert not throttle.should_emit({"stage": "forward_isochrone"}, 0.03)
    assert throttle.should_emit({"stage": "forward_isochrone"}, 0.13)
    assert throttle.should_emit({"stage": "complete"}, 0.135)


def test_worker_forwards_engine_progress_without_blocking_result():
    worker = NativeRoutingWorker()
    progress = []
    try:
        generation = worker.submit_synthetic(engine="main")
        status = _wait(worker, progress=progress)

        assert status.generation == generation
        assert status.state == "completed"
        assert progress, "worker did not forward any engine progress"
        stages = [update["stage"] for update in progress]
        assert stages[0] == "preflight"
        assert "validation" in stages
        assert stages[-1] == "complete"
        assert all("effort_percent" in update for update in progress)
        assert worker.take_progress() is None
    finally:
        worker.shutdown()


class _FakeConnection:
    def __init__(self, messages):
        self.messages = list(messages)

    def poll(self, _timeout):
        return bool(self.messages)

    def recv(self):
        return self.messages.pop(0)

    def close(self):
        pass


def test_worker_rejects_progress_from_a_superseded_generation():
    worker = NativeRoutingWorker()
    worker._active_generation = 2
    worker._status = NativeJobStatus(2, "running")
    worker._process = SimpleNamespace(
        is_alive=lambda: True,
        exitcode=None,
        terminate=lambda: None,
        kill=lambda: None,
        join=lambda timeout=None: None,
    )
    worker._connection = _FakeConnection(
        [
            {
                "kind": "progress",
                "generation": 1,
                "payload": {"stage": "validation"},
            },
            {
                "kind": "progress",
                "generation": 2,
                "payload": {"stage": "complete"},
            },
        ]
    )
    try:
        assert worker.poll(0) is None
        assert worker.take_progress() is None
        assert worker.poll(0) is None
        assert worker.take_progress() == {"stage": "complete"}
    finally:
        worker.shutdown()


def test_cancel_discards_pending_progress():
    worker = NativeRoutingWorker()
    try:
        worker.submit_synthetic(engine="main")
        worker._pending_progress = {"stage": "validation"}
        status = worker.cancel()
        assert status.state == "cancelled"
        assert worker.take_progress() is None
    finally:
        worker.shutdown()


def test_worker_returns_an_explicit_synthetic_diagnostic():
    worker = NativeRoutingWorker()
    try:
        generation = worker.submit_synthetic(engine="main")
        assert worker.running
        status = _wait(worker)

        assert status.generation == generation
        assert status.state == "completed"
        payload = status.payload
        assert payload["accepted"] is True
        assert payload["status"].startswith("complete")
        assert payload["layers"]
        assert payload["validation"]["passed"] is True
    finally:
        worker.shutdown()


@pytest.mark.parametrize(
    ("mode", "layer_count"),
    [("standard", 0), ("original", 30), ("professional", 5)],
)
def test_worker_delivers_qualified_mode(mode, layer_count):
    worker = NativeRoutingWorker()
    try:
        generation = worker.submit_synthetic(engine=mode)
        status = _wait(worker)
        assert status.generation == generation
        assert status.state == "completed"
        assert status.payload["status"] == "complete"
        assert status.payload["accepted"] is True
        assert status.payload["validation"]["passed"] is True
        assert status.payload["independent_validation"]["passed"] is True
        assert status.payload["layer_count"] == layer_count
    finally:
        worker.shutdown()


def test_superseded_generation_never_publishes():
    worker = NativeRoutingWorker()
    try:
        first = worker.submit_synthetic(engine="main")
        second = worker.submit_synthetic(engine="main")
        assert second == first + 1

        status = _wait(worker)
        assert status.generation == second
        assert status.state == "completed"
        # The superseded generation is never observable as a completion.
        assert worker.status.generation == second
    finally:
        worker.shutdown()


def test_cancel_is_observable_through_poll_once():
    worker = NativeRoutingWorker()
    try:
        worker.submit_synthetic(engine="main")
        status = worker.cancel()

        assert status.state == "cancelled"
        assert status.payload is None
        # A poll-only caller still observes the terminal cancellation once.
        polled = worker.poll(0)
        assert polled is not None and polled.state == "cancelled"
        assert polled.payload is None
        assert worker.poll(0) is None
        assert not worker.running
    finally:
        worker.shutdown()


def test_aborted_worker_is_a_failure_not_a_route():
    worker = NativeRoutingWorker()
    try:
        worker.submit_synthetic(engine="main")
        deadline = time.monotonic() + 30
        while not worker.running and time.monotonic() < deadline:
            time.sleep(0.05)
        worker._process.terminate()  # simulate an abnormal exit

        status = _wait(worker)
        assert status.state == "failed"
        assert status.payload is None
        assert "without publishing" in status.message
        assert status.exit_code not in (0, None)
    finally:
        worker.shutdown()


def test_shutdown_releases_the_process():
    worker = NativeRoutingWorker()
    worker.submit_synthetic(engine="main")
    assert worker.running
    worker.shutdown()
    assert not worker.running
    assert worker.status is None


class _RecordingEvent:
    def __init__(self):
        self.set_called = False

    def set(self):
        self.set_called = True


class _CooperativeProcess:
    """Child that exits by itself once the shared cancel event is set."""

    exitcode = 0

    def __init__(self):
        self.joins = []
        self.terminated = False
        self.killed = False

    def is_alive(self):
        return not self.joins

    def join(self, timeout=None):
        self.joins.append(timeout)

    def terminate(self):  # pragma: no cover - asserted not to run
        self.terminated = True

    def kill(self):  # pragma: no cover - asserted not to run
        self.killed = True


class _StubbornProcess:
    """Child that ignores the event, forcing the termination fallback."""

    exitcode = None

    def __init__(self):
        self.terminated = False
        self.killed = False

    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_cancel_trips_the_shared_event_before_any_termination():
    worker = NativeRoutingWorker()
    event = _RecordingEvent()
    process = _CooperativeProcess()
    worker._cancel_event = event
    worker._process = process
    worker._connection = _FakeConnection([])
    worker._active_generation = 1
    worker._status = NativeJobStatus(1, "running")
    worker._pending_progress = {"stage": "validation"}
    try:
        status = worker.cancel()

        assert event.set_called
        assert process.joins and process.joins[0] == CANCEL_GRACE_SECONDS
        assert not process.terminated and not process.killed
        assert status.state == "cancelled"
        assert status.payload is None
        assert worker.take_progress() is None
        polled = worker.poll(0)
        assert polled is not None and polled.state == "cancelled"
        assert polled.payload is None
        assert worker.poll(0) is None
    finally:
        worker.shutdown()


def test_cancel_falls_back_to_termination_when_the_child_ignores_the_event():
    worker = NativeRoutingWorker()
    process = _StubbornProcess()
    worker._cancel_event = _RecordingEvent()
    worker._process = process
    worker._connection = _FakeConnection([])
    worker._active_generation = 1
    worker._status = NativeJobStatus(1, "running")
    try:
        status = worker.cancel()

        assert process.terminated
        assert process.killed
        assert status.state == "cancelled"
        assert status.payload is None
    finally:
        worker.shutdown()
